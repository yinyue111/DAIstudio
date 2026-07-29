"""Image/video generation quote creation and validation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import GenerationQuote, ModelConfig
from .gateway_config_errors import raise_gateway_config_http
from .generation_model_runtime import (
    FrozenAdapterConfigError,
    ModelSnapshotMismatchError,
    model_from_persisted_snapshot,
    validate_frozen_adapter_snapshot,
)
from .generation_pricing import image_quality_tier, is_edit_or_subject_task
from .generation_quote_core import (
    _invalid_quote_snapshot,
    create_execution_quote,
)
from .model_versions import sync_model_versions


def quote_breakdown(
    *,
    category: str,
    stage: str,
    params: dict,
    source_type: str | None,
    pricing: dict,
    estimated_credits: int,
) -> dict[str, Any]:
    if category == "image":
        count = max(1, int(params.get("n") or 1))
        tier = image_quality_tier(params.get("size"))
        dimension = "image_edit" if is_edit_or_subject_task(params, source_type) else "image"
        table = pricing.get(dimension) if isinstance(pricing.get(dimension), dict) else {}
        unit = max(0, int(table.get(tier) or 0))
        return {
            "dimension": dimension,
            "quality_tier": tier,
            "unit_credits": unit,
            "quantity": count,
            "estimated_credits": int(estimated_credits),
        }
    if stage == "preview":
        return {
            "dimension": "video_preview",
            "unit_credits": max(0, int(pricing.get("video_preview_cost") or 0)),
            "quantity": 1,
            "estimated_credits": int(estimated_credits),
        }
    resolution = str(params.get("resolution") or params.get("target_resolution") or "720p")
    duration = max(1, int(params.get("duration") or params.get("target_duration") or 5))
    rates = pricing.get("video_per_second") if isinstance(pricing.get("video_per_second"), dict) else {}
    return {
        "dimension": "video_seconds",
        "resolution": resolution,
        "unit_credits": max(0, int(rates.get(resolution) or 0)),
        "quantity": duration,
        "estimated_credits": int(estimated_credits),
    }


def create_generation_quote(
    db: Session,
    *,
    user_id: int,
    model: ModelConfig,
    request_fingerprint: str,
    category: str,
    stage: str,
    request_snapshot: dict,
    model_snapshot: dict,
    params: dict,
    source_type: str | None,
    estimated_credits: int,
) -> GenerationQuote:
    frozen_snapshot = deepcopy(model_snapshot)
    try:
        validate_frozen_adapter_snapshot(frozen_snapshot, category)
    except FrozenAdapterConfigError as exc:
        _invalid_quote_snapshot(f"报价出站适配器非法: {exc}")
    capability, price = sync_model_versions(db, model)
    snapshot_extra = deepcopy(frozen_snapshot.get("extra") or {})
    snapshot_extra["capabilities"] = deepcopy(capability.capabilities or {})
    snapshot_extra["credit_pricing"] = deepcopy(price.pricing or {})
    frozen_snapshot.update(
        {
            "model_config_id": int(model.id),
            "cost_credits": int(price.base_cost_credits or 0),
            "unlock_cost": int(price.unlock_cost_credits or 0),
            "extra": snapshot_extra,
            "capability_version_id": int(capability.id),
            "capability_version": int(capability.version),
            "price_version_id": int(price.id),
            "price_version": int(price.version),
        }
    )
    pricing = deepcopy(price.pricing or {})
    return create_execution_quote(
        db,
        user_id=user_id,
        kind="generation",
        client_request_id=(request_snapshot or {}).get("client_request_id"),
        model_config_id=int(model.id),
        capability_version_id=int(capability.id),
        price_version_id=int(price.id),
        request_fingerprint=request_fingerprint,
        category=category,
        stage=stage,
        request_snapshot=request_snapshot,
        model_snapshot=frozen_snapshot,
        pricing_snapshot=pricing,
        price_breakdown=quote_breakdown(
            category=category,
            stage=stage,
            params=params,
            source_type=source_type,
            pricing=pricing,
            estimated_credits=estimated_credits,
        ),
        estimated_credits=estimated_credits,
    )


def lock_generation_quote(db: Session, *, quote_id: int, user_id: int) -> GenerationQuote:
    from .generation_quote_core import lock_execution_quote

    return lock_execution_quote(
        db,
        quote_id=quote_id,
        user_id=user_id,
        kind="generation",
    )


def validate_generation_quote(
    db: Session,
    quote: GenerationQuote,
    *,
    request_fingerprint: str,
    model: ModelConfig,
) -> dict:
    if quote.kind != "generation":
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价不是生成类型"},
        )
    if quote.request_fingerprint != request_fingerprint:
        raise HTTPException(409, detail={"code": "QUOTE_MISMATCH", "message": "生成参数已变化，请重新报价"})
    if int(quote.model_config_id) != int(model.id):
        raise HTTPException(409, detail={"code": "QUOTE_MISMATCH", "message": "生成模型已变化，请重新报价"})
    from .generation_quote_core import validate_quote_snapshot_integrity

    snapshot = validate_quote_snapshot_integrity(db, quote)
    try:
        model_from_persisted_snapshot(db, snapshot, model, str(quote.category or model.use))
    except ModelSnapshotMismatchError as exc:
        raise_gateway_config_http(exc)
    return snapshot


def validate_quote_snapshot_integrity(db: Session, quote: GenerationQuote) -> dict:
    from .generation_quote_core import validate_quote_snapshot_integrity as _impl

    return _impl(db, quote)


def quoted_model_snapshot(
    quote: GenerationQuote,
    _current_snapshot: dict | None = None,
) -> dict:
    """Return the quote snapshot verbatim; current catalog fields are irrelevant."""
    return deepcopy(quote.model_snapshot or {})


def consume_generation_quote(quote: GenerationQuote, *, task_id: int) -> None:
    from .generation_quote_core import consume_execution_quote

    consume_execution_quote(quote, ref_type="gen_task", ref_id=task_id)
