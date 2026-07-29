"""Read models and serialization for Studio prompt optimization."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import (
    ModelCapabilityVersion,
    ModelConfig,
    PromptOptimizationProposal,
    ReverseOperation,
    ReverseResultRevision,
)
from . import reverse_lineage
from .config_store import ModelConfigResolutionError, resolve_model_config
from .model_versions import sync_model_versions


class PromptOptimizationError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


_COMPILER_CAPABILITY_KEYS = frozenset(
    {
        "text_to_image",
        "image_to_image",
        "reference_image",
        "multi_reference",
        "text_to_video",
        "image_to_video",
        "video_to_video",
        "first_last_frame",
        "aspect_ratios",
        "resolutions",
        "durations",
        "max_duration_seconds",
        "max_reference_images",
    }
)


def _as_dict(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def _serialize_proposal(row: PromptOptimizationProposal) -> dict[str, Any]:
    return {
        "proposal_id": int(row.id),
        "proposal_version": int(row.version),
        "mode": row.mode,
        "optimization_kind": row.optimization_kind,
        "original": deepcopy(row.original),
        "suggestion": deepcopy(row.suggestion),
        "segments": deepcopy(row.diff or []),
        "constraint_coverage": deepcopy(row.constraint_coverage or []),
        "warnings": deepcopy(row.warnings or []),
        "provenance": deepcopy(row.provenance or {}),
        "compiler_profile": deepcopy(row.catalog_snapshot),
        "charged_credits": int(row.charged_credits or 0),
    }


def _aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _proposal_history_state(
    row: PromptOptimizationProposal,
) -> tuple[str, bool]:
    expired = _aware_datetime(row.expires_at) <= datetime.now(timezone.utc)
    status = "expired" if row.status == "proposed" and expired else row.status
    execution_status = str(_as_dict(row.metrics).get("execution_status") or "succeeded")
    can_decide = status == "proposed" and execution_status == "succeeded"
    return status, can_decide


def _serialize_proposal_detail(row: PromptOptimizationProposal) -> dict[str, Any]:
    status, can_decide = _proposal_history_state(row)
    return {
        **_serialize_proposal(row),
        "status": status,
        "category": row.category,
        "target_model_config_id": int(row.target_model_config_id),
        "source_operation_id": (
            int(row.source_operation_id) if row.source_operation_id is not None else None
        ),
        "source_revision_id": (
            int(row.source_revision_id) if row.source_revision_id is not None else None
        ),
        "accepted_segment_ids": deepcopy(row.accepted_segment_ids or []),
        "rejected_segment_ids": deepcopy(row.rejected_segment_ids or []),
        "decision_result": deepcopy(row.decision_result),
        "expires_at": row.expires_at,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "can_decide": can_decide,
    }


def _preview_text(value: Any, *, limit: int = 160) -> str:
    collapsed = " ".join(str(value or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1].rstrip()}…"


def _serialize_proposal_summary(row: PromptOptimizationProposal) -> dict[str, Any]:
    status, can_decide = _proposal_history_state(row)
    original = _as_dict(row.original)
    suggestion = _as_dict(row.suggestion)
    profile = _as_dict(row.catalog_snapshot)
    return {
        "proposal_id": int(row.id),
        "proposal_version": int(row.version),
        "status": status,
        "category": row.category,
        "mode": row.mode,
        "optimization_kind": row.optimization_kind,
        "target_model_config_id": int(row.target_model_config_id),
        "target_model_id": str(profile.get("model_id") or "") or None,
        "original_preview": _preview_text(original.get("final_text")),
        "suggestion_preview": _preview_text(suggestion.get("final_text")),
        "changed_segment_count": sum(
            1
            for segment in (row.diff or [])
            if isinstance(segment, dict) and bool(segment.get("changed"))
        ),
        "charged_credits": int(row.charged_credits or 0),
        "expires_at": row.expires_at,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
        "can_decide": can_decide,
    }


def list_proposals(
    db: Session,
    *,
    user_id: int,
    status: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    statement = select(PromptOptimizationProposal).where(
        PromptOptimizationProposal.user_id == int(user_id)
    )
    if status == "expired":
        statement = statement.where(
            or_(
                PromptOptimizationProposal.status == "expired",
                and_(
                    PromptOptimizationProposal.status == "proposed",
                    PromptOptimizationProposal.expires_at <= datetime.now(timezone.utc),
                ),
            )
        )
    elif status == "proposed":
        statement = statement.where(
            PromptOptimizationProposal.status == "proposed",
            PromptOptimizationProposal.expires_at > datetime.now(timezone.utc),
        )
    elif status:
        statement = statement.where(PromptOptimizationProposal.status == status)
    rows = list(
        db.scalars(
            statement.order_by(
                PromptOptimizationProposal.created_at.desc(),
                PromptOptimizationProposal.id.desc(),
            )
            .offset(int(offset))
            .limit(int(limit) + 1)
        )
    )
    return {
        "items": [_serialize_proposal_summary(row) for row in rows[:limit]],
        "limit": int(limit),
        "offset": int(offset),
        "has_more": len(rows) > limit,
    }


def get_proposal(
    db: Session,
    *,
    user_id: int,
    proposal_id: int,
) -> dict[str, Any]:
    proposal = db.scalar(
        select(PromptOptimizationProposal).where(
            PromptOptimizationProposal.id == int(proposal_id),
            PromptOptimizationProposal.user_id == int(user_id),
        )
    )
    if proposal is None:
        raise PromptOptimizationError(404, "优化建议不存在")
    return _serialize_proposal_detail(proposal)


def _safe_compiler_capabilities(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    result = {
        key: deepcopy(raw[key])
        for key in _COMPILER_CAPABILITY_KEYS
        if key in raw and isinstance(raw[key], (bool, int, str, list))
    }
    profile = raw.get("prompt_profile")
    if isinstance(profile, dict):
        safe_profile: dict[str, Any] = {}
        for key in ("supported_fields", "dropped_fields"):
            if isinstance(profile.get(key), list) and all(
                isinstance(item, str) for item in profile[key]
            ):
                safe_profile[key] = profile[key][:100]
        transforms = profile.get("transforms")
        if isinstance(transforms, dict):
            safe_profile["transforms"] = {
                str(key)[:128]: str(description)[:500]
                for key, description in list(transforms.items())[:100]
                if isinstance(key, str)
            }
        if safe_profile:
            result["prompt_profile"] = safe_profile
    return result


def _load_lineage_source(
    db: Session,
    *,
    user_id: int,
    operation_id: int,
    revision_id: int,
) -> tuple[ReverseOperation, ReverseResultRevision, list[ReverseResultRevision]]:
    operation = db.get(ReverseOperation, int(operation_id))
    revision = db.get(ReverseResultRevision, int(revision_id))
    if (
        operation is None
        or revision is None
        or int(operation.user_id) != int(user_id)
        or int(revision.user_id) != int(user_id)
        or int(revision.operation_id) != int(operation.id)
    ):
        raise PromptOptimizationError(404, "反推版本不存在")
    if operation.status != "succeeded":
        raise PromptOptimizationError(409, "只有成功的反推任务才能优化")
    try:
        chain = reverse_lineage.validate_revision_chain(
            db,
            revision,
            terminal_source=revision.source,
        )
    except reverse_lineage.ReverseLineageError as exc:
        raise PromptOptimizationError(409, str(exc)) from exc
    return operation, revision, chain


def _resolve_user_edit_parent(
    revision: ReverseResultRevision,
    chain: list[ReverseResultRevision],
) -> ReverseResultRevision:
    """Resolve the exact branch point for an optimized user_edit revision."""
    if not chain or int(chain[0].id) != int(revision.id):
        raise PromptOptimizationError(409, "反推源版本血缘无法验证")
    if revision.source in {"normalized", "user_edit"}:
        return revision
    if revision.source == "applied":
        parent_id = int(revision.parent_revision_id or 0)
        parent = next(
            (item for item in chain if int(item.id) == parent_id),
            None,
        )
        if parent is not None and parent.source == "user_edit":
            return parent
        raise PromptOptimizationError(409, "applied 版本缺少有效的 user_edit 父节点")
    raise PromptOptimizationError(
        409,
        f"不支持从 {revision.source} 版本创建提示词优化分支",
    )


def _active_compiler_profile(
    db: Session,
    *,
    model_config_id: int,
    category: str,
) -> tuple[ModelConfig, ModelCapabilityVersion, dict[str, Any]]:
    try:
        model = resolve_model_config(db, category, model_config_id)
    except ModelConfigResolutionError as exc:
        raise PromptOptimizationError(400, str(exc)) from exc
    if model is None or not model.enabled:
        raise PromptOptimizationError(404, "目标生成模型不存在或未启用")
    capability = db.scalar(
        select(ModelCapabilityVersion).where(
            ModelCapabilityVersion.model_config_id == model.id,
            ModelCapabilityVersion.is_active.is_(True),
        )
    )
    if capability is None:
        capability, _ = sync_model_versions(db, model)
    snapshot = {
        "model_config_id": int(model.id),
        "model_id": model.model_id,
        "provider": model.provider,
        "capability_version_id": int(capability.id),
        "capability_version": int(capability.version),
        "schema_version": capability.schema_version,
        "capabilities": _safe_compiler_capabilities(capability.capabilities),
    }
    return model, capability, snapshot


def _prompt_from_payload(payload: dict[str, Any]) -> str:
    value = str(payload.get("final_text") or "").strip()
    if value:
        return value
    structured = payload.get("structured")
    if isinstance(structured, dict) and structured:
        return json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    raise PromptOptimizationError(409, "反推版本没有可优化的提示词")


def _server_context(operation: ReverseOperation, payload: dict[str, Any]) -> dict[str, Any]:
    request_context = (
        operation.request_context if isinstance(operation.request_context, dict) else {}
    )
    workspace_snapshot = request_context.get("workspace_snapshot_v3")
    if not isinstance(workspace_snapshot, dict):
        workspace_snapshot = request_context.get("workspace_snapshot_v2")
    if not isinstance(workspace_snapshot, dict):
        workspace_snapshot = {}
    subject_mode = (
        str(
            request_context.get("subject_mode")
            or workspace_snapshot.get("subject_mode")
            or workspace_snapshot.get("subjectMode")
            or ""
        )
        .strip()
        .lower()
    )
    if not subject_mode and operation.target == "product_profile":
        subject_mode = "product"
    elif not subject_mode and operation.target == "portrait_profile":
        subject_mode = "portrait"
    return {
        "target": operation.target,
        "output_purpose": operation.output_purpose,
        "analysis_focus": operation.analysis_focus,
        "structured": deepcopy(payload.get("structured") or {}),
        "image_evidence": deepcopy(payload.get("image_evidence") or []),
        "parameters": deepcopy(payload.get("parameters") or {}),
        "video_analysis": deepcopy(payload.get("video_analysis") or {}),
        "source_type": request_context.get("source_type"),
        "source_asset_url": operation.asset_url,
        "subject_mode": subject_mode or None,
        "product_lock_mode": (
            request_context.get("product_lock_mode")
            or workspace_snapshot.get("product_lock_mode")
            or workspace_snapshot.get("productLockMode")
        ),
        "product_video_template": (
            request_context.get("product_video_template")
            or workspace_snapshot.get("product_video_template")
            or workspace_snapshot.get("productVideoTemplate")
        ),
    }
