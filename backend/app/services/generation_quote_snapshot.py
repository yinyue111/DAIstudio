"""Integrity validation for immutable quote snapshots."""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy.orm import Session

from ..models import GenerationQuote, ModelCapabilityVersion, ModelPriceVersion
from .generation_model_runtime import (
    FrozenAdapterConfigError,
    validate_frozen_adapter_snapshot,
)
from .generation_quote_contracts import _invalid_quote_snapshot


def validate_quote_snapshot_integrity(db: Session, quote: GenerationQuote) -> dict:
    """Verify that the immutable quote snapshot still matches its version rows."""
    snapshot = deepcopy(quote.model_snapshot) if isinstance(quote.model_snapshot, dict) else {}
    extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else None
    required = (
        "model_config_id",
        "model_id",
        "provider",
        "base_url",
        "gateway_format",
        "gateway_source",
        "gateway_key_fingerprint",
        "cost_credits",
        "unlock_cost",
        "capability_version_id",
        "capability_version",
        "price_version_id",
        "price_version",
    )
    if extra is None or any(field not in snapshot for field in required):
        _invalid_quote_snapshot("报价模型快照不完整,请重新报价")
    try:
        validate_frozen_adapter_snapshot(snapshot, str(quote.category or ""))
    except FrozenAdapterConfigError as exc:
        _invalid_quote_snapshot(f"报价出站适配器非法: {exc}")
    try:
        snapshot_model_id = int(snapshot["model_config_id"])
        snapshot_capability_id = int(snapshot["capability_version_id"])
        snapshot_price_id = int(snapshot["price_version_id"])
        snapshot_capability_version = int(snapshot["capability_version"])
        snapshot_price_version = int(snapshot["price_version"])
        snapshot_cost = int(snapshot["cost_credits"])
        snapshot_unlock_cost = int(snapshot["unlock_cost"])
    except (TypeError, ValueError):
        _invalid_quote_snapshot("报价模型快照版本非法,请重新报价")
    if (
        snapshot_model_id != int(quote.model_config_id)
        or snapshot_capability_id != int(quote.capability_version_id)
        or snapshot_price_id != int(quote.price_version_id)
    ):
        _invalid_quote_snapshot("报价模型快照与报价版本不一致,请重新报价")

    capability = db.get(ModelCapabilityVersion, int(quote.capability_version_id))
    price = db.get(ModelPriceVersion, int(quote.price_version_id))
    if (
        capability is None
        or price is None
        or int(capability.model_config_id) != int(quote.model_config_id)
        or int(price.model_config_id) != int(quote.model_config_id)
    ):
        _invalid_quote_snapshot("报价引用的模型版本不存在,请重新报价")
    if (
        snapshot_capability_version != int(capability.version)
        or snapshot_price_version != int(price.version)
    ):
        _invalid_quote_snapshot("报价模型快照版本号不一致,请重新报价")
    if deepcopy(extra.get("capabilities") or {}) != deepcopy(capability.capabilities or {}):
        _invalid_quote_snapshot("报价能力版本与模型快照不一致,请重新报价")
    version_pricing = deepcopy(price.pricing or {})
    if (
        deepcopy(extra.get("credit_pricing") or {}) != version_pricing
        or deepcopy(quote.pricing_snapshot or {}) != version_pricing
        or snapshot_cost != int(price.base_cost_credits or 0)
        or snapshot_unlock_cost != int(price.unlock_cost_credits or 0)
    ):
        _invalid_quote_snapshot("报价价格版本与模型快照不一致,请重新报价")
    return snapshot
