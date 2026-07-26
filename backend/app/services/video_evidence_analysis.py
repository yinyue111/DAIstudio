"""Independent, source-addressable evidence analyzers for reverse video."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import math
import re
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from PIL import Image

from ..config import settings
from . import gateway, image_evidence_analysis

CONTRACT_VERSION = "video-evidence.v1"
SEMANTIC_CONTRACT_VERSION = "video-semantic-evidence.v1"
# 光流运镜分类的封闭标签集：下游证据门用它和 VLM 的运镜文本做标签级对账。
CAMERA_MOTION_LABELS = ("pan", "tilt", "zoom", "static")
# 每个标签允许的方向取值（static 无方向）。
CAMERA_MOTION_DIRECTIONS = {
    "pan": ("left", "right"),
    "tilt": ("up", "down"),
    "zoom": ("in", "out"),
    "static": (),
}
# ffmpeg 硬切没有 lavfi 分值时的保守默认置信度。
DEFAULT_CUT_TRANSITION_CONFIDENCE = 0.5
SEMANTIC_CAPABILITIES = ("subject_tracking", "pose", "action", "transition")
_SEMANTIC_EVIDENCE_KEYS = {
    "subject_tracking": "tracks",
    "pose": "observations",
    "action": "events",
    "transition": "events",
}
_SEMANTIC_EVIDENCE_PREFIXES = {
    "subject_tracking": "subject-track-",
    "pose": "pose-",
    "action": "action-",
    "transition": "transition-",
}
_PROVIDER_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_PROVIDER_LAST_SUCCESS: dict[str, Any] = {}
_PROVIDER_HEALTH_CACHE: dict[str, tuple[str, float, dict[str, Any]]] = {}
_PROVIDER_STATE_LOCK = threading.RLock()
_PROVIDER_HEALTH_PROBE_LOCK = threading.Lock()
_MAX_PROVIDER_ROWS = 512
_MAX_TRACK_OBSERVATIONS = 256
_MAX_POSE_KEYPOINTS = 128
_MAX_SEMANTIC_FRAMES = 64
_MIN_VIDEO_OCR_CONFIDENCE = 0.70
_MIN_SHORT_VIDEO_OCR_CONFIDENCE = 0.85


def _semantic_runtime_config() -> dict[str, Any]:
    return {
        "url": str(settings.video_evidence_semantic_url or "").strip(),
        "api_key": str(settings.video_evidence_semantic_api_key or "").strip(),
        "timeout_seconds": settings.video_evidence_semantic_timeout_seconds,
        "health_url": str(settings.video_evidence_semantic_health_url or "").strip(),
        "health_timeout_seconds": settings.video_evidence_semantic_health_timeout_seconds,
    }


def _aggregate_analyzer_status(statuses: list[dict[str, Any]]) -> str:
    if not statuses:
        return "unsupported"
    values = [str(row.get("status") or "degraded") for row in statuses]
    if all(value == "analyzed" for value in values):
        return "analyzed"
    if all(value == "unsupported" for value in values):
        return "unsupported"
    return "degraded"


def _decode(value: str) -> Image.Image:
    header, encoded = str(value).split(",", 1)
    if not header.lower().startswith("data:image/"):
        raise ValueError("sampled frame is not an image data URI")
    image = Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))).convert("RGB")
    image.load()
    return image


def _content_hash(value: str) -> str:
    _header, encoded = str(value).split(",", 1)
    return hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()


def _stable_id(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(raw.encode()).hexdigest()[:20]


def _finite_number(
    value: Any,
    *,
    path: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be a finite number")
    if minimum is not None and number < minimum:
        raise ValueError(f"{path} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{path} must be at most {maximum}")
    return number


def _positive_index(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{path} must be a positive integer")
    return value


def _probability(value: Any, *, path: str) -> float:
    return round(_finite_number(value, path=path, minimum=0, maximum=1), 6)


def _provider_evidence_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not _PROVIDER_EVIDENCE_ID_RE.fullmatch(value):
        raise ValueError(f"{path} must be a safe ASCII evidence id")
    return value


def _provider_label(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    label = value.strip()
    if not label or len(label) > 128 or not label.isprintable():
        raise ValueError(f"{path} must be 1-128 printable characters")
    return label


def _provider_reason(value: Any, *, path: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{path} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    reason = value.strip()
    if required and not reason:
        raise ValueError(f"{path} is required")
    if reason and (len(reason) > 200 or not reason.isprintable()):
        raise ValueError(f"{path} must be at most 200 printable characters")
    return reason or None


def _normalized_bbox(value: Any, *, path: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    try:
        x, y, width, height = (
            _finite_number(value[key], path=f"{path}.{key}", minimum=0, maximum=1)
            for key in ("x", "y", "width", "height")
        )
    except KeyError as exc:
        raise ValueError(f"{path}.{exc.args[0]} is required") from exc
    if width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise ValueError(f"{path} must have positive in-bounds dimensions")
    return {
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
    }


def _frame_lookup(frames: list[dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    lookup: dict[tuple[int, int], dict[str, Any]] = {}
    for frame in frames:
        segment = _positive_index(
            frame.get("source_segment_index"), path="frames[].source_segment_index"
        )
        frame_index = _positive_index(frame.get("frame_index"), path="frames[].frame_index")
        timestamp = _finite_number(
            frame.get("timestamp_seconds"),
            path="frames[].absolute_timestamp_seconds",
            minimum=0,
        )
        key = (segment, frame_index)
        if key in lookup:
            raise ValueError("sampled frame identities must be unique within a source segment")
        lookup[key] = {
            **frame,
            "source_segment_index": segment,
            "frame_index": frame_index,
            "timestamp_seconds": round(timestamp, 3),
        }
    return lookup


def _segment_bounds(
    lookup: dict[tuple[int, int], dict[str, Any]],
) -> dict[int, tuple[float, float]]:
    grouped: dict[int, list[float]] = {}
    for (segment, _frame_index), frame in lookup.items():
        grouped.setdefault(segment, []).append(float(frame["timestamp_seconds"]))
    return {segment: (min(values), max(values)) for segment, values in grouped.items()}


def _segment(value: Any, *, path: str, bounds: dict[int, tuple[float, float]]) -> int:
    segment = _positive_index(value, path=path)
    if segment not in bounds:
        raise ValueError(f"{path} does not identify a sampled source segment")
    return segment


def _absolute_time(
    value: Any,
    *,
    path: str,
    segment: int,
    bounds: dict[int, tuple[float, float]],
) -> float:
    timestamp = _finite_number(value, path=path, minimum=0)
    minimum, maximum = bounds[segment]
    if timestamp < minimum - 0.001 or timestamp > maximum + 0.001:
        raise ValueError(f"{path} is outside the sampled absolute-time range")
    return round(timestamp, 3)


def _known_frame(
    raw: dict[str, Any],
    *,
    path: str,
    segment: int,
    lookup: dict[tuple[int, int], dict[str, Any]],
) -> tuple[int, float, dict[str, Any]]:
    frame_index = _positive_index(raw.get("frame_index"), path=f"{path}.frame_index")
    frame = lookup.get((segment, frame_index))
    if frame is None:
        raise ValueError(f"{path}.frame_index does not identify a sampled frame")
    timestamp = _finite_number(
        raw.get("absolute_timestamp_seconds"),
        path=f"{path}.absolute_timestamp_seconds",
        minimum=0,
    )
    expected = float(frame["timestamp_seconds"])
    if not math.isclose(timestamp, expected, abs_tol=0.001):
        raise ValueError(f"{path}.absolute_timestamp_seconds does not match the sampled frame")
    return frame_index, round(expected, 3), frame


def _semantic_capability_status(
    capability: str,
    status: str,
    *,
    analyzer: str,
    analyzer_version: str,
    evidence: list[dict[str, Any]] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    rows = evidence or []
    return {
        "status": status,
        "analyzer": analyzer,
        "analyzer_version": analyzer_version,
        "evidence_count": len(rows),
        "degraded_reason": reason,
        _SEMANTIC_EVIDENCE_KEYS[capability]: rows,
    }


def _semantic_failure(status: str, reason: str) -> dict[str, Any]:
    analyzer = "http_video_semantic_provider"
    return {
        "contract_version": SEMANTIC_CONTRACT_VERSION,
        **{
            capability: _semantic_capability_status(
                capability,
                status,
                analyzer=analyzer,
                analyzer_version="unconfigured" if status == "unsupported" else "unknown",
                reason=reason,
            )
            for capability in SEMANTIC_CAPABILITIES
        },
    }


def _validate_subject_tracks(
    value: Any,
    *,
    lookup: dict[tuple[int, int], dict[str, Any]],
    bounds: dict[int, tuple[float, float]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    if not isinstance(value, list) or len(value) > _MAX_PROVIDER_ROWS:
        raise ValueError("subject_tracking evidence must be an array of at most 512 rows")
    rows: list[dict[str, Any]] = []
    by_provider_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        path = f"subject_tracking.evidence[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an object")
        provider_id = _provider_evidence_id(raw.get("evidence_id"), path=f"{path}.evidence_id")
        if provider_id in by_provider_id:
            raise ValueError(f"{path}.evidence_id must be unique")
        segment = _segment(
            raw.get("source_segment_index"),
            path=f"{path}.source_segment_index",
            bounds=bounds,
        )
        observations = raw.get("observations")
        if not isinstance(observations, list) or not 1 <= len(observations) <= _MAX_TRACK_OBSERVATIONS:
            raise ValueError(f"{path}.observations must contain 1-256 rows")
        normalized_observations: list[dict[str, Any]] = []
        frame_indices: set[int] = set()
        for observation_index, observation in enumerate(observations):
            observation_path = f"{path}.observations[{observation_index}]"
            if not isinstance(observation, dict):
                raise ValueError(f"{observation_path} must be an object")
            frame_index, timestamp, frame = _known_frame(
                observation,
                path=observation_path,
                segment=segment,
                lookup=lookup,
            )
            if frame_index in frame_indices:
                raise ValueError(f"{path}.observations cannot repeat a sampled frame")
            frame_indices.add(frame_index)
            normalized_observations.append({
                "frame_index": frame_index,
                "timestamp_seconds": timestamp,
                "bbox": _normalized_bbox(
                    observation.get("bbox"), path=f"{observation_path}.bbox"
                ),
                "confidence": _probability(
                    observation.get("confidence"), path=f"{observation_path}.confidence"
                ),
                "source_content_hash": str(frame.get("source_content_hash") or ""),
                "source_fingerprint": f"sha256:{frame.get('source_content_hash')}",
            })
        normalized_observations.sort(key=lambda item: (item["timestamp_seconds"], item["frame_index"]))
        start = _absolute_time(
            raw.get("start_seconds"), path=f"{path}.start_seconds", segment=segment, bounds=bounds
        )
        end = _absolute_time(
            raw.get("end_seconds"), path=f"{path}.end_seconds", segment=segment, bounds=bounds
        )
        if end < start:
            raise ValueError(f"{path}.end_seconds must not precede start_seconds")
        if not math.isclose(start, normalized_observations[0]["timestamp_seconds"], abs_tol=0.001):
            raise ValueError(f"{path}.start_seconds must match the first observation")
        if not math.isclose(end, normalized_observations[-1]["timestamp_seconds"], abs_tol=0.001):
            raise ValueError(f"{path}.end_seconds must match the last observation")
        canonical = {
            "label": _provider_label(raw.get("label"), path=f"{path}.label"),
            "source_segment_index": segment,
            "start_seconds": start,
            "end_seconds": end,
            "confidence": _probability(raw.get("confidence"), path=f"{path}.confidence"),
            "observations": normalized_observations,
        }
        evidence_id = _stable_id(_SEMANTIC_EVIDENCE_PREFIXES["subject_tracking"], canonical)
        row = {
            **canonical,
            "evidence_id": evidence_id,
            "track_id": evidence_id,
            "provider_evidence_id": provider_id,
            "frame_indices": [item["frame_index"] for item in normalized_observations],
        }
        rows.append(row)
        by_provider_id[provider_id] = row
    return rows, by_provider_id, set(by_provider_id)


def _subject_reference(
    value: Any,
    *,
    path: str,
    subjects: dict[str, dict[str, Any]],
    segment: int,
    timestamp: float | None = None,
    start: float | None = None,
    end: float | None = None,
    required: bool,
) -> tuple[str | None, str | None]:
    if value is None and not required:
        return None, None
    provider_id = _provider_evidence_id(value, path=path)
    subject = subjects.get(provider_id)
    if subject is None:
        raise ValueError(f"{path} does not identify a validated subject track")
    if int(subject["source_segment_index"]) != segment:
        raise ValueError(f"{path} belongs to another source segment")
    subject_start = float(subject["start_seconds"])
    subject_end = float(subject["end_seconds"])
    if timestamp is not None and not subject_start - 0.001 <= timestamp <= subject_end + 0.001:
        raise ValueError(f"{path} does not cover the evidence timestamp")
    if start is not None and end is not None and (end < subject_start or start > subject_end):
        raise ValueError(f"{path} does not overlap the evidence time range")
    return provider_id, str(subject["evidence_id"])


def _validate_pose_observations(
    value: Any,
    *,
    lookup: dict[tuple[int, int], dict[str, Any]],
    bounds: dict[int, tuple[float, float]],
    subjects: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(value, list) or len(value) > _MAX_PROVIDER_ROWS:
        raise ValueError("pose evidence must be an array of at most 512 rows")
    rows: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    for index, raw in enumerate(value):
        path = f"pose.evidence[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an object")
        provider_id = _provider_evidence_id(raw.get("evidence_id"), path=f"{path}.evidence_id")
        if provider_id in provider_ids:
            raise ValueError(f"{path}.evidence_id must be unique")
        segment = _segment(
            raw.get("source_segment_index"),
            path=f"{path}.source_segment_index",
            bounds=bounds,
        )
        frame_index, timestamp, frame = _known_frame(
            raw,
            path=path,
            segment=segment,
            lookup=lookup,
        )
        subject_provider_id, subject_ref = _subject_reference(
            raw.get("subject_evidence_id"),
            path=f"{path}.subject_evidence_id",
            subjects=subjects,
            segment=segment,
            timestamp=timestamp,
            required=True,
        )
        keypoints = raw.get("keypoints")
        if not isinstance(keypoints, list) or not 1 <= len(keypoints) <= _MAX_POSE_KEYPOINTS:
            raise ValueError(f"{path}.keypoints must contain 1-128 rows")
        normalized_keypoints: list[dict[str, Any]] = []
        names: set[str] = set()
        for keypoint_index, keypoint in enumerate(keypoints):
            keypoint_path = f"{path}.keypoints[{keypoint_index}]"
            if not isinstance(keypoint, dict):
                raise ValueError(f"{keypoint_path} must be an object")
            name = _provider_label(keypoint.get("name"), path=f"{keypoint_path}.name")
            if name.casefold() in names:
                raise ValueError(f"{path}.keypoints names must be unique")
            names.add(name.casefold())
            normalized_keypoints.append({
                "name": name,
                "x": round(
                    _finite_number(
                        keypoint.get("x"), path=f"{keypoint_path}.x", minimum=0, maximum=1
                    ),
                    6,
                ),
                "y": round(
                    _finite_number(
                        keypoint.get("y"), path=f"{keypoint_path}.y", minimum=0, maximum=1
                    ),
                    6,
                ),
                "confidence": _probability(
                    keypoint.get("confidence"), path=f"{keypoint_path}.confidence"
                ),
            })
        canonical = {
            "source_segment_index": segment,
            "frame_index": frame_index,
            "timestamp_seconds": timestamp,
            "subject_evidence_ref": subject_ref,
            "confidence": _probability(raw.get("confidence"), path=f"{path}.confidence"),
            "keypoints": normalized_keypoints,
            "source_content_hash": str(frame.get("source_content_hash") or ""),
            "source_fingerprint": f"sha256:{frame.get('source_content_hash')}",
        }
        if raw.get("bbox") is not None:
            canonical["bbox"] = _normalized_bbox(raw.get("bbox"), path=f"{path}.bbox")
        rows.append({
            **canonical,
            "evidence_id": _stable_id(_SEMANTIC_EVIDENCE_PREFIXES["pose"], canonical),
            "provider_evidence_id": provider_id,
            "subject_provider_evidence_id": subject_provider_id,
        })
        provider_ids.add(provider_id)
    return rows, provider_ids


def _validate_action_events(
    value: Any,
    *,
    lookup: dict[tuple[int, int], dict[str, Any]],
    bounds: dict[int, tuple[float, float]],
    subjects: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(value, list) or len(value) > _MAX_PROVIDER_ROWS:
        raise ValueError("action evidence must be an array of at most 512 rows")
    rows: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    for index, raw in enumerate(value):
        path = f"action.evidence[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an object")
        provider_id = _provider_evidence_id(raw.get("evidence_id"), path=f"{path}.evidence_id")
        if provider_id in provider_ids:
            raise ValueError(f"{path}.evidence_id must be unique")
        segment = _segment(
            raw.get("source_segment_index"),
            path=f"{path}.source_segment_index",
            bounds=bounds,
        )
        start = _absolute_time(
            raw.get("start_seconds"), path=f"{path}.start_seconds", segment=segment, bounds=bounds
        )
        end = _absolute_time(
            raw.get("end_seconds"), path=f"{path}.end_seconds", segment=segment, bounds=bounds
        )
        if end < start:
            raise ValueError(f"{path}.end_seconds must not precede start_seconds")
        frame_indices = raw.get("frame_indices")
        if not isinstance(frame_indices, list) or not 1 <= len(frame_indices) <= _MAX_TRACK_OBSERVATIONS:
            raise ValueError(f"{path}.frame_indices must contain 1-256 sampled frames")
        normalized_indices: list[int] = []
        seen_indices: set[int] = set()
        source_hashes_by_index: dict[int, str] = {}
        for frame_offset, value_index in enumerate(frame_indices):
            frame_index = _positive_index(value_index, path=f"{path}.frame_indices[{frame_offset}]")
            frame = lookup.get((segment, frame_index))
            if frame is None:
                raise ValueError(f"{path}.frame_indices[{frame_offset}] is not a sampled frame")
            if frame_index in seen_indices:
                raise ValueError(f"{path}.frame_indices cannot contain duplicates")
            timestamp = float(frame["timestamp_seconds"])
            if not start - 0.001 <= timestamp <= end + 0.001:
                raise ValueError(f"{path}.frame_indices includes a frame outside the event range")
            seen_indices.add(frame_index)
            normalized_indices.append(frame_index)
            source_hashes_by_index[frame_index] = str(frame.get("source_content_hash") or "")
        normalized_indices.sort(key=lambda frame_index: float(lookup[(segment, frame_index)]["timestamp_seconds"]))
        subject_provider_id, subject_ref = _subject_reference(
            raw.get("subject_evidence_id"),
            path=f"{path}.subject_evidence_id",
            subjects=subjects,
            segment=segment,
            start=start,
            end=end,
            required=False,
        )
        canonical = {
            "label": _provider_label(raw.get("label"), path=f"{path}.label"),
            "source_segment_index": segment,
            "start_seconds": start,
            "end_seconds": end,
            "frame_indices": normalized_indices,
            "subject_evidence_ref": subject_ref,
            "confidence": _probability(raw.get("confidence"), path=f"{path}.confidence"),
            "source_content_hashes": [
                source_hashes_by_index[frame_index] for frame_index in normalized_indices
            ],
        }
        rows.append({
            **canonical,
            "evidence_id": _stable_id(_SEMANTIC_EVIDENCE_PREFIXES["action"], canonical),
            "provider_evidence_id": provider_id,
            "subject_provider_evidence_id": subject_provider_id,
        })
        provider_ids.add(provider_id)
    return rows, provider_ids


def _validate_transition_events(
    value: Any,
    *,
    lookup: dict[tuple[int, int], dict[str, Any]],
    bounds: dict[int, tuple[float, float]],
) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(value, list) or len(value) > _MAX_PROVIDER_ROWS:
        raise ValueError("transition evidence must be an array of at most 512 rows")
    rows: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    for index, raw in enumerate(value):
        path = f"transition.evidence[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an object")
        provider_id = _provider_evidence_id(raw.get("evidence_id"), path=f"{path}.evidence_id")
        if provider_id in provider_ids:
            raise ValueError(f"{path}.evidence_id must be unique")
        segment = _segment(
            raw.get("source_segment_index"),
            path=f"{path}.source_segment_index",
            bounds=bounds,
        )
        before_index = _positive_index(
            raw.get("before_frame_index"), path=f"{path}.before_frame_index"
        )
        after_index = _positive_index(
            raw.get("after_frame_index"), path=f"{path}.after_frame_index"
        )
        before = lookup.get((segment, before_index))
        after = lookup.get((segment, after_index))
        if before is None or after is None:
            raise ValueError(f"{path} must reference two sampled frames in the source segment")
        before_time = float(before["timestamp_seconds"])
        after_time = float(after["timestamp_seconds"])
        if before_index == after_index or after_time <= before_time:
            raise ValueError(f"{path}.after_frame_index must follow before_frame_index")
        timestamp = _absolute_time(
            raw.get("timestamp_seconds"),
            path=f"{path}.timestamp_seconds",
            segment=segment,
            bounds=bounds,
        )
        if not before_time <= timestamp <= after_time:
            raise ValueError(f"{path}.timestamp_seconds must lie between its evidence frames")
        canonical = {
            "label": _provider_label(raw.get("label"), path=f"{path}.label"),
            "source_segment_index": segment,
            "timestamp_seconds": timestamp,
            "before_frame_index": before_index,
            "after_frame_index": after_index,
            "confidence": _probability(raw.get("confidence"), path=f"{path}.confidence"),
            "source_content_hashes": [
                str(before.get("source_content_hash") or ""),
                str(after.get("source_content_hash") or ""),
            ],
        }
        rows.append({
            **canonical,
            "evidence_id": _stable_id(_SEMANTIC_EVIDENCE_PREFIXES["transition"], canonical),
            "provider_evidence_id": provider_id,
        })
        provider_ids.add(provider_id)
    return rows, provider_ids


def _normalize_semantic_provider_response(
    payload: Any,
    *,
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("semantic provider response must be an object")
    if payload.get("contract_version") != SEMANTIC_CONTRACT_VERSION:
        raise ValueError(f"semantic provider must return {SEMANTIC_CONTRACT_VERSION}")
    analyzer = _provider_label(
        payload.get("analyzer"), path="semantic provider response.analyzer"
    )
    analyzer_version = _provider_label(
        payload.get("analyzer_version"), path="semantic provider response.analyzer_version"
    )
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("semantic provider response.capabilities must be an object")
    if set(capabilities) != set(SEMANTIC_CAPABILITIES):
        raise ValueError(
            "semantic provider response.capabilities must contain exactly "
            + ", ".join(SEMANTIC_CAPABILITIES)
        )
    lookup = _frame_lookup(frames)
    if not lookup:
        raise ValueError("semantic analysis requires at least one decoded sampled frame")
    bounds = _segment_bounds(lookup)
    normalized: dict[str, Any] = {"contract_version": SEMANTIC_CONTRACT_VERSION}
    subjects: dict[str, dict[str, Any]] = {}
    used_provider_ids: set[str] = set()

    for capability in SEMANTIC_CAPABILITIES:
        raw = capabilities.get(capability)
        try:
            if not isinstance(raw, dict):
                raise ValueError(f"capabilities.{capability} must be an object")
            status = raw.get("status")
            if status not in {"analyzed", "partial", "unsupported", "degraded"}:
                raise ValueError(f"capabilities.{capability}.status is invalid")
            evidence = raw.get("evidence")
            reason = _provider_reason(
                raw.get("degraded_reason"),
                path=f"capabilities.{capability}.degraded_reason",
                required=status in {"partial", "unsupported", "degraded"},
            )
            if status in {"unsupported", "degraded"}:
                if evidence != []:
                    raise ValueError(
                        f"capabilities.{capability} cannot return evidence with status={status}"
                    )
                normalized[capability] = _semantic_capability_status(
                    capability,
                    str(status),
                    analyzer=analyzer,
                    analyzer_version=analyzer_version,
                    reason=reason,
                )
                continue
            if status == "analyzed" and reason:
                raise ValueError(
                    f"capabilities.{capability} analyzed response cannot include degraded_reason"
                )
            if capability == "subject_tracking":
                rows, validated_subjects, provider_ids = _validate_subject_tracks(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                )
                subjects = validated_subjects
            elif capability == "pose":
                rows, provider_ids = _validate_pose_observations(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                    subjects=subjects,
                )
            elif capability == "action":
                rows, provider_ids = _validate_action_events(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                    subjects=subjects,
                )
            else:
                rows, provider_ids = _validate_transition_events(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                )
            duplicate_ids = used_provider_ids & provider_ids
            if duplicate_ids:
                raise ValueError(
                    f"capabilities.{capability} reused provider evidence ids: "
                    + ", ".join(sorted(duplicate_ids)[:3])
                )
            used_provider_ids.update(provider_ids)
            effective_status = str(status)
            if not rows:
                effective_status = "partial"
                reason = reason or "provider 未返回可验证的独立证据"
            normalized[capability] = _semantic_capability_status(
                capability,
                effective_status,
                analyzer=analyzer,
                analyzer_version=analyzer_version,
                evidence=rows,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            if capability == "subject_tracking":
                subjects = {}
            normalized[capability] = _semantic_capability_status(
                capability,
                "degraded",
                analyzer=analyzer,
                analyzer_version=analyzer_version,
                reason=str(exc)[:200],
            )
    return normalized


def _record_semantic_provider_success(result: dict[str, Any], *, source: str) -> None:
    analyzer = "http_video_semantic_provider"
    analyzer_version = "unknown"
    capability_statuses: dict[str, str] = {}
    for capability in SEMANTIC_CAPABILITIES:
        status = result.get(capability)
        if not isinstance(status, dict):
            continue
        analyzer = str(status.get("analyzer") or analyzer)
        analyzer_version = str(status.get("analyzer_version") or analyzer_version)
        capability_statuses[capability] = str(status.get("status") or "degraded")
    record = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "last_success_source": source,
        "last_analyzer": analyzer,
        "last_analyzer_version": analyzer_version,
        "last_capability_statuses": capability_statuses,
    }
    with _PROVIDER_STATE_LOCK:
        _PROVIDER_LAST_SUCCESS.clear()
        _PROVIDER_LAST_SUCCESS.update(record)
        _PROVIDER_HEALTH_CACHE.clear()


def http_semantic_provider(
    images: list[Image.Image],
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    """Call the configured video semantic analyzer with a bounded strict contract."""
    config = _semantic_runtime_config()
    url = config["url"]
    api_key = config["api_key"]
    if not url:
        return _semantic_failure("unsupported", "video semantic provider 未配置")
    if not images or len(images) != len(frames):
        return _semantic_failure("degraded", "video semantic provider 缺少对应的抽样帧")
    if len(images) > _MAX_SEMANTIC_FRAMES:
        return _semantic_failure("degraded", "video semantic provider 单次最多接收 64 个抽样帧")
    try:
        timeout = min(
            120,
            max(1, int(config["timeout_seconds"])),
        )
    except (TypeError, ValueError):
        return _semantic_failure("degraded", "video semantic provider timeout 配置无效")
    request_frames: list[dict[str, Any]] = []
    try:
        for image, frame in zip(images, frames, strict=True):
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            frame_index = _positive_index(
                frame.get("frame_index"), path="sampled_frames[].frame_index"
            )
            segment = _positive_index(
                frame.get("source_segment_index"),
                path="sampled_frames[].source_segment_index",
            )
            timestamp = _finite_number(
                frame.get("timestamp_seconds"),
                path="sampled_frames[].absolute_timestamp_seconds",
                minimum=0,
            )
            request_frames.append({
                "frame_index": frame_index,
                "absolute_timestamp_seconds": round(timestamp, 3),
                "source_segment_index": segment,
                "source_content_hash": str(frame.get("source_content_hash") or ""),
                "width": image.width,
                "height": image.height,
                "image_media_type": "image/png",
                "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            })
        payload = gateway._request_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            payload={
                "contract_version": SEMANTIC_CONTRACT_VERSION,
                "capabilities": list(SEMANTIC_CAPABILITIES),
                "frames": request_frames,
            },
            timeout=timeout,
            retries=0,
            trusted_hosts=settings.trusted_analyzer_host_list,
        )
        result = _normalize_semantic_provider_response(payload, frames=frames)
        if any(
            result[capability].get("status") in {"analyzed", "partial"}
            and int(result[capability].get("evidence_count") or 0) > 0
            for capability in SEMANTIC_CAPABILITIES
        ):
            _record_semantic_provider_success(result, source="analysis")
        return result
    except Exception as exc:  # noqa: BLE001
        return _semantic_failure("degraded", str(exc)[:200])


def _semantic_provider_health() -> dict[str, Any]:
    config = _semantic_runtime_config()
    url = config["url"]
    health_url = config["health_url"]
    api_key = config["api_key"]
    analyzer = "http_video_semantic_provider"
    if not url:
        return {
            "status": "unsupported",
            "analyzer": analyzer,
            "configured": False,
            "verification_status": "unconfigured",
            "health_url_configured": False,
            "degraded_reason": "video semantic provider 未配置",
        }
    with _PROVIDER_STATE_LOCK:
        last_success = deepcopy(_PROVIDER_LAST_SUCCESS)
    if not health_url:
        if last_success:
            return {
                "status": "available",
                "analyzer": analyzer,
                "configured": True,
                "verification_status": "last_success",
                "health_url_configured": False,
                "degraded_reason": None,
                **last_success,
            }
        return {
            "status": "degraded",
            "analyzer": analyzer,
            "configured": True,
            "verification_status": "configured_unverified",
            "health_url_configured": False,
            "degraded_reason": "provider 已配置，但尚无 health probe 或成功分析记录",
        }
    cache_key = hashlib.sha256(json.dumps(
        {
            "url": url,
            "health_url": health_url,
            "api_key_sha256": hashlib.sha256(api_key.encode()).hexdigest(),
            "timeout": config["health_timeout_seconds"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()

    def cached_result() -> dict[str, Any] | None:
        now = time.monotonic()
        with _PROVIDER_STATE_LOCK:
            cached = _PROVIDER_HEALTH_CACHE.get("semantic")
            if cached and cached[0] == cache_key and cached[1] > now:
                return deepcopy(cached[2])
            if cached:
                _PROVIDER_HEALTH_CACHE.pop("semantic", None)
        return None

    cached = cached_result()
    if cached is not None:
        return cached

    with _PROVIDER_HEALTH_PROBE_LOCK:
        cached = cached_result()
        if cached is not None:
            return cached
        with _PROVIDER_STATE_LOCK:
            last_success = deepcopy(_PROVIDER_LAST_SUCCESS)
        try:
            timeout = min(10, max(1, int(config["health_timeout_seconds"])))
            payload = gateway._request_json(
                "GET",
                health_url,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                payload=None,
                timeout=timeout,
                retries=0,
                trusted_hosts=settings.trusted_analyzer_host_list,
            )
            if not isinstance(payload, dict):
                raise ValueError("health response must be an object")
            if payload.get("contract_version") != SEMANTIC_CONTRACT_VERSION:
                raise ValueError(
                    f"health response.contract_version must be {SEMANTIC_CONTRACT_VERSION}"
                )
            health_status = str(payload.get("status") or "").strip().lower()
            if payload.get("ok") is not True and health_status not in {
                "ok", "healthy", "available", "ready",
            }:
                raise ValueError("health response did not report ready")
            provider_analyzer = _provider_label(
                payload.get("analyzer"), path="health response.analyzer"
            )
            version = _provider_label(
                payload.get("analyzer_version"),
                path="health response.analyzer_version",
            )
            capability_statuses = payload.get("capability_statuses")
            if not isinstance(capability_statuses, dict):
                raise ValueError("health response.capability_statuses must be an object")
            if set(capability_statuses) != set(SEMANTIC_CAPABILITIES) or any(
                value not in {"available", "degraded", "unsupported"}
                for value in capability_statuses.values()
            ):
                raise ValueError(
                    "health response.capability_statuses must report every semantic capability"
                )
            success = {
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "last_success_source": "health_probe",
                "last_analyzer": provider_analyzer,
                "last_analyzer_version": version,
                "last_capability_statuses": dict(capability_statuses),
            }
            with _PROVIDER_STATE_LOCK:
                _PROVIDER_LAST_SUCCESS.clear()
                _PROVIDER_LAST_SUCCESS.update(deepcopy(success))
            result = {
                "status": "available",
                "analyzer": provider_analyzer,
                "analyzer_version": version,
                "configured": True,
                "verification_status": "health_probe",
                "health_url_configured": True,
                "degraded_reason": None,
                **success,
            }
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "degraded",
                "analyzer": analyzer,
                "configured": True,
                "verification_status": "health_probe_failed",
                "health_url_configured": True,
                "degraded_reason": str(exc)[:200],
                **last_success,
            }
        try:
            ttl = min(
                60,
                max(1, int(settings.video_evidence_semantic_health_cache_ttl_seconds)),
            )
        except (TypeError, ValueError):
            ttl = 15
        with _PROVIDER_STATE_LOCK:
            _PROVIDER_HEALTH_CACHE["semantic"] = (
                cache_key,
                time.monotonic() + ttl,
                deepcopy(result),
            )
        return result


def _iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    lx, ly, lw, lh = (float(left[key]) for key in ("x", "y", "width", "height"))
    rx, ry, rw, rh = (float(right[key]) for key in ("x", "y", "width", "height"))
    intersection = max(0.0, min(lx + lw, rx + rw) - max(lx, rx)) * max(
        0.0, min(ly + lh, ry + rh) - max(ly, ry)
    )
    union = lw * lh + rw * rh - intersection
    return intersection / union if union > 0 else 0.0


def build_ocr_tracks(frame_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    for row in sorted(frame_rows, key=lambda item: (item["source_segment_index"], item["timestamp_seconds"])):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        candidate = next((
            track for track in reversed(tracks)
            if track["source_segment_index"] == row["source_segment_index"]
            and track["normalized_text"] == text.casefold()
            and _iou(track["observations"][-1]["bbox"], row["bbox"]) >= 0.2
        ), None)
        if candidate is None:
            candidate = {
                "track_id": _stable_id("ocr-track-", {
                    "segment": row["source_segment_index"], "text": text.casefold(),
                    "bbox": row["bbox"], "first_frame": row["frame_index"],
                }),
                "text": text,
                "normalized_text": text.casefold(),
                "source_segment_index": row["source_segment_index"],
                "observations": [],
            }
            tracks.append(candidate)
        candidate["observations"].append(row)
    for track in tracks:
        observations = track["observations"]
        track["start_seconds"] = observations[0]["timestamp_seconds"]
        track["end_seconds"] = observations[-1]["timestamp_seconds"]
        track["frame_indices"] = [row["frame_index"] for row in observations]
        track["confidence"] = round(
            sum(float(row["confidence"]) for row in observations) / len(observations), 6
        )
        track["analyzer_status"] = "analyzed"
        track["analyzer_source"] = observations[0]["analyzer_source"]
        track["analyzer_version"] = observations[0]["analyzer_version"]
    return tracks


def _credible_video_ocr_track(track: dict[str, Any]) -> bool:
    """Reject isolated OCR fragments while keeping stable labels and copy."""
    text = re.sub(r"\s+", " ", str(track.get("text") or "")).strip()
    visible = "".join(re.findall(r"[A-Za-z0-9\u3400-\u9fff]", text))
    if not visible:
        return False
    try:
        confidence = float(track.get("confidence") or 0)
    except (TypeError, ValueError):
        return False
    observations = track.get("observations")
    observation_count = len(observations) if isinstance(observations, list) else 0
    if confidence < _MIN_VIDEO_OCR_CONFIDENCE:
        return False
    if len(visible) == 1:
        return observation_count >= 2 and confidence >= 0.92
    if re.fullmatch(r"[A-Za-z]{2}", visible):
        return observation_count >= 2 and confidence >= _MIN_SHORT_VIDEO_OCR_CONFIDENCE
    if re.fullmatch(r"[a-z]{3}", visible):
        return observation_count >= 2 and confidence >= _MIN_SHORT_VIDEO_OCR_CONFIDENCE
    return True


def cv2_motion_analysis(images: list[Image.Image], frames: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        return {
            "status": "unsupported", "analyzer": "opencv_lk_homography",
            "analyzer_version": "unavailable",
            "camera_labels": list(CAMERA_MOTION_LABELS), "samples": [],
            "excluded_cross_cut_pairs": 0,
            "degraded_reason": "服务器未安装 OpenCV/NumPy，无法形成光流运镜证据",
        }
    if len(images) < 2:
        return {
            "status": "degraded", "analyzer": "opencv_lk_homography",
            "analyzer_version": str(cv2.__version__),
            "camera_labels": list(CAMERA_MOTION_LABELS), "samples": [],
            "excluded_cross_cut_pairs": 0,
            "degraded_reason": "至少需要两帧才能分析运动",
        }
    samples: list[dict[str, Any]] = []
    excluded_cross_cut_pairs = 0
    for index in range(1, len(images)):
        previous_meta, current_meta = frames[index - 1], frames[index]
        if previous_meta["source_segment_index"] != current_meta["source_segment_index"]:
            continue
        previous_shot = previous_meta.get("detected_shot_index")
        current_shot = current_meta.get("detected_shot_index")
        if (
            isinstance(previous_shot, int)
            and isinstance(current_shot, int)
            and previous_shot != current_shot
        ):
            # 跨 ffmpeg 切点的帧对测到的是剪辑跳变而不是运镜，会产生伪运镜样本。
            excluded_cross_cut_pairs += 1
            continue
        previous = cv2.cvtColor(np.array(images[index - 1]), cv2.COLOR_RGB2GRAY)
        current = cv2.cvtColor(np.array(images[index]), cv2.COLOR_RGB2GRAY)
        points = cv2.goodFeaturesToTrack(previous, maxCorners=200, qualityLevel=0.01, minDistance=7)
        if points is None or len(points) < 4:
            continue
        moved, status, _error = cv2.calcOpticalFlowPyrLK(previous, current, points, None)
        if moved is None or status is None:
            continue
        source_points = points[status.reshape(-1) == 1]
        target_points = moved[status.reshape(-1) == 1]
        if len(source_points) < 4:
            continue
        matrix, inliers = cv2.estimateAffinePartial2D(
            source_points, target_points, method=cv2.RANSAC, ransacReprojThreshold=3.0
        )
        if matrix is None:
            continue
        width, height = previous.shape[1], previous.shape[0]
        center_x, center_y = width / 2, height / 2
        moved_center_x = (
            float(matrix[0, 0]) * center_x
            + float(matrix[0, 1]) * center_y
            + float(matrix[0, 2])
        )
        moved_center_y = (
            float(matrix[1, 0]) * center_x
            + float(matrix[1, 1]) * center_y
            + float(matrix[1, 2])
        )
        dx = (moved_center_x - center_x) / width
        dy = (moved_center_y - center_y) / height
        scale = (float(matrix[0, 0]) ** 2 + float(matrix[0, 1]) ** 2) ** 0.5
        pan, tilt, zoom = min(1.0, abs(dx) * 20), min(1.0, abs(dy) * 20), min(1.0, abs(scale - 1) * 20)
        static = max(0.0, 1.0 - max(pan, tilt, zoom))
        labels = {"pan": pan, "tilt": tilt, "zoom": zoom, "static": static}
        camera = max(labels, key=labels.get)
        # 方向约定：仿射把前一帧坐标映射到当前帧。画面内容右移(dx>0)说明相机
        # 左摇；内容下移(dy>0)说明相机上仰；scale>1 说明推近。
        if camera == "pan":
            camera_direction = "left" if dx > 0 else "right"
        elif camera == "tilt":
            camera_direction = "up" if dy > 0 else "down"
        elif camera == "zoom":
            camera_direction = "in" if scale >= 1 else "out"
        else:
            camera_direction = None
        inlier_ratio = float(inliers.mean()) if inliers is not None else 0.0
        sample = {
            "start_seconds": previous_meta["timestamp_seconds"],
            "end_seconds": current_meta["timestamp_seconds"],
            "source_segment_index": current_meta["source_segment_index"],
            "frame_indices": [previous_meta["frame_index"], current_meta["frame_index"]],
            "camera": camera,
            "camera_direction": camera_direction,
            "camera_confidence": round(labels[camera], 6),
            "camera_scores": {key: round(value, 6) for key, value in labels.items()},
            "background_motion_confidence": round(inlier_ratio, 6),
            "subject_motion_confidence": round(max(0.0, 1.0 - inlier_ratio), 6),
        }
        sample["evidence_id"] = _stable_id("motion-", sample)
        samples.append(sample)
    if samples:
        degraded_reason = None
    elif excluded_cross_cut_pairs:
        degraded_reason = "抽样帧对全部跨越镜头切点，无法形成同镜头光流证据"
    else:
        degraded_reason = "抽样帧缺少足够可跟踪特征"
    return {
        "status": "analyzed" if samples else "degraded",
        "analyzer": "opencv_lk_homography",
        "analyzer_version": str(cv2.__version__),
        "camera_labels": list(CAMERA_MOTION_LABELS),
        "samples": samples,
        "excluded_cross_cut_pairs": excluded_cross_cut_pairs,
        "degraded_reason": degraded_reason,
    }


def build_cut_transition_evidence(frame_meta: list[dict[str, Any]]) -> dict[str, Any]:
    """把 ffmpeg 场景切点整理成帧对可验证的 transition 证据。

    抽样帧带有 detected_shot_* 元数据（来自 video_frames 的镜头切分）。相邻两个
    抽样帧落在不同检测镜头时，两帧之间必然发生了硬切——切点时刻、前后帧指纹与
    lavfi 场景分值一起构成独立于 VLM 的转场证据，供下游证据门放行 transition。
    """
    analyzer_fields = {
        "analyzer": "ffmpeg_scene",
        "analyzer_version": "scene-threshold-v1",
    }
    rows = sorted(
        (meta for meta in frame_meta if isinstance(meta, dict)),
        key=lambda meta: (
            int(meta.get("source_segment_index") or 1),
            float(meta.get("timestamp_seconds") or 0),
        ),
    )
    if not any(isinstance(meta.get("detected_shot_index"), int) for meta in rows):
        return {
            "status": "unsupported",
            **analyzer_fields,
            "events": [],
            "evidence_count": 0,
            "degraded_reason": "抽样帧缺少镜头切点元数据，无法形成切点转场证据",
        }
    events: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_segment = int(previous.get("source_segment_index") or 1)
        current_segment = int(current.get("source_segment_index") or 1)
        if previous_segment != current_segment:
            continue
        previous_shot = previous.get("detected_shot_index")
        current_shot = current.get("detected_shot_index")
        if (
            not isinstance(previous_shot, int)
            or not isinstance(current_shot, int)
            or current_shot <= previous_shot
        ):
            continue
        cut_timestamp = current.get("detected_shot_start_seconds")
        if isinstance(cut_timestamp, bool) or not isinstance(cut_timestamp, (int, float)):
            continue
        previous_ts = float(previous.get("timestamp_seconds") or 0)
        current_ts = float(current.get("timestamp_seconds") or 0)
        cut_ts = float(cut_timestamp)
        if not previous_ts - 0.001 <= cut_ts <= current_ts + 0.001:
            continue
        score = current.get("detected_shot_cut_score")
        has_score = (
            not isinstance(score, bool)
            and isinstance(score, (int, float))
            and 0.0 <= float(score) <= 1.0
        )
        canonical = {
            "label": "hard_cut",
            "source_segment_index": current_segment,
            "timestamp_seconds": round(min(max(cut_ts, previous_ts), current_ts), 3),
            "before_frame_index": int(previous.get("frame_index") or 0),
            "after_frame_index": int(current.get("frame_index") or 0),
            "before_timestamp_seconds": round(previous_ts, 3),
            "after_timestamp_seconds": round(current_ts, 3),
            # 两帧之间跨越的切点数下界（>1 说明中间还有未采样到的快剪镜头）。
            "cut_count": current_shot - previous_shot,
            "confidence": (
                round(float(score), 6) if has_score
                else DEFAULT_CUT_TRANSITION_CONFIDENCE
            ),
            "confidence_source": (
                "ffmpeg_scene_score" if has_score else "default_hard_cut"
            ),
        }
        events.append({
            **canonical,
            **analyzer_fields,
            "evidence_id": _stable_id("cut-transition-", canonical),
            "source_content_hashes": [
                str(previous.get("source_content_hash") or ""),
                str(current.get("source_content_hash") or ""),
            ],
        })
    return {
        "status": "analyzed",
        **analyzer_fields,
        "events": events,
        "evidence_count": len(events),
        "degraded_reason": None,
    }


def analyze_video_evidence(
    refs: list[str],
    sampled_frames: list[dict[str, Any]],
    *,
    ocr_analyzer: Callable[[Image.Image], dict[str, Any]] | None = None,
    motion_analyzer: Callable[[list[Image.Image], list[dict[str, Any]]], dict[str, Any]] | None = None,
    semantic_analyzer: Callable[
        [list[Image.Image], list[dict[str, Any]]], dict[str, Any]
    ] | None = None,
) -> dict[str, Any]:
    ocr_analyzer = ocr_analyzer or image_evidence_analysis.tesseract_ocr
    motion_analyzer = motion_analyzer or cv2_motion_analysis
    semantic_analyzer = semantic_analyzer or http_semantic_provider
    images: list[Image.Image] = []
    frame_meta: list[dict[str, Any]] = []
    ocr_rows: list[dict[str, Any]] = []
    ocr_candidate_count = 0
    statuses: list[dict[str, Any]] = []
    for offset, ref in enumerate(refs):
        raw_meta = sampled_frames[offset] if offset < len(sampled_frames) else {}
        frame_index = int(raw_meta.get("index") or offset + 1)
        timestamp = float(raw_meta.get("absolute_timestamp_seconds", raw_meta.get("timestamp_seconds", 0)))
        segment_index = int(raw_meta.get("source_segment_index") or 1)
        meta = {
            "frame_index": frame_index,
            "timestamp_seconds": round(timestamp, 3),
            "source_segment_index": segment_index,
        }
        # 帧的镜头归属元数据（video_frames 的 ffmpeg 切分结果）：光流分析用它
        # 排除跨切点帧对，切点转场证据用它定位帧对之间的硬切。
        shot_ordinal = raw_meta.get("detected_shot_index")
        if (
            not isinstance(shot_ordinal, bool)
            and isinstance(shot_ordinal, int)
            and shot_ordinal >= 1
        ):
            meta["detected_shot_index"] = shot_ordinal
            shot_id = str(raw_meta.get("detected_shot_id") or "").strip()
            if shot_id:
                meta["detected_shot_id"] = shot_id
            shot_start = raw_meta.get("detected_shot_start_seconds")
            if (
                not isinstance(shot_start, bool)
                and isinstance(shot_start, (int, float))
                and math.isfinite(float(shot_start))
                and float(shot_start) >= 0
            ):
                meta["detected_shot_start_seconds"] = round(float(shot_start), 3)
            cut_score = raw_meta.get("detected_shot_cut_score")
            if (
                not isinstance(cut_score, bool)
                and isinstance(cut_score, (int, float))
                and 0.0 <= float(cut_score) <= 1.0
            ):
                meta["detected_shot_cut_score"] = round(float(cut_score), 6)
        try:
            image = _decode(ref)
            content_hash = _content_hash(ref)
        except Exception as exc:  # noqa: BLE001
            statuses.append({**meta, "status": "degraded", "degraded_reason": str(exc)[:200]})
            continue
        meta.update({
            "source_content_hash": content_hash,
            "source_fingerprint": f"sha256:{content_hash}",
            "width": image.width,
            "height": image.height,
        })
        images.append(image)
        frame_meta.append(meta)
        try:
            result = ocr_analyzer(image)
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "degraded", "analyzer": "independent_ocr",
                "analyzer_version": "unknown", "evidence": [], "degraded_reason": str(exc)[:200],
            }
        statuses.append({
            **meta, "status": result.get("status"), "analyzer": result.get("analyzer"),
            "analyzer_version": result.get("analyzer_version"),
            "degraded_reason": result.get("degraded_reason"),
        })
        for row in result.get("evidence") or []:
            if row.get("evidence_type") != "ocr" or not isinstance(row.get("bbox"), dict):
                continue
            ocr_candidate_count += 1
            text = re.sub(r"\s+", " ", str(row.get("evidence_text") or "")).strip()
            try:
                confidence = float(row.get("confidence") or 0)
            except (TypeError, ValueError):
                continue
            if (
                confidence < _MIN_VIDEO_OCR_CONFIDENCE
                or not re.search(r"[A-Za-z0-9\u3400-\u9fff]", text)
            ):
                continue
            observation = {
                "frame_index": frame_index, "timestamp_seconds": round(timestamp, 3),
                "source_segment_index": segment_index, "bbox": row["bbox"],
                "source_content_hash": content_hash,
                "source_fingerprint": f"sha256:{content_hash}",
                "text": text,
                "confidence": confidence,
                "analyzer_status": result.get("status"),
                "analyzer_source": result.get("analyzer"),
                "analyzer_version": result.get("analyzer_version"),
            }
            observation["evidence_id"] = _stable_id("frame-ocr-", observation)
            ocr_rows.append(observation)
    try:
        motion = motion_analyzer(images, frame_meta)
    except Exception as exc:  # noqa: BLE001
        motion = {
            "status": "degraded", "analyzer": "motion_analyzer",
            "analyzer_version": "unknown", "samples": [], "degraded_reason": str(exc)[:200],
        }
    try:
        semantic = semantic_analyzer(images, frame_meta)
    except Exception as exc:  # noqa: BLE001
        semantic = _semantic_failure("degraded", str(exc)[:200])
    tracks = [track for track in build_ocr_tracks(ocr_rows) if _credible_video_ocr_track(track)]
    accepted_ids = {
        str(row.get("evidence_id"))
        for track in tracks
        for row in track.get("observations") or []
        if row.get("evidence_id")
    }
    ocr_rows = [row for row in ocr_rows if str(row.get("evidence_id")) in accepted_ids]
    ocr_status = _aggregate_analyzer_status(statuses)
    return {
        "contract_version": CONTRACT_VERSION,
        "semantic_contract_version": semantic.get(
            "contract_version", SEMANTIC_CONTRACT_VERSION
        ),
        "frame_ocr": {
            "status": ocr_status, "frames": statuses, "observations": ocr_rows,
            "tracks": tracks, "evidence_count": len(ocr_rows),
            "rejected_evidence_count": ocr_candidate_count - len(ocr_rows),
        },
        "motion": motion,
        # Explicit name for new clients; motion remains as a compatibility key.
        "camera_motion": motion,
        # ffmpeg 场景切点整理出的硬切转场证据，独立于 http 语义 provider。
        "shot_transitions": build_cut_transition_evidence(frame_meta),
        **{
            capability: semantic.get(capability)
            or _semantic_capability_status(
                capability,
                "degraded",
                analyzer="video_semantic_analyzer",
                analyzer_version="unknown",
                reason=f"{capability} analyzer 未返回状态",
            )
            for capability in SEMANTIC_CAPABILITIES
        },
    }


def _summarize_camera_motion(
    samples: list[dict[str, Any]],
    *,
    analyzer_block: dict[str, Any],
    evidence_refs: list[str],
) -> dict[str, Any]:
    """把镜头时间窗内的光流样本聚合成可直接对账的运镜摘要。

    下游证据门拿 dominant_label / label_scores 与 VLM 的运镜文本做标签级比对，
    不再只看时间窗重叠。
    """
    usable = [
        sample for sample in samples
        if isinstance(sample.get("camera_scores"), dict)
    ]
    base = {
        "analyzer": analyzer_block.get("analyzer"),
        "analyzer_version": analyzer_block.get("analyzer_version"),
        "camera_labels": list(CAMERA_MOTION_LABELS),
        "sample_count": len(usable),
        "evidence_refs": list(evidence_refs),
    }
    if not usable:
        return {
            **base,
            "status": "no_evidence",
            "dominant_label": None,
            "dominant_direction": None,
            "label_scores": None,
            "confidence": None,
        }
    label_scores: dict[str, float] = {}
    for label in CAMERA_MOTION_LABELS:
        values = [
            float(sample["camera_scores"].get(label) or 0.0)
            for sample in usable
        ]
        label_scores[label] = round(sum(values) / len(values), 6)
    dominant = max(CAMERA_MOTION_LABELS, key=lambda label: label_scores[label])
    direction_counts: dict[str, int] = {}
    for sample in usable:
        direction = sample.get("camera_direction")
        if sample.get("camera") == dominant and isinstance(direction, str) and direction:
            direction_counts[direction] = direction_counts.get(direction, 0) + 1
    dominant_direction = None
    if direction_counts:
        dominant_direction = sorted(
            direction_counts.items(), key=lambda item: (-item[1], item[0])
        )[0][0]
    return {
        **base,
        "status": "analyzed",
        "dominant_label": dominant,
        "dominant_direction": dominant_direction,
        "label_scores": label_scores,
        "confidence": label_scores[dominant],
    }


def attach_evidence_to_shots(shots: list[dict[str, Any]], evidence: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = evidence.get("frame_ocr", {}).get("tracks", [])
    camera_motion = evidence.get("camera_motion")
    if not isinstance(camera_motion, dict):
        camera_motion = evidence.get("motion", {})
    motion_samples = camera_motion.get("samples", [])
    shot_transitions = evidence.get("shot_transitions")
    if not isinstance(shot_transitions, dict):
        shot_transitions = {}
    cut_transition_events = shot_transitions.get("events") or []
    subject_tracks = evidence.get("subject_tracking", {}).get("tracks", [])
    pose_observations = evidence.get("pose", {}).get("observations", [])
    action_events = evidence.get("action", {}).get("events", [])
    transition_events = evidence.get("transition", {}).get("events", [])
    attached: list[dict[str, Any]] = []
    for index, raw in enumerate(shots):
        shot = dict(raw)
        shot.setdefault("shot_id", _stable_id("shot-", {
            "segment": shot.get("source_segment_index", 1),
            "start": shot.get("start_seconds"), "end": shot.get("end_seconds"), "index": index,
        }))
        segment = int(shot.get("source_segment_index") or 1)
        start, end = float(shot.get("start_seconds") or 0), float(shot.get("end_seconds") or 0)
        matching_tracks = [
            track for track in tracks
            if int(track.get("source_segment_index") or 1) == segment
            and float(track.get("end_seconds") or 0) >= start
            and float(track.get("start_seconds") or 0) <= end
        ]
        matching_motion = [
            sample for sample in motion_samples
            if int(sample.get("source_segment_index") or 1) == segment
            and float(sample.get("end_seconds") or 0) >= start
            and float(sample.get("start_seconds") or 0) <= end
        ]
        matching_subjects = [
            track for track in subject_tracks
            if int(track.get("source_segment_index") or 1) == segment
            and float(track.get("end_seconds") or 0) >= start
            and float(track.get("start_seconds") or 0) <= end
        ]
        matching_pose = [
            observation for observation in pose_observations
            if int(observation.get("source_segment_index") or 1) == segment
            and start <= float(observation.get("timestamp_seconds") or 0) <= end
        ]
        matching_actions = [
            event for event in action_events
            if int(event.get("source_segment_index") or 1) == segment
            and float(event.get("end_seconds") or 0) >= start
            and float(event.get("start_seconds") or 0) <= end
        ]
        matching_transitions = [
            event for event in transition_events
            if int(event.get("source_segment_index") or 1) == segment
            and start <= float(event.get("timestamp_seconds") or 0) <= end
        ]
        matching_cut_transitions = [
            event for event in cut_transition_events
            if isinstance(event, dict)
            and int(event.get("source_segment_index") or 1) == segment
            and start <= float(event.get("timestamp_seconds") or 0) <= end
        ]
        vlm_description = str(shot.get("ocr") or "").strip()
        shot["vlm_text_description"] = vlm_description or None
        shot["ocr_track_refs"] = [track["track_id"] for track in matching_tracks]
        shot["ocr"] = " ".join(dict.fromkeys(track["text"] for track in matching_tracks))
        shot["motion_evidence_refs"] = [
            sample.get("evidence_id") or _stable_id("motion-", sample)
            for sample in matching_motion
        ]
        shot["camera_motion_evidence_refs"] = list(shot["motion_evidence_refs"])
        shot["camera_motion_summary"] = _summarize_camera_motion(
            matching_motion,
            analyzer_block=camera_motion,
            evidence_refs=shot["motion_evidence_refs"],
        )
        shot["cut_transition_evidence_refs"] = [
            str(event["evidence_id"])
            for event in matching_cut_transitions
            if event.get("evidence_id")
        ]
        shot["subject_track_refs"] = [
            str(track["evidence_id"])
            for track in matching_subjects
            if track.get("evidence_id")
        ]
        shot["pose_evidence_refs"] = [
            str(observation["evidence_id"])
            for observation in matching_pose
            if observation.get("evidence_id")
        ]
        shot["action_evidence_refs"] = [
            str(event["evidence_id"])
            for event in matching_actions
            if event.get("evidence_id")
        ]
        shot["transition_evidence_refs"] = [
            str(event["evidence_id"])
            for event in matching_transitions
            if event.get("evidence_id")
        ]
        shot["analyzer_status"] = {
            **dict(shot.get("analyzer_status") or {}),
            "ocr": evidence.get("frame_ocr", {}).get("status", "unsupported"),
            "motion": camera_motion.get("status", "unsupported"),
            "camera_motion": camera_motion.get("status", "unsupported"),
            "shot_transitions": shot_transitions.get("status", "unsupported"),
            **{
                capability: evidence.get(capability, {}).get("status", "unsupported")
                for capability in SEMANTIC_CAPABILITIES
            },
        }
        shot.setdefault("audio_refs", [])
        shot.setdefault("locked", False)
        attached.append(shot)
    return attached


def attach_audio_evidence_to_shots(
    shots: list[dict[str, Any]],
    audio_analysis: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    evidence = (
        audio_analysis.get("evidence")
        if isinstance(audio_analysis, dict) and isinstance(audio_analysis.get("evidence"), list)
        else []
    )
    status = str((audio_analysis or {}).get("status") or "not_requested")
    attached: list[dict[str, Any]] = []
    for raw in shots:
        shot = dict(raw)
        segment = int(shot.get("source_segment_index") or 1)
        start = float(shot.get("start_seconds") or 0)
        end = float(shot.get("end_seconds") or start)
        matching = [
            row for row in evidence
            if isinstance(row, dict)
            and row.get("evidence_id")
            and int(row.get("source_segment_index") or segment) == segment
            and float(row.get("end_seconds") or row.get("start_seconds") or 0) > start
            and float(row.get("start_seconds") or 0) < end
        ]
        existing = [str(value) for value in shot.get("audio_refs") or [] if str(value)]
        shot["audio_refs"] = list(dict.fromkeys([
            *existing,
            *(str(row["evidence_id"]) for row in matching),
        ]))
        analyzer_status = dict(shot.get("analyzer_status") or {})
        analyzer_status["audio"] = status
        shot["analyzer_status"] = analyzer_status
        attached.append(shot)
    return attached


def analyzer_health() -> dict[str, Any]:
    cv2_available = importlib.util.find_spec("cv2") is not None
    numpy_available = importlib.util.find_spec("numpy") is not None
    version = None
    if cv2_available:
        try:
            import cv2  # type: ignore[import-not-found]
            version = str(cv2.__version__)
        except ImportError:
            cv2_available = False
    semantic_provider = _semantic_provider_health()
    provider_capability_statuses = semantic_provider.get("last_capability_statuses")
    if not isinstance(provider_capability_statuses, dict):
        provider_capability_statuses = {}
    camera_motion_health = {
        "status": "available" if cv2_available and numpy_available else "unsupported",
        "analyzer": "opencv_lk_homography",
        "analyzer_version": version,
        "opencv_available": cv2_available,
        "numpy_available": numpy_available,
    }
    health = {
        "contract_version": CONTRACT_VERSION,
        "motion": camera_motion_health,
        "camera_motion": camera_motion_health,
        "semantic_provider": semantic_provider,
    }
    for capability in SEMANTIC_CAPABILITIES:
        recorded = str(provider_capability_statuses.get(capability) or "")
        provider_status = str(semantic_provider.get("status") or "degraded")
        status = (
            {
                "analyzed": "available",
                "available": "available",
                "partial": "degraded",
                "degraded": "degraded",
                "unsupported": "unsupported",
            }.get(recorded, provider_status)
            if provider_status == "available"
            else provider_status
        )
        health[capability] = {
            **semantic_provider,
            "status": status,
            "capability": capability,
            "provider_status": semantic_provider.get("status"),
        }
    return health
