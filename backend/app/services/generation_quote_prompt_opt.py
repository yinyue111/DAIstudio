"""Prompt-optimization quote creation and validation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import (
    GenerationQuote,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
)
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from .generation_model_runtime import model_snapshot as runtime_model_snapshot
from .generation_quote_core import (
    _balance_warning,
    _expected_action_breakdown,
    _expired_quote_needs_reissue,
    _invalid_quote_snapshot,
    _json_fingerprint,
    _normalized_client_request_id,
    _quote_mismatch,
    _runtime_identity,
    _utc,
    _versioned_runtime_model_snapshot,
    _without_action_fingerprints,
    create_execution_quote,
    find_idempotent_quote,
    normalized_price_breakdown,
)
from .model_versions import sync_model_versions


def _prepare_prompt_optimization_quote(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
) -> dict[str, Any]:
    from . import prompt_optimization

    try:
        prepared = prompt_optimization.prepare_proposal_request(
            db,
            user_id=user_id,
            body=body,
        )
    except prompt_optimization.PromptOptimizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    target_model = prepared["target_model"]
    target_capability, _ = sync_model_versions(db, target_model)
    if int(prepared["capability"].id) != int(target_capability.id):
        try:
            prepared = prompt_optimization.prepare_proposal_request(
                db,
                user_id=user_id,
                body=body,
            )
        except prompt_optimization.PromptOptimizationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
    return prepared


def _prompt_context_fingerprint(prepared: dict[str, Any]) -> str:
    operation = prepared.get("operation")
    revision = prepared.get("revision")
    resolved_parent = prepared.get("resolved_parent")
    return _json_fingerprint(
        {
            "source": prepared.get("source"),
            "original": prepared.get("original"),
            "context": prepared.get("context"),
            "constraints": prepared.get("constraints"),
            "operation_id": int(operation.id) if operation is not None else None,
            "revision_id": int(revision.id) if revision is not None else None,
            "revision_hash": getattr(revision, "payload_hash", None),
            "resolved_parent_id": (
                int(resolved_parent.id) if resolved_parent is not None else None
            ),
            "resolved_parent_hash": getattr(resolved_parent, "payload_hash", None),
        }
    )


def _prompt_pricing_snapshot(
    price: ModelPriceVersion,
    *,
    quoted_credits: int,
) -> dict[str, Any]:
    return {
        "model_config_id": int(price.model_config_id),
        "price_version_id": int(price.id),
        "price_version": int(price.version),
        "base_cost_credits": int(price.base_cost_credits or 0),
        "unlock_cost_credits": int(price.unlock_cost_credits or 0),
        "catalog": deepcopy(price.pricing or {}),
        "quoted_credits": max(0, int(quoted_credits)),
    }


def _prompt_subject_snapshot(
    prepared: dict[str, Any],
    *,
    billing_model: ModelConfig,
    billing_capability: ModelCapabilityVersion,
    billing_price: ModelPriceVersion,
) -> dict[str, Any]:
    target_model = prepared["target_model"]
    optimizer = prepared.get("optimizer")
    return {
        "schema_version": "prompt-optimization-quote.v1",
        "mode": prepared.get("compile_only") and "model_compile" or "rewrite",
        "compile_only": bool(prepared.get("compile_only")),
        "target_profile": deepcopy(prepared["profile"]),
        "target_runtime_identity": _runtime_identity(runtime_model_snapshot(target_model)),
        "derived_context_fingerprint": _prompt_context_fingerprint(prepared),
        "optimizer_model_config_id": int(optimizer.id) if optimizer is not None else None,
        "billing_model_config_id": int(billing_model.id),
        "billing_capability_version_id": int(billing_capability.id),
        "billing_capability_version": int(billing_capability.version),
        "billing_price_version_id": int(billing_price.id),
        "billing_price_version": int(billing_price.version),
    }


def create_prompt_optimization_quote(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
) -> GenerationQuote:
    from . import prompt_optimization

    client_request_id = _normalized_client_request_id(body.idempotency_key)
    if client_request_id != body.idempotency_key:
        raise HTTPException(
            422,
            detail={
                "code": "QUOTE_REQUEST_INVALID",
                "message": "idempotency_key 不得包含首尾空白",
            },
        )
    request_snapshot = body.model_dump(mode="json", exclude={"quote_id"})
    fingerprint = prompt_optimization.request_fingerprint(body)
    replay = find_idempotent_quote(
        db,
        user_id=user_id,
        kind="prompt_optimization",
        client_request_id=client_request_id,
        request_fingerprint=fingerprint,
    )
    if replay is not None and not _expired_quote_needs_reissue(replay):
        return replay

    prepared = _prepare_prompt_optimization_quote(
        db,
        user_id=user_id,
        body=body,
    )
    target_model = prepared["target_model"]
    optimizer = prepared.get("optimizer")
    billing_model = optimizer or target_model
    billing_capability, billing_price = sync_model_versions(db, billing_model)
    total = 0 if prepared["compile_only"] else int(prepared["estimated_credits"])
    if not prepared["compile_only"] and total != int(billing_price.base_cost_credits or 0):
        raise HTTPException(
            409,
            detail={
                "code": "PROMPT_OPTIMIZATION_PRICING_INVALID",
                "message": "提示词优化模型价格版本与执行价格不一致",
            },
        )
    pricing_snapshot = _prompt_pricing_snapshot(
        billing_price,
        quoted_credits=total,
    )
    subject_snapshot = _prompt_subject_snapshot(
        prepared,
        billing_model=billing_model,
        billing_capability=billing_capability,
        billing_price=billing_price,
    )
    model_snapshot = _versioned_runtime_model_snapshot(
        billing_model,
        billing_capability,
        billing_price,
    )
    model_snapshot["_quote_subject_fingerprint"] = _json_fingerprint(subject_snapshot)
    model_snapshot["_quote_pricing_fingerprint"] = _json_fingerprint(pricing_snapshot)
    return create_execution_quote(
        db,
        user_id=user_id,
        kind="prompt_optimization",
        client_request_id=client_request_id,
        request_fingerprint=fingerprint,
        category=prepared["category"],
        stage="preview",
        request_snapshot=request_snapshot,
        model_snapshot=model_snapshot,
        pricing_snapshot=pricing_snapshot,
        price_breakdown=_expected_action_breakdown(
            code=("prompt_model_compile" if prepared["compile_only"] else "prompt_optimization"),
            label=("目标模型编译" if prepared["compile_only"] else "提示词优化"),
            credits=total,
        ),
        estimated_credits=total,
        model_config_id=int(billing_model.id),
        capability_version_id=int(billing_capability.id),
        price_version_id=int(billing_price.id),
        subject_snapshot=subject_snapshot,
        warnings=_balance_warning(db, user_id=user_id, total=total),
    )


def _validate_prompt_optimization_quote_state(quote: GenerationQuote) -> None:
    if quote.status == "active":
        if (
            quote.task_id is not None
            or quote.consumed_ref_type is not None
            or quote.consumed_ref_id is not None
            or quote.consumed_at is not None
        ):
            _invalid_quote_snapshot("提示词优化报价的未消费状态不一致，请重新报价")
        if _utc(quote.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(
                409,
                detail={"code": "QUOTE_EXPIRED", "message": "报价已过期，请重新报价"},
            )
        return
    if quote.status == "consumed":
        if (
            quote.task_id is not None
            or quote.consumed_ref_type != "prompt_optimization"
            or quote.consumed_ref_id is None
            or quote.consumed_at is None
        ):
            _invalid_quote_snapshot("提示词优化报价的消费引用不完整，请重新报价")
        return
    _invalid_quote_snapshot("提示词优化报价状态不可执行，请重新报价")


def validate_prompt_optimization_quote(
    db: Session,
    quote: GenerationQuote,
    *,
    body: StudioPromptOptimizationIn,
    prepared: dict[str, Any],
) -> int:
    """Validate a prompt quote without mutating catalog-version history."""
    from . import prompt_optimization

    if quote.kind != "prompt_optimization":
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价不是提示词优化类型"},
        )
    _validate_prompt_optimization_quote_state(quote)
    if body.quote_id is not None and int(body.quote_id) != int(quote.id):
        _quote_mismatch("提示词优化报价与执行请求不一致")

    request_snapshot = body.model_dump(mode="json", exclude={"quote_id"})
    if quote.request_fingerprint != prompt_optimization.request_fingerprint(body):
        _quote_mismatch("提示词优化参数已变化，请重新报价")
    if quote.request_snapshot != request_snapshot:
        _invalid_quote_snapshot("提示词优化请求快照不一致，请重新报价")
    if quote.client_request_id != body.idempotency_key:
        _invalid_quote_snapshot("提示词优化幂等键与报价不一致，请重新报价")

    target_model = prepared.get("target_model")
    optimizer = prepared.get("optimizer")
    billing_model = optimizer or target_model
    if not isinstance(target_model, ModelConfig) or not isinstance(billing_model, ModelConfig):
        _invalid_quote_snapshot("提示词优化报价缺少可验证的模型上下文")
    if (
        quote.model_config_id is None
        or quote.capability_version_id is None
        or quote.price_version_id is None
        or int(quote.model_config_id) != int(billing_model.id)
        or quote.tool_version_id is not None
    ):
        _invalid_quote_snapshot("提示词优化报价的模型版本引用不完整")

    billing_capability = db.get(
        ModelCapabilityVersion,
        int(quote.capability_version_id),
    )
    billing_price = db.get(ModelPriceVersion, int(quote.price_version_id))
    if (
        billing_capability is None
        or billing_price is None
        or int(billing_capability.model_config_id) != int(billing_model.id)
        or int(billing_price.model_config_id) != int(billing_model.id)
    ):
        _invalid_quote_snapshot("提示词优化报价引用的能力或价格版本不存在")

    subject = quote.subject_snapshot if isinstance(quote.subject_snapshot, dict) else {}
    pricing = quote.pricing_snapshot if isinstance(quote.pricing_snapshot, dict) else {}
    snapshot = quote.model_snapshot if isinstance(quote.model_snapshot, dict) else {}
    if snapshot.get("_quote_subject_fingerprint") != _json_fingerprint(subject):
        _invalid_quote_snapshot("提示词优化报价主体快照已被篡改，请重新报价")
    if snapshot.get("_quote_pricing_fingerprint") != _json_fingerprint(pricing):
        _invalid_quote_snapshot("提示词优化报价价格快照已被篡改，请重新报价")

    compile_only = bool(prepared.get("compile_only"))
    amount = 0 if compile_only else max(0, int(billing_price.base_cost_credits or 0))
    expected_subject = _prompt_subject_snapshot(
        prepared,
        billing_model=billing_model,
        billing_capability=billing_capability,
        billing_price=billing_price,
    )
    expected_pricing = _prompt_pricing_snapshot(
        billing_price,
        quoted_credits=amount,
    )
    if subject != expected_subject:
        _quote_mismatch("提示词优化来源、目标模型或上下文已变化，请重新报价")
    if pricing != expected_pricing:
        _invalid_quote_snapshot("提示词优化报价价格版本快照不一致")

    expected_model_snapshot = _versioned_runtime_model_snapshot(
        billing_model,
        billing_capability,
        billing_price,
    )
    if _without_action_fingerprints(snapshot) != expected_model_snapshot:
        _invalid_quote_snapshot("提示词优化报价的模型、能力或价格快照不一致")

    expected_breakdown = _expected_action_breakdown(
        code="prompt_model_compile" if compile_only else "prompt_optimization",
        label="目标模型编译" if compile_only else "提示词优化",
        credits=amount,
    )
    if (
        quote.category != prepared.get("category")
        or quote.stage != "preview"
        or int(quote.estimated_credits or 0) != amount
        or normalized_price_breakdown(quote.price_breakdown, total_credits=amount)
        != expected_breakdown
    ):
        _invalid_quote_snapshot("提示词优化报价金额或用途不一致，请重新报价")
    return amount
