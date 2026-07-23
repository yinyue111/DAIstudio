"""Lineage-safe Studio prompt optimization and target-model compilation."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AuditLog,
    GenerationQuote,
    ModelCapabilityVersion,
    ModelConfig,
    PromptOptimizationProposal,
    ReverseOperation,
    ReverseResultRevision,
)
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from . import audit, credits, gateway, reverse_lineage, reverse_operations, usage
from .config_store import ModelConfigResolutionError, resolve_model_config
from .generation_model_runtime import FrozenAdapterConfigError, frozen_image_adapter
from .generation_prompts import (
    compile_image_prompt_profile,
    generation_prompt_for_model,
    product_fidelity_prompt,
)
from .model_gateway_config import runtime_config_for_model
from .model_versions import sync_model_versions
from .video_prompt_compiler import (
    COMPILER_VERSION as VIDEO_PROMPT_COMPILER_VERSION,
)
from .video_prompt_compiler import (
    VIDEO_SUBMIT_CONTRACT_VERSION,
    build_video_prompt_references,
    compile_video_prompt,
)


class PromptOptimizationError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


log = logging.getLogger(__name__)


_QUOTED_TEXT_RE = re.compile(r"[“「『\"']([^\"'”」』\n]{1,200})[”」』\"']")
_SEMANTIC_KEY_RE = re.compile(
    r"(约束|不可改|必须保持|一致性|保护|constraint|locked|preserve)",
    re.IGNORECASE,
)
_COMPILER_VERSION = "studio-prompt-compiler.v2"
_IMAGE_PROMPT_COMPILER_VERSION = "image-generation-runtime.v1"
_PROPOSAL_TTL = timedelta(minutes=30)
_COMPILER_CAPABILITY_KEYS = frozenset({
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
})


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _as_dict(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def request_fingerprint(body: StudioPromptOptimizationIn) -> str:
    raw = json.dumps(
        body.model_dump(mode="json", exclude={"quote_id"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


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
                    PromptOptimizationProposal.expires_at
                    <= datetime.now(timezone.utc),
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
            statement
            .order_by(
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
    request_context = operation.request_context if isinstance(operation.request_context, dict) else {}
    workspace_snapshot = request_context.get("workspace_snapshot_v3")
    if not isinstance(workspace_snapshot, dict):
        workspace_snapshot = request_context.get("workspace_snapshot_v2")
    if not isinstance(workspace_snapshot, dict):
        workspace_snapshot = {}
    subject_mode = str(
        request_context.get("subject_mode")
        or workspace_snapshot.get("subject_mode")
        or workspace_snapshot.get("subjectMode")
        or ""
    ).strip().lower()
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


def _constraint(
    *,
    scope: str,
    constraint_type: str,
    source_field: str,
    value: Any,
) -> dict[str, Any]:
    return {
        "constraint_id": _stable_id("constraint", scope, constraint_type, source_field, value),
        "type": constraint_type,
        "source_field": source_field,
        "value": deepcopy(value),
    }


def _lineage_constraints(revision: ReverseResultRevision, payload: dict[str, Any]) -> list[dict[str, Any]]:
    scope = f"revision:{int(revision.id)}"
    constraints: list[dict[str, Any]] = []
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    for key in ("aspect_ratio", "ratio"):
        if parameters.get(key):
            constraints.append(_constraint(
                scope=scope,
                constraint_type="aspect_ratio",
                source_field=f"parameters.{key}",
                value=str(parameters[key]),
            ))
            break
    for key in ("duration", "vDuration"):
        if parameters.get(key) is not None:
            constraints.append(_constraint(
                scope=scope,
                constraint_type="duration",
                source_field=f"parameters.{key}",
                value=parameters[key],
            ))
            break
    negative = payload.get("negative") or payload.get("negative_prompt")
    if negative:
        values = (
            [str(item).strip() for item in negative if str(item).strip()]
            if isinstance(negative, list)
            else [item.strip() for item in re.split(r"[,，;；\n]+", str(negative)) if item.strip()]
        )
        if values:
            constraints.append(_constraint(
                scope=scope,
                constraint_type="negative_list",
                source_field="negative",
                value=values,
            ))
    final_text = str(payload.get("final_text") or "")
    for index, match in enumerate(_QUOTED_TEXT_RE.finditer(final_text)):
        constraints.append(_constraint(
            scope=scope,
            constraint_type="exact_text",
            source_field=f"final_text.quote[{index}]",
            value=match.group(1),
        ))
    structured = payload.get("structured") if isinstance(payload.get("structured"), dict) else {}
    for key, value in structured.items():
        if _SEMANTIC_KEY_RE.search(str(key)) and value not in (None, "", [], {}):
            constraints.append(_constraint(
                scope=scope,
                constraint_type="semantic",
                source_field=f"structured.{key}",
                value=value,
            ))
    evidence = payload.get("image_evidence") if isinstance(payload.get("image_evidence"), list) else []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or not item.get("protected"):
            continue
        constraints.append(_constraint(
            scope=scope,
            constraint_type="protected_evidence",
            source_field=f"image_evidence[{index}]",
            value={
                "evidence_id": item.get("evidence_id") or f"index-{index}",
                "field_key": item.get("field_key"),
                "evidence_text": item.get("evidence_text"),
            },
        ))
    return constraints


def _request_constraints(body: StudioPromptOptimizationIn, *, scope: str) -> list[dict[str, Any]]:
    return [
        _constraint(
            scope=scope,
            constraint_type=item.type,
            source_field=f"request.protected_constraints[{index}]",
            value=item.value,
        )
        for index, item in enumerate(body.protected_constraints)
    ]


def _get_path(payload: dict[str, Any], field_path: str) -> Any:
    current: Any = payload
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(payload: dict[str, Any], field_path: str, value: Any) -> None:
    parts = field_path.split(".")
    current = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = deepcopy(value)


def _delete_path(payload: dict[str, Any], field_path: str) -> bool:
    parts = field_path.split(".")
    current: Any = payload
    for part in parts[:-1]:
        if not isinstance(current, dict):
            return False
        current = current.get(part)
    if not isinstance(current, dict) or parts[-1] not in current:
        return False
    del current[parts[-1]]
    return True


def _apply_compiler_contract(
    payload: dict[str, Any],
    *,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    compiled = deepcopy(payload)
    parameters = compiled.get("parameters")
    if isinstance(parameters, dict):
        fields = {
            "aspect_ratios": ("aspect_ratio", "ratio"),
            "resolutions": ("resolution", "vResolution"),
            "durations": ("duration", "vDuration"),
        }
        for capability_key, aliases in fields.items():
            choices = capabilities.get(capability_key)
            if not isinstance(choices, list):
                continue
            supported = {str(item) for item in choices}
            for alias in aliases:
                value = parameters.get(alias)
                if value not in (None, "") and str(value) not in supported:
                    parameters.pop(alias, None)
    profile = capabilities.get("prompt_profile")
    if isinstance(profile, dict):
        for raw_path in profile.get("dropped_fields") or []:
            path = str(raw_path or "").strip()
            if path in {"negative", "negative_prompt"} or re.fullmatch(
                r"(?:structured|parameters)\.[^\.]{1,128}", path
            ):
                _delete_path(compiled, path)
    return compiled


def _positive_duration(value: Any, *, default: float = 10.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _model_compiled_preview(
    source_payload: dict[str, Any],
    source: str,
    *,
    category: str,
    target_model: ModelConfig,
    context: dict[str, Any],
    capability_profile: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the same deterministic compiler used by the generation path."""
    candidate = deepcopy(source_payload)
    structured = (
        deepcopy(source_payload.get("structured"))
        if isinstance(source_payload.get("structured"), dict)
        else {}
    )
    # Studio stores the analysis as a nested object, while the generation
    # payload exposes reviewed structured fields at the prompt root.
    generation_prompt_payload = {**structured, **deepcopy(source_payload)}
    parameters = (
        deepcopy(source_payload.get("parameters"))
        if isinstance(source_payload.get("parameters"), dict)
        else {}
    )
    subject_mode = str(
        context.get("subject_mode")
        or parameters.get("subject_mode")
        or ("product" if context.get("product_mode") else "")
    ).strip().lower()
    source_asset_url = str(context.get("source_asset_url") or "").strip() or None
    source_type = str(context.get("source_type") or "").strip() or None
    compiler_warnings: list[dict[str, str]] = []

    if category == "video":
        references = build_video_prompt_references(
            source_asset_url=source_asset_url,
            source_type=source_type,
            params=parameters,
        )
        extra = deepcopy(target_model.extra) if isinstance(target_model.extra, dict) else {}
        model_profiles = extra.get("video_prompt_profiles") or extra.get("prompt_profiles")
        if not isinstance(model_profiles, dict):
            model_profiles = None
        duration = _positive_duration(
            parameters.get("duration")
            or parameters.get("vDuration")
            or context.get("duration")
        )
        compiled = compile_video_prompt(
            generation_prompt_payload,
            duration=duration,
            model_id=str(target_model.model_id or ""),
            provider=str(target_model.provider or ""),
            extra=extra,
            references=references,
            product_reference=any(item.get("role") == "product" for item in references),
            portrait_reference=any(item.get("role") == "character" for item in references),
            product_lock_mode=str(
                parameters.get("product_lock_mode")
                or context.get("product_lock_mode")
                or "locked"
            ),
            product_video_template=str(
                parameters.get("product_video_template")
                or context.get("product_video_template")
                or "prompt_driven"
            ),
            model_profiles=model_profiles,
            fit_mode="single_clip",
        )
        compiled_prompt = str(compiled.get("prompt") or "").strip()
        if not compiled_prompt:
            raise PromptOptimizationError(502, "视频提示词编译器未返回有效结果")
        plan = compiled.get("plan") if isinstance(compiled.get("plan"), dict) else {}
        post_production = {
            "subtitles": list(plan.get("post_overlays") or []),
            "voiceover": str(plan.get("voiceover") or "").strip(),
            "sfx": list(plan.get("sfx") or []),
        }
        compiler_metadata = {
            "version": str(compiled.get("compiler_version") or VIDEO_PROMPT_COMPILER_VERSION),
            "orchestrator_version": _COMPILER_VERSION,
            "submit_contract_version": VIDEO_SUBMIT_CONTRACT_VERSION,
            "category": "video",
            "target_model_config_id": int(target_model.id),
            "target_model_id": target_model.model_id,
            "capability_version_id": int(capability_profile["capability_version_id"]),
            "capability_version": int(capability_profile["capability_version"]),
            "profile": deepcopy(compiled.get("profile") or {}),
            "metadata": deepcopy(compiled.get("metadata") or {}),
            "sequence_required": bool(compiled.get("sequence_required")),
            "post_production": post_production,
        }
        for message in plan.get("warnings") or []:
            if str(message or "").strip():
                compiler_warnings.append({
                    "code": "video_compiler_warning",
                    "field": "final_text",
                    "action": "info",
                    "message": str(message).strip()[:500],
                })
        reference_roles = deepcopy(references)
    else:
        task_parameters = deepcopy(parameters)
        if subject_mode:
            task_parameters["subject_mode"] = subject_mode
        # A reverse source selected in Studio is the same reference that the
        # generation request will carry. Expose it to the production helpers so
        # product/portrait fidelity guards match the eventual outbound prompt.
        if source_type == "image" and source_asset_url:
            task_parameters.setdefault("reference_image_url", source_asset_url)
        elif subject_mode in {"product", "portrait"}:
            task_parameters.setdefault(
                "reference_image_url",
                "studio://declared-reference",
            )
        compile_task = SimpleNamespace(
            category="image",
            prompt=generation_prompt_payload,
            params=task_parameters,
            source_type=source_type,
            source_asset_url=source_asset_url,
        )
        compiled_prompt = generation_prompt_for_model(source, compile_task)
        compiled_prompt = product_fidelity_prompt(compiled_prompt, compile_task)
        try:
            adapter = frozen_image_adapter(target_model.extra)
        except FrozenAdapterConfigError as exc:
            raise PromptOptimizationError(409, f"目标图片模型编译配置无效：{exc}") from exc
        compiled_prompt = compile_image_prompt_profile(
            compiled_prompt,
            adapter.prompt_profile,
        ).strip()
        if not compiled_prompt:
            raise PromptOptimizationError(502, "图片提示词编译器未返回有效结果")
        post_production = {"subtitles": [], "voiceover": "", "sfx": []}
        compiler_metadata = {
            "version": _IMAGE_PROMPT_COMPILER_VERSION,
            "orchestrator_version": _COMPILER_VERSION,
            "category": "image",
            "target_model_config_id": int(target_model.id),
            "target_model_id": target_model.model_id,
            "capability_version_id": int(capability_profile["capability_version_id"]),
            "capability_version": int(capability_profile["capability_version"]),
            "prompt_profile": deepcopy(adapter.prompt_profile),
            "post_production": post_production,
        }
        reference_roles = []

    candidate["final_text"] = compiled_prompt
    candidate["compiler_metadata"] = compiler_metadata
    candidate["model_compiled"] = {
        "schema_version": "model-compiled-preview.v1",
        "category": category,
        "source_prompt": source,
        "prompt": compiled_prompt,
        "target_model": {
            "model_config_id": int(target_model.id),
            "model_id": target_model.model_id,
            "provider": target_model.provider,
        },
        "compiler": deepcopy(compiler_metadata),
        "reference_roles": reference_roles,
        "post_production": post_production,
    }
    return candidate, {
        "prompt": compiled_prompt,
        "compiled_prompt": compiled_prompt,
        "constraint_coverage": [],
        "optimizer_model_id": None,
        "compiler_warnings": compiler_warnings,
    }


def _candidate_payload(source_payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(source_payload)
    optimized = str(result.get("prompt") or result.get("compiled_prompt") or "").strip()
    if not optimized:
        raise PromptOptimizationError(502, "优化模型未返回有效提示词")
    candidate["final_text"] = optimized
    suggestions = result.get("field_suggestions")
    if isinstance(suggestions, dict):
        for path, value in suggestions.items():
            normalized = str(path or "").strip()
            if normalized == "final_text" or normalized == "negative" or re.fullmatch(
                r"(?:structured|parameters)\.[^\.]{1,128}", normalized
            ):
                _set_path(candidate, normalized, value)
    return candidate


def _segments(original: dict[str, Any], suggestion: dict[str, Any]) -> list[dict[str, Any]]:
    paths = {"final_text", "negative", "compiler_metadata"}
    for root in ("structured", "parameters"):
        before = original.get(root) if isinstance(original.get(root), dict) else {}
        after = suggestion.get(root) if isinstance(suggestion.get(root), dict) else {}
        paths.update(f"{root}.{key}" for key in set(before).union(after))
    segments: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: (item != "final_text", item)):
        before = _get_path(original, path)
        after = _get_path(suggestion, path)
        if before == after and path != "final_text":
            continue
        segments.append({
            "id": _stable_id("segment", path, before, after),
            "field_path": path,
            "label": path.replace("structured.", "结构化字段 ").replace("parameters.", "参数 "),
            "original": deepcopy(before),
            "suggestion": deepcopy(after),
            "changed": before != after,
        })
    return segments


def _compiler_warnings(
    original: dict[str, Any],
    candidate: dict[str, Any],
    *,
    category: str,
    capabilities: dict[str, Any],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    parameters = (
        original.get("parameters") if isinstance(original.get("parameters"), dict) else {}
    )
    candidate_parameters = (
        candidate.get("parameters")
        if isinstance(candidate.get("parameters"), dict)
        else {}
    )
    requested = {
        "aspect_ratio": parameters.get("aspect_ratio") or parameters.get("ratio"),
        "resolution": parameters.get("resolution") or parameters.get("vResolution"),
        "duration": parameters.get("duration") or parameters.get("vDuration"),
    }
    supported = {
        "aspect_ratio": capabilities.get("aspect_ratios"),
        "resolution": capabilities.get("resolutions"),
        "duration": capabilities.get("durations"),
    }
    for field, value in requested.items():
        choices = supported[field]
        if value in (None, ""):
            continue
        if not isinstance(choices, list):
            warnings.append({
                "code": "capability_field_undeclared",
                "field": field,
                "action": "unverified",
                "message": f"目标模型能力版本未声明 {field} 支持范围，不能声称已兼容。",
            })
        elif str(value) not in {str(item) for item in choices}:
            aliases = {
                "aspect_ratio": ("aspect_ratio", "ratio"),
                "resolution": ("resolution", "vResolution"),
                "duration": ("duration", "vDuration"),
            }[field]
            dropped = all(candidate_parameters.get(alias) in (None, "") for alias in aliases)
            warnings.append({
                "code": "unsupported_field_value",
                "field": field,
                "action": "dropped" if dropped else "unverified",
                "message": (
                    f"目标模型能力版本不支持 {field}={value}，编译结果已移除该参数。"
                    if dropped
                    else f"目标模型能力版本不支持 {field}={value}，但候选结果仍保留该值。"
                ),
            })
    required_capability = "text_to_video" if category == "video" else "text_to_image"
    if capabilities.get(required_capability) is False:
        warnings.append({
            "code": "unsupported_generation_mode",
            "field": required_capability,
            "action": "unverified",
            "message": f"目标模型能力版本明确不支持 {required_capability}。",
        })
    profile = capabilities.get("prompt_profile")
    if isinstance(profile, dict):
        for field in profile.get("dropped_fields") or []:
            if isinstance(field, str) and _get_path(original, field) not in (None, "", [], {}):
                dropped = _get_path(candidate, field) in (None, "", [], {})
                warnings.append({
                    "code": "profile_dropped_field",
                    "field": field,
                    "action": "dropped" if dropped else "unverified",
                    "message": (
                        f"目标模型提示词配置文件已移除 {field}。"
                        if dropped
                        else f"目标模型提示词配置要求移除 {field}，但候选结果仍保留该字段。"
                    ),
                })
        transforms = profile.get("transforms")
        if isinstance(transforms, dict):
            for field, description in transforms.items():
                before = _get_path(original, field) if isinstance(field, str) else None
                after = _get_path(candidate, field) if isinstance(field, str) else None
                if isinstance(field, str) and before not in (None, "", [], {}):
                    transformed = before != after
                    warnings.append({
                        "code": "profile_transformed_field",
                        "field": field,
                        "action": "transformed" if transformed else "unverified",
                        "message": str(description or f"目标模型会转换 {field}。")[:500],
                    })
    return warnings


def _contains_exact_phrase(text: str, phrase: Any) -> bool:
    normalized_phrase = " ".join(str(phrase or "").casefold().split())
    if not normalized_phrase:
        return False
    phrase_pattern = r"\s+".join(
        re.escape(part) for part in normalized_phrase.split(" ")
    )
    return re.search(
        rf"(?<!\w){phrase_pattern}(?!\w)",
        str(text or "").casefold(),
    ) is not None


def _coverage(
    constraints: list[dict[str, Any]],
    *,
    original: dict[str, Any],
    suggestion: dict[str, Any],
    model_coverage: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    mapped = {
        str(item.get("constraint_id")): item
        for item in (model_coverage if isinstance(model_coverage, list) else [])
        if isinstance(item, dict) and item.get("constraint_id")
    }
    rows: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for constraint in constraints:
        kind = constraint["type"]
        value = constraint["value"]
        source_field = constraint["source_field"]
        evidence: list[dict[str, Any]] = []
        verified = False
        if kind == "aspect_ratio":
            candidate_value = (
                _get_path(suggestion, source_field)
                if source_field.startswith("parameters.")
                else _get_path(suggestion, "parameters.aspect_ratio")
                or _get_path(suggestion, "parameters.ratio")
            )
            verified = str(candidate_value) == str(value)
            if verified:
                evidence.append({"validator": "exact_field", "field": "parameters.aspect_ratio", "value": candidate_value})
        elif kind == "duration":
            candidate_value = (
                _get_path(suggestion, source_field)
                if source_field.startswith("parameters.")
                else _get_path(suggestion, "parameters.duration")
                or _get_path(suggestion, "parameters.vDuration")
            )
            verified = str(candidate_value) == str(value)
            if verified:
                evidence.append({"validator": "exact_field", "field": "parameters.duration", "value": candidate_value})
        elif kind == "negative_list":
            candidate = suggestion.get("negative") or suggestion.get("negative_prompt")
            candidate_values = candidate if isinstance(candidate, list) else [candidate]
            items = value if isinstance(value, list) else [value]
            verified = all(
                any(
                    _contains_exact_phrase(str(candidate_value or ""), item)
                    for candidate_value in candidate_values
                )
                for item in items
            )
            if verified:
                evidence.append({"validator": "exact_negative_items", "field": source_field, "items": items})
        elif kind == "exact_text":
            prompt_fields = [
                suggestion.get(field)
                for field in ("final_text", "compiled_prompt", "prompt")
                if isinstance(suggestion.get(field), str)
            ]
            verified = any(str(value) in field for field in prompt_fields)
            if verified:
                evidence.append({"validator": "exact_quoted_text", "quote": value})
            else:
                post_production = _get_path(
                    suggestion,
                    "model_compiled.post_production",
                )
                if isinstance(post_production, dict):
                    post_production_text = json.dumps(
                        post_production,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    verified = str(value) in post_production_text
                    if verified:
                        evidence.append({
                            "validator": "exact_post_production_text",
                            "field": "model_compiled.post_production",
                            "quote": value,
                        })
        elif kind == "protected_evidence":
            verified = original.get("image_evidence") == suggestion.get("image_evidence")
            if verified:
                evidence.append({"validator": "immutable_evidence_row", "field": source_field})
        if verified:
            rows.append({**constraint, "status": "verified", "confidence": 1.0, "evidence": evidence})
            continue
        claim = mapped.get(constraint["constraint_id"])
        if isinstance(claim, dict):
            field = str(claim.get("suggestion_field") or "").strip()
            quote = str(claim.get("evidence_quote") or "").strip()
            field_value = _get_path(suggestion, field) if field else None
            if field and quote and quote in str(field_value or ""):
                try:
                    confidence = min(max(float(claim.get("confidence") or 0), 0.0), 1.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                rows.append({
                    **constraint,
                    "status": "mapped",
                    "confidence": confidence,
                    "evidence": [{"field": field, "quote": quote, "source": "optimizer_mapping"}],
                    "warning": "语义覆盖仅有模型映射证据，未通过确定性验证。",
                })
                warnings.append({
                    "code": "semantic_coverage_unverified",
                    "field": source_field,
                    "action": "unverified",
                    "message": f"约束 {constraint['constraint_id']} 只有语义映射证据，需人工确认。",
                })
                continue
        rows.append({
            **constraint,
            "status": "unverified",
            "confidence": 0.0,
            "evidence": [],
            "warning": "未找到可验证的覆盖证据。",
        })
        warnings.append({
            "code": "constraint_unverified",
            "field": source_field,
            "action": "unverified",
            "message": f"约束 {constraint['constraint_id']} 未验证，不得视为已保留。",
        })
    return rows, warnings


def _invoke_optimizer(
    source: str,
    *,
    optimizer: ModelConfig,
    body: StudioPromptOptimizationIn,
    category: str,
    context: dict[str, Any],
    target_model: ModelConfig,
    capability_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return gateway.optimize_prompt(
        source,
        optimizer.model_id,
        category=category,
        product_mode=bool(context.get("product_mode")),
        duration=context.get("duration"),
        subject_mode=context.get("subject_mode"),
        reference_type=context.get("reference_type"),
        subject_profile={
            "structured": context.get("structured") or {},
            "image_evidence": context.get("image_evidence") or [],
            "video_analysis": context.get("video_analysis") or {},
            "protected_constraints": context.get("protected_constraints") or [],
        },
        target_model_id=target_model.model_id,
        target_model_provider=target_model.provider,
        aspect_ratio=context.get("aspect_ratio"),
        resolution=context.get("resolution"),
        target_model_extra={"capabilities": capability_snapshot.get("capabilities") or {}},
        direction="model_adaptation" if body.mode == "target_model_adaptation" else body.mode,
        target_language=body.target_language or "en",
        gateway_config=runtime_config_for_model(optimizer, "prompt"),
    )


def prepare_proposal_request(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
) -> dict[str, Any]:
    """Resolve immutable source, catalog profile, and billable optimizer without calling AI."""
    operation = None
    revision = None
    resolved_parent = None
    chain: list[ReverseResultRevision] = []
    if body.reverse_operation_id is not None:
        operation, revision, chain = _load_lineage_source(
            db,
            user_id=user_id,
            operation_id=body.reverse_operation_id,
            revision_id=body.reverse_revision_id,
        )
        resolved_parent = _resolve_user_edit_parent(revision, chain)
        original = _as_dict(revision.payload)
        source = _prompt_from_payload(original)
        category = "video" if operation.target == "video" else "image"
        context = _server_context(operation, original)
        parameters = context.get("parameters") or {}
        context.update({
            "duration": parameters.get("duration") or parameters.get("vDuration"),
            "aspect_ratio": parameters.get("aspect_ratio") or parameters.get("ratio"),
            "resolution": parameters.get("resolution") or parameters.get("vResolution"),
            "product_mode": operation.target == "product_profile",
        })
        constraints = _lineage_constraints(revision, original)
        constraint_scope = f"revision:{revision.id}"
    else:
        source = str(body.prompt or "").strip()
        category = body.category or "image"
        original = {"final_text": source}
        if body.duration is not None or body.aspect_ratio or body.resolution:
            original["parameters"] = {
                key: value
                for key, value in {
                    "duration": body.duration,
                    "aspect_ratio": body.aspect_ratio,
                    "resolution": body.resolution,
                }.items()
                if value is not None
            }
        context = {
            "duration": body.duration,
            "aspect_ratio": body.aspect_ratio,
            "resolution": body.resolution,
            "product_mode": bool(body.product_mode),
        }
        constraints = []
        constraint_scope = _stable_id("ordinary", source)
    constraints.extend(_request_constraints(body, scope=constraint_scope))
    context["protected_constraints"] = deepcopy(constraints)
    target_model, capability, profile = _active_compiler_profile(
        db,
        model_config_id=body.target_model_config_id,
        category=category,
    )
    compile_only = body.mode in {"model_adaptation", "target_model_adaptation"}
    optimizer = None
    estimated_credits = 0
    if not compile_only:
        try:
            optimizer = resolve_model_config(db, "prompt", body.optimizer_model_config_id)
        except ModelConfigResolutionError as exc:
            raise PromptOptimizationError(400, str(exc)) from exc
        if optimizer is None or not optimizer.enabled:
            raise PromptOptimizationError(503, "提示词优化模型未启用")
        estimated_credits = max(0, int(optimizer.cost_credits or 0))
    return {
        "operation": operation,
        "revision": revision,
        "resolved_parent": resolved_parent,
        "chain": chain,
        "original": original,
        "source": source,
        "category": category,
        "context": context,
        "constraints": constraints,
        "target_model": target_model,
        "capability": capability,
        "profile": profile,
        "compile_only": compile_only,
        "optimizer": optimizer,
        "estimated_credits": estimated_credits,
    }


def _proposal_artifacts(
    *,
    original: dict[str, Any],
    candidate: dict[str, Any],
    result: dict[str, Any],
    constraints: list[dict[str, Any]],
    profile: dict[str, Any],
    category: str,
    source: str,
    operation: ReverseOperation | None,
    revision: ReverseResultRevision | None,
    resolved_parent: ReverseResultRevision | None,
    optimizer: ModelConfig | None,
    compile_only: bool,
    request_hash: str,
) -> dict[str, Any]:
    candidate = _apply_compiler_contract(
        candidate,
        capabilities=profile["capabilities"],
    )
    segments = _segments(original, candidate)
    warnings = _compiler_warnings(
        original,
        candidate,
        category=category,
        capabilities=profile["capabilities"],
    )
    warnings.extend(deepcopy(result.get("compiler_warnings") or []))
    coverage, coverage_warnings = _coverage(
        constraints,
        original=original,
        suggestion=candidate,
        model_coverage=result.get("constraint_coverage"),
    )
    warnings.extend(coverage_warnings)
    provenance = {
        "reverse_operation_id": int(operation.id) if operation is not None else None,
        "reverse_revision_id": int(revision.id) if revision is not None else None,
        "reverse_revision_version": int(revision.version) if revision is not None else None,
        "reverse_revision_hash": revision.payload_hash if revision is not None else None,
        "selected_revision_id": int(revision.id) if revision is not None else None,
        "selected_revision_source": revision.source if revision is not None else None,
        "resolved_parent_revision_id": (
            int(resolved_parent.id) if resolved_parent is not None else None
        ),
        "resolved_parent_revision_source": (
            resolved_parent.source if resolved_parent is not None else None
        ),
        "optimizer_model_config_id": (
            int(optimizer.id) if not compile_only and optimizer is not None else None
        ),
        "optimizer_model_id": (
            optimizer.model_id if not compile_only and optimizer is not None else None
        ),
        "compiler_version": str(
            (
                (candidate.get("compiler_metadata") or {}).get("version")
                if isinstance(candidate.get("compiler_metadata"), dict)
                else None
            )
            or _COMPILER_VERSION
        ),
        "catalog_capability_version_id": profile["capability_version_id"],
    }
    metrics = {
        "request_hash": request_hash,
        "execution_status": "succeeded",
        "source_chars": len(source),
        "suggestion_chars": len(str(candidate.get("final_text") or "")),
        "edit_distance_ratio": 1.0 - SequenceMatcher(
            None,
            source,
            str(candidate.get("final_text") or ""),
        ).ratio(),
    }
    return {
        "candidate": candidate,
        "segments": segments,
        "warnings": warnings,
        "coverage": coverage,
        "provenance": provenance,
        "metrics": metrics,
    }


def _refund_reserved_proposal(
    db: Session,
    *,
    proposal_id: int,
    user_id: int,
    reserved: int,
    error: Exception,
) -> None:
    proposal = db.scalar(
        select(PromptOptimizationProposal)
        .where(PromptOptimizationProposal.id == int(proposal_id))
        .with_for_update()
    )
    if proposal is None:
        db.rollback()
        return
    if reserved:
        credits.refund(
            db,
            user_id,
            reserved,
            proposal.id,
            biz_type="prompt_optimize",
            commit=False,
        )
    proposal.status = "expired"
    proposal.charged_credits = 0
    proposal.expires_at = datetime.now(timezone.utc)
    proposal.metrics = {
        **_as_dict(proposal.metrics),
        "execution_status": "failed",
        "execution_error": str(error)[:500],
    }
    db.commit()


def reap_stale_running_proposals(
    db: Session,
    *,
    max_seconds: int | None = None,
) -> int:
    """Refund paid rewrites abandoned after the reservation was committed.

    The provider call intentionally runs outside a database transaction. A
    process crash in that window leaves an otherwise valid quote consumed and
    the user's credits frozen. Rows are rechecked under a lock so a concurrent
    successful completion wins cleanly.
    """
    timeout_seconds = max(
        60,
        int(
            max_seconds
            if max_seconds is not None
            else settings.prompt_optimization_running_timeout_seconds
        ),
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=timeout_seconds)
    candidate_ids = list(
        db.scalars(
            select(PromptOptimizationProposal.id).where(
                PromptOptimizationProposal.status == "proposed",
                PromptOptimizationProposal.quote_id.is_not(None),
                PromptOptimizationProposal.updated_at < cutoff,
            )
        )
    )
    reaped = 0
    for proposal_id in candidate_ids:
        try:
            proposal = db.scalar(
                select(PromptOptimizationProposal)
                .where(PromptOptimizationProposal.id == int(proposal_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if proposal is None:
                db.rollback()
                continue
            updated_at = proposal.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            execution_status = str(
                _as_dict(proposal.metrics).get("execution_status") or ""
            )
            if (
                proposal.status != "proposed"
                or execution_status != "running"
                or updated_at >= cutoff
            ):
                db.rollback()
                continue
            quote = db.get(GenerationQuote, int(proposal.quote_id))
            reserved = int(quote.estimated_credits or 0) if quote is not None else 0
            if reserved:
                credits.refund(
                    db,
                    int(proposal.user_id),
                    reserved,
                    int(proposal.id),
                    biz_type="prompt_optimize",
                    commit=False,
                )
            proposal.status = "expired"
            proposal.charged_credits = 0
            proposal.expires_at = now
            proposal.metrics = {
                **_as_dict(proposal.metrics),
                "execution_status": "failed",
                "execution_error": "提示词优化执行超时，冻结积分已退回",
                "reaped_at": now.isoformat(),
            }
            db.commit()
            reaped += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("failed to reap prompt optimization proposal %s", proposal_id)
    if reaped:
        log.info("reaped %s stale prompt optimization proposal(s)", reaped)
    return reaped


def create_proposal(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
) -> dict[str, Any]:
    request_hash = request_fingerprint(body)
    existing = db.scalar(
        select(PromptOptimizationProposal).where(
            PromptOptimizationProposal.user_id == user_id,
            PromptOptimizationProposal.idempotency_key == body.idempotency_key,
        )
    )
    if existing is not None:
        if str((existing.metrics or {}).get("request_hash") or "") != request_hash:
            raise PromptOptimizationError(409, "idempotency_key 已用于不同的优化请求")
        execution_status = str((existing.metrics or {}).get("execution_status") or "succeeded")
        if execution_status == "running":
            raise PromptOptimizationError(409, "该提示词优化请求仍在处理中")
        if execution_status == "failed":
            raise PromptOptimizationError(409, "该提示词优化请求已失败并退款，请重新发起报价")
        if existing.quote_id is not None and int(body.quote_id or 0) != int(existing.quote_id):
            raise PromptOptimizationError(409, "提示词优化报价与幂等请求不匹配")
        return _serialize_proposal(existing)
    prepared = prepare_proposal_request(db, user_id=user_id, body=body)
    operation = prepared["operation"]
    revision = prepared["revision"]
    resolved_parent = prepared["resolved_parent"]
    original = prepared["original"]
    source = prepared["source"]
    category = prepared["category"]
    context = prepared["context"]
    constraints = prepared["constraints"]
    target_model = prepared["target_model"]
    profile = prepared["profile"]
    compile_only = prepared["compile_only"]
    optimizer = prepared["optimizer"]
    charged_credits = int(prepared["estimated_credits"])
    reserved_proposal: PromptOptimizationProposal | None = None
    if compile_only:
        candidate, result = _model_compiled_preview(
            original,
            source,
            category=category,
            target_model=target_model,
            context=context,
            capability_profile=profile,
        )
    else:
        assert optimizer is not None
        if charged_credits:
            if body.quote_id is None:
                raise PromptOptimizationError(422, "付费提示词优化必须先获取并确认服务端报价")
            from .generation_quotes import (
                consume_execution_quote,
                lock_execution_quote,
                validate_prompt_optimization_quote,
            )

            quote = lock_execution_quote(
                db,
                quote_id=body.quote_id,
                user_id=user_id,
                kind="prompt_optimization",
            )
            charged_credits = validate_prompt_optimization_quote(
                db,
                quote,
                body=body,
                prepared=prepared,
            )
            reserved_proposal = PromptOptimizationProposal(
                user_id=user_id,
                source_operation_id=int(operation.id) if operation is not None else None,
                source_revision_id=int(revision.id) if revision is not None else None,
                target_model_config_id=int(target_model.id),
                capability_version_id=int(profile["capability_version_id"]),
                optimizer_model_config_id=int(optimizer.id),
                quote_id=int(quote.id),
                idempotency_key=body.idempotency_key,
                status="proposed",
                version=1,
                category=category,
                mode=body.mode,
                optimization_kind="rewrite",
                original=deepcopy(original),
                suggestion=deepcopy(original),
                diff=[],
                constraint_coverage=[],
                warnings=[],
                provenance={"optimizer_model_config_id": int(optimizer.id)},
                catalog_snapshot=deepcopy(profile),
                charged_credits=0,
                metrics={"request_hash": request_hash, "execution_status": "running"},
                expires_at=datetime.now(timezone.utc) + _PROPOSAL_TTL,
            )
            db.add(reserved_proposal)
            try:
                db.flush()
                credits.freeze(
                    db,
                    user_id,
                    charged_credits,
                    reserved_proposal.id,
                    biz_type="prompt_optimize",
                    commit=False,
                )
                consume_execution_quote(
                    quote,
                    ref_type="prompt_optimization",
                    ref_id=int(reserved_proposal.id),
                )
                db.commit()
            except credits.InsufficientCredits as exc:
                db.rollback()
                raise PromptOptimizationError(400, str(exc)) from exc
        try:
            result = _invoke_optimizer(
                source,
                optimizer=optimizer,
                body=body,
                category=category,
                context=context,
                target_model=target_model,
                capability_snapshot=profile,
            )
            candidate = _candidate_payload(original, result)
        except Exception as exc:
            usage.record_call(
                db,
                kind="prompt_optimize",
                model_id=optimizer.model_id,
                model_config_id=optimizer.id,
                user_id=user_id,
                status="failed",
                detail={"mode": body.mode, "studio": True, "error": str(exc)[:300]},
            )
            if reserved_proposal is not None:
                _refund_reserved_proposal(
                    db,
                    proposal_id=int(reserved_proposal.id),
                    user_id=user_id,
                    reserved=charged_credits,
                    error=exc,
                )
            if isinstance(exc, PromptOptimizationError):
                raise
            if isinstance(exc, gateway.GatewayError):
                raise PromptOptimizationError(502, f"提示词优化失败：{exc}") from exc
            raise
        usage.record_call(
            db,
            kind="prompt_optimize",
            model_id=optimizer.model_id,
            model_config_id=optimizer.id,
            user_id=user_id,
            status="ok",
            latency_ms=result.get("latency_ms"),
            usage=result.get("usage"),
            detail={"mode": body.mode, "studio": True, "lineage": revision is not None},
        )
    try:
        artifacts = _proposal_artifacts(
            original=original,
            candidate=candidate,
            result=result,
            constraints=constraints,
            profile=profile,
            category=category,
            source=source,
            operation=operation,
            revision=revision,
            resolved_parent=resolved_parent,
            optimizer=optimizer,
            compile_only=compile_only,
            request_hash=request_hash,
        )
    except Exception as exc:
        if reserved_proposal is not None:
            _refund_reserved_proposal(
                db,
                proposal_id=int(reserved_proposal.id),
                user_id=user_id,
                reserved=charged_credits,
                error=exc,
            )
        raise

    if reserved_proposal is None:
        proposal = PromptOptimizationProposal(
            user_id=user_id,
            source_operation_id=int(operation.id) if operation is not None else None,
            source_revision_id=int(revision.id) if revision is not None else None,
            target_model_config_id=int(target_model.id),
            capability_version_id=int(profile["capability_version_id"]),
            optimizer_model_config_id=(
                int(optimizer.id) if not compile_only and optimizer is not None else None
            ),
            idempotency_key=body.idempotency_key,
            status="proposed",
            version=1,
            category=category,
            mode=body.mode,
            optimization_kind="model_compile" if compile_only else "rewrite",
            original=original,
            suggestion=artifacts["candidate"],
            diff=artifacts["segments"],
            constraint_coverage=artifacts["coverage"],
            warnings=artifacts["warnings"],
            provenance=artifacts["provenance"],
            catalog_snapshot=profile,
            charged_credits=charged_credits,
            metrics=artifacts["metrics"],
            expires_at=datetime.now(timezone.utc) + _PROPOSAL_TTL,
        )
    else:
        proposal = db.scalar(
            select(PromptOptimizationProposal)
            .where(PromptOptimizationProposal.id == int(reserved_proposal.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if proposal is None:
            raise PromptOptimizationError(409, "提示词优化预留记录丢失，已停止结算")
        if (
            proposal.status != "proposed"
            or str(_as_dict(proposal.metrics).get("execution_status") or "") != "running"
        ):
            db.rollback()
            raise PromptOptimizationError(
                409,
                "提示词优化执行已结束或超时，本次结果未结算",
            )
        proposal.suggestion = artifacts["candidate"]
        proposal.diff = artifacts["segments"]
        proposal.constraint_coverage = artifacts["coverage"]
        proposal.warnings = artifacts["warnings"]
        proposal.provenance = artifacts["provenance"]
        proposal.catalog_snapshot = profile
        proposal.charged_credits = charged_credits
        proposal.metrics = artifacts["metrics"]
    try:
        if reserved_proposal is None:
            db.add(proposal)
        elif charged_credits:
            credits.settle(
                db,
                user_id,
                charged_credits,
                charged_credits,
                proposal.id,
                biz_type="prompt_optimize",
                commit=False,
            )
        db.commit()
        db.refresh(proposal)
    except IntegrityError as exc:
        db.rollback()
        duplicate = db.scalar(
            select(PromptOptimizationProposal).where(
                PromptOptimizationProposal.user_id == user_id,
                PromptOptimizationProposal.idempotency_key == body.idempotency_key,
            )
        )
        if duplicate is None or str((duplicate.metrics or {}).get("request_hash")) != request_hash:
            if reserved_proposal is not None:
                _refund_reserved_proposal(
                    db,
                    proposal_id=int(reserved_proposal.id),
                    user_id=user_id,
                    reserved=charged_credits,
                    error=exc,
                )
            raise PromptOptimizationError(409, "优化建议写入冲突") from exc
        return _serialize_proposal(duplicate)
    except Exception as exc:
        db.rollback()
        if reserved_proposal is not None:
            _refund_reserved_proposal(
                db,
                proposal_id=int(reserved_proposal.id),
                user_id=user_id,
                reserved=charged_credits,
                error=exc,
            )
        raise
    persisted_provenance = _as_dict(proposal.provenance)
    audit.log(
        db,
        user_id=user_id,
        action="studio.prompt_optimization.proposed",
        biz_type="prompt_optimization",
        biz_id=int(proposal.id),
        detail={
            "mode": proposal.mode,
            "source_operation_id": proposal.source_operation_id,
            "source_revision_id": proposal.source_revision_id,
            "selected_revision_source": persisted_provenance.get(
                "selected_revision_source"
            ),
            "resolved_parent_revision_id": persisted_provenance.get(
                "resolved_parent_revision_id"
            ),
            "resolved_parent_revision_source": persisted_provenance.get(
                "resolved_parent_revision_source"
            ),
            "capability_version_id": proposal.capability_version_id,
            "charged_credits": charged_credits,
        },
    )
    return _serialize_proposal(proposal)


def _load_owned_proposal(
    db: Session,
    *,
    user_id: int,
    proposal_id: int,
) -> PromptOptimizationProposal:
    proposal = db.scalar(
        select(PromptOptimizationProposal)
        .where(
            PromptOptimizationProposal.id == int(proposal_id),
            PromptOptimizationProposal.user_id == int(user_id),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if proposal is None:
        raise PromptOptimizationError(404, "优化建议不存在")
    return proposal


def decide_proposal(
    db: Session,
    *,
    user_id: int,
    proposal_id: int,
    proposal_version: int,
    idempotency_key: str,
    accepted_segment_ids: list[str],
    rejected_segment_ids: list[str],
    reject_all: bool = False,
) -> dict[str, Any]:
    proposal = _load_owned_proposal(db, user_id=user_id, proposal_id=proposal_id)
    if proposal.status != "proposed":
        if (
            proposal.decision_idempotency_key == idempotency_key
            and isinstance(proposal.decision_result, dict)
        ):
            return deepcopy(proposal.decision_result)
        raise PromptOptimizationError(409, "该优化建议已处理")
    if int(proposal.version) != int(proposal_version):
        raise PromptOptimizationError(409, "优化建议版本已更新，请刷新后重试")
    expires_at = proposal.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        proposal.status = "expired"
        proposal.version += 1
        db.commit()
        raise PromptOptimizationError(410, "优化建议已过期")
    segments = deepcopy(proposal.diff or [])
    segment_map = {str(item.get("id")): item for item in segments if isinstance(item, dict)}
    all_ids = set(segment_map)
    accepted = set(accepted_segment_ids)
    rejected = set(rejected_segment_ids)
    if not accepted.issubset(all_ids) or not rejected.issubset(all_ids):
        raise PromptOptimizationError(422, "决策中包含不属于该建议的分段")
    if reject_all:
        accepted = set()
        rejected = all_ids
    elif not accepted and not rejected:
        accepted = all_ids
    else:
        rejected |= all_ids - accepted - rejected
    original = _as_dict(proposal.original)
    result = deepcopy(original)
    for segment_id in accepted:
        segment = segment_map[segment_id]
        _set_path(result, str(segment["field_path"]), segment.get("suggestion"))
    provenance = deepcopy(proposal.provenance or {})
    revision_out = None
    if accepted and provenance.get("reverse_operation_id") and provenance.get("reverse_revision_id"):
        operation, revision, chain = _load_lineage_source(
            db,
            user_id=user_id,
            operation_id=int(provenance["reverse_operation_id"]),
            revision_id=int(provenance["reverse_revision_id"]),
        )
        resolved_parent = _resolve_user_edit_parent(revision, chain)
        if (
            revision.payload_hash != provenance.get("reverse_revision_hash")
            or int(revision.id) != int(provenance.get("selected_revision_id") or 0)
            or revision.source != provenance.get("selected_revision_source")
            or int(resolved_parent.id)
            != int(provenance.get("resolved_parent_revision_id") or 0)
            or resolved_parent.source
            != provenance.get("resolved_parent_revision_source")
        ):
            raise PromptOptimizationError(409, "反推源版本已无法验证")
        try:
            edited = reverse_operations.create_result_revision(
                db,
                operation_id=int(operation.id),
                user_id=user_id,
                source="user_edit",
                payload=result,
                parent_revision_id=int(resolved_parent.id),
                commit=False,
            )
            created = reverse_operations.create_result_revision(
                db,
                operation_id=int(operation.id),
                user_id=user_id,
                source="applied",
                payload=result,
                parent_revision_id=int(edited.id),
                commit=False,
            )
        except (
            reverse_operations.ReverseOperationInvalid,
            reverse_operations.ReverseOperationConflict,
            reverse_operations.ReverseOperationNotFound,
        ) as exc:
            db.rollback()
            raise PromptOptimizationError(409, str(exc)) from exc
        revision_out = {
            "id": int(created.id),
            "operation_id": int(created.operation_id),
            "version": int(created.version),
            "source": created.source,
            "payload": deepcopy(created.payload),
            "parent_revision_id": int(created.parent_revision_id),
            "payload_hash": created.payload_hash,
            "lineage_status": created.lineage_status,
            "user_edit_revision_id": int(edited.id),
            "selected_revision_id": int(revision.id),
            "selected_revision_source": revision.source,
            "resolved_parent_revision_id": int(resolved_parent.id),
            "resolved_parent_revision_source": resolved_parent.source,
        }
    status = (
        "rejected"
        if not accepted
        else "accepted"
        if accepted == all_ids
        else "partially_accepted"
    )
    decision_result = {
        "proposal_id": int(proposal.id),
        "proposal_version": int(proposal.version) + 1,
        "status": status,
        "accepted_segment_ids": sorted(accepted),
        "rejected_segment_ids": sorted(rejected),
        "result": result if accepted else None,
        "revision": revision_out,
    }
    metrics = {
        **deepcopy(proposal.metrics or {}),
        "adoption_rate": len(accepted) / max(1, len(all_ids)),
        "accepted_segments": len(accepted),
        "total_segments": len(all_ids),
        "converted_to_revision": revision_out is not None,
    }
    changed = db.execute(
        update(PromptOptimizationProposal)
        .where(
            PromptOptimizationProposal.id == proposal.id,
            PromptOptimizationProposal.user_id == user_id,
            PromptOptimizationProposal.status == "proposed",
            PromptOptimizationProposal.version == proposal_version,
        )
        .values(
            status=status,
            version=proposal_version + 1,
            decision_idempotency_key=idempotency_key,
            accepted_segment_ids=sorted(accepted),
            rejected_segment_ids=sorted(rejected),
            decision_result=decision_result,
            metrics=metrics,
            decided_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    if changed.rowcount != 1:
        db.rollback()
        raise PromptOptimizationError(409, "优化建议已被并发处理")
    db.add(AuditLog(
        user_id=user_id,
        action=f"studio.prompt_optimization.{status}",
        biz_type="prompt_optimization",
        biz_id=int(proposal.id),
        detail={
            "accepted_segment_ids": sorted(accepted),
            "rejected_segment_ids": sorted(rejected),
            "revision_id": revision_out["id"] if revision_out else None,
            "selected_revision_id": (
                revision_out["selected_revision_id"] if revision_out else None
            ),
            "selected_revision_source": (
                revision_out["selected_revision_source"] if revision_out else None
            ),
            "resolved_parent_revision_id": (
                revision_out["resolved_parent_revision_id"] if revision_out else None
            ),
            "adoption_rate": metrics["adoption_rate"],
        },
    ))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise PromptOptimizationError(
            409,
            "反推结果版本已被并发更新，请刷新后重试",
        ) from exc
    return decision_result
