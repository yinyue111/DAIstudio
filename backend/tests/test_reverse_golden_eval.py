import json
from pathlib import Path

from scripts.evaluate_reverse_golden import evaluate_records, load_records, main


def test_reverse_golden_metrics_cover_quality_latency_and_cost():
    report = evaluate_records([
        {
            "id": "video-ok",
            "result": {
                "final_text": "ACME product in a static scene",
                "shots": [
                    {"evidence_frame_indices": [0, 1]},
                    {"evidence_frame_indices": [2]},
                ],
            },
            "expected": {
                "required_fields": ["final_text", "shots"],
                "ocr_tokens": ["ACME", "missing"],
                "forbidden_claims": ["dolly zoom"],
                "evidence_units": [0, 1, 2, 3],
            },
            "latency_ms": 100,
            "detail": {"cost_credits": 8},
        },
        {
            "id": "image-with-error",
            "result": {"final_text": "camera performs dolly zoom"},
            "expected": {
                "required_fields": ["final_text", "subject"],
                "forbidden_claims": ["dolly zoom"],
            },
            "latency_ms": 300,
            "cost_credits": 2,
        },
    ])

    assert report["summary"] == {
        "sample_count": 2,
        "field_completeness": 0.75,
        "ocr_match": 0.5,
        "factual_error_count": 1,
        "factual_error_sample_rate": 0.5,
        "evidence_coverage": 0.75,
        "latency_ms": {"mean": 200.0, "p50": 200.0, "p95": 290.0},
        "cost_credits": {"total": 10.0, "mean": 5.0},
    }
    assert report["samples"][1]["matched_forbidden_claims"] == ["dollyzoom"]


def test_reverse_golden_cli_accepts_jsonl_and_writes_report(tmp_path):
    source = tmp_path / "golden.jsonl"
    source.write_text(
        json.dumps({"id": "one", "result": {"final_text": "ok"}}) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"

    assert load_records(source)[0]["id"] == "one"
    assert main([str(source), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["sample_count"] == 1
    assert report["summary"]["field_completeness"] == 1.0


def test_sanitized_reverse_golden_baseline_has_full_contract_coverage():
    fixture = Path(__file__).parent / "fixtures" / "reverse_golden_samples.json"

    report = evaluate_records(load_records(fixture))

    assert report["summary"]["sample_count"] == 4
    assert report["summary"]["field_completeness"] == 1.0
    assert report["summary"]["ocr_match"] == 1.0
    assert report["summary"]["factual_error_count"] == 0
    assert report["summary"]["evidence_coverage"] == 1.0
