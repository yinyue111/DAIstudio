"""Editable video-shot timelines for reverse-result revisions."""
from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReverseResultRevision
from . import reverse_lineage
from .reverse_operation_records import _get_owned
from .reverse_quotes import ReverseOperationConflict, ReverseOperationInvalid


def _revision_shots(
    revision: ReverseResultRevision,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = deepcopy(revision.payload) if isinstance(revision.payload, dict) else {}
    analysis = payload.get("video_analysis")
    if not isinstance(analysis, dict) or not isinstance(analysis.get("shots"), list):
        raise ReverseOperationInvalid("反推版本没有可编辑的分镜时间线")
    shots = [dict(row) for row in analysis["shots"] if isinstance(row, dict)]
    if not shots or any(not str(row.get("shot_id") or "").strip() for row in shots):
        raise ReverseOperationInvalid("反推版本缺少稳定 shot_id")
    return payload, shots


revision_shots = _revision_shots


def _latest_timeline_revision(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
) -> ReverseResultRevision:
    operation = _get_owned(db, operation_id, user_id)
    if operation.status != "succeeded" or operation.target != "video":
        raise ReverseOperationConflict("只有成功的视频反推任务可以编辑分镜")
    revision = db.execute(
        select(ReverseResultRevision)
        .where(
            ReverseResultRevision.operation_id == operation_id,
            ReverseResultRevision.user_id == user_id,
            ReverseResultRevision.source.in_(("user_edit", "normalized")),
        )
        .order_by(ReverseResultRevision.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise ReverseOperationInvalid("反推任务没有可编辑的结果版本")
    return revision


def _shot_by_id(shots: list[dict[str, Any]], shot_id: str) -> dict[str, Any]:
    shot = next((row for row in shots if str(row.get("shot_id")) == shot_id), None)
    if shot is None:
        raise ReverseOperationInvalid("shot_id 不存在")
    return shot


_TIMED_SHOT_REFERENCE_SPECS = {
    "ocr_track_refs": ("frame_ocr", "tracks", ("track_id", "evidence_id")),
    "motion_evidence_refs": (
        "camera_motion",
        "samples",
        ("evidence_id",),
    ),
    "camera_motion_evidence_refs": (
        "camera_motion",
        "samples",
        ("evidence_id",),
    ),
    "subject_track_refs": (
        "subject_tracking",
        "tracks",
        ("evidence_id",),
    ),
    "pose_evidence_refs": ("pose", "observations", ("evidence_id",)),
    "action_evidence_refs": ("action", "events", ("evidence_id",)),
    "transition_evidence_refs": (
        "transition",
        "events",
        ("evidence_id",),
    ),
}


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timed_row_overlaps_shot(row: dict[str, Any], shot: dict[str, Any]) -> bool:
    shot_segment = int(shot.get("source_segment_index") or 1)
    try:
        row_segment = int(row.get("source_segment_index") or shot_segment)
    except (TypeError, ValueError):
        return False
    if row_segment != shot_segment:
        return False
    shot_start = _finite_float(shot.get("start_seconds"))
    shot_end = _finite_float(shot.get("end_seconds"))
    if shot_start is None or shot_end is None:
        return False
    timestamp = _finite_float(
        row.get("timestamp_seconds")
        if row.get("timestamp_seconds") is not None
        else row.get("absolute_timestamp_seconds")
    )
    if timestamp is not None:
        return shot_start <= timestamp <= shot_end
    row_start = _finite_float(row.get("start_seconds"))
    row_end = _finite_float(row.get("end_seconds"))
    if row_start is None and row_end is None:
        return False
    row_start = row_end if row_start is None else row_start
    row_end = row_start if row_end is None else row_end
    return bool(row_end >= shot_start and row_start <= shot_end)


def _reference_id(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    return next(
        (
            str(row.get(key)).strip()
            for key in keys
            if str(row.get(key) or "").strip()
        ),
        None,
    )


def _timed_evidence_sources(
    analysis: dict[str, Any],
) -> dict[str, tuple[bool, list[dict[str, Any]], tuple[str, ...]]]:
    evidence = analysis.get("evidence_analyzers")
    evidence = evidence if isinstance(evidence, dict) else {}
    sources: dict[str, tuple[bool, list[dict[str, Any]], tuple[str, ...]]] = {}
    for field, (capability, rows_key, id_keys) in _TIMED_SHOT_REFERENCE_SPECS.items():
        capability_result = evidence.get(capability)
        if capability == "camera_motion" and not isinstance(capability_result, dict):
            capability_result = evidence.get("motion")
        available = isinstance(capability_result, dict) and isinstance(
            capability_result.get(rows_key), list
        )
        rows = (
            [row for row in capability_result[rows_key] if isinstance(row, dict)]
            if available
            else []
        )
        sources[field] = (available, rows, id_keys)
    audio = analysis.get("audio")
    audio_available = isinstance(audio, dict) and isinstance(audio.get("evidence"), list)
    audio_rows = (
        [row for row in audio["evidence"] if isinstance(row, dict)]
        if audio_available
        else []
    )
    sources["audio_refs"] = (audio_available, audio_rows, ("evidence_id",))
    return sources


def _rebind_shot_timed_evidence(
    analysis: dict[str, Any],
    shots: list[dict[str, Any]],
    *,
    clear_unresolved: bool,
) -> None:
    sampled_frames = analysis.get("sampled_frames")
    frames_available = isinstance(sampled_frames, list)
    frame_rows = (
        [row for row in sampled_frames if isinstance(row, dict)]
        if frames_available
        else []
    )
    sources = _timed_evidence_sources(analysis)
    for shot in shots:
        if frames_available:
            frame_indices: set[int] = set()
            for row in frame_rows:
                if not _timed_row_overlaps_shot(row, shot):
                    continue
                try:
                    frame_indices.add(int(row.get("index")))
                except (TypeError, ValueError):
                    continue
            shot["evidence_frame_indices"] = sorted(frame_indices)
        elif clear_unresolved:
            shot["evidence_frame_indices"] = []
        for field, (available, rows, id_keys) in sources.items():
            if available:
                shot[field] = list(
                    dict.fromkeys(
                        ref
                        for row in rows
                        if _timed_row_overlaps_shot(row, shot)
                        and (ref := _reference_id(row, id_keys))
                    )
                )
            elif clear_unresolved:
                shot[field] = []


def _invalidate_timeline_compilation(
    payload: dict[str, Any], shots: list[dict[str, Any]]
) -> None:
    for shot in shots:
        shot.pop("compiled_prompt", None)
        shot.pop("compilation", None)
    payload.pop("model_compiled", None)
    payload.pop("compiler_metadata", None)
    payload.pop("compiled_prompt", None)


def edit_shot_timeline(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    body: Any,
) -> ReverseResultRevision:
    replay = db.execute(
        select(ReverseResultRevision)
        .where(
            ReverseResultRevision.operation_id == operation_id,
            ReverseResultRevision.user_id == user_id,
            ReverseResultRevision.source == "user_edit",
        )
        .order_by(ReverseResultRevision.version.desc())
    ).scalars()
    for revision in replay:
        payload = revision.payload if isinstance(revision.payload, dict) else {}
        metadata = payload.get("timeline_edit")
        if isinstance(metadata, dict) and metadata.get("client_request_id") == body.client_request_id:
            if metadata.get("request_hash") != reverse_lineage.canonical_payload_hash(
                body.model_dump(mode="json")
            ):
                raise ReverseOperationConflict("client_request_id 已用于不同分镜编辑")
            return revision

    parent = _latest_timeline_revision(db, operation_id=operation_id, user_id=user_id)
    payload, shots = _revision_shots(parent)
    action = body.action
    if action == "lock":
        shot = _shot_by_id(shots, body.shot_id)
        shot["locked"] = bool(body.locked)
    elif action == "split":
        shot = _shot_by_id(shots, body.shot_id)
        if shot.get("locked"):
            raise ReverseOperationConflict("锁定镜头不能拆分")
        start, end = float(shot["start_seconds"]), float(shot["end_seconds"])
        split = float(body.split_seconds)
        if not start < split < end:
            raise ReverseOperationInvalid("拆分时间必须位于镜头内部")
        position = shots.index(shot)
        base = str(shot["shot_id"])
        left = {
            **deepcopy(shot),
            "shot_id": f"{base}:a:{hashlib.sha256(str(split).encode()).hexdigest()[:8]}",
            "end_seconds": split,
        }
        right = {
            **deepcopy(shot),
            "shot_id": f"{base}:b:{hashlib.sha256(str(split).encode()).hexdigest()[:8]}",
            "start_seconds": split,
        }
        analysis_payload = payload.get("video_analysis") or {}
        _rebind_shot_timed_evidence(
            analysis_payload, [left, right], clear_unresolved=True
        )
        _invalidate_timeline_compilation(payload, [left, right])
        shots[position:position + 1] = [left, right]
    elif action == "merge":
        selected = [_shot_by_id(shots, shot_id) for shot_id in body.shot_ids]
        positions = sorted(shots.index(row) for row in selected)
        if positions != list(range(positions[0], positions[-1] + 1)):
            raise ReverseOperationInvalid("只能合并时间线中相邻的镜头")
        if any(row.get("locked") for row in selected):
            raise ReverseOperationConflict("锁定镜头不能合并")
        segments = {int(row.get("source_segment_index") or 1) for row in selected}
        if len(segments) != 1:
            raise ReverseOperationInvalid("不能跨源片段合并镜头")
        ordered = [shots[index] for index in positions]
        merged = {
            **ordered[0],
            "shot_id": "shot-merge-" + hashlib.sha256(
                "|".join(str(row["shot_id"]) for row in ordered).encode()
            ).hexdigest()[:20],
            "start_seconds": min(float(row["start_seconds"]) for row in ordered),
            "end_seconds": max(float(row["end_seconds"]) for row in ordered),
            "evidence_frame_indices": sorted({
                int(value) for row in ordered for value in row.get("evidence_frame_indices", [])
            }),
            "ocr_track_refs": sorted({
                str(value) for row in ordered for value in row.get("ocr_track_refs", [])
            }),
            "audio_refs": sorted({
                str(value) for row in ordered for value in row.get("audio_refs", [])
            }),
            "confidence": round(
                sum(float(row.get("confidence") or 0) for row in ordered) / len(ordered),
                6,
            ),
        }
        for key in ("visual", "action", "camera", "lighting", "transition", "ocr", "audio_cue"):
            merged[key] = "；".join(dict.fromkeys(
                str(row.get(key) or "").strip()
                for row in ordered
                if str(row.get(key) or "").strip()
            ))
        for key in _TIMED_SHOT_REFERENCE_SPECS:
            merged[key] = list(dict.fromkeys(
                str(value)
                for row in ordered
                for value in row.get(key, [])
                if str(value)
            ))
        analysis_payload = payload.get("video_analysis") or {}
        _rebind_shot_timed_evidence(
            analysis_payload, [merged], clear_unresolved=False
        )
        _invalidate_timeline_compilation(payload, [merged])
        shots[positions[0]:positions[-1] + 1] = [merged]
    elif action == "reorder":
        current = [str(row["shot_id"]) for row in shots]
        if len(body.ordered_shot_ids) != len(current) or set(body.ordered_shot_ids) != set(current):
            raise ReverseOperationInvalid("ordered_shot_ids 必须完整且不能重复")
        by_id = {str(row["shot_id"]): row for row in shots}
        shots = [by_id[shot_id] for shot_id in body.ordered_shot_ids]
    elif action == "boundary":
        left = _shot_by_id(shots, body.shot_ids[0])
        right = _shot_by_id(shots, body.shot_ids[1])
        left_index = shots.index(left)
        if left_index + 1 >= len(shots) or shots[left_index + 1] is not right:
            raise ReverseOperationInvalid("只能调整相邻镜头的共享边界")
        if left.get("locked") or right.get("locked"):
            raise ReverseOperationConflict("锁定镜头的边界不能调整")
        if int(left.get("source_segment_index") or 1) != int(
            right.get("source_segment_index") or 1
        ):
            raise ReverseOperationInvalid("不能跨源片段调整镜头边界")
        boundary = float(body.boundary_seconds)
        if not float(left["start_seconds"]) < boundary < float(right["end_seconds"]):
            raise ReverseOperationInvalid("边界必须位于两个镜头的总时间范围内")
        original_left_indices = {
            int(value) for value in left.get("evidence_frame_indices", [])
        }
        original_right_indices = {
            int(value) for value in right.get("evidence_frame_indices", [])
        }
        evidence_indices = original_left_indices | original_right_indices
        analysis_payload = payload.get("video_analysis") or {}
        frame_times: dict[int, float] = {}
        for row in analysis_payload.get("sampled_frames") or []:
            if not isinstance(row, dict):
                continue
            try:
                frame_index = int(row.get("index"))
                timestamp = float(
                    row.get("absolute_timestamp_seconds")
                    if row.get("absolute_timestamp_seconds") is not None
                    else row.get("timestamp_seconds")
                )
            except (TypeError, ValueError):
                continue
            if int(row.get("source_segment_index") or 1) == int(
                left.get("source_segment_index") or 1
            ):
                frame_times[frame_index] = timestamp
        left["end_seconds"] = boundary
        right["start_seconds"] = boundary
        left["evidence_frame_indices"] = sorted(
            index
            for index in evidence_indices
            if frame_times.get(index, boundary + 1) <= boundary
            or index in original_left_indices and index not in frame_times
        )
        right["evidence_frame_indices"] = sorted(
            index
            for index in evidence_indices
            if frame_times.get(index, boundary - 1) >= boundary
            or index in original_right_indices and index not in frame_times
        )
        _rebind_shot_timed_evidence(
            analysis_payload, [left, right], clear_unresolved=True
        )
        _invalidate_timeline_compilation(payload, [left, right])

    analysis = dict(payload["video_analysis"])
    analysis["shots"] = shots
    payload["video_analysis"] = analysis
    payload["timeline_edit"] = {
        "client_request_id": body.client_request_id,
        "request_hash": reverse_lineage.canonical_payload_hash(body.model_dump(mode="json")),
        "action": action,
    }
    from .reverse_revision_lifecycle import create_result_revision

    return create_result_revision(
        db,
        operation_id=operation_id,
        user_id=user_id,
        source="user_edit",
        payload=payload,
        parent_revision_id=int(parent.id),
    )
