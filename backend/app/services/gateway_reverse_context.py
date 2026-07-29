"""Source-video context rendering for reverse-prompt requests."""
from __future__ import annotations

import math
import re

from .gateway_prompt_audio import (
    normalize_video_audio_feature_statuses as _normalize_video_audio_feature_statuses,
)
from .gateway_prompt_audio import (
    video_audio_context_rows,
)


def _video_analysis_context_text(video_analysis: dict) -> str:
    source = video_analysis.get("source") or {}
    duration = source.get("duration_seconds")
    fps = source.get("fps")
    audio = video_analysis.get("audio") if isinstance(video_analysis.get("audio"), dict) else {}
    audio_segments = audio.get("segments") if isinstance(audio.get("segments"), list) else []
    audio_feature_statuses = _normalize_video_audio_feature_statuses(
        audio,
        audio_analyzed=bool(source.get("audio_analyzed")),
    )
    audio_evidence = bool(
        audio_feature_statuses["asr"] in {"analyzed", "partial"}
        and any(isinstance(item, dict) and str(item.get("text") or "").strip() for item in audio_segments)
    )
    if audio_evidence:
        audio_state = "已完成带时间戳的音频转写，可依据下方证据分析"
    elif source.get("has_audio"):
        audio_state = "已检测到音轨,但未形成可验证的转写证据"
    else:
        audio_state = "未检测到音轨"
    analysis_mode = video_analysis.get("analysis_mode")
    mode_text = (
        "封面单帧降级分析,只能确认静态画面,不得推断完整时序动作"
        if analysis_mode == "cover_fallback"
        else "多片段关键帧分析"
        if analysis_mode == "keyframes_multi_segment"
        else "全时段关键帧分析"
    )
    lines = [
        "以下是后端探测的源视频权威事实,必须原样采用,不得根据静帧重新猜测:",
        f"- 分析模式: {mode_text}",
        f"- 显示尺寸: {source.get('width')}x{source.get('height')}"
        if source.get("width") and source.get("height")
        else "- 显示尺寸: 未知",
        f"- 画幅比例: {source.get('ratio') or '未知'}",
        f"- 本次分析片段时长: {float(duration):.3f} 秒" if duration is not None else "- 本次分析片段时长: 未知",
        f"- 帧率: 约 {float(fps):.3f} fps" if fps is not None else "- 帧率: 未知",
        f"- 音频: {audio_state}",
    ]
    if audio:
        lines.append(
            "- 音频子能力状态: "
            + ", ".join(
                f"{feature}={audio_feature_statuses[feature]}"
                for feature in ("asr", "speaker", "music", "beat", "sfx")
            )
        )
        lines.append(
            "- 每项音频结论必须由对应子能力独立支撑;"
            "ASR 仅支撑转写文字,不得推断说话人、音乐、BPM/节拍或音效。"
        )
    source_range = source.get("source_range")
    if isinstance(source_range, dict):
        lines.append(
            "- 原视频分析范围: "
            f"{float(source_range.get('start_seconds') or 0):.3f}-"
            f"{float(source_range.get('end_seconds') or 0):.3f} 秒；"
            "分镜起止时间必须使用片段内相对时间"
        )
    source_ranges = source.get("source_ranges")
    if isinstance(source_ranges, list) and len(source_ranges) > 1:
        lines.append("- 本次选中多个原视频片段：")
        for index, item in enumerate(source_ranges, start=1):
            if not isinstance(item, dict):
                continue
            lines.append(
                f"  - 片段 {index}: "
                f"{float(item.get('start_seconds') or 0):.3f}-"
                f"{float(item.get('end_seconds') or 0):.3f} 秒"
            )
        lines.append(
            "- 多片段分镜必须输出 source_segment_index，"
            "start_seconds/end_seconds 使用原视频绝对时间，不得跨片段。"
        )
    else:
        lines.append(
            "- 本次只有 1 个源片段；source_segment_index 是源片段号而不是帧号，"
            "如输出该字段必须固定为 1。"
        )
    if source.get("total_duration_seconds") is not None:
        lines.append(f"- 原视频总时长: {float(source['total_duration_seconds']):.3f} 秒")
    shot_detection = (
        video_analysis.get("shot_detection")
        if isinstance(video_analysis.get("shot_detection"), dict)
        else {}
    )
    detected_shots = (
        shot_detection.get("shots")
        if isinstance(shot_detection.get("shots"), list)
        else []
    )
    if shot_detection:
        lines.append(
            "- FFmpeg 场景切分: "
            f"{shot_detection.get('status') or 'unknown'}，"
            f"检测到 {int(shot_detection.get('scene_boundary_count') or 0)} 个场景变化边界；"
            "这些边界是服务端时序证据，分镜应优先与其对齐，除非采样帧明确支持不同边界。"
        )
        multi_segment = isinstance(source_ranges, list) and len(source_ranges) > 1
        for shot in detected_shots[:100]:
            if not isinstance(shot, dict):
                continue
            start_key = "start_seconds" if multi_segment else "relative_start_seconds"
            end_key = "end_seconds" if multi_segment else "relative_end_seconds"
            try:
                start = float(shot.get(start_key))
                end = float(shot.get(end_key))
            except (TypeError, ValueError):
                continue
            segment = int(shot.get("source_segment_index") or 1)
            segment_text = f"片段 {segment} " if multi_segment else ""
            lines.append(f"  - {segment_text}{start:.3f}-{end:.3f} 秒")
        if shot_detection.get("degraded_reason"):
            lines.append(
                f"- 场景切分降级原因: {str(shot_detection['degraded_reason'])[:300]}"
            )
    if audio_evidence:
        lines.append("- 音频证据时间使用原视频绝对时间；只可依据以下分段转写描述旁白/对白：")
        evidence_chars = 0
        for segment in audio_segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(segment.get("start_seconds"))
                end = float(segment.get("end_seconds"))
            except (TypeError, ValueError):
                continue
            row = f"  - [{start:.3f}-{end:.3f}] {text[:500]}"
            if evidence_chars + len(row) > 12_000:
                lines.append("  - [其余音频证据因上下文长度限制省略]")
                break
            lines.append(row)
            evidence_chars += len(row)
    signal_rows = video_audio_context_rows(audio)
    if signal_rows:
        lines.append(
            "- 本地音频信号证据如下。BPM 只用于结合画面判断舒缓、平稳、轻快或紧凑等相对节奏，"
            "注意半拍/双拍误差，不得输出精确 BPM；未分类瞬态声必须与同一时段的明确可见动作对齐，"
            "只有声源清晰时才转写为‘动作发生时的具体声音’，无法判断就留空。"
            "不得猜测曲目、乐器或音乐风格，也不得输出‘未分类瞬态声’等检测术语："
        )
        lines.extend(f"  - {row}" for row in signal_rows)
    if not audio_evidence and audio.get("degraded_reason"):
        lines.append(f"- 音频分析降级原因: {str(audio['degraded_reason'])[:300]}")
    return "\n".join(lines) + "\n"


def _video_source_spec(video_analysis: dict) -> str:
    source = video_analysis.get("source") or {}
    parts = []
    if source.get("width") and source.get("height"):
        parts.append(f"{source['width']}x{source['height']}")
    ratio = str(source.get("ratio") or "")
    try:
        measured_ratio = float(source["width"]) / float(source["height"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        measured_ratio = None
    if measured_ratio:
        candidates = {
            "1:1": 1.0,
            "3:4": 3 / 4,
            "4:3": 4 / 3,
            "9:16": 9 / 16,
            "16:9": 16 / 9,
        }
        nearest = min(candidates, key=lambda key: abs(measured_ratio - candidates[key]))
        if abs(measured_ratio - candidates[nearest]) <= 0.03:
            ratio = nearest
    if ratio:
        parts.append(ratio)
    if source.get("duration_seconds") is not None:
        parts.append(f"{float(source['duration_seconds']):.3f}秒")
    if source.get("fps") is not None:
        parts.append(f"约{float(source['fps']):.3f}fps")
    return "，".join(parts)


def _video_generation_duration_suggestion(
    duration_seconds: object,
    *,
    shot_count: int = 0,
) -> str:
    """Translate source duration into a duration current video models can render."""
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return "建议按所选生成模型的时长上限设置"
    if not math.isfinite(duration) or duration <= 0:
        return "建议按所选生成模型的时长上限设置"
    if duration <= 15:
        if shot_count > 3:
            return (
                f"源片含 {shot_count} 个高密度镜头，建议逐镜生成；"
                "单镜含多步动作时再按主动作拆分，按原时间码剪辑成片"
            )
        display = f"{duration:.1f}".rstrip("0").rstrip(".")
        return f"建议生成约 {display} 秒单段视频"
    segment_count = max(2, math.ceil(duration / 15))
    return (
        f"完整复刻建议拆分为 {segment_count} 段，"
        "按原时间轴分别生成后顺序合成"
    )


def _video_shots_timeline(shots: list[dict]) -> str:
    rows = []
    for shot in shots:
        start = float(shot["start_seconds"])
        end = float(shot["end_seconds"])
        details = [
            str(shot.get(field) or "").strip()
            for field in (
                "visual", "subject_tracking", "pose", "action", "camera", "transition",
            )
        ]
        detail = "，".join(dict.fromkeys(value for value in details if value))
        rows.append(f"{start:.3f}-{end:.3f}s {detail or '按参考帧复刻'}")
    return "；".join(rows)


def _without_conflicting_video_durations(text: str, duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return str(text or "").strip()
    duration_re = re.compile(
        r"(?P<label>(?:总时长|视频时长|成片时长|全片时长|片长|建议生成|"
        r"(?:total\s+)?(?:duration|runtime|length))"
        r"\s*(?:为|约|建议)?\s*)?"
        r"(?<![\d.\-])(?P<value>\d+(?:\.\d+)?)\s*"
        r"(?:秒钟?|seconds?|secs?|s)(?![A-Za-z])",
        re.IGNORECASE,
    )
    source_text = str(text or "")

    def replace(match: re.Match[str]) -> str:
        before = source_text[max(0, match.start() - 8) : match.start()]
        after = source_text[match.end() : match.end() + 24]
        shot_context = bool(
            re.search(r"(?:前|后|每|单个|第\d+个?|镜头|持续|停留|动作)\s*$", before)
            or re.search(
                r"(?:first|last|next|previous)\s*$|"
                r"(?:each|every|single|per)\s+(?:shot|scene)\s+(?:lasts?|holds?|for)\s*$|"
                r"(?:shot|scene)\s+(?:lasts?|holds?|for)\s*$",
                source_text[max(0, match.start() - 40) : match.start()],
                re.IGNORECASE,
            )
        )
        total_context = bool(match.group("label")) or bool(
            re.match(
                r"[^，,。；;\n]{0,20}(?:广告|短片|视频|成片|ad|video|clip|film)",
                after,
                re.IGNORECASE,
            )
        )
        value = float(match.group("value"))
        conflicts = abs(value - float(duration_seconds)) > 0.01
        return "" if total_context and conflicts and not shot_context else match.group(0)

    cleaned = duration_re.sub(replace, source_text)
    cleaned = re.sub(r"([，,。；;])\s*([，,。；;])+", r"\1", cleaned)
    return cleaned.strip(" ，,。；;")
