"""Validation and evidence-bound result processing for reverse operations."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReverseOperation, ReverseResultRevision
from . import gateway
from .gateway_prompting import (
    ReverseResultValidationError,
    compose_video_generation_draft,
    compose_visual_final_text,
    constrain_video_shots_to_evidence,
)
from .gateway_prompting import (
    validate_reverse_result as validate_reverse_contract,
)


def validate_reverse_result(
    result: Any,
    target: str,
    *,
    source_count: int | None = None,
) -> dict[str, Any]:
    """Revalidate an adapter result against the selected target contract.

    ``gateway.reverse_prompt`` normally returns a normalized object after its
    own provider-output validation. This boundary check is still required:
    tests, wrappers, and future adapters can replace that function, and no
    result may reach credit settlement based only on a shallow shape check.
    """
    if not isinstance(result, dict):
        raise gateway.GatewayError(
            "视觉模型返回结果不是 JSON 对象",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )
    structured = result.get("structured")
    final_text = result.get("final_text")
    provider_final_text = result.get("provider_final_text", final_text)
    if not isinstance(structured, dict):
        raise gateway.GatewayError(
            "视觉模型返回的 structured 类型错误",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )
    if not isinstance(final_text, str) or not final_text.strip():
        raise gateway.GatewayError(
            "视觉模型返回的 final_text 必须是非空字符串",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )
    if not isinstance(provider_final_text, str) or not provider_final_text.strip():
        raise gateway.GatewayError(
            "视觉模型返回的 provider_final_text 必须是非空字符串",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )

    contract_payload = dict(structured)
    contract_payload["final_text"] = provider_final_text
    if "shots" in result:
        contract_payload["shots"] = result.get("shots")
    if "image_evidence" in result:
        contract_payload["image_evidence"] = result.get("image_evidence")
    try:
        validated = validate_reverse_contract(
            contract_payload,
            target,
            source_count=source_count,
        )
    except ReverseResultValidationError as exc:
        raise gateway.GatewayError(
            f"视觉模型返回结果不符合 {target} 契约: {str(exc)[:500]}",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        ) from exc

    normalized = dict(result)
    normalized.update(validated)
    normalized["provider_final_text"] = provider_final_text
    return normalized


def _merge_video_analysis_result(
    result: dict[str, Any],
    collected_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Preserve gateway evidence while attaching normalized shots and gaps."""
    gateway_analysis = result.pop("video_analysis", None)
    gateway_analysis = gateway_analysis if isinstance(gateway_analysis, dict) else {}
    merged = {**collected_analysis, **gateway_analysis}
    collected_source = collected_analysis.get("source")
    gateway_source = gateway_analysis.get("source")
    if isinstance(collected_source, dict) or isinstance(gateway_source, dict):
        merged["source"] = {
            **(collected_source if isinstance(collected_source, dict) else {}),
            **(gateway_source if isinstance(gateway_source, dict) else {}),
        }
        # Audio state is a server-side evidence fact. Provider JSON can enrich
        # prose, but it cannot promote an untranscribed track to analyzed.
        authoritative_source = collected_source if isinstance(collected_source, dict) else {}
        audio = collected_analysis.get("audio")
        audio_segments = audio.get("segments") if isinstance(audio, dict) else None
        audio_analyzed = bool(
            authoritative_source.get("audio_analyzed")
            and isinstance(audio_segments, list)
            and any(isinstance(item, dict) and str(item.get("text") or "").strip() for item in audio_segments)
        )
        merged["source"]["audio_analyzed"] = audio_analyzed
        if isinstance(audio, dict):
            normalized_audio = deepcopy(audio)
            normalized_segments = []
            for row in normalized_audio.get("segments") or []:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                item.setdefault("evidence_id", "audio-" + hashlib.sha256(
                    json.dumps(item, sort_keys=True, default=str).encode()
                ).hexdigest()[:20])
                item.setdefault("analyzer_status", normalized_audio.get("status"))
                item.setdefault("analyzer_source", "timestamped_asr_gateway")
                item.setdefault("analyzer_version", "audio-evidence.v1")
                item.setdefault("evidence_type", "speech")
                normalized_segments.append(item)
            normalized_audio["segments"] = normalized_segments
            normalized_evidence = [deepcopy(item) for item in normalized_segments]
            features = normalized_audio.get("features")
            if isinstance(features, dict):
                for feature_name in ("music", "beat", "sfx"):
                    feature = features.get(feature_name)
                    if not isinstance(feature, dict):
                        continue
                    for row in feature.get("evidence") or []:
                        if not isinstance(row, dict):
                            continue
                        item = deepcopy(row)
                        item.setdefault("evidence_type", feature_name)
                        item.setdefault("evidence_id", "audio-" + hashlib.sha256(
                            json.dumps(item, sort_keys=True, default=str).encode()
                        ).hexdigest()[:20])
                        item.setdefault("analyzer_status", feature.get("status"))
                        item.setdefault("analyzer_source", feature.get("analyzer"))
                        item.setdefault("analyzer_version", feature.get("analyzer_version"))
                        normalized_evidence.append(item)
            normalized_audio["evidence"] = normalized_evidence
            merged["audio"] = normalized_audio
    if "shots" in result:
        merged["shots"] = result.pop("shots")
    if "analysis_gaps" in result:
        merged["analysis_gaps"] = result.pop("analysis_gaps")
    return merged


def _finalize_evidence_bound_video_result(result: dict[str, Any]) -> None:
    """Rebuild user-facing timeline and prompt after independent evidence attachment."""
    analysis = result.get("video_analysis")
    if not isinstance(analysis, dict) or not isinstance(analysis.get("shots"), list):
        return
    # evidence_analyzers 即 analyze_video_evidence() 的返回值（含
    # shot_transitions.events），传入后硬切放行的
    # evidence_gate.transition.score 会带上 lavfi scene_score 数值置信度。
    shots = constrain_video_shots_to_evidence(
        analysis["shots"],
        evidence=analysis.get("evidence_analyzers"),
    )
    if not shots:
        return
    analysis["shots"] = shots
    structured = result.get("structured")
    if not isinstance(structured, dict):
        structured = {}
        result["structured"] = structured

    # structured / final_text 是交付给生成链路和用户复制的提示词层，模板
    # 规则明确要求其中不得携带证据说明或置信度话术——「（未验证）」这类
    # 标注一旦进入 final_text 会被生成模型当成画面内容。因此这里一律使用
    # canonical shots 原文；已验证/未验证的区分只保留在 shot["evidence_gate"]
    # 的机器可读置信度里，由前端时间线按 gate 渲染徽标。
    temporal_values = {
        "主体追踪": [str(shot.get("subject_tracking") or "").strip() for shot in shots],
        "姿态变化": [str(shot.get("pose") or "").strip() for shot in shots],
        "主体动作": [str(shot.get("action") or "").strip() for shot in shots],
        "镜头运动": [str(shot.get("camera") or "").strip() for shot in shots],
        "转场": [str(shot.get("transition") or "").strip() for shot in shots],
    }
    for key in (
        "主体追踪", "姿态变化", "主体动作", "可迁移主体动作", "迁移生成指令",
        "镜头运动", "剪辑节奏", "转场",
    ):
        structured.pop(key, None)
    for key, values in temporal_values.items():
        unique = list(dict.fromkeys(value for value in values if value))
        if unique:
            structured[key] = "；".join(unique)
    timeline = gateway._video_shots_timeline(shots)
    if timeline:
        structured["时序分镜"] = timeline
    else:
        structured.pop("时序分镜", None)
    result["final_text"] = compose_video_generation_draft(structured, shots)


_OCR_CLAIM_QUOTE_PAIRS = (
    ('"', '"'),
    ("“", "”"),
    ("「", "」"),
    ("『", "』"),
    ("'", "'"),
    ("‘", "’"),
)


def _replace_ocr_claim(text: str, claim: str, replacement: str) -> str:
    """在文字字段里把被拦截/被推翻的 VLM 文字描述替换为 OCR 裁决文本。

    优先匹配带引号的形式（含中英文引号），避免误伤字段里的其他描述；
    replacement 为空即删除该主张。找不到匹配则原样返回。
    """
    if not claim:
        return text
    for left, right in _OCR_CLAIM_QUOTE_PAIRS:
        quoted = f"{left}{claim}{right}"
        if quoted in text:
            return text.replace(
                quoted,
                f"{left}{replacement}{right}" if replacement else "",
            )
    if claim in text:
        return text.replace(claim, replacement)
    return text


def _tidy_gated_text_field(text: str) -> str:
    cleaned = re.sub(r"[，,、]{2,}", "，", text)
    cleaned = re.sub(r"[；;]{2,}", "；", cleaned)
    return cleaned.strip("，,、；; \t\n")


def _apply_image_ocr_gate_to_text(
    result: dict[str, Any],
    evidence_rows: list[dict[str, Any]] | None,
    target: str,
) -> None:
    """把 OCR 门控裁决回写到 structured 文字字段与 final_text。

    final_text 在 gateway_prompting 阶段先于独立证据分析生成，被
    ``ocr_gate`` 判为 rejected/overridden 的文字描述可能已进入
    ``structured["文字版式"]`` 与 ``final_text``。此处按行内
    ``ocr_gate.status`` 收口（五态）：

    - ``overridden`` → 用 OCR 文本覆写字段中被推翻的描述；
    - ``rejected`` → 从字段中清除幻觉描述；若某字段的全部 VLM 文字
      主张均被拦截，则整字段移除（即使描述在字段里已被截断改写、
      无法精确定位，也绝不让被拦截文字留在提示词里）；
    - ``confirmed`` / ``low_confidence`` / ``unavailable`` → 保留不动。

    任一字段被改写后，基于净化的 structured 重新合成 final_text（与
    validate_reverse_result 的合成路径一致，图片类 target 无 shots）。
    """
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return
    verdicts: list[tuple[str, str, str, str]] = []
    statuses_by_field: dict[str, list[str]] = {}
    for row in evidence_rows or []:
        if not isinstance(row, dict):
            continue
        gate = row.get("ocr_gate")
        if not isinstance(gate, dict):
            continue
        status = str(gate.get("status") or "")
        field_key = str(row.get("field_key") or "").strip() or "文字版式"
        statuses_by_field.setdefault(field_key, []).append(status)
        claim = str(row.get("vlm_text_description") or "").strip()
        if status in ("rejected", "overridden") and claim:
            verdicts.append(
                (field_key, status, claim, str(gate.get("ocr_text") or "").strip())
            )
    changed = False
    for field_key, status, claim, ocr_text in verdicts:
        value = structured.get(field_key)
        if not isinstance(value, str) or not value:
            continue
        replacement = ocr_text if status == "overridden" else ""
        rewritten = _tidy_gated_text_field(_replace_ocr_claim(value, claim, replacement))
        if rewritten == value:
            continue
        changed = True
        if rewritten:
            structured[field_key] = rewritten
        else:
            structured.pop(field_key, None)
    for field_key, statuses in statuses_by_field.items():
        if statuses and all(status == "rejected" for status in statuses):
            if structured.pop(field_key, None) is not None:
                changed = True
    if changed:
        result["final_text"] = compose_visual_final_text(structured, target)


def _has_timestamped_audio_evidence(audio: dict[str, Any] | None) -> bool:
    if not isinstance(audio, dict) or audio.get("status") not in {"analyzed", "partial"}:
        return False
    evidence = audio.get("evidence")
    if not isinstance(evidence, list):
        evidence = audio.get("segments")
    return bool(
        isinstance(evidence, list)
        and any(
            isinstance(item, dict)
            and item.get("start_seconds") is not None
            and item.get("end_seconds") is not None
            and (
                str(item.get("text") or "").strip()
                or str(item.get("evidence_type") or "").strip()
            )
            for item in evidence
        )
    )


def _merge_completed_shot_reanalysis_impl(
    db: Session,
    *,
    child_operation: ReverseOperation,
    child_revision: ReverseResultRevision,
    result: dict[str, Any],
    revision_shots,
    shot_by_id,
    create_result_revision,
) -> dict[str, Any] | None:
    link = (child_operation.request_context or {}).get("shot_reanalysis")
    if not isinstance(link, dict):
        return None
    try:
        parent_operation_id = int(link["parent_operation_id"])
        parent_revision_id = int(link["parent_revision_id"])
        parent_shot_id = str(link["parent_shot_id"])
    except (KeyError, TypeError, ValueError):
        return {"status": "failed", "reason": "重分析父分镜关联无效"}
    parent_operation = db.execute(
        select(ReverseOperation)
        .where(
            ReverseOperation.id == parent_operation_id,
            ReverseOperation.user_id == child_operation.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if parent_operation is None or parent_operation.status != "succeeded":
        return {"status": "failed", "reason": "原反推任务不存在或不可编辑"}
    latest = db.execute(
        select(ReverseResultRevision)
        .where(
            ReverseResultRevision.operation_id == parent_operation_id,
            ReverseResultRevision.user_id == child_operation.user_id,
            ReverseResultRevision.source.in_(("normalized", "user_edit")),
        )
        .order_by(ReverseResultRevision.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None or int(latest.id) != parent_revision_id:
        return {
            "status": "stale",
            "reason": "原分镜在重分析期间已更新，未自动覆盖",
            "parent_operation_id": parent_operation_id,
            "expected_parent_revision_id": parent_revision_id,
            "actual_parent_revision_id": int(latest.id) if latest is not None else None,
            "parent_shot_id": parent_shot_id,
        }
    payload, shots = revision_shots(latest)
    parent_shot = shot_by_id(shots, parent_shot_id)
    if parent_shot.get("locked"):
        return {
            "status": "stale",
            "reason": "原分镜已锁定，未自动覆盖",
            "parent_operation_id": parent_operation_id,
            "parent_revision_id": int(latest.id),
            "parent_shot_id": parent_shot_id,
        }
    child_analysis = result.get("video_analysis")
    child_shots = (
        child_analysis.get("shots")
        if isinstance(child_analysis, dict) and isinstance(child_analysis.get("shots"), list)
        else []
    )
    child_shots = [row for row in child_shots if isinstance(row, dict)]
    if not child_shots:
        return {"status": "failed", "reason": "单镜头重分析没有返回可合并分镜"}
    for field in (
        "visual", "subject_tracking", "pose", "action", "camera", "lighting",
        "transition", "ocr", "audio_cue",
    ):
        values = [
            str(row.get(field) or "").strip()
            for row in child_shots
            if str(row.get(field) or "").strip()
        ]
        if values:
            parent_shot[field] = "；".join(dict.fromkeys(values))
    confidences = [
        float(row.get("confidence"))
        for row in child_shots
        if isinstance(row.get("confidence"), (int, float))
    ]
    if confidences:
        parent_shot["confidence"] = round(sum(confidences) / len(confidences), 6)
    parent_shot.pop("compiled_prompt", None)
    parent_shot.pop("compilation", None)
    parent_shot["reanalysis_evidence"] = {
        "operation_id": int(child_operation.id),
        "revision_id": int(child_revision.id),
        "source_range": deepcopy(link.get("source_range")),
        "shots": deepcopy(child_shots),
    }
    analysis = dict(payload["video_analysis"])
    analysis["shots"] = shots
    payload["video_analysis"] = analysis
    payload["shot_reanalysis_merge"] = {
        "child_operation_id": int(child_operation.id),
        "child_revision_id": int(child_revision.id),
        "parent_shot_id": parent_shot_id,
    }
    merged_revision = create_result_revision(
        db,
        operation_id=parent_operation_id,
        user_id=int(child_operation.user_id),
        source="user_edit",
        payload=payload,
        parent_revision_id=int(latest.id),
        commit=False,
    )
    return {
        "status": "merged",
        "parent_operation_id": parent_operation_id,
        "parent_revision_id": int(merged_revision.id),
        "parent_shot_id": parent_shot_id,
    }
