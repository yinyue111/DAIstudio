"""Shared constants and pure helpers for executable quotes."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import ModelCapabilityVersion, ModelConfig, ModelPriceVersion, User

QUOTE_KINDS = {
    "generation",
    "reverse",
    "reverse_batch",
    "workflow",
    "prompt_optimization",
    "asset_unlock",
}


def normalized_price_breakdown(
    value: dict[str, Any] | None,
    *,
    total_credits: int,
) -> dict[str, Any]:
    """Return one stable quote breakdown shape for every executable kind."""
    raw = deepcopy(value) if isinstance(value, dict) else {}
    if isinstance(raw.get("items"), list):
        items = [dict(item) for item in raw["items"] if isinstance(item, dict)]
    elif raw:
        legacy = dict(raw)
        legacy.pop("items", None)
        legacy.pop("total_credits", None)
        items = [legacy]
    else:
        items = []
    return {
        "items": items,
        "total_credits": max(0, int(total_credits)),
    }


def quote_item(
    *,
    code: str,
    label: str,
    credits: int,
    quantity: int = 1,
    unit_credits: int | None = None,
    **detail: Any,
) -> dict[str, Any]:
    amount = max(0, int(credits))
    item = {
        "code": str(code),
        "label": str(label),
        "credits": amount,
        "quantity": max(1, int(quantity)),
        **detail,
    }
    if unit_credits is not None:
        item["unit_credits"] = max(0, int(unit_credits))
    return item


def _json_fingerprint(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_client_request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not 8 <= len(normalized) <= 128:
        raise HTTPException(
            422,
            detail={
                "code": "QUOTE_REQUEST_INVALID",
                "message": "client_request_id 长度必须为 8-128",
            },
        )
    return normalized


def _expected_action_breakdown(
    *,
    code: str,
    label: str,
    credits: int,
) -> dict[str, Any]:
    return normalized_price_breakdown(
        {"items": [quote_item(code=code, label=label, credits=credits)]},
        total_credits=credits,
    )


def _versioned_runtime_model_snapshot(
    model: ModelConfig,
    capability: ModelCapabilityVersion,
    price: ModelPriceVersion,
) -> dict[str, Any]:
    from .generation_model_runtime import model_snapshot as runtime_model_snapshot

    snapshot = runtime_model_snapshot(model)
    extra = deepcopy(snapshot.get("extra") or {})
    extra["capabilities"] = deepcopy(capability.capabilities or {})
    extra["credit_pricing"] = deepcopy(price.pricing or {})
    snapshot.update(
        {
            "extra": extra,
            "model_config_id": int(model.id),
            "cost_credits": int(price.base_cost_credits or 0),
            "unlock_cost": int(price.unlock_cost_credits or 0),
            "capability_version_id": int(capability.id),
            "capability_version": int(capability.version),
            "price_version_id": int(price.id),
            "price_version": int(price.version),
        }
    )
    return snapshot


def _runtime_identity(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    values = snapshot if isinstance(snapshot, dict) else {}
    extra = deepcopy(values.get("extra") or {}) if isinstance(values.get("extra"), dict) else {}
    for key in ("capabilities", "credit_pricing", "reverse_pricing", "official_pricing"):
        extra.pop(key, None)
    return {
        "outbound_adapter_schema_version": values.get("outbound_adapter_schema_version"),
        "model_config_id": values.get("model_config_id"),
        "model_name": values.get("model_name"),
        "model_id": values.get("model_id"),
        "provider": values.get("provider"),
        "base_url": values.get("base_url"),
        "gateway_format": values.get("gateway_format"),
        "gateway_source": values.get("gateway_source"),
        "gateway_key_fingerprint": values.get("gateway_key_fingerprint"),
        "route_snapshot": deepcopy(values.get("route_snapshot")),
        "extra": extra,
    }


def _without_action_fingerprints(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    value = deepcopy(snapshot) if isinstance(snapshot, dict) else {}
    value.pop("_quote_subject_fingerprint", None)
    value.pop("_quote_pricing_fingerprint", None)
    return value


def _expired_quote_needs_reissue(quote) -> bool:
    return bool(
        quote is not None
        and quote.status == "expired"
        and quote.task_id is None
        and quote.consumed_ref_id is None
    )


def _quote_mismatch(message: str) -> None:
    raise HTTPException(409, detail={"code": "QUOTE_MISMATCH", "message": message})


def _invalid_quote_snapshot(message: str) -> None:
    raise HTTPException(
        409,
        detail={"code": "QUOTE_SNAPSHOT_INVALID", "message": message},
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _balance_warning(db: Session, *, user_id: int, total: int) -> list[dict[str, Any]]:
    user = db.get(User, int(user_id))
    if user is None or int(user.balance_credits or 0) >= int(total):
        return []
    return [
        {
            "code": "INSUFFICIENT_BALANCE",
            "message": "当前可用积分不足，确认执行前请先充值",
        }
    ]
