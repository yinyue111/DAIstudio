"""Server-authoritative audio and ASR evidence normalization."""
from __future__ import annotations

import re
from math import isfinite

_AUDIO_FEATURE_KEYS = ("asr", "speaker", "music", "beat", "sfx")
_AUDIO_AVAILABLE_STATUSES = frozenset({"analyzed", "partial"})
_AUDIO_KNOWN_STATUSES = frozenset({
    "analyzed",
    "partial",
    "degraded",
    "unsupported",
    "disabled",
    "failed",
    "no_audio",
    "not_analyzed",
})


def normalize_video_audio_feature_statuses(
    audio_evidence: dict | None,
    *,
    audio_analyzed: bool = False,
) -> dict[str, str]:
    """Resolve each audio analyzer independently; ASR never promotes peers."""
    evidence = audio_evidence if isinstance(audio_evidence, dict) else {}
    features = evidence.get("features") if isinstance(evidence.get("features"), dict) else {}
    evidence_status = str(evidence.get("status") or "").strip().lower()
    result: dict[str, str] = {}
    for feature in _AUDIO_FEATURE_KEYS:
        raw = features.get(feature)
        status = (
            str(raw.get("status") or "").strip().lower()
            if isinstance(raw, dict)
            else ""
        )
        if not status and feature == "asr":
            # Compatibility for evidence produced before the feature contract:
            # the server-side boolean was true only for timestamped ASR rows.
            if audio_analyzed:
                status = "partial" if evidence_status == "partial" else "analyzed"
            elif evidence_status in _AUDIO_KNOWN_STATUSES - _AUDIO_AVAILABLE_STATUSES:
                status = evidence_status
        if status not in _AUDIO_KNOWN_STATUSES:
            status = "unsupported"
        result[feature] = status
    return result


def _timestamped_asr_segments(
    audio_evidence: dict | None,
    statuses: dict[str, str],
) -> list[dict]:
    if statuses.get("asr") not in _AUDIO_AVAILABLE_STATUSES:
        return []
    raw_segments = (
        audio_evidence.get("segments")
        if isinstance(audio_evidence, dict) and isinstance(audio_evidence.get("segments"), list)
        else []
    )
    segments: list[dict] = []
    for raw in raw_segments[:1000]:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        try:
            start = float(raw.get("start_seconds"))
            end = float(raw.get("end_seconds"))
        except (TypeError, ValueError):
            continue
        if not text or not isfinite(start) or not isfinite(end) or end <= start or start < 0:
            continue
        item = {
            "start_seconds": start,
            "end_seconds": end,
            "text": text[:2000],
        }
        try:
            segment_index = int(raw.get("source_segment_index"))
        except (TypeError, ValueError):
            segment_index = None
        if segment_index is not None and segment_index >= 1:
            item["source_segment_index"] = segment_index
        segments.append(item)
    segments.sort(key=lambda item: (item["start_seconds"], item["end_seconds"]))
    return segments


def _asr_transcript_text(segments: list[dict]) -> str:
    rows = [
        f"[{item['start_seconds']:.3f}-{item['end_seconds']:.3f}秒] {item['text']}"
        for item in segments
    ]
    return "；".join(rows)[:24_000]


def _asr_cue_for_shot(
    shot: dict,
    segments: list[dict],
    *,
    time_offset_seconds: float = 0.0,
) -> str:
    try:
        shot_start = float(shot.get("start_seconds")) + time_offset_seconds
        shot_end = float(shot.get("end_seconds")) + time_offset_seconds
    except (TypeError, ValueError):
        return ""
    try:
        shot_segment_index = int(shot.get("source_segment_index"))
    except (TypeError, ValueError):
        shot_segment_index = None
    texts: list[str] = []
    for segment in segments:
        segment_index = segment.get("source_segment_index")
        if (
            shot_segment_index is not None
            and segment_index is not None
            and shot_segment_index != segment_index
        ):
            continue
        if segment["end_seconds"] <= shot_start or segment["start_seconds"] >= shot_end:
            continue
        text = str(segment.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
    return ("对白/旁白：" + " / ".join(texts))[:4000] if texts else ""


def _feature(audio_evidence: dict | None, key: str) -> dict:
    if not isinstance(audio_evidence, dict):
        return {}
    features = audio_evidence.get("features")
    if not isinstance(features, dict):
        return {}
    value = features.get(key)
    return value if isinstance(value, dict) else {}


def _timestamped_feature_rows(audio_evidence: dict | None, key: str) -> list[dict]:
    rows: list[dict] = []
    for raw in _feature(audio_evidence, key).get("evidence") or []:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start_seconds"))
            end = float(raw.get("end_seconds"))
        except (TypeError, ValueError):
            continue
        if not isfinite(start) or not isfinite(end) or end <= start or start < 0:
            continue
        item = {**raw, "start_seconds": start, "end_seconds": end}
        try:
            source_segment_index = int(raw.get("source_segment_index"))
        except (TypeError, ValueError):
            source_segment_index = None
        if source_segment_index is not None and source_segment_index >= 1:
            item["source_segment_index"] = source_segment_index
        rows.append(item)
    return sorted(rows, key=lambda item: (item["start_seconds"], item["end_seconds"]))


def _music_assessment_text(value: object) -> str:
    return {
        "likely": "检测到持续音乐可能性较高",
        "uncertain": "持续音乐可能性不确定",
        "unlikely": "未检测到明显持续音乐",
    }.get(str(value or "").strip().lower(), "")


def _feature_bpm(feature: dict, source_segment_index: int | None = None) -> float | None:
    candidates = []
    if source_segment_index is not None:
        for row in feature.get("segments") or []:
            if not isinstance(row, dict):
                continue
            try:
                row_segment_index = int(row.get("source_segment_index") or 0)
            except (TypeError, ValueError):
                continue
            if row_segment_index == source_segment_index:
                candidates.append(row.get("bpm"))
    candidates.append(feature.get("bpm"))
    for raw in candidates:
        try:
            bpm = float(raw)
        except (TypeError, ValueError):
            continue
        if isfinite(bpm) and 20 <= bpm <= 400:
            return bpm
    return None


def _shot_window(
    shot: dict,
    *,
    time_offset_seconds: float = 0.0,
) -> tuple[float, float, int | None] | None:
    try:
        start = float(shot.get("start_seconds")) + time_offset_seconds
        end = float(shot.get("end_seconds")) + time_offset_seconds
    except (TypeError, ValueError):
        return None
    if not isfinite(start) or not isfinite(end) or end <= start:
        return None
    try:
        source_segment_index = int(shot.get("source_segment_index"))
    except (TypeError, ValueError):
        source_segment_index = None
    return start, end, source_segment_index


def _overlapping_rows(
    rows: list[dict],
    window: tuple[float, float, int | None] | None,
) -> list[dict]:
    if window is None:
        return []
    start, end, source_segment_index = window
    matched = []
    for row in rows:
        row_segment_index = row.get("source_segment_index")
        if (
            source_segment_index is not None
            and row_segment_index is not None
            and source_segment_index != row_segment_index
        ):
            continue
        if row["end_seconds"] <= start or row["start_seconds"] >= end:
            continue
        matched.append(row)
    return matched


def _non_speech_audio_summary(
    audio_evidence: dict | None,
    statuses: dict[str, str],
) -> str:
    parts: list[str] = []
    music = _feature(audio_evidence, "music")
    music_rows = _timestamped_feature_rows(audio_evidence, "music")
    if statuses.get("music") in _AUDIO_AVAILABLE_STATUSES and music_rows:
        assessment = music_rows[0].get("assessment") or music.get("assessment")
        text = _music_assessment_text(assessment)
        if text:
            parts.append(text)

    beat = _feature(audio_evidence, "beat")
    beat_rows = _timestamped_feature_rows(audio_evidence, "beat")
    if statuses.get("beat") in _AUDIO_AVAILABLE_STATUSES and beat_rows:
        bpm = _feature_bpm(beat)
        parts.append(f"节拍约 {bpm:.1f} BPM" if bpm is not None else "检测到节拍点")

    transient_rows = [
        row for row in _timestamped_feature_rows(audio_evidence, "sfx")
        if str(row.get("label") or "").strip() == "unclassified_transient"
    ]
    if statuses.get("sfx") in _AUDIO_AVAILABLE_STATUSES and transient_rows:
        times = "、".join(f"{row['start_seconds']:.2f}秒" for row in transient_rows[:6])
        suffix = "等" if len(transient_rows) > 6 else ""
        parts.append(
            f"在 {times}{suffix} 检测到 {len(transient_rows)} 个未分类瞬态声"
        )
    return "；".join(parts)[:24_000]


def _non_speech_cue_for_shot(
    shot: dict,
    audio_evidence: dict | None,
    statuses: dict[str, str],
    *,
    time_offset_seconds: float = 0.0,
) -> str:
    window = _shot_window(shot, time_offset_seconds=time_offset_seconds)
    parts: list[str] = []

    music = _feature(audio_evidence, "music")
    music_rows = _overlapping_rows(
        _timestamped_feature_rows(audio_evidence, "music"),
        window,
    )
    if statuses.get("music") in _AUDIO_AVAILABLE_STATUSES and music_rows:
        assessment = music_rows[0].get("assessment") or music.get("assessment")
        if str(assessment or "").strip().lower() == "likely":
            parts.append("持续背景音乐")

    return "；".join(parts)[:4000]


_NON_EXECUTABLE_AUDIO_CUE_RE = re.compile(
    r"BPM|未分类瞬态|检测到节拍点|节拍点秒数|持续音乐可能性|"
    r"未检测到明显持续音乐|(?:可能|疑似|不确定|无法)(?:是|为|判断|确认)?",
    re.IGNORECASE,
)
_SPEECH_AUDIO_CUE_RE = re.compile(r"对白|旁白|台词|说话|人声转写")
_EMPTY_AUDIO_CUES = frozenset({"", "无", "未见", "未分析", "未支持", "不确定", "未知"})


def _provider_semantic_audio_cue(value: object) -> str:
    """Keep model-interpreted sound semantics, never raw detector prose."""
    clauses: list[str] = []
    for raw in re.split(r"[；;\n]+", str(value or "")):
        clause = raw.strip(" ,，;；。、")
        if (
            clause in _EMPTY_AUDIO_CUES
            or _NON_EXECUTABLE_AUDIO_CUE_RE.search(clause)
            or _SPEECH_AUDIO_CUE_RE.search(clause)
        ):
            continue
        clauses.append(clause)
    return "；".join(dict.fromkeys(clauses))[:4000]


def _shot_has_non_speech_evidence(
    shot: dict,
    audio_evidence: dict | None,
    statuses: dict[str, str],
    *,
    time_offset_seconds: float = 0.0,
) -> bool:
    window = _shot_window(shot, time_offset_seconds=time_offset_seconds)
    return any(
        statuses.get(feature) in _AUDIO_AVAILABLE_STATUSES
        and bool(_overlapping_rows(_timestamped_feature_rows(audio_evidence, feature), window))
        for feature in ("music", "beat", "sfx")
    )


def video_audio_context_rows(audio_evidence: dict | None) -> list[str]:
    """Render bounded, non-semantic signal evidence for the main model."""
    if not isinstance(audio_evidence, dict):
        return []
    statuses = normalize_video_audio_feature_statuses(audio_evidence)
    rows: list[str] = []
    music = _feature(audio_evidence, "music")
    if statuses.get("music") in _AUDIO_AVAILABLE_STATUSES:
        for item in _timestamped_feature_rows(audio_evidence, "music")[:12]:
            assessment = item.get("assessment") or music.get("assessment") or "unknown"
            rows.append(
                f"音乐信号 [{item['start_seconds']:.3f}-{item['end_seconds']:.3f}] "
                f"持续音乐可能性={assessment}"
            )
    beat = _feature(audio_evidence, "beat")
    bpm = _feature_bpm(beat)
    beat_rows = (
        _timestamped_feature_rows(audio_evidence, "beat")
        if statuses.get("beat") in _AUDIO_AVAILABLE_STATUSES
        else []
    )
    if bpm is not None and beat_rows:
        rows.append(f"节拍估计 BPM={bpm:.3f}")
    if beat_rows:
        points = ", ".join(f"{item['start_seconds']:.3f}" for item in beat_rows[:24])
        rows.append(f"节拍点秒数={points}" + (" ..." if len(beat_rows) > 24 else ""))
    transient_rows = (
        [
            item for item in _timestamped_feature_rows(audio_evidence, "sfx")
            if str(item.get("label") or "").strip() == "unclassified_transient"
        ]
        if statuses.get("sfx") in _AUDIO_AVAILABLE_STATUSES
        else []
    )
    if transient_rows:
        points = ", ".join(f"{item['start_seconds']:.3f}" for item in transient_rows[:24])
        rows.append(
            f"未分类瞬态声秒数={points}"
            + (" ..." if len(transient_rows) > 24 else "")
        )
    return rows


def _missing_asr_value(
    audio_evidence: dict | None,
    statuses: dict[str, str],
) -> str:
    if not isinstance(audio_evidence, dict):
        return "未见" if statuses.get("asr") in _AUDIO_AVAILABLE_STATUSES else "未分析"
    if statuses.get("asr") == "unsupported":
        return "未支持"
    if statuses.get("asr") in _AUDIO_AVAILABLE_STATUSES:
        return "未见"
    return "未分析"


def sanitize_video_audio_evidence(
    structured: dict,
    shots: list[dict],
    *,
    audio_evidence: dict | None = None,
    audio_analyzed: bool = False,
    shot_time_offset_seconds: float = 0.0,
) -> dict[str, str]:
    """Bind model-interpreted audio semantics to server-side signal evidence."""
    statuses = normalize_video_audio_feature_statuses(
        audio_evidence,
        audio_analyzed=audio_analyzed,
    )
    segments = _timestamped_asr_segments(audio_evidence, statuses)
    if isinstance(structured, dict):
        structured["旁白"] = _asr_transcript_text(segments) if segments else _missing_asr_value(
            audio_evidence,
            statuses,
        )
        non_asr_statuses = [statuses[key] for key in ("music", "beat", "sfx")]
        non_speech_summary = _non_speech_audio_summary(audio_evidence, statuses)
        if not isinstance(audio_evidence, dict):
            structured["音效"] = "未分析"
        elif non_speech_summary:
            structured["音效"] = non_speech_summary
        elif all(status == "unsupported" for status in non_asr_statuses):
            structured["音效"] = "未支持"
        elif any(status in _AUDIO_AVAILABLE_STATUSES for status in non_asr_statuses):
            # A status without normalized event rows still cannot authorize
            # arbitrary provider prose. A future analyzer must supply evidence.
            structured["音效"] = "未见"
        else:
            structured["音效"] = "未分析"
    for shot in shots if isinstance(shots, list) else []:
        if not isinstance(shot, dict):
            continue
        provider_signal_cue = (
            _provider_semantic_audio_cue(shot.get("audio_cue"))
            if _shot_has_non_speech_evidence(
                shot,
                audio_evidence,
                statuses,
                time_offset_seconds=shot_time_offset_seconds,
            )
            else ""
        )
        speech_cue = _asr_cue_for_shot(
            shot,
            segments,
            time_offset_seconds=shot_time_offset_seconds,
        )
        signal_cue = _non_speech_cue_for_shot(
            shot,
            audio_evidence,
            statuses,
            time_offset_seconds=shot_time_offset_seconds,
        )
        shot["audio_cue"] = "；".join(dict.fromkeys(
            filter(None, [speech_cue, provider_signal_cue, signal_cue])
        )) or _missing_asr_value(
            audio_evidence,
            statuses,
        )
    return statuses
