"""Shared comparison primitives for reproduction assessment analysis engines.

Split out of :mod:`app.services.reproduction_assessment`; the functions are
verbatim moves. ``ReproductionAssessmentError`` lives here (and is re-exported
by :mod:`app.services.reproduction_assessment`) so this low-level module stays
free of a circular import back onto the service module.
"""
from __future__ import annotations

import io
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from . import image_evidence_analysis

_IMAGE_MAX_EDGE = 1024
_SCORE_THRESHOLD = 0.82


class ReproductionAssessmentError(ValueError):
    status_code = 422
    code = "REPRODUCTION_ASSESSMENT_INVALID"


def _open_image(value: Path | bytes) -> Image.Image:
    try:
        source = io.BytesIO(value) if isinstance(value, bytes) else value
        image = Image.open(source).convert("RGB")
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ReproductionAssessmentError("资产不是可解码的图片") from exc
    image.thumbnail((_IMAGE_MAX_EDGE, _IMAGE_MAX_EDGE), Image.Resampling.LANCZOS)
    return image


def _dhash_similarity(source_gray: np.ndarray, generated_gray: np.ndarray) -> float:
    source_small = cv2.resize(source_gray, (9, 8), interpolation=cv2.INTER_AREA)
    generated_small = cv2.resize(generated_gray, (9, 8), interpolation=cv2.INTER_AREA)
    source_bits = source_small[:, 1:] > source_small[:, :-1]
    generated_bits = generated_small[:, 1:] > generated_small[:, :-1]
    return float(1.0 - np.mean(source_bits != generated_bits))


def _image_pair(source_image: Image.Image, generated_image: Image.Image) -> dict[str, Any]:
    source_rgb = np.asarray(source_image.convert("RGB"), dtype=np.uint8)
    generated_resized = generated_image.convert("RGB").resize(
        source_image.size, Image.Resampling.LANCZOS
    )
    generated_rgb = np.asarray(generated_resized, dtype=np.uint8)
    source_gray = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2GRAY)
    generated_gray = cv2.cvtColor(generated_rgb, cv2.COLOR_RGB2GRAY)

    gray_similarity = 1.0 - float(
        np.mean(np.abs(source_gray.astype(np.float32) - generated_gray.astype(np.float32)))
        / 255.0
    )
    source_edges = cv2.Canny(source_gray, 60, 160) > 0
    generated_edges = cv2.Canny(generated_gray, 60, 160) > 0
    edge_total = int(source_edges.sum() + generated_edges.sum())
    edge_dice = (
        1.0
        if edge_total == 0
        else float(2 * np.logical_and(source_edges, generated_edges).sum() / edge_total)
    )
    dhash_similarity = _dhash_similarity(source_gray, generated_gray)
    source_ratio = source_image.width / max(1, source_image.height)
    generated_ratio = generated_image.width / max(1, generated_image.height)
    aspect_ratio_score = min(source_ratio, generated_ratio) / max(source_ratio, generated_ratio)
    structure_score = float(
        np.clip(
            0.45 * gray_similarity
            + 0.30 * edge_dice
            + 0.15 * dhash_similarity
            + 0.10 * aspect_ratio_score,
            0,
            1,
        )
    )

    source_lab = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    generated_lab = cv2.cvtColor(generated_rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    color_score = float(
        np.clip(1.0 - np.mean(np.abs(source_lab - generated_lab)) / 255.0, 0, 1)
    )
    source_lap = cv2.Laplacian(source_gray, cv2.CV_32F)
    generated_lap = cv2.Laplacian(generated_gray, cv2.CV_32F)
    source_sharpness = float(np.std(source_lap))
    generated_sharpness = float(np.std(generated_lap))
    sharpness_score = (
        1.0
        if max(source_sharpness, generated_sharpness) < 1e-6
        else min(source_sharpness, generated_sharpness)
        / max(source_sharpness, generated_sharpness)
    )
    gradient_similarity = 1.0 - float(
        np.mean(np.abs(np.abs(source_lap) - np.abs(generated_lap))) / 255.0
    )
    detail_score = float(np.clip(0.6 * sharpness_score + 0.4 * gradient_similarity, 0, 1))
    diff = np.mean(
        np.abs(source_rgb.astype(np.float32) - generated_rgb.astype(np.float32)), axis=2
    )
    return {
        "structure_layout": round(structure_score, 6),
        "color_light": round(color_score, 6),
        "detail_material": round(detail_score, 6),
        "components": {
            "gray_similarity": round(float(np.clip(gray_similarity, 0, 1)), 6),
            "edge_dice": round(float(np.clip(edge_dice, 0, 1)), 6),
            "perceptual_hash_similarity": round(float(np.clip(dhash_similarity, 0, 1)), 6),
            "aspect_ratio_similarity": round(float(np.clip(aspect_ratio_score, 0, 1)), 6),
            "source_sharpness": round(source_sharpness, 6),
            "generated_sharpness": round(generated_sharpness, 6),
        },
        "diff": diff,
    }


def _heatmap(diff: np.ndarray) -> dict[str, Any]:
    width = min(24, max(8, int(diff.shape[1] // 32) or 8))
    height = min(24, max(8, int(diff.shape[0] // 32) or 8))
    resized = cv2.resize(diff / 255.0, (width, height), interpolation=cv2.INTER_AREA)
    return {
        "width": width,
        "height": height,
        "values": np.round(np.clip(resized, 0, 1), 4).tolist(),
    }


def _difference_regions(diff: np.ndarray) -> list[dict[str, float]]:
    threshold = max(20.0, float(np.percentile(diff, 70)))
    mask = (diff >= threshold).astype(np.uint8) * 255
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    height, width = diff.shape[:2]
    minimum_area = max(4, int(width * height * 0.005))
    regions: list[tuple[int, dict[str, float]]] = []
    for index in range(1, count):
        x, y, region_width, region_height, area = (int(value) for value in stats[index])
        if area < minimum_area:
            continue
        regions.append(
            (
                area,
                {
                    "x": round(x / width, 6),
                    "y": round(y / height, 6),
                    "width": round(region_width / width, 6),
                    "height": round(region_height / height, 6),
                },
            )
        )
    regions.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in regions[:5]] or [
        {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
    ]


def _severity(score: float) -> str:
    if score < 0.35:
        return "critical"
    if score < 0.55:
        return "high"
    if score < 0.72:
        return "medium"
    return "low"


def _analyzer_record(
    *,
    status: str,
    version: str,
    reason: str | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "version": str(version or "unknown"),
        "reason": reason,
        **metadata,
    }


def _ocr_record(result: dict[str, Any]) -> dict[str, Any]:
    return _analyzer_record(
        status=str(result.get("status") or "degraded"),
        version=str(result.get("analyzer_version") or "unknown"),
        reason=str(result.get("degraded_reason") or "") or None,
        analyzer=str(result.get("analyzer") or "tesseract_tsv"),
        evidence_count=int(result.get("evidence_count") or 0),
    )


def _visible_text(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    rows = [item for item in result.get("evidence") or [] if isinstance(item, dict)]
    text = " ".join(str(item.get("evidence_text") or "").strip() for item in rows).strip()
    return text, rows


def _ocr_dimension(
    source_result: dict[str, Any], generated_result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source_status = str(source_result.get("status") or "degraded")
    generated_status = str(generated_result.get("status") or "degraded")
    if source_status != "analyzed" or generated_status != "analyzed":
        reason = "; ".join(
            str(item.get("degraded_reason") or "")
            for item in (source_result, generated_result)
            if item.get("degraded_reason")
        ) or "OCR 分析器不可用"
        status = "unsupported" if "unsupported" in {source_status, generated_status} else "degraded"
        return {"status": status, "score": None, "reason": reason}, None
    source_text, source_rows = _visible_text(source_result)
    generated_text, generated_rows = _visible_text(generated_result)
    score = float(SequenceMatcher(None, source_text.casefold(), generated_text.casefold()).ratio())
    dimension = {
        "status": "analyzed",
        "score": round(float(np.clip(score, 0, 1)), 6),
        "source_text": source_text,
        "generated_text": generated_text,
        "source_evidence_count": len(source_rows),
        "generated_evidence_count": len(generated_rows),
    }
    if score >= 0.95:
        return dimension, None
    bbox = source_rows[0].get("bbox") if source_rows else None
    return dimension, {
        "finding_key": "image-ocr-text",
        "dimension": "ocr_text",
        "kind": "text_mismatch",
        "severity": _severity(score),
        "confidence": round(1.0 - score, 6),
        "message": "画面文字与源素材不一致",
        "bbox": bbox or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        "time_range": None,
        "shot_id": None,
        "evidence": {"source_text": source_text, "generated_text": generated_text},
        "metrics": {"score": round(score, 6)},
    }


def _region_bbox(row: dict[str, Any]) -> dict[str, float] | None:
    bbox = row.get("bbox")
    if isinstance(bbox, dict):
        try:
            return {
                key: float(bbox[key])
                for key in ("x", "y", "width", "height")
            }
        except (KeyError, TypeError, ValueError):
            return None
    polygon = row.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None
    try:
        xs = [float(point["x"]) for point in polygon]
        ys = [float(point["y"]) for point in polygon]
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(xs) - min(xs),
        "height": max(ys) - min(ys),
    }


def _bbox_iou(left: dict[str, float] | None, right: dict[str, float] | None) -> float:
    if left is None or right is None:
        return 0.0
    lx1, ly1 = left["x"], left["y"]
    rx1, ry1 = right["x"], right["y"]
    lx2, ly2 = lx1 + left["width"], ly1 + left["height"]
    rx2, ry2 = rx1 + right["width"], ry1 + right["height"]
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0, min(ly2, ry2) - max(ly1, ry1)
    )
    union = left["width"] * left["height"] + right["width"] * right["height"] - intersection
    return intersection / union if union > 0 else 0.0


def _normalized_event_center(row: dict[str, Any], duration: float | None) -> float | None:
    if not duration or duration <= 0:
        return None
    try:
        start = float(row.get("start_seconds", row.get("timestamp_seconds")))
        end = float(row.get("end_seconds", row.get("timestamp_seconds")))
    except (TypeError, ValueError):
        return None
    return float(np.clip(((start + end) / 2) / duration, 0, 1))


def _labeled_evidence_similarity(
    source_rows: list[dict[str, Any]],
    generated_rows: list[dict[str, Any]],
    *,
    source_duration: float | None = None,
    generated_duration: float | None = None,
    spatial_weight: float = 0.0,
    temporal_weight: float = 0.0,
) -> dict[str, Any]:
    def prepared(rows: list[dict[str, Any]], duration: float | None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            label = " ".join(str(row.get("label") or "").casefold().split())
            if not label:
                continue
            normalized.append(
                {
                    "label": label,
                    "bbox": _region_bbox(row),
                    "time": _normalized_event_center(row, duration),
                    "raw": row,
                }
            )
        return normalized

    source = prepared(source_rows, source_duration)
    generated = prepared(generated_rows, generated_duration)
    if not source and not generated:
        return {
            "status": "not_applicable",
            "score": None,
            "reason": "分析器未在两侧检测到可比较的语义对象",
            "source_labels": [],
            "generated_labels": [],
        }

    available = set(range(len(generated)))
    matches: list[tuple[int, int, float, float]] = []
    for source_index, source_row in enumerate(source):
        candidates: list[tuple[float, int, float, float]] = []
        for generated_index in available:
            generated_row = generated[generated_index]
            if source_row["label"] != generated_row["label"]:
                continue
            spatial = _bbox_iou(source_row["bbox"], generated_row["bbox"])
            temporal = (
                1.0 - abs(float(source_row["time"]) - float(generated_row["time"]))
                if source_row["time"] is not None and generated_row["time"] is not None
                else 0.0
            )
            candidates.append((spatial_weight * spatial + temporal_weight * temporal, generated_index, spatial, temporal))
        if not candidates:
            continue
        _rank, generated_index, spatial, temporal = max(candidates, key=lambda item: item[0])
        available.remove(generated_index)
        matches.append((source_index, generated_index, spatial, temporal))

    matched_count = len(matches)
    label_f1 = (
        2 * matched_count / (len(source) + len(generated))
        if source or generated
        else 1.0
    )
    spatial_score = (
        float(np.mean([item[2] for item in matches])) if matches and spatial_weight else None
    )
    temporal_score = (
        float(np.mean([item[3] for item in matches])) if matches and temporal_weight else None
    )
    label_weight = max(0.0, 1.0 - spatial_weight - temporal_weight)
    score = label_weight * label_f1
    if spatial_weight:
        score += spatial_weight * float(spatial_score or 0)
    if temporal_weight:
        score += temporal_weight * float(temporal_score or 0)
    matched_source = {item[0] for item in matches}
    unmatched_source = [
        source[index]["raw"] for index in range(len(source)) if index not in matched_source
    ]
    return {
        "status": "analyzed",
        "score": round(float(np.clip(score, 0, 1)), 6),
        "label_f1": round(float(np.clip(label_f1, 0, 1)), 6),
        "spatial_score": (
            round(float(np.clip(spatial_score, 0, 1)), 6)
            if spatial_score is not None
            else None
        ),
        "temporal_score": (
            round(float(np.clip(temporal_score, 0, 1)), 6)
            if temporal_score is not None
            else None
        ),
        "source_labels": [item["label"] for item in source],
        "generated_labels": [item["label"] for item in generated],
        "matched_count": matched_count,
        "unmatched_source": unmatched_source,
    }


def _pair_failure_status(
    source_results: list[dict[str, Any]], generated_results: list[dict[str, Any]]
) -> tuple[str, str]:
    rows = [*source_results, *generated_results]
    statuses = {str(row.get("status") or "degraded") for row in rows}
    reasons = list(
        dict.fromkeys(
            str(row.get("degraded_reason") or "").strip()
            for row in rows
            if str(row.get("degraded_reason") or "").strip()
        )
    )
    status = "unsupported" if statuses == {"unsupported"} else "degraded"
    return status, "; ".join(reasons) or "语义分析证据不完整"


def _image_subject_semantics(
    source_image: Image.Image, generated_image: Image.Image
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    source_results: dict[str, dict[str, Any]] = {}
    generated_results: dict[str, dict[str, Any]] = {}
    comparisons: dict[str, dict[str, Any]] = {}
    for capability in ("detector", "segmenter"):
        source_results[capability] = image_evidence_analysis.http_region_provider(
            source_image, capability=capability
        )
        generated_results[capability] = image_evidence_analysis.http_region_provider(
            generated_image, capability=capability
        )
        if (
            source_results[capability].get("status") == "analyzed"
            and generated_results[capability].get("status") == "analyzed"
        ):
            comparisons[capability] = _labeled_evidence_similarity(
                [row for row in source_results[capability].get("evidence") or [] if isinstance(row, dict)],
                [row for row in generated_results[capability].get("evidence") or [] if isinstance(row, dict)],
                spatial_weight=0.25,
            )

    scored = [row for row in comparisons.values() if row.get("status") == "analyzed"]
    if scored:
        score = round(float(np.mean([float(row["score"]) for row in scored])), 6)
        dimension = {
            "status": "analyzed",
            "score": score,
            "capabilities": comparisons,
        }
        analyzer_status, reason = "analyzed", None
    elif comparisons and all(row.get("status") == "not_applicable" for row in comparisons.values()):
        dimension = {
            "status": "not_applicable",
            "score": None,
            "reason": "检测器和分割器均未在两侧发现可比较主体",
            "capabilities": comparisons,
        }
        analyzer_status, reason = "analyzed", None
        score = None
    else:
        analyzer_status, reason = _pair_failure_status(
            list(source_results.values()), list(generated_results.values())
        )
        dimension = {"status": analyzer_status, "score": None, "reason": reason}
        score = None

    analyzer = _analyzer_record(
        status=analyzer_status,
        version="region-semantic-compare-v1",
        reason=reason,
        capabilities={
            capability: {
                "source_status": source_results[capability].get("status"),
                "generated_status": generated_results[capability].get("status"),
                "source_analyzer": source_results[capability].get("analyzer"),
                "generated_analyzer": generated_results[capability].get("analyzer"),
                "source_version": source_results[capability].get("analyzer_version"),
                "generated_version": generated_results[capability].get("analyzer_version"),
                "source_evidence_count": len(source_results[capability].get("evidence") or []),
                "generated_evidence_count": len(generated_results[capability].get("evidence") or []),
            }
            for capability in ("detector", "segmenter")
        },
    )
    finding = None
    if score is not None and score < _SCORE_THRESHOLD:
        unmatched = next(
            (
                row
                for comparison in scored
                for row in comparison.get("unmatched_source") or []
                if isinstance(row, dict)
            ),
            None,
        )
        source_rows = [
            row
            for result in source_results.values()
            for row in result.get("evidence") or []
            if isinstance(row, dict)
        ]
        finding = {
            "finding_key": "image-subject-semantics",
            "dimension": "subject_semantics",
            "kind": "subject_mismatch",
            "severity": _severity(score),
            "confidence": round(1.0 - score, 6),
            "message": "主体类别、数量或位置与源素材不一致",
            "bbox": _region_bbox(unmatched or (source_rows[0] if source_rows else {}))
            or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "time_range": None,
            "shot_id": None,
            "evidence": {
                "capabilities": {
                    key: {
                        "source_labels": value.get("source_labels", []),
                        "generated_labels": value.get("generated_labels", []),
                    }
                    for key, value in comparisons.items()
                }
            },
            "metrics": {"score": score},
        }
    return dimension, analyzer, finding
