"""Evidence-bound video shot normalization and gap detection."""
from __future__ import annotations

from math import isfinite

from .gateway_prompt_audio import (
    _asr_cue_for_shot,
    _missing_asr_value,
    _timestamped_asr_segments,
    normalize_video_audio_feature_statuses,
)


def _normalized_source_ranges(source_ranges: list[dict] | None) -> list[tuple[int, float, float]]:
    ranges = []
    for index, raw_range in enumerate(source_ranges or [], start=1):
        if not isinstance(raw_range, dict):
            continue
        try:
            range_start = float(raw_range["start_seconds"])
            range_end = float(raw_range["end_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if isfinite(range_start) and isfinite(range_end) and range_end > range_start >= 0:
            ranges.append((index, range_start, range_end))
    return ranges


def _frame_evidence_maps(
    sampled_frames: list[dict] | None,
    *,
    multi_segment: bool,
    single_range_offset: float,
) -> tuple[dict[int, float], dict[int, tuple[str, int, float, float]]]:
    frame_timestamps: dict[int, float] = {}
    frame_detected_shots: dict[int, tuple[str, int, float, float]] = {}
    for frame in sampled_frames or []:
        if not isinstance(frame, dict):
            continue
        try:
            index = int(frame.get("index"))
            timestamp = float(
                frame.get("absolute_timestamp_seconds")
                if multi_segment and frame.get("absolute_timestamp_seconds") is not None
                else frame.get("relative_timestamp_seconds")
                if frame.get("relative_timestamp_seconds") is not None
                else frame.get("timestamp_seconds")
            )
        except (TypeError, ValueError):
            continue
        if index >= 1 and isfinite(timestamp):
            frame_timestamps[index] = timestamp
        detected_shot_id = str(frame.get("detected_shot_id") or "").strip()
        try:
            detected_segment = max(1, int(frame.get("source_segment_index") or 1))
            detected_start = float(frame.get("detected_shot_start_seconds"))
            detected_end = float(frame.get("detected_shot_end_seconds"))
        except (TypeError, ValueError):
            continue
        if detected_shot_id and isfinite(detected_start) and isfinite(detected_end):
            if not multi_segment:
                detected_start -= single_range_offset
                detected_end -= single_range_offset
            if detected_end > detected_start:
                frame_detected_shots[index] = (
                    detected_shot_id,
                    detected_segment,
                    detected_start,
                    detected_end,
                )
    return frame_timestamps, frame_detected_shots


def normalize_video_shots(
    shots,
    *,
    duration_seconds: float | None = None,
    frame_count: int | None = None,
    sampled_frames: list[dict] | None = None,
    audio_analyzed: bool = False,
    audio_evidence: dict | None = None,
    audio_time_offset_seconds: float = 0.0,
    source_ranges: list[dict] | None = None,
) -> list[dict]:
    duration = float(duration_seconds) if duration_seconds is not None else None
    ranges = _normalized_source_ranges(source_ranges)
    multi_segment = len(ranges) > 1
    single_range_offset = ranges[0][1] if len(ranges) == 1 else 0.0
    frame_timestamps, frame_detected_shots = _frame_evidence_maps(
        sampled_frames,
        multi_segment=multi_segment,
        single_range_offset=single_range_offset,
    )

    audio_statuses = normalize_video_audio_feature_statuses(
        audio_evidence,
        audio_analyzed=audio_analyzed,
    )
    asr_segments = _timestamped_asr_segments(audio_evidence, audio_statuses)

    if not isinstance(shots, list):
        shots = []
    candidates = []
    for raw in shots:
        if not isinstance(raw, dict):
            continue
        try:
            raw_start = float(raw.get("start_seconds"))
            end = float(raw.get("end_seconds"))
        except (TypeError, ValueError):
            continue
        if not isfinite(raw_start) or not isfinite(end):
            continue
        start = max(0.0, raw_start)
        segment_index = None
        if multi_segment:
            try:
                requested_segment = int(raw.get("source_segment_index") or 0)
            except (TypeError, ValueError):
                requested_segment = 0
            matching = [
                item for item in ranges
                if item[1] <= start < item[2] and end > item[1]
            ]
            selected = next((item for item in matching if item[0] == requested_segment), None)
            selected = selected or (matching[0] if matching else None)
            if selected is None:
                continue
            segment_index, range_start, range_end = selected
            start = max(start, range_start)
            end = min(end, range_end)
        candidates.append((segment_index or 0, start, end, raw))
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))

    normalized: list[dict] = []
    previous_verified_end: dict[int, float] = {
        index: start for index, start, _end in ranges
    }
    previous_verified_end.setdefault(0, 0.0)
    text_fields = (
        "visual", "subject_tracking", "pose", "action", "camera", "lighting",
        "transition", "ocr",
    )
    for segment_index, start, end, raw in candidates:
        if duration is not None and not multi_segment:
            if start >= duration:
                continue
            end = min(end, duration)
        if end <= start:
            continue
        evidence = raw.get("evidence_frame_indices")
        if isinstance(evidence, list):
            valid_indices = []
            for value in evidence:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if index < 1 or (frame_count is not None and index > frame_count):
                    continue
                if frame_timestamps and index not in frame_timestamps:
                    continue
                if index not in valid_indices:
                    valid_indices.append(index)
        else:
            valid_indices = []
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = round(max(0.0, min(1.0, confidence)), 3)
        if not valid_indices or confidence <= 0:
            continue
        detected_rows = [
            frame_detected_shots[index]
            for index in valid_indices
            if index in frame_detected_shots
        ]
        detected_ids = {row[0] for row in detected_rows}
        if detected_rows and len(detected_rows) == len(valid_indices) and len(detected_ids) == 1:
            _detected_id, detected_segment, detected_start, detected_end = detected_rows[0]
            start = detected_start
            end = detected_end
            if multi_segment:
                segment_index = detected_segment
        has_cross_frame_evidence = len(valid_indices) >= 2
        start = max(start, previous_verified_end.get(segment_index, 0.0))
        if end <= start:
            continue
        if frame_timestamps:
            evidence_times = sorted(frame_timestamps[index] for index in valid_indices)
            has_cross_frame_evidence = len(set(evidence_times)) >= 2
            tolerance = 0.75
            inside_range = any(
                start - tolerance <= timestamp <= end + tolerance
                for timestamp in evidence_times
            )
            brackets_range = (
                len(evidence_times) >= 2
                and evidence_times[0] <= start
                and evidence_times[-1] >= end
            )
            single_frame_local = (
                len(evidence_times) == 1
                and end - start <= tolerance * 2
                and inside_range
            )
            if len(evidence_times) == 1:
                if not single_frame_local:
                    continue
            elif not brackets_range:
                if not inside_range:
                    continue
                supported_start = max(0.0, evidence_times[0] - tolerance)
                supported_end = evidence_times[-1] + tolerance
                if duration is not None and not multi_segment:
                    supported_end = min(duration, supported_end)
                start = max(start, supported_start)
                end = min(end, supported_end)
                if end <= start:
                    continue
        item = {
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
        }
        if multi_segment:
            item["source_segment_index"] = segment_index
        for field in text_fields:
            item[field] = str(raw.get(field) or "").strip()
        if not has_cross_frame_evidence:
            # One still frame can establish appearance and lighting, but it
            # cannot prove subject continuity, pose change, motion, camera
            # movement, or a transition. Keep
            # those provider claims out of normalized generation data.
            for field in ("subject_tracking", "pose", "action", "camera", "transition"):
                item[field] = ""
        item["audio_cue"] = _asr_cue_for_shot(
            item,
            asr_segments,
            time_offset_seconds=audio_time_offset_seconds,
        ) or _missing_asr_value(audio_evidence, audio_statuses)
        item["evidence_frame_indices"] = valid_indices
        item["confidence"] = confidence
        normalized.append(item)
        previous_verified_end[segment_index] = end
    return normalized


def video_analysis_gaps(
    shots: list[dict],
    *,
    duration_seconds: float | None = None,
    analysis_mode: str | None = None,
    source_ranges: list[dict] | None = None,
) -> list[dict]:
    ranges = []
    for index, raw_range in enumerate(source_ranges or [], start=1):
        if not isinstance(raw_range, dict):
            continue
        try:
            start = float(raw_range["start_seconds"])
            end = float(raw_range["end_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if isfinite(start) and isfinite(end) and end > start >= 0:
            ranges.append((index, start, end))
    if len(ranges) > 1:
        gaps: list[dict] = []
        for segment_index, range_start, range_end in ranges:
            cursor = range_start
            segment_shots = []
            for shot in shots:
                if not isinstance(shot, dict):
                    continue
                try:
                    shot_segment_index = int(shot.get("source_segment_index") or 0)
                except (TypeError, ValueError):
                    continue
                if shot_segment_index == segment_index:
                    segment_shots.append(shot)
            for shot in segment_shots:
                evidence = shot.get("evidence_frame_indices")
                try:
                    confidence = float(shot.get("confidence") or 0)
                    start = max(range_start, min(range_end, float(shot["start_seconds"])))
                    end = max(range_start, min(range_end, float(shot["end_seconds"])))
                except (KeyError, TypeError, ValueError):
                    continue
                if not evidence or confidence <= 0 or end <= start:
                    continue
                if start > cursor + 0.001:
                    gaps.append({
                        "source_segment_index": segment_index,
                        "start_seconds": round(cursor, 3),
                        "end_seconds": round(start, 3),
                    })
                cursor = max(cursor, end)
            if cursor < range_end - 0.001:
                gaps.append({
                    "source_segment_index": segment_index,
                    "start_seconds": round(cursor, 3),
                    "end_seconds": round(range_end, 3),
                })
        return gaps
    try:
        duration = float(duration_seconds) if duration_seconds is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is None or not isfinite(duration) or duration <= 0:
        return []
    if analysis_mode == "cover_fallback":
        return [{"start_seconds": 0.0, "end_seconds": round(duration, 3)}]

    gaps: list[dict] = []
    cursor = 0.0
    for shot in shots if isinstance(shots, list) else []:
        evidence = shot.get("evidence_frame_indices") if isinstance(shot, dict) else None
        try:
            confidence = float(shot.get("confidence")) if isinstance(shot, dict) else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        if not isinstance(evidence, list) or not evidence or confidence <= 0:
            continue
        try:
            start = max(0.0, min(duration, float(shot["start_seconds"])))
            end = max(0.0, min(duration, float(shot["end_seconds"])))
        except (KeyError, TypeError, ValueError):
            continue
        if not isfinite(start) or not isfinite(end) or end <= start:
            continue
        if start > cursor + 0.001:
            gaps.append({
                "start_seconds": round(cursor, 3),
                "end_seconds": round(start, 3),
            })
        cursor = max(cursor, end)
    if cursor < duration - 0.001:
        gaps.append({
            "start_seconds": round(cursor, 3),
            "end_seconds": round(duration, 3),
        })
    return gaps
