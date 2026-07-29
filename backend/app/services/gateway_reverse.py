"""Reverse-prompt (vision→text) gateway calls."""
from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..config import settings
from . import gateway_reverse_context as _gateway_reverse_context_module
from . import gateway_reverse_repair as _gateway_reverse_repair_module
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding
from .gateway_prompting import ReverseResultValidationError
from .gateway_prompting import compose_visual_final_text as _compose_visual_final_text
from .gateway_prompting import mock_reverse as _mock_reverse
from .gateway_prompting import normalize_video_shots as _normalize_video_shots
from .gateway_prompting import reverse_template as _reverse_template
from .gateway_prompting import sanitize_video_audio_evidence as _sanitize_video_audio_evidence
from .gateway_prompting import validate_reverse_result as _validate_reverse_result
from .gateway_prompting import video_analysis_gaps as _video_analysis_gaps
from .gateway_reverse_context import (  # noqa: F401
    _video_analysis_context_text,
    _video_generation_duration_suggestion,
    _video_shots_timeline,
    _video_source_spec,
    _without_conflicting_video_durations,
)
from .gateway_reverse_repair import (  # noqa: F401
    _REPAIR_SHOT_FACT_TEXT_FIELDS,
    _REPAIR_SHOT_FACT_TIME_FIELDS,
    _REVERSE_REASONING_MODEL_RE,
    _VIDEO_TEMPORAL_EVIDENCE_FIELDS,
    _anthropic_image_source,
    _anthropic_reverse_content,
    _drop_unverified_video_timeline,
    _enforce_repair_shot_facts,
    _merge_gateway_usage,
    _missing_required_video_frame_indices,
    _repair_missing_video_frames_once,
    _repair_reverse_json_locally,
    _repair_reverse_result_once,
    _repair_shot_fact_key,
    _repair_shot_sort_key,
    _reverse_completion_controls,
    _reverse_response_content,
    _reverse_usage,
)
from .gateway_transport import (
    GatewayError,
    _ensure_gateway_configured,
    _gateway_mock,
    _post,  # noqa: F401 - compatibility forwarding for gateway._post patches
)
from .model_gateway_config import RuntimeGatewayConfig
from .video_prompt_compiler import split_video_post_production

log = logging.getLogger("gateway")

# ---------------------------------------------------------------- reverse prompt
_REVERSE_AUDIT_CONTEXT_FIELDS = ("operation_id", "preset", "cost_credits")


def _reverse_audit_context(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {key: None for key in _REVERSE_AUDIT_CONTEXT_FIELDS}
    return {
        key: (
            raw
            if raw is None or isinstance(raw, (str, int, float, bool))
            else str(raw)[:128]
        )
        for key in _REVERSE_AUDIT_CONTEXT_FIELDS
        for raw in (value.get(key),)
    }


def _log_reverse_audit_event(
    *,
    status: str,
    phase: str,
    target: str,
    audit_context: dict | None,
    latency_ms: int | None = None,
    error_code: str | None = None,
) -> None:
    event = {
        "event": "reverse_gateway_call",
        "status": status,
        "operation_id": None,
        "phase": phase,
        "error_code": error_code,
        "target": target,
        "preset": None,
        "cost_credits": None,
        "latency_ms": latency_ms,
        **_reverse_audit_context(audit_context),
    }
    message = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if status == "failed":
        log.warning("reverse_gateway_event=%s", message)
    else:
        log.info("reverse_gateway_event=%s", message)


@dataclass(frozen=True)
class _ReverseEvidenceContext:
    refs: list[str]
    source_count: int
    video_segment_count: int | None
    frame_rows: list
    required_frame_indices: list[int] | None
    frame_timestamps: dict[int, float]
    frame_segments: dict[int, int]


def _reverse_evidence_context(
    image_refs,
    *,
    target: str,
    video_analysis: dict | None,
    gateway_config: RuntimeGatewayConfig | None,
    require_video_frame_coverage: bool,
) -> _ReverseEvidenceContext:
    refs = (
        [image_refs]
        if isinstance(image_refs, str)
        else [ref for ref in image_refs if ref]
    )
    source_count = max(1, len(refs))
    video_segment_count = None
    if target == "video" and isinstance(video_analysis, dict):
        source = video_analysis.get("source")
        source = source if isinstance(source, dict) else {}
        ranges = source.get("source_ranges")
        video_segment_count = (
            len([row for row in ranges if isinstance(row, dict)])
            if isinstance(ranges, list) and ranges
            else 1
        )
    frame_rows = (video_analysis or {}).get("sampled_frames") or []
    required_indices = None
    frame_timestamps: dict[int, float] = {}
    frame_segments: dict[int, int] = {}
    coverage_required = (
        target == "video"
        and require_video_frame_coverage
        and not _gateway_mock(gateway_config)
        and isinstance(frame_rows, list)
        and bool(frame_rows)
    )
    if coverage_required:
        required_indices = [
            int(row.get("index"))
            if isinstance(row, dict)
            and isinstance(row.get("index"), int)
            and not isinstance(row.get("index"), bool)
            and int(row["index"]) > 0
            else index
            for index, row in enumerate(frame_rows, start=1)
        ]
        for index, row in zip(required_indices, frame_rows, strict=True):
            if not isinstance(row, dict):
                continue
            timestamp_value = (
                row.get("absolute_timestamp_seconds")
                if video_segment_count
                and video_segment_count > 1
                and row.get("absolute_timestamp_seconds") is not None
                else row.get("relative_timestamp_seconds")
                if row.get("relative_timestamp_seconds") is not None
                else row.get("timestamp_seconds")
            )
            try:
                timestamp = float(timestamp_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(timestamp):
                frame_timestamps[index] = timestamp
            try:
                frame_segments[index] = max(
                    1,
                    int(row.get("source_segment_index") or 1),
                )
            except (TypeError, ValueError):
                frame_segments[index] = 1
    return _ReverseEvidenceContext(
        refs=refs,
        source_count=source_count,
        video_segment_count=video_segment_count,
        frame_rows=frame_rows,
        required_frame_indices=required_indices,
        frame_timestamps=frame_timestamps,
        frame_segments=frame_segments,
    )


def _reverse_reference_label(
    index: int,
    context_row: dict,
    frame_rows: list,
) -> str:
    if context_row:
        role = str(context_row.get("role") or "reference")
        timestamp = context_row.get("timestamp_seconds")
        relative = context_row.get("relative_timestamp_seconds")
        if role == "frame":
            label = (
                f"第 {index} 帧，源视频时间戳 {float(timestamp):.3f} 秒"
                if timestamp is not None
                else f"第 {index} 帧"
            )
            source_segment_index = context_row.get("source_segment_index")
            if source_segment_index is not None:
                label += f"，来自已选片段 {int(source_segment_index)}"
            if relative is not None:
                label += f"，分析片段内 {float(relative):.3f} 秒"
            detected_shot_index = context_row.get("detected_shot_index")
            detected_shot_id = str(
                context_row.get("detected_shot_id") or ""
            ).strip()
            detected_start = context_row.get("detected_shot_start_seconds")
            detected_end = context_row.get("detected_shot_end_seconds")
            if detected_shot_index is not None:
                label += f"，属于服务端检测镜头 {int(detected_shot_index)}"
            if detected_shot_id:
                label += f"（{detected_shot_id[:96]}）"
            if detected_start is not None and detected_end is not None:
                label += (
                    "，该镜头边界 "
                    f"{float(detected_start):.3f}-{float(detected_end):.3f} 秒"
                )
            return label
        label = f"参考 {index}，角色 {role}"
        if context_row.get("label"):
            label += f"，说明 {str(context_row['label'])[:64]}"
        return label
    row = frame_rows[index - 1] if index <= len(frame_rows) else {}
    timestamp = row.get("timestamp_seconds")
    label = (
        f"第 {index} 帧，时间戳 {float(timestamp):.3f} 秒"
        if timestamp is not None
        else f"第 {index} 帧，时间戳未知"
    )
    detected_shot_index = row.get("detected_shot_index")
    detected_start = row.get("detected_shot_start_seconds")
    detected_end = row.get("detected_shot_end_seconds")
    if detected_shot_index is not None:
        label += f"，属于服务端检测镜头 {int(detected_shot_index)}"
    if detected_start is not None and detected_end is not None:
        label += (
            "，该镜头边界 "
            f"{float(detected_start):.3f}-{float(detected_end):.3f} 秒"
        )
    return label


def _reverse_request_content(
    context: _ReverseEvidenceContext,
    *,
    target: str,
    video_analysis: dict | None,
    template_override: str | None,
    reference_context: list[dict] | None,
) -> list[dict]:
    content: list[dict] = []
    if target == "video" and video_analysis:
        content.append(
            {
                "type": "text",
                "text": _video_analysis_context_text(video_analysis),
            }
        )
    for index, ref in enumerate(context.refs, start=1):
        context_row = (
            reference_context[index - 1]
            if reference_context and index <= len(reference_context)
            else {}
        )
        if context_row or (target == "video" and video_analysis):
            content.append(
                {
                    "type": "text",
                    "text": _reverse_reference_label(
                        index,
                        context_row,
                        context.frame_rows,
                    ),
                }
            )
        content.append(
            {"type": "image_url", "image_url": {"url": ref}}
        )
    template = (
        template_override
        if isinstance(template_override, str) and template_override.strip()
        else _reverse_template(target, n_frames=len(context.refs))
    )
    if target == "video" and context.required_frame_indices:
        required_text = ", ".join(
            (
                f"{index}@{context.frame_timestamps[index]:.3f}s"
                if index in context.frame_timestamps
                else str(index)
            )
            for index in context.required_frame_indices
        )
        template = (
            "本次用于生成的视频反推必须完整覆盖采样帧编号 "
            f"{required_text}；每个编号至少出现在一个 shot 的 "
            "evidence_frame_indices 中，不得遗漏或用空镜头占位。"
            "仅由一个采样帧支撑的静态 shot，时间范围必须围绕该帧时间戳且跨度不超过 1.5 秒。\n"
            + template
        )
    content.append({"type": "text", "text": template})
    return content


def _request_reverse_provider(
    context: _ReverseEvidenceContext,
    *,
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
    video_analysis: dict | None,
    template_override: str | None,
    reference_context: list[dict] | None,
) -> tuple[dict, str]:
    from . import gateway as gateway_service

    if not context.refs:
        raise GatewayError("反推缺少可用的参考图")
    content = _reverse_request_content(
        context,
        target=target,
        video_analysis=video_analysis,
        template_override=template_override,
        reference_context=reference_context,
    )
    anthropic = (
        gateway_config is not None
        and gateway_config.gateway_format == "anthropic"
    )
    payload = {
        "model": vision_model_id,
        "messages": [
            {
                "role": "user",
                "content": (
                    _anthropic_reverse_content(content)
                    if anthropic
                    else content
                ),
            }
        ],
        "temperature": 0.2,
        **_reverse_completion_controls(vision_model_id),
    }
    data = gateway_service._post(
        "/messages" if anthropic else "/chat/completions",
        payload,
        timeout=int(settings.reverse_gateway_timeout_seconds or 240),
        config=gateway_config,
        retries=max(0, int(settings.reverse_gateway_max_retries or 0)),
    )
    return data, _reverse_response_content(data)


def _before_reverse_repair(
    *,
    target: str,
    audit_context: dict | None,
    before_repair: Callable[[], bool | None] | None,
) -> None:
    _log_reverse_audit_event(
        status="repairing",
        phase="repairing",
        target=target,
        audit_context=audit_context,
    )
    if before_repair is None:
        return
    try:
        should_continue = before_repair()
    except Exception as exc:
        if isinstance(exc, GatewayError) and exc.phase is None:
            exc.phase = "repairing"
        else:
            exc.reverse_phase = "repairing"
        raise
    if should_continue is False:
        raise GatewayError(
            "反推任务已在文本修复前取消",
            error_code="CANCELED",
            phase="repairing",
        )


def _repair_invalid_reverse_result(
    content_text: str,
    validation_error: ReverseResultValidationError,
    context: _ReverseEvidenceContext,
    *,
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
) -> tuple[dict, dict | None]:
    try:
        missing_indices = _missing_required_video_frame_indices(
            content_text,
            context.required_frame_indices,
        )
        if target == "video" and missing_indices:
            return _repair_missing_video_frames_once(
                content_text,
                missing_frame_indices=missing_indices,
                frame_refs={
                    index: context.refs[index - 1]
                    for index in missing_indices
                    if 1 <= index <= len(context.refs)
                },
                frame_timestamps=context.frame_timestamps,
                frame_segments=context.frame_segments,
                frame_rows=context.frame_rows,
                target=target,
                vision_model_id=vision_model_id,
                gateway_config=gateway_config,
                source_count=context.source_count,
                video_segment_count=context.video_segment_count,
                required_video_frame_indices=(
                    context.required_frame_indices or ()
                ),
            )
        return _repair_reverse_result_once(
            content_text,
            validation_error,
            target=target,
            vision_model_id=vision_model_id,
            gateway_config=gateway_config,
            source_count=context.source_count,
            video_segment_count=context.video_segment_count,
            required_video_frame_indices=context.required_frame_indices,
            video_frame_timestamps=context.frame_timestamps,
        )
    except GatewayError as exc:
        exc.phase = exc.phase or "repairing"
        exc.error_code = exc.error_code or "REPAIR_FAILED"
        raise


def _missing_result_frame_indices(
    result: dict,
    required_frame_indices: list[int] | None,
) -> list[int]:
    if not required_frame_indices:
        return []
    covered = {
        int(index)
        for shot in result.get("shots") or []
        if isinstance(shot, dict)
        and str(shot.get("visual") or "").strip()
        for index in shot.get("evidence_frame_indices") or []
        if isinstance(index, int) and not isinstance(index, bool) and index > 0
    }
    return sorted(set(required_frame_indices) - covered)


def _provider_content_from_result(result: dict) -> str:
    payload = dict(result.get("structured") or {})
    payload["shots"] = list(result.get("shots") or [])
    payload["final_text"] = str(
        result.get("provider_final_text") or result.get("final_text") or "视频反推结果"
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _repair_partial_video_result(
    result: dict,
    context: _ReverseEvidenceContext,
    *,
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
) -> tuple[dict, dict | None]:
    missing_indices = _missing_result_frame_indices(
        result,
        context.required_frame_indices,
    )
    if target != "video" or not missing_indices:
        return result, None
    return _repair_missing_video_frames_once(
        _provider_content_from_result(result),
        missing_frame_indices=missing_indices,
        frame_refs={
            index: context.refs[index - 1]
            for index in missing_indices
            if 1 <= index <= len(context.refs)
        },
        frame_timestamps=context.frame_timestamps,
        frame_segments=context.frame_segments,
        frame_rows=context.frame_rows,
        target=target,
        vision_model_id=vision_model_id,
        gateway_config=gateway_config,
        source_count=context.source_count,
        video_segment_count=context.video_segment_count,
        required_video_frame_indices=context.required_frame_indices or (),
    )


def _provider_reverse_result(
    context: _ReverseEvidenceContext,
    *,
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
    video_analysis: dict | None,
    before_repair: Callable[[], bool | None] | None,
    audit_context: dict | None,
    template_override: str | None,
    reference_context: list[dict] | None,
) -> tuple[dict, dict, bool, dict | None]:
    data, content_text = _request_reverse_provider(
        context,
        target=target,
        vision_model_id=vision_model_id,
        gateway_config=gateway_config,
        video_analysis=video_analysis,
        template_override=template_override,
        reference_context=reference_context,
    )
    try:
        result = _validate_reverse_result(
            content_text,
            target,
            source_count=context.source_count,
            video_segment_count=context.video_segment_count,
            required_video_frame_indices=context.required_frame_indices,
            video_frame_timestamps=context.frame_timestamps,
        )
        if _missing_result_frame_indices(result, context.required_frame_indices):
            _before_reverse_repair(
                target=target,
                audit_context=audit_context,
                before_repair=before_repair,
            )
            result, repair_usage = _repair_partial_video_result(
                result,
                context,
                target=target,
                vision_model_id=vision_model_id,
                gateway_config=gateway_config,
            )
            return result, data, True, repair_usage
        return result, data, False, None
    except ReverseResultValidationError as validation_error:
        locally_repaired = _repair_reverse_json_locally(content_text)
        if locally_repaired is not None:
            try:
                result = _validate_reverse_result(
                    locally_repaired,
                    target,
                    source_count=context.source_count,
                    video_segment_count=context.video_segment_count,
                    required_video_frame_indices=context.required_frame_indices,
                    video_frame_timestamps=context.frame_timestamps,
                )
            except ReverseResultValidationError:
                result = None
            if result is not None:
                if _missing_result_frame_indices(
                    result,
                    context.required_frame_indices,
                ):
                    _before_reverse_repair(
                        target=target,
                        audit_context=audit_context,
                        before_repair=before_repair,
                    )
                    result, repair_usage = _repair_partial_video_result(
                        result,
                        context,
                        target=target,
                        vision_model_id=vision_model_id,
                        gateway_config=gateway_config,
                    )
                    return result, data, True, repair_usage
                return result, data, True, None
        _before_reverse_repair(
            target=target,
            audit_context=audit_context,
            before_repair=before_repair,
        )
        result, repair_usage = _repair_invalid_reverse_result(
            content_text,
            validation_error,
            context,
            target=target,
            vision_model_id=vision_model_id,
            gateway_config=gateway_config,
        )
        if _missing_result_frame_indices(result, context.required_frame_indices):
            result, coverage_usage = _repair_partial_video_result(
                result,
                context,
                target=target,
                vision_model_id=vision_model_id,
                gateway_config=gateway_config,
            )
            repair_usage = _merge_gateway_usage(repair_usage, coverage_usage)
        return result, data, True, repair_usage


def _mock_reverse_result(
    context: _ReverseEvidenceContext,
    *,
    target: str,
) -> dict:
    mock_result = _mock_reverse(target)
    payload = dict(mock_result.get("structured") or {})
    payload["final_text"] = mock_result.get("final_text")
    if "shots" in mock_result:
        payload["shots"] = mock_result.get("shots")
    return _validate_reverse_result(
        payload,
        target,
        source_count=context.source_count,
        video_segment_count=context.video_segment_count,
        required_video_frame_indices=context.required_frame_indices,
        video_frame_timestamps=context.frame_timestamps,
    )


def _recover_video_postproduction(result: dict, *, target: str) -> None:
    if target not in {"video", "image_to_video"}:
        return
    provider_text = result.get("provider_final_text")
    post = split_video_post_production(
        provider_text if isinstance(provider_text, str) else ""
    )
    overlay_text = "；".join(post["post_overlays"])
    if (
        overlay_text
        and str(result["structured"].get("字幕卖点") or "").strip()
        in {"", "无", "未分析"}
    ):
        result["structured"]["字幕卖点"] = overlay_text
    if target != "video":
        result["structured"]["旁白"] = "未分析"
        result["structured"]["音效"] = "未分析"
        for shot in result.get("shots") or []:
            shot["audio_cue"] = "未分析"


def _video_audio_offset(source: dict) -> float:
    source_ranges = source.get("source_ranges")
    source_range = source.get("source_range")
    if (
        not isinstance(source_range, dict)
        and isinstance(source_ranges, list)
        and len(source_ranges) == 1
    ):
        source_range = source_ranges[0]
    try:
        return (
            float(source_range.get("start_seconds") or 0)
            if isinstance(source_range, dict)
            else 0.0
        )
    except (TypeError, ValueError):
        return 0.0


def _normalized_frame_coverage_gaps(
    shots: list[dict],
    required_indices: list[int] | None,
    frame_timestamps: dict[int, float] | None = None,
) -> list[dict]:
    if required_indices is None:
        return []
    coverage = {
        int(index)
        for shot in shots
        for index in shot.get("evidence_frame_indices") or []
    }
    missing = sorted(set(required_indices) - coverage)
    return [
        {
            "kind": "sampled_frame_uncovered",
            "frame_index": index,
            **(
                {"timestamp_seconds": round(float(frame_timestamps[index]), 3)}
                if frame_timestamps is not None and index in frame_timestamps
                else {}
            ),
            "message": (
                f"采样帧 {index} 未形成可信分镜"
                + (
                    f"（{float(frame_timestamps[index]):.3f}s）"
                    if frame_timestamps is not None and index in frame_timestamps
                    else ""
                )
            ),
        }
        for index in missing
    ]


def _normalize_video_reverse_result(
    result: dict,
    context: _ReverseEvidenceContext,
    *,
    video_analysis: dict | None,
) -> None:
    result["structured"].pop("源视频规格", None)
    source = (video_analysis or {}).get("source") or {}
    source_ranges = source.get("source_ranges")
    audio_evidence = (
        (video_analysis or {}).get("audio")
        if isinstance((video_analysis or {}).get("audio"), dict)
        else None
    )
    audio_analyzed = bool(source.get("audio_analyzed"))
    audio_offset = _video_audio_offset(source)
    if video_analysis:
        result["shots"] = _normalize_video_shots(
            result.get("shots"),
            duration_seconds=source.get("duration_seconds"),
            frame_count=len(context.frame_rows),
            sampled_frames=context.frame_rows,
            audio_analyzed=audio_analyzed,
            audio_evidence=audio_evidence,
            audio_time_offset_seconds=audio_offset,
            source_ranges=source_ranges,
        )
        result["analysis_gaps"] = _video_analysis_gaps(
            result["shots"],
            duration_seconds=source.get("duration_seconds"),
            analysis_mode=video_analysis.get("analysis_mode"),
            source_ranges=source_ranges,
        ) + _normalized_frame_coverage_gaps(
            result["shots"],
            context.required_frame_indices,
            context.frame_timestamps,
        )
    else:
        result["shots"] = []
        result["analysis_gaps"] = [
            {"message": "缺少视频帧分析证据"}
        ]
    _sanitize_video_audio_evidence(
        result["structured"],
        result["shots"],
        audio_evidence=audio_evidence,
        audio_analyzed=audio_analyzed,
        shot_time_offset_seconds=audio_offset,
    )
    _drop_unverified_video_timeline(result)
    timeline = _video_shots_timeline(result["shots"])
    if timeline:
        result["structured"]["时序分镜"] = timeline
    source_spec = _video_source_spec(video_analysis or {})
    if source_spec:
        result["structured"]["源视频规格"] = source_spec
        result["structured"]["时长建议"] = (
            _video_generation_duration_suggestion(
                source.get("duration_seconds"),
                shot_count=len(result["shots"]),
            )
        )


def _finalize_reverse_prompt_result(
    result: dict,
    context: _ReverseEvidenceContext,
    *,
    target: str,
    video_analysis: dict | None,
    data: dict,
    repair_usage: dict | None,
    repair_attempted: bool,
    started_at: float,
) -> dict:
    _recover_video_postproduction(result, target=target)
    if target == "video":
        _normalize_video_reverse_result(
            result,
            context,
            video_analysis=video_analysis,
        )
    try:
        result["final_text"] = _compose_visual_final_text(
            result["structured"],
            target,
            result.get("shots"),
        )
    except ReverseResultValidationError as exc:
        raise GatewayError(
            str(exc),
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        ) from exc
    result["usage"] = _merge_gateway_usage(
        _reverse_usage(data),
        repair_usage,
    )
    result["latency_ms"] = int((time.time() - started_at) * 1000)
    result["repair_attempted"] = repair_attempted
    result["repair_succeeded"] = repair_attempted
    return result


def _reverse_prompt_impl(
    image_refs,
    vision_model_id: str,
    target: str = "image",
    gateway_config: RuntimeGatewayConfig | None = None,
    video_analysis: dict | None = None,
    before_repair: Callable[[], bool | None] | None = None,
    audit_context: dict | None = None,
    template_override: str | None = None,
    reference_context: list[dict] | None = None,
    require_video_frame_coverage: bool = False,
) -> dict:
    """Reverse one or more images into a structured generation prompt."""
    supported = {
        "image",
        "video",
        "product_profile",
        "portrait_profile",
        "image_to_video",
    }
    if target not in supported:
        raise GatewayError(f"不支持的反推目标: {target}")
    _ensure_gateway_configured(gateway_config, "反推")
    started_at = time.time()
    context = _reverse_evidence_context(
        image_refs,
        target=target,
        video_analysis=video_analysis,
        gateway_config=gateway_config,
        require_video_frame_coverage=require_video_frame_coverage,
    )
    if _gateway_mock(gateway_config):
        result = _mock_reverse_result(context, target=target)
        data: dict = {}
        repair_attempted = False
        repair_usage = None
    else:
        result, data, repair_attempted, repair_usage = (
            _provider_reverse_result(
                context,
                target=target,
                vision_model_id=vision_model_id,
                gateway_config=gateway_config,
                video_analysis=video_analysis,
                before_repair=before_repair,
                audit_context=audit_context,
                template_override=template_override,
                reference_context=reference_context,
            )
        )
    return _finalize_reverse_prompt_result(
        result,
        context,
        target=target,
        video_analysis=video_analysis,
        data=data,
        repair_usage=repair_usage,
        repair_attempted=repair_attempted,
        started_at=started_at,
    )

def _attach_image_to_video_analysis(result: dict, video_analysis: dict | None) -> dict:
    """Attach explicit single-image evidence without inventing a video timeline."""
    provided = dict(video_analysis) if isinstance(video_analysis, dict) else {}
    analysis_mode = str(provided.get("analysis_mode") or "image_motion")
    source = dict(provided.get("source")) if isinstance(provided.get("source"), dict) else {}
    source["audio_analyzed"] = False
    source.setdefault("source_type", "image" if analysis_mode == "image_motion" else "video_cover")
    sampled_frames = provided.get("sampled_frames")
    if not isinstance(sampled_frames, list) or not sampled_frames:
        sampled_frames = [{"index": 1, "timestamp_seconds": 0}]
    gaps = provided.get("analysis_gaps")
    if not isinstance(gaps, list) or not gaps:
        message = (
            "单图输入没有可观察的视频时间线"
            if analysis_mode == "image_motion"
            else "封面单帧无法覆盖源视频时间线"
        )
        gaps = [{"message": message}]
    analysis = {
        **provided,
        "analysis_mode": analysis_mode,
        "source": source,
        "sampled_frames": sampled_frames,
        "analysis_gaps": gaps,
    }
    normalized = dict(result)
    normalized["analysis_gaps"] = gaps
    normalized["video_analysis"] = analysis
    return normalized


def reverse_prompt(
    image_refs,
    vision_model_id: str,
    target: str = "image",
    gateway_config: RuntimeGatewayConfig | None = None,
    video_analysis: dict | None = None,
    before_repair: Callable[[], bool | None] | None = None,
    audit_context: dict | None = None,
    template_override: str | None = None,
    reference_context: list[dict] | None = None,
    require_video_frame_coverage: bool = False,
) -> dict:
    """Run reverse analysis and emit one bounded audit event for its outcome."""
    started = time.time()
    _log_reverse_audit_event(
        status="started",
        phase="calling_model",
        target=target,
        audit_context=audit_context,
    )
    try:
        result = _reverse_prompt_impl(
            image_refs,
            vision_model_id,
            target=target,
            gateway_config=gateway_config,
            video_analysis=video_analysis,
            before_repair=before_repair,
            audit_context=audit_context,
            template_override=template_override,
            reference_context=reference_context,
            require_video_frame_coverage=require_video_frame_coverage,
        )
        if target == "image_to_video":
            result = _attach_image_to_video_analysis(result, video_analysis)
    except Exception as error:
        phase = str(
            getattr(error, "phase", None)
            or getattr(error, "reverse_phase", None)
            or "calling_model"
        )
        error_code = str(
            getattr(error, "error_code", None)
            or ("GATEWAY_ERROR" if isinstance(error, GatewayError) else "INTERNAL_ERROR")
        )
        _log_reverse_audit_event(
            status="failed",
            phase=phase,
            target=target,
            audit_context=audit_context,
            latency_ms=int((time.time() - started) * 1000),
            error_code=error_code,
        )
        raise
    _log_reverse_audit_event(
        status="ok",
        phase="completed",
        target=target,
        audit_context=audit_context,
        latency_ms=int(result.get("latency_ms") or (time.time() - started) * 1000),
    )
    return result


_install_assignment_forwarding(
    __name__,
    (
        _gateway_reverse_context_module,
        _gateway_reverse_repair_module,
    ),
)
