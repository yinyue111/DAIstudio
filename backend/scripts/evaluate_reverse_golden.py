"""Offline quality evaluation for reverse-prompt golden samples.

The evaluator never calls a model or a remote service. It accepts JSON or
JSONL records so sanitized historical outputs can be evaluated in CI.

Example record::

    {
      "id": "video-001",
      "result": {"final_text": "...", "shots": [...]},
      "expected": {
        "required_fields": ["final_text", "shots"],
        "ocr_tokens": ["ACME"],
        "forbidden_claims": ["camera zooms"],
        "evidence_units": [0, 1, 2]
      },
      "latency_ms": 1200,
      "cost_credits": 8
    }
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _normalise_text(value: Any) -> str:
    if isinstance(value, dict):
        value = " ".join(_normalise_text(item) for item in value.values())
    elif isinstance(value, list):
        value = " ".join(_normalise_text(item) for item in value)
    elif value is None:
        value = ""
    else:
        value = str(value)
    return "".join(
        char.lower()
        for char in unicodedata.normalize("NFKC", value)
        if not char.isspace()
    )


def _path_value(document: Any, path: str) -> Any:
    current = document
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return None
    return current


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _as_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return default


def _actual_evidence_units(record: dict[str, Any]) -> set[str]:
    result = record.get("result")
    if not isinstance(result, dict):
        return set()
    units: set[str] = set()
    for item in result.get("evidence_frame_indices") or []:
        if isinstance(item, (int, str)) and not isinstance(item, bool):
            units.add(str(item))
    for shot in result.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        for item in shot.get("evidence_frame_indices") or []:
            if isinstance(item, (int, str)) and not isinstance(item, bool):
                units.add(str(item))
    return units


def evaluate_sample(record: dict[str, Any], index: int = 0) -> dict[str, Any]:
    result = record.get("result")
    if not isinstance(result, dict):
        result = {}
    expected = record.get("expected")
    if not isinstance(expected, dict):
        expected = {}

    required_fields = [
        str(item) for item in expected.get("required_fields") or ["final_text"]
    ]
    present_fields = sum(_present(_path_value(result, path)) for path in required_fields)
    field_completeness = present_fields / len(required_fields) if required_fields else 1.0

    text = _normalise_text(result)
    ocr_tokens = [
        token for token in (_normalise_text(item) for item in expected.get("ocr_tokens") or [])
        if token
    ]
    ocr_matches = sum(token in text for token in ocr_tokens)
    ocr_match = ocr_matches / len(ocr_tokens) if ocr_tokens else None

    forbidden_claims = [
        token
        for token in (
            _normalise_text(item) for item in expected.get("forbidden_claims") or []
        )
        if token
    ]
    matched_forbidden = [token for token in forbidden_claims if token in text]

    expected_evidence = {
        str(item)
        for item in expected.get("evidence_units") or []
        if isinstance(item, (int, str)) and not isinstance(item, bool)
    }
    actual_evidence = _actual_evidence_units(record)
    evidence_coverage = (
        len(expected_evidence & actual_evidence) / len(expected_evidence)
        if expected_evidence
        else None
    )

    latency_ms = _as_number(record.get("latency_ms"))
    cost_credits = _as_number(
        record.get("cost_credits", (record.get("detail") or {}).get("cost_credits"))
    )
    return {
        "id": str(record.get("id") or f"sample-{index + 1}"),
        "field_completeness": round(field_completeness, 6),
        "required_field_count": len(required_fields),
        "present_field_count": present_fields,
        "ocr_match": None if ocr_match is None else round(ocr_match, 6),
        "ocr_token_count": len(ocr_tokens),
        "ocr_match_count": ocr_matches,
        "factual_error_count": len(matched_forbidden),
        "matched_forbidden_claims": matched_forbidden,
        "evidence_coverage": (
            None if evidence_coverage is None else round(evidence_coverage, 6)
        ),
        "expected_evidence_count": len(expected_evidence),
        "covered_evidence_count": len(expected_evidence & actual_evidence),
        "latency_ms": latency_ms,
        "cost_credits": cost_credits,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def evaluate_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    samples = [evaluate_sample(record, index) for index, record in enumerate(records)]
    ocr_samples = [item["ocr_match"] for item in samples if item["ocr_match"] is not None]
    evidence_samples = [
        item["evidence_coverage"]
        for item in samples
        if item["evidence_coverage"] is not None
    ]
    latencies = [float(item["latency_ms"]) for item in samples]
    costs = [float(item["cost_credits"]) for item in samples]
    sample_count = len(samples)
    return {
        "summary": {
            "sample_count": sample_count,
            "field_completeness": round(
                statistics.fmean(item["field_completeness"] for item in samples), 6
            ) if samples else 0.0,
            "ocr_match": round(statistics.fmean(ocr_samples), 6) if ocr_samples else None,
            "factual_error_count": sum(item["factual_error_count"] for item in samples),
            "factual_error_sample_rate": round(
                sum(item["factual_error_count"] > 0 for item in samples) / sample_count,
                6,
            ) if sample_count else 0.0,
            "evidence_coverage": round(statistics.fmean(evidence_samples), 6)
            if evidence_samples else None,
            "latency_ms": {
                "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                "p50": round(_percentile(latencies, 0.50), 3),
                "p95": round(_percentile(latencies, 0.95), 3),
            },
            "cost_credits": {
                "total": round(sum(costs), 6),
                "mean": round(statistics.fmean(costs), 6) if costs else 0.0,
            },
        },
        "samples": samples,
    }


def load_records(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        values = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        document = json.loads(raw)
        values = document.get("samples") if isinstance(document, dict) else document
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise ValueError("input must be a JSON array, {samples: [...]}, or JSONL objects")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Golden sample JSON or JSONL file")
    parser.add_argument("--output", type=Path, help="Optional report JSON path")
    args = parser.parse_args(argv)
    try:
        report = evaluate_records(load_records(args.input))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"reverse golden evaluation failed: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
