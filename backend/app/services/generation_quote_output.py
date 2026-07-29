"""Public serialization for executable quotes."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from ..models import GenerationQuote, ModelCapabilityVersion, ModelPriceVersion, User
from .generation_quote_contracts import normalized_price_breakdown
from .recipe_usage import attribution_from_snapshot


def _public_quote_subject(quote: GenerationQuote) -> dict[str, Any]:
    subject = deepcopy(quote.subject_snapshot or {})
    if quote.kind == "reverse":
        template = subject.get("template_snapshot")
        descriptor: dict[str, Any] = {}
        if isinstance(template, dict):
            raw = json.dumps(
                template,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            descriptor = {
                "version": template.get("version"),
                "fingerprint": hashlib.sha256(raw).hexdigest(),
            }
        return {
            "retry_of_operation_id": subject.get("retry_of_operation_id"),
            "template": descriptor,
        }
    if quote.kind == "reverse_batch":
        allocations = subject.get("allocations")
        return {
            "item_count": len(allocations) if isinstance(allocations, list) else 0,
        }
    return subject


def generation_quote_out(db: Session, quote: GenerationQuote) -> dict:
    capability = (
        db.get(ModelCapabilityVersion, quote.capability_version_id)
        if quote.capability_version_id is not None
        else None
    )
    price = (
        db.get(ModelPriceVersion, quote.price_version_id)
        if quote.price_version_id is not None
        else None
    )
    snapshot = quote.model_snapshot if isinstance(quote.model_snapshot, dict) else {}
    route_snapshot = (
        snapshot.get("route_snapshot")
        if isinstance(snapshot.get("route_snapshot"), dict)
        else {}
    )
    request_snapshot = quote.request_snapshot if isinstance(quote.request_snapshot, dict) else {}
    attribution = attribution_from_snapshot(request_snapshot.get("creation_recipe"))
    user = db.get(User, int(quote.user_id))
    total = max(0, int(quote.estimated_credits or 0))
    return {
        "id": quote.id,
        "quote_id": quote.id,
        "kind": quote.kind or "generation",
        "status": quote.status,
        "category": quote.category,
        "stage": quote.stage,
        "model_config_id": quote.model_config_id,
        "model_name": snapshot.get("model_name"),
        "model_id": snapshot.get("model_id"),
        "model_provider": snapshot.get("provider"),
        "route_id": route_snapshot.get("route_id"),
        "route_key": route_snapshot.get("route_key"),
        "route_name": route_snapshot.get("route_name"),
        "route_selection_state": route_snapshot.get("selection_state"),
        "route_health_at_quote": route_snapshot.get("health") or None,
        "creation_recipe_id": attribution.recipe_id if attribution else None,
        "creation_recipe_version": attribution.recipe_version if attribution else None,
        "creation_recipe_source": attribution.source if attribution else None,
        "creation_recipe_share_id": attribution.share_id if attribution else None,
        "capability_version_id": quote.capability_version_id,
        "capability_version": capability.version if capability is not None else None,
        "price_version_id": quote.price_version_id,
        "price_version": price.version if price is not None else None,
        "estimated_credits": total,
        "total_credits": total,
        "price_breakdown": normalized_price_breakdown(
            quote.price_breakdown,
            total_credits=total,
        ),
        "balance_after_estimate": (
            int(user.balance_credits or 0) - total if user is not None else None
        ),
        "warnings": deepcopy(quote.warnings or []),
        "subject_snapshot": _public_quote_subject(quote),
        "client_request_id": quote.client_request_id,
        "tool_version_id": quote.tool_version_id,
        "request_fingerprint": quote.request_fingerprint,
        "expires_at": quote.expires_at,
        "consumed_at": quote.consumed_at,
        "task_id": quote.task_id,
        "consumed_ref_type": quote.consumed_ref_type,
        "consumed_ref_id": quote.consumed_ref_id,
        "created_at": quote.created_at,
    }
