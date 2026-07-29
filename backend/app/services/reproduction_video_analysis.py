"""Video-side reproduction assessment analysis engine.

Split out of :mod:`app.services.reproduction_assessment`; the functions are
verbatim moves.
"""

from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
from sqlalchemy.orm import Session

from ..models import ReproductionAssessment, ReverseResultRevision
from . import (
    image_evidence_analysis,
    video_audio,
    video_evidence_analysis,
    video_frames,
)
from .reproduction_comparators import (
    _SCORE_THRESHOLD,
    _analyzer_record,
    _difference_regions,
    _image_pair,
    _labeled_evidence_similarity,
    _ocr_dimension,
    _open_image,
    _pair_failure_status,
    _region_bbox,
    _severity,
)

_VIDEO_SAMPLE_COUNT = 8


def _revision_shots(db: Session, row: ReproductionAssessment) -> list[dict[str, Any]]:
    if row.reverse_revision_id is None:
        return []
    revision = db.get(ReverseResultRevision, int(row.reverse_revision_id))
    payload = (
        revision.payload if revision is not None and isinstance(revision.payload, dict) else {}
    )
    analysis = (
        payload.get("video_analysis") if isinstance(payload.get("video_analysis"), dict) else {}
    )
    return [item for item in analysis.get("shots") or [] if isinstance(item, dict)]


def _shot_id(shots: list[dict[str, Any]], timestamp: float) -> str | None:
    for shot in shots:
        try:
            start = float(shot.get("start_seconds") or 0)
            end = float(shot.get("end_seconds"))
        except (TypeError, ValueError):
            continue
        if start <= timestamp <= end + 0.001:
            value = str(shot.get("shot_id") or shot.get("detected_shot_id") or "").strip()
            return value or None
    return None


def _frame_time_range(timestamps: list[float], index: int, duration: float) -> dict[str, float]:
    current = timestamps[index]
    previous = timestamps[index - 1] if index else 0.0
    following = timestamps[index + 1] if index + 1 < len(timestamps) else duration
    start = max(0.0, (previous + current) / 2 if index else 0.0)
    end = min(duration, (current + following) / 2 if index + 1 < len(timestamps) else duration)
    if end <= start:
        end = min(duration, start + max(0.05, duration / 100))
    return {"start_seconds": round(start, 3), "end_seconds": round(end, 3)}


def _motion_score(
    source_pairs: list[dict[str, Any]], generated_pairs: list[dict[str, Any]]
) -> float:
    if len(source_pairs) < 2 or len(generated_pairs) < 2:
        return 1.0
    source_changes = [
        abs(float(source_pairs[index]["luma"]) - float(source_pairs[index - 1]["luma"]))
        for index in range(1, len(source_pairs))
    ]
    generated_changes = [
        abs(float(generated_pairs[index]["luma"]) - float(generated_pairs[index - 1]["luma"]))
        for index in range(1, len(generated_pairs))
    ]
    count = min(len(source_changes), len(generated_changes))
    return float(
        np.clip(
            1.0 - np.mean(np.abs(np.asarray(source_changes[:count]) - generated_changes[:count])),
            0,
            1,
        )
    )


def _video_semantic_inputs(
    frames: list[Any],
) -> tuple[list[Image.Image], list[dict[str, Any]]]:
    images: list[Image.Image] = []
    metadata: list[dict[str, Any]] = []
    for index, frame in enumerate(frames, start=1):
        raw = bytes(frame.jpeg)
        image = _open_image(raw)
        images.append(image)
        metadata.append(
            {
                "frame_index": index,
                "timestamp_seconds": round(float(frame.timestamp_seconds), 3),
                "source_segment_index": 1,
                "source_content_hash": hashlib.sha256(raw).hexdigest(),
                "width": image.width,
                "height": image.height,
            }
        )
    return images, metadata


def _subject_track_rows(capability: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track in capability.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        observations = [
            row
            for row in track.get("observations") or []
            if isinstance(row, dict) and _region_bbox(row) is not None
        ]
        bboxes = [_region_bbox(row) for row in observations]
        bboxes = [bbox for bbox in bboxes if bbox is not None]
        normalized = dict(track)
        if bboxes:
            normalized["bbox"] = {
                key: float(np.mean([bbox[key] for bbox in bboxes]))
                for key in ("x", "y", "width", "height")
            }
        rows.append(normalized)
    return rows


def _video_semantic_capability_comparison(
    source: dict[str, Any],
    generated: dict[str, Any],
    *,
    capability: str,
    source_duration: float,
    generated_duration: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_capability = source.get(capability)
    generated_capability = generated.get(capability)
    if not isinstance(source_capability, dict) or not isinstance(generated_capability, dict):
        source_capability = source_capability if isinstance(source_capability, dict) else {}
        generated_capability = (
            generated_capability if isinstance(generated_capability, dict) else {}
        )
    source_status = str(source_capability.get("status") or "degraded")
    generated_status = str(generated_capability.get("status") or "degraded")
    evidence_key = {
        "subject_tracking": "tracks",
        "action": "events",
        "transition": "events",
    }[capability]
    comparable_statuses = {"analyzed", "partial"}
    if source_status in comparable_statuses and generated_status in comparable_statuses:
        source_rows = [
            row for row in source_capability.get(evidence_key) or [] if isinstance(row, dict)
        ]
        generated_rows = [
            row for row in generated_capability.get(evidence_key) or [] if isinstance(row, dict)
        ]
        if capability == "subject_tracking":
            source_rows = _subject_track_rows({"tracks": source_rows})
            generated_rows = _subject_track_rows({"tracks": generated_rows})
            comparison = _labeled_evidence_similarity(
                source_rows,
                generated_rows,
                source_duration=source_duration,
                generated_duration=generated_duration,
                spatial_weight=0.25,
                temporal_weight=0.15,
            )
        else:
            comparison = _labeled_evidence_similarity(
                source_rows,
                generated_rows,
                source_duration=source_duration,
                generated_duration=generated_duration,
                temporal_weight=0.25 if capability == "action" else 0.3,
            )
        comparison["source_rows"] = source_rows
        comparison["generated_rows"] = generated_rows
        dimension = {
            key: value
            for key, value in comparison.items()
            if key not in {"unmatched_source", "source_rows", "generated_rows"}
        }
        analyzer_status = "analyzed"
        reason = None
    else:
        analyzer_status, reason = _pair_failure_status([source_capability], [generated_capability])
        comparison = {
            "status": analyzer_status,
            "score": None,
            "reason": reason,
            "source_rows": [],
            "generated_rows": [],
        }
        dimension = {
            "status": analyzer_status,
            "score": None,
            "reason": reason,
        }
    analyzer = _analyzer_record(
        status=analyzer_status,
        version="video-semantic-compare-v1",
        reason=reason,
        capability=capability,
        source_status=source_status,
        generated_status=generated_status,
        source_analyzer=source_capability.get("analyzer"),
        generated_analyzer=generated_capability.get("analyzer"),
        source_version=source_capability.get("analyzer_version"),
        generated_version=generated_capability.get("analyzer_version"),
        source_evidence_count=int(source_capability.get("evidence_count") or 0),
        generated_evidence_count=int(generated_capability.get("evidence_count") or 0),
    )
    return dimension, {"analyzer": analyzer, "comparison": comparison}


def _semantic_video_finding(
    *,
    capability: str,
    comparison: dict[str, Any],
    source_duration: float,
    shots: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if comparison.get("status") != "analyzed":
        return None
    score = float(comparison.get("score") or 0)
    if score >= _SCORE_THRESHOLD:
        return None
    source_rows = [row for row in comparison.get("source_rows") or [] if isinstance(row, dict)]
    unmatched = next(
        (row for row in comparison.get("unmatched_source") or [] if isinstance(row, dict)),
        source_rows[0] if source_rows else {},
    )
    try:
        timestamp = float(unmatched.get("timestamp_seconds"))
        start = max(0.0, timestamp - max(0.05, source_duration * 0.01))
        end = min(source_duration, timestamp + max(0.05, source_duration * 0.01))
    except (TypeError, ValueError):
        try:
            start = float(unmatched.get("start_seconds") or 0)
            end = float(unmatched.get("end_seconds") or source_duration)
        except (TypeError, ValueError):
            start, end = 0.0, source_duration
    if end <= start:
        end = min(source_duration, start + max(0.05, source_duration * 0.01))
    center = (start + end) / 2
    dimension = {
        "subject_tracking": "subject_semantics",
        "action": "action_semantics",
        "transition": "transition_semantics",
    }[capability]
    message = {
        "subject_tracking": "视频主体类别、数量、位置或持续时间与源视频不一致",
        "action": "视频主体动作或动作时序与源视频不一致",
        "transition": "视频转场类型或转场时点与源视频不一致",
    }[capability]
    return {
        "finding_key": f"video-{dimension}",
        "dimension": dimension,
        "kind": f"{capability}_mismatch",
        "severity": _severity(score),
        "confidence": round(1.0 - score, 6),
        "message": message,
        "bbox": _region_bbox(unmatched) if capability == "subject_tracking" else None,
        "time_range": {
            "start_seconds": round(max(0.0, start), 3),
            "end_seconds": round(max(start + 0.001, end), 3),
        },
        "shot_id": _shot_id(shots, center),
        "evidence": {
            "source_labels": comparison.get("source_labels", []),
            "generated_labels": comparison.get("generated_labels", []),
        },
        "metrics": {"score": round(score, 6)},
    }


def _camera_motion_comparison(
    source: dict[str, Any],
    generated: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    source_status = str(source.get("status") or "degraded")
    generated_status = str(generated.get("status") or "degraded")
    source_samples = [row for row in source.get("samples") or [] if isinstance(row, dict)]
    generated_samples = [row for row in generated.get("samples") or [] if isinstance(row, dict)]
    if source_status == generated_status == "analyzed" and source_samples and generated_samples:
        count = min(len(source_samples), len(generated_samples))
        rows: list[dict[str, Any]] = []
        for index in range(count):
            source_scores = source_samples[index].get("camera_scores") or {}
            generated_scores = generated_samples[index].get("camera_scores") or {}
            score = 1.0 - float(
                np.mean(
                    [
                        abs(
                            float(source_scores.get(key) or 0)
                            - float(generated_scores.get(key) or 0)
                        )
                        for key in ("pan", "tilt", "zoom", "static")
                    ]
                )
            )
            rows.append(
                {
                    "score": float(np.clip(score, 0, 1)),
                    "source": source_samples[index],
                    "generated": generated_samples[index],
                }
            )
        score = round(float(np.mean([row["score"] for row in rows])), 6)
        dimension = {
            "status": "analyzed",
            "score": score,
            "sample_count": count,
            "source_labels": [str(row.get("camera") or "") for row in source_samples[:count]],
            "generated_labels": [str(row.get("camera") or "") for row in generated_samples[:count]],
        }
        analyzer_status, reason = "analyzed", None
        worst = min(rows, key=lambda row: row["score"])
    else:
        analyzer_status, reason = _pair_failure_status([source], [generated])
        dimension = {"status": analyzer_status, "score": None, "reason": reason}
        score = None
        worst = None
    analyzer = _analyzer_record(
        status=analyzer_status,
        version="opencv-camera-motion-compare-v1",
        reason=reason,
        source_status=source_status,
        generated_status=generated_status,
        source_version=source.get("analyzer_version"),
        generated_version=generated.get("analyzer_version"),
        source_sample_count=len(source_samples),
        generated_sample_count=len(generated_samples),
    )
    finding = None
    if score is not None and score < _SCORE_THRESHOLD and worst is not None:
        source_row = worst["source"]
        start = float(source_row.get("start_seconds") or 0)
        end = float(source_row.get("end_seconds") or start + 0.05)
        finding = {
            "finding_key": "video-camera-motion",
            "dimension": "camera_motion",
            "kind": "camera_motion_mismatch",
            "severity": _severity(score),
            "confidence": round(1.0 - score, 6),
            "message": "运镜类型或运动强度与源视频不一致",
            "bbox": None,
            "time_range": {
                "start_seconds": round(start, 3),
                "end_seconds": round(max(start + 0.001, end), 3),
            },
            "shot_id": None,
            "evidence": {
                "source_camera": source_row.get("camera"),
                "generated_camera": worst["generated"].get("camera"),
            },
            "metrics": {"score": score},
        }
    return dimension, analyzer, finding


def _audio_comparison(source: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    source_status = str(source.get("status") or "degraded")
    generated_status = str(generated.get("status") or "degraded")
    if source_status == generated_status == "no_audio":
        return {"status": "not_applicable", "score": None, "reason": "两侧都没有音轨"}
    if source_status not in {"analyzed", "partial"} or generated_status not in {
        "analyzed",
        "partial",
    }:
        return {
            "status": "unsupported"
            if "disabled" in {source_status, generated_status}
            else "degraded",
            "score": None,
            "reason": "音频分析证据不完整",
        }
    source_text = str(source.get("transcript") or "")
    generated_text = str(generated.get("transcript") or "")
    transcript_score = SequenceMatcher(
        None, source_text.casefold(), generated_text.casefold()
    ).ratio()
    source_beat = (source.get("features") or {}).get("beat") or {}
    generated_beat = (generated.get("features") or {}).get("beat") or {}
    source_bpm = source_beat.get("bpm")
    generated_bpm = generated_beat.get("bpm")
    bpm_score = None
    try:
        source_bpm = float(source_bpm)
        generated_bpm = float(generated_bpm)
        bpm_score = min(source_bpm, generated_bpm) / max(source_bpm, generated_bpm)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    scores = [transcript_score, *([bpm_score] if bpm_score is not None else [])]
    return {
        "status": "analyzed",
        "score": round(float(np.clip(np.mean(scores), 0, 1)), 6),
        "transcript_score": round(float(np.clip(transcript_score, 0, 1)), 6),
        "bpm_score": round(float(np.clip(bpm_score, 0, 1)), 6) if bpm_score is not None else None,
    }


def _sample_video_frames(path: Path, duration: float, fractions: list[float]) -> list[Any]:
    if not duration:
        return []
    sample = video_frames.sample_video_from_path(
        str(path),
        _VIDEO_SAMPLE_COUNT,
        custom_timestamps=[duration * value for value in fractions],
    )
    return list(sample.frames) if sample else []


def _aligned_frame_analysis(
    source_frames: list[Any],
    generated_frames: list[Any],
    fractions: list[float],
    shots: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[float]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[float],
]:
    frame_pairs: list[dict[str, Any]] = []
    source_motion: list[dict[str, Any]] = []
    generated_motion: list[dict[str, Any]] = []
    source_timestamps: list[float] = []
    dimension_scores: dict[str, list[float]] = {
        "frame_structure": [],
        "frame_color_light": [],
        "frame_detail_material": [],
    }
    for index in range(min(len(source_frames), len(generated_frames))):
        source_frame = source_frames[index]
        generated_frame = generated_frames[index]
        source_image = _open_image(source_frame.jpeg)
        generated_image = _open_image(generated_frame.jpeg)
        pair = _image_pair(source_image, generated_image)
        source_timestamp = float(source_frame.timestamp_seconds)
        source_timestamps.append(source_timestamp)
        frame_pairs.append(
            {
                "index": index + 1,
                "normalized_position": round(float(fractions[index]), 6),
                "source_timestamp_seconds": round(source_timestamp, 3),
                "generated_timestamp_seconds": round(float(generated_frame.timestamp_seconds), 3),
                "shot_id": _shot_id(shots, source_timestamp),
                "scores": {
                    "structure": pair["structure_layout"],
                    "color_light": pair["color_light"],
                    "detail_material": pair["detail_material"],
                },
                "bbox": _difference_regions(pair["diff"])[0],
            }
        )
        dimension_scores["frame_structure"].append(float(pair["structure_layout"]))
        dimension_scores["frame_color_light"].append(float(pair["color_light"]))
        dimension_scores["frame_detail_material"].append(float(pair["detail_material"]))
        source_motion.append(
            {"luma": float(np.asarray(source_image.convert("L"), dtype=np.float32).mean() / 255)}
        )
        generated_motion.append(
            {"luma": float(np.asarray(generated_image.convert("L"), dtype=np.float32).mean() / 255)}
        )
    return frame_pairs, dimension_scores, source_motion, generated_motion, source_timestamps


def _frame_structure_findings(
    frame_pairs: list[dict[str, Any]],
    source_timestamps: list[float],
    source_duration: float,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for index, frame_pair in enumerate(frame_pairs):
        score = float(frame_pair["scores"]["structure"])
        if score >= _SCORE_THRESHOLD:
            continue
        findings.append(
            {
                "finding_key": f"video-frame-{index + 1}-structure",
                "dimension": "frame_structure",
                "kind": "frame_difference",
                "severity": _severity(score),
                "confidence": round(1.0 - score, 6),
                "message": f"源视频 {frame_pair['source_timestamp_seconds']:g}s 附近画面结构不一致",
                "bbox": frame_pair["bbox"],
                "time_range": _frame_time_range(source_timestamps, index, source_duration),
                "shot_id": frame_pair["shot_id"],
                "evidence": {
                    "source_timestamp_seconds": frame_pair["source_timestamp_seconds"],
                    "generated_timestamp_seconds": frame_pair["generated_timestamp_seconds"],
                    "normalized_position": frame_pair["normalized_position"],
                },
                "metrics": {"score": round(score, 6)},
            }
        )
    return findings


def _frame_dimensions(
    dimension_scores: dict[str, list[float]],
) -> dict[str, dict[str, Any]]:
    dimensions: dict[str, dict[str, Any]] = {}
    for key, scores in dimension_scores.items():
        dimensions[key] = (
            {
                "status": "analyzed",
                "score": round(float(np.clip(np.mean(scores), 0, 1)), 6),
                "sample_count": len(scores),
            }
            if scores
            else {
                "status": "unsupported",
                "score": None,
                "reason": "无法获取可对齐的视频帧",
            }
        )
    return dimensions


def _duration_comparison(
    source_duration: float,
    generated_duration: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if source_duration <= 0 or generated_duration <= 0:
        return {
            "status": "unsupported",
            "score": None,
            "reason": "无法读取两侧视频时长",
        }, None
    score = min(source_duration, generated_duration) / max(source_duration, generated_duration)
    dimension = {
        "status": "analyzed",
        "score": round(float(np.clip(score, 0, 1)), 6),
        "source_duration_seconds": round(source_duration, 3),
        "generated_duration_seconds": round(generated_duration, 3),
    }
    if score >= 0.9:
        return dimension, None
    return dimension, {
        "finding_key": "video-duration-pacing",
        "dimension": "duration_pacing",
        "kind": "duration_difference",
        "severity": _severity(score),
        "confidence": round(1.0 - score, 6),
        "message": "生成视频时长与源视频不一致",
        "bbox": None,
        "time_range": {"start_seconds": 0.0, "end_seconds": round(source_duration, 3)},
        "shot_id": None,
        "evidence": {
            "source_duration_seconds": round(source_duration, 3),
            "generated_duration_seconds": round(generated_duration, 3),
        },
        "metrics": {"score": round(score, 6)},
    }


def _motion_dimension(
    source_motion: list[dict[str, Any]],
    generated_motion: list[dict[str, Any]],
    count: int,
) -> dict[str, Any]:
    score = _motion_score(source_motion, generated_motion)
    return {
        "status": "analyzed" if count >= 2 else "unsupported",
        "score": round(score, 6) if count >= 2 else None,
        "sample_count": count,
        **({"reason": "运动帧数不足"} if count < 2 else {}),
    }


def _semantic_analysis(
    source_frames: list[Any],
    generated_frames: list[Any],
    source_duration: float,
    generated_duration: float,
    shots: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    tuple[list[Image.Image], list[dict[str, Any]]],
    tuple[list[Image.Image], list[dict[str, Any]]],
]:
    source_inputs = _video_semantic_inputs(source_frames)
    generated_inputs = _video_semantic_inputs(generated_frames)
    source_semantics = video_evidence_analysis.http_semantic_provider(*source_inputs)
    generated_semantics = video_evidence_analysis.http_semantic_provider(*generated_inputs)
    dimensions: dict[str, dict[str, Any]] = {}
    analyzers: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    for capability, dimension_key in (
        ("subject_tracking", "subject_semantics"),
        ("action", "action_semantics"),
        ("transition", "transition_semantics"),
    ):
        dimension, detail = _video_semantic_capability_comparison(
            source_semantics,
            generated_semantics,
            capability=capability,
            source_duration=source_duration,
            generated_duration=generated_duration,
        )
        dimensions[dimension_key] = dimension
        analyzers[dimension_key] = detail["analyzer"]
        finding = _semantic_video_finding(
            capability=capability,
            comparison=detail["comparison"],
            source_duration=source_duration,
            shots=shots,
        )
        if finding:
            findings.append(finding)
    return dimensions, analyzers, findings, source_inputs, generated_inputs


def _camera_analysis(
    source_inputs: tuple[list[Image.Image], list[dict[str, Any]]],
    generated_inputs: tuple[list[Image.Image], list[dict[str, Any]]],
    shots: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    source_camera = video_evidence_analysis.cv2_motion_analysis(*source_inputs)
    generated_camera = video_evidence_analysis.cv2_motion_analysis(*generated_inputs)
    dimension, analyzer, finding = _camera_motion_comparison(source_camera, generated_camera)
    if finding:
        time_range = finding["time_range"]
        finding["shot_id"] = _shot_id(
            shots,
            (float(time_range["start_seconds"]) + float(time_range["end_seconds"])) / 2,
        )
    return dimension, analyzer, finding


def _collapsed_ocr_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": (
            "analyzed"
            if all(item.get("status") == "analyzed" for item in results)
            else str(results[0].get("status") or "degraded")
        ),
        "evidence": [evidence for result in results for evidence in result.get("evidence") or []],
    }


def _ocr_analysis(
    source_frames: list[Any],
    generated_frames: list[Any],
    count: int,
) -> tuple[dict[str, Any], str]:
    source_results: list[dict[str, Any]] = []
    generated_results: list[dict[str, Any]] = []
    for index in sorted({0, max(0, count // 2), max(0, count - 1)}):
        if index >= count:
            continue
        source_results.append(
            image_evidence_analysis.tesseract_ocr(_open_image(source_frames[index].jpeg))
        )
        generated_results.append(
            image_evidence_analysis.tesseract_ocr(_open_image(generated_frames[index].jpeg))
        )
    if not source_results or not generated_results:
        return {
            "status": "unsupported",
            "score": None,
            "reason": "无视频帧可用于 OCR",
        }, "unsupported"
    source_ocr = _collapsed_ocr_results(source_results)
    generated_ocr = _collapsed_ocr_results(generated_results)
    dimension, _finding = _ocr_dimension(source_ocr, generated_ocr)
    status = (
        "analyzed"
        if source_ocr["status"] == generated_ocr["status"] == "analyzed"
        else "unsupported"
    )
    return dimension, status


def _audio_analysis(
    source_path: Path,
    generated_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = video_audio.analyze_video_audio_from_path(str(source_path))
    generated = video_audio.analyze_video_audio_from_path(str(generated_path))
    source_status = str(source.get("status") or "degraded")
    generated_status = str(generated.get("status") or "degraded")
    analyzer_status = source_status if source_status == generated_status else "partial"
    return _audio_comparison(source, generated), _analyzer_record(
        status=analyzer_status,
        version="video-audio-evidence-v1",
        source_status=source_status,
        generated_status=generated_status,
    )


def _video_analyzers(
    *,
    source_duration: float,
    generated_duration: float,
    count: int,
    ocr_status: str,
    audio_analyzer: dict[str, Any],
    camera_analyzer: dict[str, Any],
    semantic_analyzers: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        "ffprobe": _analyzer_record(
            status="analyzed" if source_duration and generated_duration else "degraded",
            version="ffprobe",
            reason=None if source_duration and generated_duration else "媒体时长不完整",
        ),
        "ffmpeg_frames": _analyzer_record(
            status="analyzed" if count else "degraded",
            version="normalized-timestamp-v1",
            reason=None if count else "抽帧不可用",
            sample_count=count,
        ),
        "opencv": _analyzer_record(status="analyzed", version=cv2.__version__),
        "motion": _analyzer_record(
            status="analyzed" if count >= 2 else "degraded",
            version="luma-delta-v1",
            reason=None if count >= 2 else "运动帧数不足",
        ),
        "ocr": _analyzer_record(status=ocr_status, version="tesseract-sampled-frames-v1"),
        "audio": audio_analyzer,
        "camera_motion": camera_analyzer,
        **semantic_analyzers,
    }


def _video_analysis(
    db: Session,
    row: ReproductionAssessment,
    source_path: Path,
    generated_path: Path,
) -> dict[str, Any]:
    source_meta = video_frames.probe_media(str(source_path)) or {}
    generated_meta = video_frames.probe_media(str(generated_path)) or {}
    source_duration = float(source_meta.get("duration_seconds") or source_meta.get("duration") or 0)
    generated_duration = float(
        generated_meta.get("duration_seconds") or generated_meta.get("duration") or 0
    )
    fractions = np.linspace(0.02, 0.98, _VIDEO_SAMPLE_COUNT).tolist()
    source_frames = _sample_video_frames(source_path, source_duration, fractions)
    generated_frames = _sample_video_frames(generated_path, generated_duration, fractions)
    count = min(len(source_frames), len(generated_frames))
    shots = _revision_shots(db, row)
    frame_pairs, scores, source_motion, generated_motion, timestamps = _aligned_frame_analysis(
        source_frames, generated_frames, fractions, shots
    )
    findings = _frame_structure_findings(frame_pairs, timestamps, source_duration)
    dimensions = _frame_dimensions(scores)
    dimensions["duration_pacing"], duration_finding = _duration_comparison(
        source_duration, generated_duration
    )
    if duration_finding:
        findings.append(duration_finding)
    dimensions["motion"] = _motion_dimension(source_motion, generated_motion, count)

    semantic_dimensions, semantic_analyzers, semantic_findings, source_inputs, generated_inputs = (
        _semantic_analysis(
            source_frames,
            generated_frames,
            source_duration,
            generated_duration,
            shots,
        )
    )
    dimensions.update(semantic_dimensions)
    findings.extend(semantic_findings)
    camera_dimension, camera_analyzer, camera_finding = _camera_analysis(
        source_inputs, generated_inputs, shots
    )
    dimensions["camera_motion"] = camera_dimension
    if camera_finding:
        findings.append(camera_finding)
    dimensions["ocr_text"], ocr_status = _ocr_analysis(source_frames, generated_frames, count)
    dimensions["audio"], audio_analyzer = _audio_analysis(source_path, generated_path)
    analyzers = _video_analyzers(
        source_duration=source_duration,
        generated_duration=generated_duration,
        count=count,
        ocr_status=ocr_status,
        audio_analyzer=audio_analyzer,
        camera_analyzer=camera_analyzer,
        semantic_analyzers=semantic_analyzers,
    )
    warnings = [
        item["reason"]
        for item in analyzers.values()
        if item.get("status") in {"unsupported", "degraded", "partial"} and item.get("reason")
    ]
    return {
        "metrics": {
            "schema_version": "reproduction-video-metrics.v2",
            "dimensions": dimensions,
            "frame_pairs": frame_pairs,
            "available_dimension_count": sum(
                item.get("status") == "analyzed" for item in dimensions.values()
            ),
        },
        "analyzers": analyzers,
        "findings": findings,
        "warnings": warnings,
    }
