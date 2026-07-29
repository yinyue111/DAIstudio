"""Request preparation for Studio prompt optimization."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from ..models import ModelConfig, ReverseResultRevision
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from . import gateway
from .config_store import ModelConfigResolutionError, resolve_model_config
from .model_gateway_config import runtime_config_for_model
from .prompt_optimization_compiler import (
    _lineage_constraints,
    _request_constraints,
    _stable_id,
)
from .prompt_optimization_read import (
    PromptOptimizationError,
    _active_compiler_profile,
    _as_dict,
    _load_lineage_source,
    _prompt_from_payload,
    _resolve_user_edit_parent,
    _server_context,
)


def request_fingerprint(body: StudioPromptOptimizationIn) -> str:
    raw = json.dumps(
        body.model_dump(mode="json", exclude={"quote_id"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


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
        context.update(
            {
                "duration": parameters.get("duration") or parameters.get("vDuration"),
                "aspect_ratio": parameters.get("aspect_ratio") or parameters.get("ratio"),
                "resolution": parameters.get("resolution") or parameters.get("vResolution"),
                "product_mode": operation.target == "product_profile",
            }
        )
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
