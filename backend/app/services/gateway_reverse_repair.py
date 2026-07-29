"""Response parsing and bounded repair calls for reverse prompting."""
from __future__ import annotations

import logging
import re
from collections.abc import Collection
from math import isfinite
from urllib.parse import urlparse

from json_repair import repair_json

from ..config import settings
from .gateway_prompting import ReverseResultValidationError
from .gateway_prompting import _decode_json_object as _decode_reverse_json_object
from .gateway_prompting import clean_visual_generation_clause as _clean_visual_generation_clause
from .gateway_prompting import reverse_repair_template as _reverse_repair_template
from .gateway_prompting import (
    validate_missing_video_frame_result as _validate_missing_video_frame_result,
)
from .gateway_prompting import validate_reverse_result as _validate_reverse_result
from .gateway_transport import GatewayError, _post
from .model_gateway_config import RuntimeGatewayConfig

log = logging.getLogger("gateway")


def _repair_reverse_json_locally(content: str) -> dict | None:
    """Repair JSON syntax only; semantic validation remains authoritative."""
    try:
        repaired = repair_json(
            str(content or ""),
            return_objects=True,
            skip_json_loads=True,
        )
    except (TypeError, ValueError, OSError):
        return None
    return repaired if isinstance(repaired, dict) else None


def _reverse_response_content(data: dict) -> str:
    """Read text from OpenAI Chat Completions or Anthropic Messages."""
    try:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            content = choices[0]["message"]["content"]
        else:
            content = data["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GatewayError(
            "反推网关返回结构不符合预期:缺少 choices.message.content 或 content"
        ) from exc
    if isinstance(content, list):
        content = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {None, "text"}
        )
    if not isinstance(content, str):
        raise GatewayError("反推网关返回的文本内容必须是字符串或文本块列表")
    return content


def _reverse_usage(data: dict) -> dict | None:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    normalized = dict(usage)
    if "input_tokens" in normalized or "output_tokens" in normalized:
        try:
            prompt_tokens = max(0, int(normalized.get("input_tokens") or 0))
        except (TypeError, ValueError):
            prompt_tokens = 0
        try:
            completion_tokens = max(0, int(normalized.get("output_tokens") or 0))
        except (TypeError, ValueError):
            completion_tokens = 0
        normalized.setdefault("prompt_tokens", prompt_tokens)
        normalized.setdefault("completion_tokens", completion_tokens)
        normalized.setdefault("total_tokens", prompt_tokens + completion_tokens)
    return normalized


def _anthropic_image_source(ref: str) -> dict:
    value = str(ref or "").strip()
    if value.startswith("data:"):
        match = re.fullmatch(
            r"data:(?P<media_type>image/[A-Za-z0-9.+-]+);base64,(?P<data>.+)",
            value,
            flags=re.DOTALL,
        )
        if match is None:
            raise GatewayError("Anthropic 视觉反推仅支持 Base64 data URI 图片")
        media_type = match.group("media_type").lower()
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            raise GatewayError(f"Anthropic 视觉反推不支持图片格式: {media_type}")
        encoded = "".join(match.group("data").split())
        if not encoded:
            raise GatewayError("Anthropic 视觉反推图片 Base64 数据为空")
        return {
            "type": "base64",
            "media_type": media_type,
            "data": encoded,
        }
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return {"type": "url", "url": value}
    raise GatewayError("Anthropic 视觉反推图片必须是 HTTP(S) URL 或 Base64 data URI")


def _anthropic_reverse_content(content: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for item in content:
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": str(item.get("text") or "")})
            continue
        if item.get("type") == "image_url":
            image_url = item.get("image_url")
            ref = image_url.get("url") if isinstance(image_url, dict) else image_url
            blocks.append({"type": "image", "source": _anthropic_image_source(str(ref or ""))})
            continue
        raise GatewayError(f"Anthropic 视觉反推不支持内容块类型: {item.get('type')}")
    return blocks


def _merge_gateway_usage(*rows: dict | None) -> dict | None:
    merged: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                merged[key] = merged.get(key, 0) + value
            elif key not in merged:
                merged[key] = value
    return merged or None


_VIDEO_TEMPORAL_EVIDENCE_FIELDS = frozenset({
    "帧间推断",
    "主体追踪",
    "姿态变化",
    "主体动作",
    "可迁移主体动作",
    "迁移生成指令",
    "镜头运动",
    "运动节奏",
    "剪辑节奏",
    "时序分镜",
    "转场",
})

_REVERSE_REASONING_MODEL_RE = re.compile(
    r"(?:^|/)(?:gpt-5(?:[.\-]|$)|o(?:1|3|4)(?:[.\-]|$))",
    re.IGNORECASE,
)


def _reverse_completion_controls(model_id: str) -> dict:
    controls = {
        "max_tokens": max(512, int(settings.reverse_gateway_max_tokens or 4096)),
    }
    effort = str(settings.reverse_gateway_reasoning_effort or "").strip().lower()
    if effort in {"low", "medium", "high"} and _REVERSE_REASONING_MODEL_RE.search(
        str(model_id or "")
    ):
        controls["reasoning_effort"] = effort
    return controls


def _drop_unverified_video_timeline(result: dict) -> None:
    """Remove structured timeline claims when no normalized shot has evidence."""
    shots = result.get("shots")
    has_cross_frame_evidence = any(
        len(set(shot.get("evidence_frame_indices") or [])) >= 2
        and any(
            str(shot.get(field) or "").strip()
            for field in ("subject_tracking", "pose", "action", "camera", "transition")
        )
        for shot in shots or []
        if isinstance(shot, dict)
    )
    if has_cross_frame_evidence:
        return
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return
    removed = {
        key: structured.pop(key)
        for key in _VIDEO_TEMPORAL_EVIDENCE_FIELDS
        if str(structured.get(key) or "").strip()
    }
    if removed:
        result["unverified_temporal_fields"] = removed


_REPAIR_SHOT_FACT_TEXT_FIELDS = (
    "visual",
    "subject_tracking",
    "pose",
    "action",
    "camera",
    "transition",
    "lighting",
    "ocr",
    "audio_cue",
)
_REPAIR_SHOT_FACT_TIME_FIELDS = ("start_seconds", "end_seconds")


def _repair_shot_fact_key(shot: dict) -> tuple[int, tuple[int, ...]]:
    """Identity of a shot for the repair diff: source segment + evidence frames."""
    evidence = tuple(sorted({
        int(index)
        for index in shot.get("evidence_frame_indices") or []
        if isinstance(index, int) and not isinstance(index, bool) and index > 0
    }))
    try:
        segment = int(shot.get("source_segment_index") or 1)
    except (TypeError, ValueError):
        segment = 1
    return (segment, evidence)


def _repair_shot_sort_key(shot: dict) -> tuple[int, float]:
    try:
        segment = int(shot.get("source_segment_index") or 1)
    except (TypeError, ValueError):
        segment = 1
    try:
        start = float(shot.get("start_seconds") or 0)
    except (TypeError, ValueError):
        start = 0.0
    return (segment, start)


def _enforce_repair_shot_facts(
    original_content: str,
    repaired_payload: dict,
) -> tuple[dict, list[dict]]:
    """Keep a structure-only repair from rewriting established shot facts."""
    try:
        original_payload = _decode_reverse_json_object(original_content)
    except ReverseResultValidationError:
        return repaired_payload, []
    if not isinstance(original_payload, dict):
        return repaired_payload, []
    original_shots = [
        shot for shot in original_payload.get("shots") or [] if isinstance(shot, dict)
    ]
    if not original_shots:
        return repaired_payload, []
    pending: dict[tuple[int, tuple[int, ...]], list[dict]] = {}
    for shot in original_shots:
        pending.setdefault(_repair_shot_fact_key(shot), []).append(shot)
    repaired_shots = [
        shot for shot in repaired_payload.get("shots") or [] if isinstance(shot, dict)
    ]
    kept: list[dict] = []
    warnings: list[dict] = []
    violation_count = 0
    for shot in repaired_shots:
        key = _repair_shot_fact_key(shot)
        bucket = pending.get(key)
        if not bucket:
            violation_count += 1
            warnings.append({
                "reason": "shot_added",
                "source_segment_index": key[0],
                "evidence_frame_indices": list(key[1]),
            })
            continue
        original = bucket.pop(0)
        rolled_back: list[str] = []
        for field in _REPAIR_SHOT_FACT_TEXT_FIELDS:
            original_value = original.get(field)
            if not isinstance(original_value, str):
                continue
            repaired_value = shot.get(field)
            if (
                not isinstance(repaired_value, str)
                or repaired_value.strip() != original_value.strip()
            ):
                shot[field] = original_value
                rolled_back.append(field)
        for field in _REPAIR_SHOT_FACT_TIME_FIELDS:
            original_value = original.get(field)
            if isinstance(original_value, bool) or not isinstance(
                original_value, (int, float)
            ):
                continue
            repaired_value = shot.get(field)
            if (
                isinstance(repaired_value, bool)
                or not isinstance(repaired_value, (int, float))
                or abs(float(repaired_value) - float(original_value)) > 0.0005
            ):
                shot[field] = original_value
                rolled_back.append(field)
        if rolled_back:
            violation_count += 1
            warnings.append({
                "reason": "fields_rewritten",
                "source_segment_index": key[0],
                "evidence_frame_indices": list(key[1]),
                "fields": rolled_back,
            })
        kept.append(shot)
    for key, bucket in pending.items():
        for original in bucket:
            violation_count += 1
            warnings.append({
                "reason": "shot_deleted",
                "source_segment_index": key[0],
                "evidence_frame_indices": list(key[1]),
            })
            kept.append(dict(original))
    if violation_count:
        log.warning(
            "反推文本修复在只修结构的指令下改动了 %d 处已有 shot 事实(基线 %d 个 shot),"
            "已按原值回滚: %s",
            violation_count,
            len(original_shots),
            warnings,
        )
    if violation_count >= 2 and violation_count * 2 > len(original_shots):
        raise GatewayError(
            f"文本修复在只修结构的指令下改写了 {violation_count} 处已有 shot 事实"
            f"(原有 {len(original_shots)} 个 shot),判定模型未遵守指令,结果不可信",
            error_code="INVALID_REVERSE_RESULT",
            phase="repairing",
        )
    repaired_payload = dict(repaired_payload)
    repaired_payload["shots"] = sorted(kept, key=_repair_shot_sort_key)
    return repaired_payload, warnings


def _repair_reverse_result_once(
    content: str,
    error: ReverseResultValidationError,
    *,
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
    source_count: int | None = None,
    video_segment_count: int | None = None,
    required_video_frame_indices: Collection[int] | None = None,
    video_frame_timestamps: dict[int, float] | None = None,
) -> tuple[dict, dict | None]:
    """Make exactly one text-only request to repair an invalid model result."""
    anthropic = bool(
        gateway_config is not None and gateway_config.gateway_format == "anthropic"
    )
    repair_text = _reverse_repair_template(content, str(error), target)
    payload = {
        "model": vision_model_id,
        "messages": [{
            "role": "user",
            "content": (
                [{"type": "text", "text": repair_text}]
                if anthropic
                else repair_text
            ),
        }],
        "temperature": 0,
        **_reverse_completion_controls(vision_model_id),
    }
    repair_data = _post(
        "/messages" if anthropic else "/chat/completions",
        payload,
        timeout=int(settings.reverse_gateway_timeout_seconds or 240),
        config=gateway_config,
        retries=0,
    )
    repaired_content = _reverse_response_content(repair_data)
    fact_rollbacks: list[dict] = []
    repaired_payload: dict | str = repaired_content
    try:
        decoded_repair = _decode_reverse_json_object(repaired_content)
    except ReverseResultValidationError:
        decoded_repair = _repair_reverse_json_locally(repaired_content)
    if isinstance(decoded_repair, dict):
        repaired_payload, fact_rollbacks = _enforce_repair_shot_facts(
            content,
            decoded_repair,
        )
    try:
        result = _validate_reverse_result(
            repaired_payload,
            target,
            source_count=source_count,
            video_segment_count=video_segment_count,
            required_video_frame_indices=required_video_frame_indices,
            video_frame_timestamps=video_frame_timestamps,
        )
    except ReverseResultValidationError as repair_error:
        raise GatewayError(
            "反推结果不符合结构契约,且一次自动修复后仍无效: "
            f"{str(repair_error)[:300]}",
            error_code="INVALID_REVERSE_RESULT",
            phase="repairing",
        ) from repair_error
    if fact_rollbacks:
        result["repair_fact_rollbacks"] = fact_rollbacks
    return result, _reverse_usage(repair_data)


def _missing_required_video_frame_indices(
    content: str,
    required_frame_indices: Collection[int] | None,
) -> list[int]:
    if not required_frame_indices:
        return []
    try:
        payload = _decode_reverse_json_object(content)
    except ReverseResultValidationError:
        return []
    covered = {
        int(index)
        for shot in payload.get("shots") or []
        if isinstance(shot, dict)
        and _clean_visual_generation_clause(shot.get("visual"))
        for index in shot.get("evidence_frame_indices") or []
        if isinstance(index, int) and not isinstance(index, bool) and index > 0
    }
    return sorted({int(index) for index in required_frame_indices} - covered)


def _detected_frame_group_key(
    frame_index: int,
    rows_by_index: dict[int, dict],
    frame_segments: dict[int, int],
) -> tuple:
    row = rows_by_index.get(frame_index) or {}
    shot_id = str(row.get("detected_shot_id") or "").strip()
    segment = int(frame_segments.get(frame_index) or 1)
    return (
        ("shot", segment, shot_id)
        if shot_id
        else ("frame", segment, frame_index)
    )


def _missing_video_frame_request(
    *,
    missing_frame_indices: list[int],
    frame_refs: dict[int, str],
    frame_timestamps: dict[int, float],
    frame_segments: dict[int, int],
    frame_rows: list[dict] | None,
) -> tuple[list[dict], dict[int, dict], set[int]]:
    missing_refs = {
        index: frame_refs[index]
        for index in missing_frame_indices
        if index in frame_refs
    }
    if set(missing_refs) != set(missing_frame_indices):
        raise GatewayError(
            "缺失采样帧没有可用图像引用",
            error_code="INVALID_REVERSE_RESULT",
            phase="repairing",
        )
    rows_by_index = {
        int(row["index"]): row
        for row in frame_rows or []
        if isinstance(row, dict)
        and isinstance(row.get("index"), int)
        and not isinstance(row.get("index"), bool)
    }
    missing_group_counts: dict[tuple, int] = {}
    for index in missing_frame_indices:
        key = _detected_frame_group_key(index, rows_by_index, frame_segments)
        missing_group_counts[key] = missing_group_counts.get(key, 0) + 1
    temporal_frame_indices = {
        index
        for index in missing_frame_indices
        if missing_group_counts.get(
            _detected_frame_group_key(index, rows_by_index, frame_segments),
            0,
        ) >= 2
    }
    frame_contract = ", ".join(
        f"{index}@{frame_timestamps[index]:.3f}s"
        if index in frame_timestamps
        else str(index)
        for index in missing_frame_indices
    )
    blocks: list[dict] = [{
        "type": "text",
        "text": (
            "你只分析下方尚未覆盖的视频采样帧，不重写已有结果。"
            f"必须为这些帧各返回一项: {frame_contract}。"
            "visual 只写当帧直接可见的主体、场景、构图和材质。"
            "只有当同一服务端检测镜头至少提供 2 张缺失采样帧时，"
            "才按时间顺序对比并填写镜头级 subject_tracking、pose、action、camera、transition；"
            "visual 和 action 用‘初态→过程→终态’或明确时序连接词写出主体接触、产品形变和最终结果，不得只罗列动作名；"
            "同一镜头的这些字段在每张帧中重复返回相同结论。"
            "单张缺失帧的上述时序字段必须为空字符串。"
            "transition 仅在画面可直接支持镜头边界时填写，不推断声音或帧外事件，"
            "不输出未知、可能、高级感等分析或提升词。"
            "ocr 只逐字记录当帧清晰可辨的包装文字或画面字幕，平台AI生成水印忽略。"
            "输出唯一 JSON 对象，不要 markdown 或解释，结构为:"
            '{"frames":[{"frame_index":8,"visual":"直接可见画面",'
            '"subject_tracking":"主体身份连续性与位置轨迹或空字符串",'
            '"pose":"姿态变化或空字符串","action":"动作过程或空字符串",'
            '"camera":"镜头运动或固定机位","transition":"转场或空字符串",'
            '"lighting":"可见光线或空字符串","ocr":"清晰原文或空字符串",'
            '"confidence":0.0}]}'
        ),
    }]
    for index in missing_frame_indices:
        timestamp = frame_timestamps.get(index)
        label = f"采样帧 {index}"
        if timestamp is not None:
            label += f"，时间戳 {timestamp:.3f} 秒"
        row = rows_by_index.get(index) or {}
        shot_id = str(row.get("detected_shot_id") or "").strip()
        if shot_id:
            label += f"，服务端检测镜头 {shot_id}"
        if index in temporal_frame_indices:
            label += "，可与同镜头其他帧做时序对比"
        else:
            label += "，仅允许静态观察"
        blocks.append({"type": "text", "text": label})
        blocks.append({"type": "image_url", "image_url": {"url": missing_refs[index]}})
    return blocks, rows_by_index, temporal_frame_indices


def _repair_missing_video_frames_once(
    content: str,
    *,
    missing_frame_indices: list[int],
    frame_refs: dict[int, str],
    frame_timestamps: dict[int, float],
    frame_segments: dict[int, int],
    frame_rows: list[dict] | None = None,
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
    source_count: int | None,
    video_segment_count: int | None,
    required_video_frame_indices: Collection[int],
) -> tuple[dict, dict | None]:
    """Analyze only uncovered frames and merge them into the original provider JSON."""
    blocks, rows_by_index, temporal_frame_indices = _missing_video_frame_request(
        missing_frame_indices=missing_frame_indices,
        frame_refs=frame_refs,
        frame_timestamps=frame_timestamps,
        frame_segments=frame_segments,
        frame_rows=frame_rows,
    )

    def detected_key(frame_index: int) -> tuple:
        return _detected_frame_group_key(
            frame_index,
            rows_by_index,
            frame_segments,
        )
    anthropic = bool(
        gateway_config is not None and gateway_config.gateway_format == "anthropic"
    )
    repair_data = _post(
        "/messages" if anthropic else "/chat/completions",
        {
            "model": vision_model_id,
            "messages": [{
                "role": "user",
                "content": _anthropic_reverse_content(blocks) if anthropic else blocks,
            }],
            "temperature": 0,
            **_reverse_completion_controls(vision_model_id),
        },
        timeout=int(settings.reverse_gateway_timeout_seconds or 240),
        config=gateway_config,
        retries=0,
    )
    repaired_content = _reverse_response_content(repair_data)
    try:
        descriptions = _validate_missing_video_frame_result(
            repaired_content,
            missing_frame_indices,
        )
        merged_payload = _decode_reverse_json_object(content)
        merged_shots = list(merged_payload.get("shots") or [])
        def join_values(values: list[str]) -> str:
            return "；".join(dict.fromkeys(value for value in values if value))

        groups: dict[tuple, list[dict]] = {}
        for description in descriptions:
            if int(description["frame_index"]) not in temporal_frame_indices:
                for field in (
                    "subject_tracking", "pose", "action", "camera", "transition",
                ):
                    description[field] = ""
            groups.setdefault(
                detected_key(int(description["frame_index"])), []
            ).append(description)

        existing_by_key: dict[tuple, int] = {}
        for shot_index, shot in enumerate(merged_shots):
            if (
                not isinstance(shot, dict)
                or not _clean_visual_generation_clause(shot.get("visual"))
            ):
                continue
            for frame_index in shot.get("evidence_frame_indices") or []:
                if isinstance(frame_index, int) and not isinstance(frame_index, bool):
                    existing_by_key.setdefault(detected_key(frame_index), shot_index)

        for group_key, group in groups.items():
            indices = sorted(int(item["frame_index"]) for item in group)
            visuals = [str(item.get("visual") or "").strip() for item in group]
            lighting = [str(item.get("lighting") or "").strip() for item in group]
            ocr = [str(item.get("ocr") or "").strip() for item in group]
            temporal_values = {
                field: [str(item.get(field) or "").strip() for item in group]
                for field in (
                    "subject_tracking", "pose", "action", "camera", "transition",
                )
            }
            confidence = max(float(item.get("confidence") or 0.5) for item in group)
            existing_index = existing_by_key.get(group_key)
            if existing_index is not None:
                existing = dict(merged_shots[existing_index])
                existing["evidence_frame_indices"] = sorted({
                    *[
                        int(value)
                        for value in existing.get("evidence_frame_indices") or []
                        if isinstance(value, int) and not isinstance(value, bool)
                    ],
                    *indices,
                })
                for field, values in (
                    ("visual", visuals),
                    ("lighting", lighting),
                    ("ocr", ocr),
                ):
                    existing[field] = join_values([
                        str(existing.get(field) or "").strip(),
                        *values,
                    ])
                for field, values in temporal_values.items():
                    if not str(existing.get(field) or "").strip():
                        existing[field] = join_values(values)
                existing["confidence"] = max(
                    float(existing.get("confidence") or 0.5),
                    confidence,
                )
                merged_shots[existing_index] = existing
                continue

            timestamps = [
                float(frame_timestamps.get(frame_index) or 0.0)
                for frame_index in indices
            ]
            rows = [rows_by_index.get(frame_index) or {} for frame_index in indices]
            bounds = []
            for row in rows:
                try:
                    start = float(row.get("detected_shot_start_seconds"))
                    end = float(row.get("detected_shot_end_seconds"))
                except (TypeError, ValueError):
                    continue
                if isfinite(start) and isfinite(end) and end > start >= 0:
                    bounds.append((start, end))
            start_seconds = (
                min(start for start, _end in bounds)
                if bounds
                else max(0.0, min(timestamps) - 0.75)
            )
            end_seconds = (
                max(end for _start, end in bounds)
                if bounds
                else max(timestamps) + 0.75
            )
            merged_shots.append({
                "source_segment_index": int(frame_segments.get(indices[0]) or 1),
                "start_seconds": round(start_seconds, 3),
                "end_seconds": round(end_seconds, 3),
                "visual": join_values(visuals),
                "subject_tracking": join_values(temporal_values["subject_tracking"]),
                "pose": join_values(temporal_values["pose"]),
                "action": join_values(temporal_values["action"]),
                "camera": join_values(temporal_values["camera"]),
                "lighting": join_values(lighting),
                "transition": join_values(temporal_values["transition"]),
                "ocr": join_values(ocr),
                "audio_cue": "",
                "evidence_frame_indices": indices,
                "confidence": confidence,
            })
        merged_payload["shots"] = sorted(
            merged_shots,
            key=lambda shot: (
                int(shot.get("source_segment_index") or 1),
                float(shot.get("start_seconds") or 0),
            ) if isinstance(shot, dict) else (0, 0.0),
        )
        result = _validate_reverse_result(
            merged_payload,
            target,
            source_count=source_count,
            video_segment_count=video_segment_count,
            required_video_frame_indices=required_video_frame_indices,
            video_frame_timestamps=frame_timestamps,
        )
    except (ReverseResultValidationError, TypeError, ValueError) as repair_error:
        raise GatewayError(
            "缺失视频帧增量补全后仍不符合契约: "
            f"{str(repair_error)[:300]}",
            error_code="INVALID_REVERSE_RESULT",
            phase="repairing",
        ) from repair_error
    return result, _reverse_usage(repair_data)
