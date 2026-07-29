"""Reverse and reverse-batch quote creation and validation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import (
    GenerationQuote,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
)
from ..schemas import ReverseBatchCreate, ReverseOperationCreate
from .generation_quote_core import (
    _balance_warning,
    _invalid_quote_snapshot,
    create_execution_quote,
    find_idempotent_quote,
    normalized_price_breakdown,
    quote_item,
)
from .model_versions import sync_model_versions


def _versioned_reverse_model_snapshot(
    prepared: dict[str, Any],
    capability: ModelCapabilityVersion,
    price: ModelPriceVersion,
) -> dict[str, Any]:
    snapshot = deepcopy(prepared["model_snapshot"])
    snapshot.update(
        {
            "capability_version_id": int(capability.id),
            "capability_version": int(capability.version),
            "price_version_id": int(price.id),
            "price_version": int(price.version),
            "capabilities": deepcopy(capability.capabilities or {}),
            "catalog_metadata": deepcopy(capability.metadata_snapshot or {}),
            "catalog_pricing": deepcopy(price.pricing or {}),
        }
    )
    return snapshot


def _reverse_price_table(price: ModelPriceVersion) -> dict[str, Any]:
    pricing = price.pricing if isinstance(price.pricing, dict) else {}
    reverse = pricing.get("reverse")
    return deepcopy(reverse) if isinstance(reverse, dict) else {}


def _reverse_single_breakdown(
    body: ReverseOperationCreate,
    allocation: dict[str, Any],
) -> dict[str, Any]:
    items = [
        quote_item(
            code="reverse_visual",
            label="视觉反推",
            credits=int(allocation["visual_cost"]),
            preset=str(allocation["preset"]),
            target=body.target,
        )
    ]
    if int(allocation["audio_surcharge"]):
        items.append(
            quote_item(
                code="reverse_audio",
                label="音频分析",
                credits=int(allocation["audio_surcharge"]),
            )
        )
    return normalized_price_breakdown(
        {"items": items},
        total_credits=int(allocation["frozen"]),
    )


def _reverse_batch_breakdown(
    operation_bodies: list[ReverseOperationCreate],
    allocations: list[dict[str, Any]],
) -> dict[str, Any]:
    items = [
        quote_item(
            code="reverse_batch_item",
            label=f"批量反推第 {index + 1} 项",
            credits=int(allocation["estimated_credits"]),
            item_index=index,
            target=operation_body.target,
            preset=str(allocation["pricing_snapshot"]["preset"]),
        )
        for index, (operation_body, allocation) in enumerate(
            zip(operation_bodies, allocations, strict=True)
        )
    ]
    return normalized_price_breakdown(
        {"items": items},
        total_credits=sum(int(item["estimated_credits"]) for item in allocations),
    )


def create_reverse_quote(
    db: Session,
    *,
    user_id: int,
    body: ReverseOperationCreate,
    retry_of_operation_id: int | None = None,
) -> GenerationQuote:
    from . import reverse_operations

    prepared = reverse_operations.prepare_reverse_quote(db, user_id=user_id, body=body)
    fingerprint = reverse_operations.quote_request_fingerprint(
        body,
        retry_of_operation_id=retry_of_operation_id,
    )
    replay = find_idempotent_quote(
        db,
        user_id=user_id,
        kind="reverse",
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
    )
    if replay is not None:
        return replay
    model = prepared["model"]
    capability, price = sync_model_versions(db, model)
    snapshot = _versioned_reverse_model_snapshot(prepared, capability, price)
    total = int(prepared["estimated_credits"])
    breakdown = _reverse_single_breakdown(body, prepared["pricing_snapshot"])
    return create_execution_quote(
        db,
        user_id=user_id,
        kind="reverse",
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
        category="video" if body.target == "video" else "image",
        stage="final",
        request_snapshot=prepared["request_snapshot"],
        model_snapshot=snapshot,
        pricing_snapshot={
            "catalog": deepcopy(price.pricing or {}),
            "allocation": deepcopy(prepared["pricing_snapshot"]),
        },
        price_breakdown=breakdown,
        estimated_credits=total,
        model_config_id=int(model.id),
        capability_version_id=int(capability.id),
        price_version_id=int(price.id),
        subject_snapshot={
            "template_snapshot": prepared["template_snapshot"],
            "retry_of_operation_id": (
                int(retry_of_operation_id) if retry_of_operation_id is not None else None
            ),
        },
        warnings=_balance_warning(db, user_id=user_id, total=total),
    )


def create_reverse_batch_quote(
    db: Session,
    *,
    user_id: int,
    body: ReverseBatchCreate,
) -> GenerationQuote:
    from . import reverse_operations

    operation_bodies = reverse_operations.batch_operation_bodies(body)
    fingerprint = reverse_operations.batch_request_fingerprint(body)
    replay = find_idempotent_quote(
        db,
        user_id=user_id,
        kind="reverse_batch",
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
    )
    if replay is not None:
        return replay
    allocations: list[dict[str, Any]] = []
    common_model = None
    common_capability = None
    common_price = None
    any_video = False
    for index, operation_body in enumerate(operation_bodies):
        prepared = reverse_operations.prepare_reverse_quote(
            db,
            user_id=user_id,
            body=operation_body,
        )
        model = prepared["model"]
        capability, price = sync_model_versions(db, model)
        if common_model is None:
            common_model, common_capability, common_price = model, capability, price
        elif int(model.id) != int(common_model.id):
            raise HTTPException(
                409,
                detail={
                    "code": "QUOTE_MODEL_MISMATCH",
                    "message": "同一批次的反推项必须使用同一视觉模型",
                },
            )
        item_total = int(prepared["estimated_credits"])
        item_fingerprint = reverse_operations.request_fingerprint(operation_body)
        any_video = any_video or operation_body.target == "video"
        allocations.append(
            {
                "index": index,
                "request_fingerprint": item_fingerprint,
                "request_snapshot": prepared["request_snapshot"],
                "model_snapshot": _versioned_reverse_model_snapshot(
                    prepared, capability, price
                ),
                "template_snapshot": prepared["template_snapshot"],
                "pricing_snapshot": prepared["pricing_snapshot"],
                "estimated_credits": item_total,
            }
        )
    assert common_model is not None and common_capability is not None and common_price is not None
    total = sum(int(item["estimated_credits"]) for item in allocations)
    request_snapshot = reverse_operations.batch_request_snapshot(body)
    return create_execution_quote(
        db,
        user_id=user_id,
        kind="reverse_batch",
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
        category="video" if any_video else "image",
        stage="final",
        request_snapshot=request_snapshot,
        model_snapshot=_versioned_reverse_model_snapshot(
            {"model_snapshot": allocations[0]["model_snapshot"]},
            common_capability,
            common_price,
        ),
        pricing_snapshot={
            "catalog": deepcopy(common_price.pricing or {}),
            "allocations": [
                deepcopy(item["pricing_snapshot"]) for item in allocations
            ],
        },
        price_breakdown=_reverse_batch_breakdown(operation_bodies, allocations),
        estimated_credits=total,
        model_config_id=int(common_model.id),
        capability_version_id=int(common_capability.id),
        price_version_id=int(common_price.id),
        subject_snapshot={"allocations": allocations},
        warnings=_balance_warning(db, user_id=user_id, total=total),
    )


def _validate_reverse_version_snapshot(
    db: Session,
    quote: GenerationQuote,
) -> tuple[dict[str, Any], ModelConfig, ModelCapabilityVersion, ModelPriceVersion]:
    from . import reverse_operations
    from .model_gateway_config import ModelGatewayConfigError

    snapshot = deepcopy(quote.model_snapshot or {})
    try:
        model_id = int(snapshot["model_config_id"])
        capability_id = int(snapshot["capability_version_id"])
        capability_version = int(snapshot["capability_version"])
        price_id = int(snapshot["price_version_id"])
        price_version = int(snapshot["price_version"])
    except (KeyError, TypeError, ValueError):
        _invalid_quote_snapshot("反推报价模型版本快照不完整，请重新报价")
    if (
        quote.model_config_id is None
        or quote.capability_version_id is None
        or quote.price_version_id is None
        or model_id != int(quote.model_config_id)
        or capability_id != int(quote.capability_version_id)
        or price_id != int(quote.price_version_id)
    ):
        _invalid_quote_snapshot("反推报价模型版本引用不一致，请重新报价")
    model = db.get(ModelConfig, model_id)
    capability = db.get(ModelCapabilityVersion, capability_id)
    price = db.get(ModelPriceVersion, price_id)
    if (
        model is None
        or capability is None
        or price is None
        or int(capability.model_config_id) != model_id
        or int(price.model_config_id) != model_id
        or int(capability.version) != capability_version
        or int(price.version) != price_version
        or deepcopy(capability.capabilities or {}) != deepcopy(snapshot.get("capabilities") or {})
        or deepcopy(capability.metadata_snapshot or {})
        != deepcopy(snapshot.get("catalog_metadata") or {})
        or deepcopy(price.pricing or {}) != deepcopy(snapshot.get("catalog_pricing") or {})
        or deepcopy(price.pricing or {})
        != deepcopy((quote.pricing_snapshot or {}).get("catalog") or {})
    ):
        _invalid_quote_snapshot("反推报价的能力或价格版本已被篡改，请重新报价")
    try:
        runtime = reverse_operations.runtime_config_for_model(model, "vision")
        reverse_operations._assert_supported_vision_runtime(runtime)
        expected_snapshot = _versioned_reverse_model_snapshot(
            {"model_snapshot": reverse_operations._model_snapshot(model, runtime)},
            capability,
            price,
        )
    except (ModelGatewayConfigError, reverse_operations.ReverseOperationInvalid) as exc:
        _invalid_quote_snapshot(f"反推报价绑定的模型运行配置不可用: {exc}")
    if snapshot != expected_snapshot:
        _invalid_quote_snapshot("反推报价模型运行快照已变化，请重新报价")
    return snapshot, model, capability, price


def validate_reverse_quote(
    db: Session,
    quote: GenerationQuote,
    *,
    body: ReverseOperationCreate,
    retry_of_operation_id: int | None = None,
) -> dict[str, Any]:
    from . import reverse_operations

    if quote.kind != "reverse":
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价不是单项反推类型"},
        )
    expected_fingerprint = reverse_operations.quote_request_fingerprint(
        body,
        retry_of_operation_id=retry_of_operation_id,
    )
    if quote.request_fingerprint != expected_fingerprint:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_MISMATCH", "message": "反推参数已变化，请重新报价"},
        )
    snapshot, model, _capability, price = _validate_reverse_version_snapshot(db, quote)
    expected_request = reverse_operations.reverse_request_snapshot(body)
    expected_request["model_config_id"] = int(model.id)
    expected_template = reverse_operations.reverse_template_snapshot(body)
    expected_allocation = reverse_operations.reverse_pricing_snapshot(
        body,
        _reverse_price_table(price),
    )
    expected_subject = {
        "template_snapshot": expected_template,
        "retry_of_operation_id": (
            int(retry_of_operation_id) if retry_of_operation_id is not None else None
        ),
    }
    expected_pricing = {
        "catalog": deepcopy(price.pricing or {}),
        "allocation": expected_allocation,
    }
    expected_total = int(expected_allocation["frozen"])
    expected_category = "video" if body.target == "video" else "image"
    if (
        quote.client_request_id != body.client_request_id
        or deepcopy(quote.request_snapshot or {}) != expected_request
        or deepcopy(quote.subject_snapshot or {}) != expected_subject
        or deepcopy(quote.pricing_snapshot or {}) != expected_pricing
        or deepcopy(quote.price_breakdown or {})
        != _reverse_single_breakdown(body, expected_allocation)
        or int(quote.estimated_credits or 0) != expected_total
        or quote.category != expected_category
        or quote.stage != "final"
        or quote.tool_version_id is not None
    ):
        _invalid_quote_snapshot("反推报价请求、模板或计价快照不一致，请重新报价")
    return {
        "model_snapshot": snapshot,
        "template_snapshot": expected_template,
        "pricing_snapshot": expected_allocation,
    }


def validate_reverse_batch_quote(
    db: Session,
    quote: GenerationQuote,
    *,
    body: ReverseBatchCreate,
) -> list[dict[str, Any]]:
    from . import reverse_operations

    if quote.kind != "reverse_batch":
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价不是批量反推类型"},
        )
    if quote.request_fingerprint != reverse_operations.batch_request_fingerprint(body):
        raise HTTPException(
            409,
            detail={"code": "QUOTE_MISMATCH", "message": "批量反推参数已变化，请重新报价"},
        )
    snapshot, model, _capability, price = _validate_reverse_version_snapshot(db, quote)
    subject = quote.subject_snapshot if isinstance(quote.subject_snapshot, dict) else {}
    allocations = subject.get("allocations")
    operation_bodies = reverse_operations.batch_operation_bodies(body)
    if not isinstance(allocations, list) or len(allocations) != len(operation_bodies):
        _invalid_quote_snapshot("批量反推报价的单项分配不完整，请重新报价")
    expected_allocations: list[dict[str, Any]] = []
    for index, (allocation, operation_body) in enumerate(zip(allocations, operation_bodies, strict=False)):
        if not isinstance(allocation, dict):
            _invalid_quote_snapshot("批量反推报价分配非法，请重新报价")
        expected = reverse_operations.request_fingerprint(operation_body)
        if int(allocation.get("index", -1)) != index or allocation.get("request_fingerprint") != expected:
            raise HTTPException(
                409,
                detail={"code": "QUOTE_MISMATCH", "message": "批量反推单项已变化，请重新报价"},
            )
        expected_request = reverse_operations.reverse_request_snapshot(operation_body)
        expected_request["model_config_id"] = int(model.id)
        expected_pricing = reverse_operations.reverse_pricing_snapshot(
            operation_body,
            _reverse_price_table(price),
        )
        expected_allocation = {
            "index": index,
            "request_fingerprint": expected,
            "request_snapshot": expected_request,
            "model_snapshot": snapshot,
            "template_snapshot": reverse_operations.reverse_template_snapshot(
                operation_body
            ),
            "pricing_snapshot": expected_pricing,
            "estimated_credits": int(expected_pricing["frozen"]),
        }
        if deepcopy(allocation) != expected_allocation:
            _invalid_quote_snapshot("批量反推单项快照已被篡改，请重新报价")
        expected_allocations.append(expected_allocation)
    total = sum(int(item["estimated_credits"]) for item in expected_allocations)
    expected_request = reverse_operations.batch_request_snapshot(body)
    expected_category = (
        "video" if any(item.target == "video" for item in operation_bodies) else "image"
    )
    expected_pricing = {
        "catalog": deepcopy(price.pricing or {}),
        "allocations": [
            deepcopy(item["pricing_snapshot"]) for item in expected_allocations
        ],
    }
    if (
        quote.client_request_id != body.client_request_id
        or deepcopy(quote.request_snapshot or {}) != expected_request
        or deepcopy(quote.subject_snapshot or {})
        != {"allocations": expected_allocations}
        or deepcopy(quote.pricing_snapshot or {}) != expected_pricing
        or deepcopy(quote.price_breakdown or {})
        != _reverse_batch_breakdown(operation_bodies, expected_allocations)
        or int(quote.estimated_credits or 0) != total
        or quote.category != expected_category
        or quote.stage != "final"
        or quote.tool_version_id is not None
    ):
        _invalid_quote_snapshot("批量反推报价请求或总额快照不一致，请重新报价")
    return expected_allocations
