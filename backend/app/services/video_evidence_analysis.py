"""Independent, source-addressable evidence analyzers for reverse video."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import math
import re
from collections.abc import Callable
from typing import Any

from PIL import Image

from . import image_evidence_analysis
from . import video_evidence_motion as _video_evidence_motion
from . import video_evidence_semantic as _video_evidence_semantic
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding

CONTRACT_VERSION = "video-evidence.v1"
SEMANTIC_CONTRACT_VERSION = _video_evidence_semantic.SEMANTIC_CONTRACT_VERSION
SEMANTIC_CAPABILITIES = _video_evidence_semantic.SEMANTIC_CAPABILITIES
_SEMANTIC_EVIDENCE_KEYS = _video_evidence_semantic._SEMANTIC_EVIDENCE_KEYS
_SEMANTIC_EVIDENCE_PREFIXES = _video_evidence_semantic._SEMANTIC_EVIDENCE_PREFIXES
_PROVIDER_LAST_SUCCESS = _video_evidence_semantic._PROVIDER_LAST_SUCCESS
_PROVIDER_HEALTH_CACHE = _video_evidence_semantic._PROVIDER_HEALTH_CACHE
_PROVIDER_STATE_LOCK = _video_evidence_semantic._PROVIDER_STATE_LOCK
_PROVIDER_HEALTH_PROBE_LOCK = _video_evidence_semantic._PROVIDER_HEALTH_PROBE_LOCK
_MAX_SEMANTIC_FRAMES = _video_evidence_semantic._MAX_SEMANTIC_FRAMES
_semantic_runtime_config = _video_evidence_semantic._semantic_runtime_config
_semantic_capability_status = _video_evidence_semantic._semantic_capability_status
_semantic_failure = _video_evidence_semantic._semantic_failure
_normalize_semantic_provider_response = (
    _video_evidence_semantic._normalize_semantic_provider_response
)
_record_semantic_provider_success = (
    _video_evidence_semantic._record_semantic_provider_success
)
http_semantic_provider = _video_evidence_semantic.http_semantic_provider
_semantic_provider_health = _video_evidence_semantic._semantic_provider_health
settings = _video_evidence_semantic.settings
gateway = _video_evidence_semantic.gateway
time = _video_evidence_semantic.time

CAMERA_MOTION_LABELS = _video_evidence_motion.CAMERA_MOTION_LABELS
CAMERA_MOTION_DIRECTIONS = _video_evidence_motion.CAMERA_MOTION_DIRECTIONS
cv2_motion_analysis = _video_evidence_motion.cv2_motion_analysis
# ffmpeg 硬切没有 lavfi 分值时的保守默认置信度。
DEFAULT_CUT_TRANSITION_CONFIDENCE = 0.5
_PROVIDER_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_MAX_PROVIDER_ROWS = 512
_MAX_TRACK_OBSERVATIONS = 256
_MAX_POSE_KEYPOINTS = 128
_MIN_VIDEO_OCR_CONFIDENCE = 0.70
_MIN_SHORT_VIDEO_OCR_CONFIDENCE = 0.85
_VIDEO_GENERATION_WATERMARK_RE = re.compile(
    r"^(?:(?:豆包|即梦|可灵)\s*)?(?:AI\s*)?生成$",
    re.IGNORECASE,
)


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
    if _VIDEO_GENERATION_WATERMARK_RE.fullmatch(text):
        return False
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
    if re.fullmatch(r"[A-Za-z]{4}", visible):
        return observation_count >= 2 or confidence >= 0.92
    return True


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
    for previous, current in zip(rows, rows[1:], strict=False):
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
    background_values = [
        max(0.0, min(1.0, float(sample.get("background_motion_confidence") or 0.0)))
        for sample in usable
    ]
    subject_values = [
        max(0.0, min(1.0, float(sample.get("subject_motion_confidence") or 0.0)))
        for sample in usable
    ]
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
        "background_motion_confidence": round(
            sum(background_values) / len(background_values), 6
        ),
        "subject_motion_confidence": round(
            sum(subject_values) / len(subject_values), 6
        ),
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


_video_evidence_semantic._bind_contract_helpers(
    finite_number=_finite_number,
    positive_index=_positive_index,
    provider_label=_provider_label,
    provider_reason=_provider_reason,
    frame_lookup=_frame_lookup,
    segment_bounds=_segment_bounds,
    validate_subject_tracks=_validate_subject_tracks,
    validate_pose_observations=_validate_pose_observations,
    validate_action_events=_validate_action_events,
    validate_transition_events=_validate_transition_events,
)

_install_assignment_forwarding(
    __name__,
    (_video_evidence_semantic, _video_evidence_motion),
)
