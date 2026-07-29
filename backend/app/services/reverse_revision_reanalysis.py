"""Single-shot reanalysis and generation preparation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReverseOperation, ReverseResultRevision
from ..schemas import ReverseOperationCreate
from .reverse_operation_records import _get_owned
from .reverse_quotes import (
    ReverseOperationConflict,
    ReverseOperationNotFound,
)
from .reverse_revision_timeline import (
    _latest_timeline_revision,
    _revision_shots,
    _shot_by_id,
)


def shot_reanalysis_body(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    client_request_id: str,
    shot_id: str,
    analysis_precision: str | None,
    include_audio: bool | None,
) -> tuple[ReverseOperationCreate, dict[str, Any]]:
    operation = _get_owned(db, operation_id, user_id)
    revision = _latest_timeline_revision(db, operation_id=operation_id, user_id=user_id)
    _payload, shots = _revision_shots(revision)
    shot = _shot_by_id(shots, shot_id)
    if shot.get("locked"):
        raise ReverseOperationConflict("锁定镜头不能重分析")
    context = dict(operation.request_context or {})
    context.update({
        "client_request_id": client_request_id,
        "source_range": None,
        "source_ranges": [{
            "start_seconds": float(shot["start_seconds"]),
            "end_seconds": float(shot["end_seconds"]),
        }],
        "custom_keyframes": [],
        "analysis_focus": "storyboard",
        "analysis_precision": analysis_precision
        or context.get("analysis_precision")
        or "standard",
        "video_analysis_preset": analysis_precision
        or context.get("analysis_precision")
        or "standard",
        "include_audio": (
            bool(context.get("include_audio"))
            if include_audio is None
            else include_audio
        ),
    })
    return ReverseOperationCreate.model_validate(context), {
        "contract_version": 1,
        "parent_operation_id": int(operation.id),
        "parent_revision_id": int(revision.id),
        "parent_shot_id": str(shot["shot_id"]),
        "source_segment_index": int(shot.get("source_segment_index") or 1),
        "source_range": {
            "start_seconds": float(shot["start_seconds"]),
            "end_seconds": float(shot["end_seconds"]),
        },
    }


def attach_shot_reanalysis_context(
    db: Session,
    *,
    operation: ReverseOperation,
    user_id: int,
    context: dict[str, Any],
) -> ReverseOperation:
    if int(operation.user_id) != int(user_id):
        raise ReverseOperationNotFound("反推任务不存在")
    request_context = dict(operation.request_context or {})
    existing = request_context.get("shot_reanalysis")
    if isinstance(existing, dict) and existing != context:
        raise ReverseOperationConflict("重分析任务已关联不同的父分镜")
    request_context["shot_reanalysis"] = deepcopy(context)
    operation.request_context = request_context
    db.commit()
    db.refresh(operation)
    return operation


def prepare_shot_generation(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    shot_id: str,
    revision_id: int | None,
    client_request_id: str,
    model_config_id: int | None,
    params: dict[str, Any],
    finding_contexts: list[dict[str, Any]] | None = None,
    prompt_patch: str | None = None,
    negative_prompt_patch: str | None = None,
    reproduction_remediation_id: int | None = None,
    reproduction_plan_item_id: str | None = None,
) -> dict[str, Any]:
    _get_owned(db, operation_id, user_id)
    if revision_id is None:
        revision = db.execute(
            select(ReverseResultRevision)
            .where(
                ReverseResultRevision.operation_id == operation_id,
                ReverseResultRevision.user_id == user_id,
                ReverseResultRevision.source == "applied",
            )
            .order_by(ReverseResultRevision.version.desc())
            .limit(1)
        ).scalar_one_or_none()
    else:
        revision = db.get(ReverseResultRevision, revision_id)
    if (
        revision is None
        or revision.source != "applied"
        or int(revision.operation_id) != operation_id
        or int(revision.user_id) != user_id
    ):
        raise ReverseOperationConflict("单镜头生成必须引用已应用的反推版本")
    _payload, shots = _revision_shots(revision)
    shot = _shot_by_id(shots, shot_id)
    correction_findings = [
        {
            key: deepcopy(item.get(key))
            for key in (
                "finding_id",
                "dimension",
                "kind",
                "severity",
                "confidence",
                "instruction",
                "time_range",
                "shot_id",
            )
            if item.get(key) is not None
        }
        for item in list(finding_contexts or [])[:100]
        if isinstance(item, dict)
    ]
    normalized_prompt_patch = str(prompt_patch or "").strip() or None
    normalized_negative_patch = str(negative_prompt_patch or "").strip() or None
    structured = {"shot": shot}
    if correction_findings or normalized_prompt_patch or normalized_negative_patch:
        structured["reproduction_correction"] = {
            "findings": correction_findings,
            "prompt_patch": normalized_prompt_patch,
            "negative_prompt_patch": normalized_negative_patch,
        }
    prompt_parts = [
        str(shot.get(key) or "").strip()
        for key in ("visual", "action", "camera", "lighting", "transition")
        if str(shot.get(key) or "").strip()
    ]
    prompt_parts.extend(
        str(item.get("instruction") or "").strip()
        for item in correction_findings
        if str(item.get("instruction") or "").strip()
    )
    if normalized_prompt_patch:
        prompt_parts.append(normalized_prompt_patch)
    prompt = {
        "structured": structured,
        "final_text": "；".join(dict.fromkeys(prompt_parts)),
    }
    request = {
        "client_request_id": client_request_id,
        "reverse_operation_id": operation_id,
        "reverse_revision_id": int(revision.id),
        "category": "video",
        "stage": "preview",
        "prompt": prompt,
        "params": {
            **dict(params or {}),
            **(
                {"negative_prompt": normalized_negative_patch}
                if normalized_negative_patch
                else {}
            ),
        },
        "shot_context": {
            "contract_version": 1,
            "source_range": {
                "start_seconds": shot["start_seconds"],
                "end_seconds": shot["end_seconds"],
            },
            "source_segment_index": shot.get("source_segment_index", 1),
            "shot_id": shot_id,
        },
        **(
            {
                "reproduction_remediation_id": int(reproduction_remediation_id),
                "reproduction_plan_item_id": str(reproduction_plan_item_id),
            }
            if reproduction_remediation_id is not None
            and reproduction_plan_item_id is not None
            else {}
        ),
        **({"model_config_id": model_config_id} if model_config_id is not None else {}),
    }
    return {
        "reverse_operation_id": operation_id,
        "source_revision_id": int(revision.id),
        "shot_id": shot_id,
        "request": request,
    }
