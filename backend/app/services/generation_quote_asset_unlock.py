"""Asset-unlock quote creation and validation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import (
    GenAsset,
    GenerationQuote,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
)
from .asset_output import unlock_cost_for_asset
from .config_store import get_model_config
from .generation_quote_core import (
    _balance_warning,
    _expected_action_breakdown,
    _expired_quote_needs_reissue,
    _invalid_quote_snapshot,
    _json_fingerprint,
    _quote_mismatch,
    _versioned_runtime_model_snapshot,
    _without_action_fingerprints,
    create_execution_quote,
    find_idempotent_quote,
    normalized_price_breakdown,
)
from .model_versions import sync_model_versions


def _asset_quote_row(db: Session, *, user_id: int, asset_id: int) -> GenAsset:
    asset = db.get(GenAsset, int(asset_id))
    if asset is None or int(asset.user_id) != int(user_id):
        raise HTTPException(404, detail={"code": "ASSET_NOT_FOUND", "message": "素材不存在"})
    if asset.moderation_status != "active":
        raise HTTPException(410, detail={"code": "ASSET_TAKEDOWN", "message": "素材已下架"})
    if asset.unlocked:
        raise HTTPException(
            409,
            detail={"code": "ASSET_ALREADY_UNLOCKED", "message": "素材已经解锁，无需报价"},
        )
    return asset


def _asset_subject_snapshot(asset: GenAsset, task: GenTask | None) -> dict[str, Any]:
    task_snapshot = (
        deepcopy((task.params or {}).get("_model_snapshot") or {})
        if task is not None and isinstance(task.params, dict)
        else {}
    )
    return {
        "schema_version": "asset-unlock-quote.v1",
        "asset": {
            "id": int(asset.id),
            "user_id": int(asset.user_id),
            "type": asset.type,
            "task_id": int(asset.task_id) if asset.task_id is not None else None,
            "unlocked": bool(asset.unlocked),
            "watermarked": bool(asset.watermarked),
            "moderation_status": asset.moderation_status,
        },
        "task": (
            {
                "id": int(task.id),
                "user_id": int(task.user_id),
                "model_config_id": (
                    int(task.model_config_id) if task.model_config_id is not None else None
                ),
                "category": task.category,
                "stage": task.stage,
                "status": task.status,
                "model_snapshot_fingerprint": _json_fingerprint(task_snapshot),
            }
            if task is not None
            else None
        ),
    }


def _historical_asset_pricing(
    db: Session,
    *,
    asset: GenAsset,
    task: GenTask,
    task_snapshot: dict[str, Any],
    unlock_cost: int,
) -> dict[str, Any]:
    model_id = task_snapshot.get("model_config_id") or task.model_config_id
    model = db.get(ModelConfig, int(model_id)) if model_id is not None else None
    price_id = task_snapshot.get("price_version_id")
    capability_id = task_snapshot.get("capability_version_id")
    price = db.get(ModelPriceVersion, int(price_id)) if price_id is not None else None
    capability = (
        db.get(ModelCapabilityVersion, int(capability_id))
        if capability_id is not None
        else None
    )
    if price_id is not None and (
        price is None
        or (model_id is not None and int(price.model_config_id) != int(model_id))
        or int(price.unlock_cost_credits or 0) != int(unlock_cost)
    ):
        raise HTTPException(
            409,
            detail={
                "code": "ASSET_PRICING_INVALID",
                "message": "素材任务的历史解锁价格版本不一致",
            },
        )
    if capability_id is not None and (
        capability is None
        or (model_id is not None and int(capability.model_config_id) != int(model_id))
    ):
        raise HTTPException(
            409,
            detail={
                "code": "ASSET_PRICING_INVALID",
                "message": "素材任务的历史能力版本不一致",
            },
        )
    pricing_snapshot = {
        "source": "task_model_snapshot",
        "asset_id": int(asset.id),
        "task_id": int(task.id),
        "model_config_id": int(model_id) if model_id is not None else None,
        "model_snapshot_fingerprint": _json_fingerprint(task_snapshot),
        "price_version_id": int(price.id) if price is not None else None,
        "price_version": int(price.version) if price is not None else None,
        "price_version_snapshot": (
            {
                "base_cost_credits": int(price.base_cost_credits or 0),
                "unlock_cost_credits": int(price.unlock_cost_credits or 0),
                "pricing": deepcopy(price.pricing or {}),
            }
            if price is not None
            else None
        ),
        "quoted_credits": int(unlock_cost),
    }
    return {
        "model": model,
        "capability": capability,
        "price": price,
        "model_snapshot": deepcopy(task_snapshot),
        "pricing_snapshot": pricing_snapshot,
        "model_config_id": int(model_id) if model_id is not None else None,
        "capability_version_id": int(capability.id) if capability is not None else None,
        "price_version_id": int(price.id) if price is not None else None,
        "estimated_credits": int(unlock_cost),
    }


def _asset_unlock_quote_material(
    db: Session,
    *,
    asset: GenAsset,
) -> dict[str, Any]:
    task = db.get(GenTask, int(asset.task_id)) if asset.task_id is not None else None
    unlock_cost = unlock_cost_for_asset(db, asset, task=task)
    task_snapshot = (
        deepcopy((task.params or {}).get("_model_snapshot") or {})
        if task is not None and isinstance(task.params, dict)
        else {}
    )
    if task is not None and task_snapshot and "unlock_cost" in task_snapshot:
        material = _historical_asset_pricing(
            db,
            asset=asset,
            task=task,
            task_snapshot=task_snapshot,
            unlock_cost=unlock_cost,
        )
    else:
        model = get_model_config(db, asset.type)
        if model is None:
            raise HTTPException(
                503,
                detail={
                    "code": "ASSET_PRICING_UNAVAILABLE",
                    "message": "当前素材类型没有可用的解锁价格配置",
                },
            )
        capability, price = sync_model_versions(db, model)
        if int(price.unlock_cost_credits or 0) != int(unlock_cost):
            raise HTTPException(
                409,
                detail={
                    "code": "ASSET_PRICING_INVALID",
                    "message": "素材解锁价格与当前价格版本不一致",
                },
            )
        material = {
            "model": model,
            "capability": capability,
            "price": price,
            "model_snapshot": _versioned_runtime_model_snapshot(model, capability, price),
            "pricing_snapshot": {
                "source": "catalog_price_version",
                "asset_id": int(asset.id),
                "task_id": int(task.id) if task is not None else None,
                "model_config_id": int(model.id),
                "model_snapshot_fingerprint": None,
                "price_version_id": int(price.id),
                "price_version": int(price.version),
                "price_version_snapshot": {
                    "base_cost_credits": int(price.base_cost_credits or 0),
                    "unlock_cost_credits": int(price.unlock_cost_credits or 0),
                    "pricing": deepcopy(price.pricing or {}),
                },
                "quoted_credits": int(unlock_cost),
            },
            "model_config_id": int(model.id),
            "capability_version_id": int(capability.id),
            "price_version_id": int(price.id),
            "estimated_credits": int(unlock_cost),
        }
    material["task"] = task
    material["subject_snapshot"] = _asset_subject_snapshot(asset, task)
    return material


def create_asset_unlock_quote(
    db: Session,
    *,
    user_id: int,
    asset_id: int,
    client_request_id: str,
) -> GenerationQuote:
    from .generation_quote_core import _normalized_client_request_id

    request_id = _normalized_client_request_id(client_request_id)
    normalized_asset_id = int(asset_id)
    request_snapshot = {"asset_id": normalized_asset_id}
    fingerprint = _json_fingerprint(request_snapshot)
    replay = find_idempotent_quote(
        db,
        user_id=user_id,
        kind="asset_unlock",
        client_request_id=request_id,
        request_fingerprint=fingerprint,
    )
    if replay is not None and not _expired_quote_needs_reissue(replay):
        return replay

    asset = _asset_quote_row(db, user_id=user_id, asset_id=normalized_asset_id)
    material = _asset_unlock_quote_material(db, asset=asset)
    total = int(material["estimated_credits"])
    subject_snapshot = material["subject_snapshot"]
    pricing_snapshot = material["pricing_snapshot"]
    model_snapshot = deepcopy(material["model_snapshot"])
    model_snapshot["_quote_subject_fingerprint"] = _json_fingerprint(subject_snapshot)
    model_snapshot["_quote_pricing_fingerprint"] = _json_fingerprint(pricing_snapshot)
    return create_execution_quote(
        db,
        user_id=user_id,
        kind="asset_unlock",
        client_request_id=request_id,
        request_fingerprint=fingerprint,
        category=asset.type,
        stage="final",
        request_snapshot=request_snapshot,
        model_snapshot=model_snapshot,
        pricing_snapshot=pricing_snapshot,
        price_breakdown=_expected_action_breakdown(
            code="asset_unlock",
            label="高清素材解锁",
            credits=total,
        ),
        estimated_credits=total,
        model_config_id=material["model_config_id"],
        capability_version_id=material["capability_version_id"],
        price_version_id=material["price_version_id"],
        subject_snapshot=subject_snapshot,
        warnings=_balance_warning(db, user_id=user_id, total=total),
    )


def validate_asset_unlock_quote(
    db: Session,
    quote: GenerationQuote,
    *,
    asset_id: int,
) -> int:
    if quote.kind != "asset_unlock":
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价不是素材解锁类型"},
        )
    normalized_asset_id = int(asset_id)
    expected_fingerprint = _json_fingerprint({"asset_id": normalized_asset_id})
    if quote.request_fingerprint != expected_fingerprint:
        _quote_mismatch("解锁素材已变化，请重新报价")
    if quote.request_snapshot != {"asset_id": normalized_asset_id}:
        _invalid_quote_snapshot("素材解锁报价请求快照不一致，请重新报价")

    asset = db.get(GenAsset, normalized_asset_id)
    if asset is None or int(asset.user_id) != int(quote.user_id):
        _quote_mismatch("解锁素材不存在或不属于当前报价用户")
    task = db.get(GenTask, int(asset.task_id)) if asset.task_id is not None else None
    expected_subject = _asset_subject_snapshot(asset, task)
    subject = quote.subject_snapshot if isinstance(quote.subject_snapshot, dict) else {}
    pricing = quote.pricing_snapshot if isinstance(quote.pricing_snapshot, dict) else {}
    snapshot = quote.model_snapshot if isinstance(quote.model_snapshot, dict) else {}
    if subject != expected_subject:
        _quote_mismatch("素材或关联任务状态已变化，请重新报价")
    if snapshot.get("_quote_subject_fingerprint") != _json_fingerprint(subject):
        _invalid_quote_snapshot("素材解锁报价主体快照已被篡改，请重新报价")
    if snapshot.get("_quote_pricing_fingerprint") != _json_fingerprint(pricing):
        _invalid_quote_snapshot("素材解锁报价价格快照已被篡改，请重新报价")

    amount = max(0, int(quote.estimated_credits or 0))
    if (
        pricing.get("asset_id") != normalized_asset_id
        or pricing.get("task_id") != (int(task.id) if task is not None else None)
        or int(pricing.get("quoted_credits") or 0) != amount
        or quote.category != asset.type
        or quote.stage != "final"
        or normalized_price_breakdown(quote.price_breakdown, total_credits=amount)
        != _expected_action_breakdown(
            code="asset_unlock",
            label="高清素材解锁",
            credits=amount,
        )
    ):
        _invalid_quote_snapshot("素材解锁报价金额或用途不一致，请重新报价")

    source = pricing.get("source")
    model_id = pricing.get("model_config_id")
    if model_id != quote.model_config_id:
        _invalid_quote_snapshot("素材解锁报价模型引用不一致，请重新报价")
    if source == "catalog_price_version":
        if (
            quote.model_config_id is None
            or quote.capability_version_id is None
            or quote.price_version_id is None
        ):
            _invalid_quote_snapshot("素材解锁报价缺少模型价格版本，请重新报价")
        capability = db.get(ModelCapabilityVersion, int(quote.capability_version_id))
        price = db.get(ModelPriceVersion, int(quote.price_version_id))
        if (
            capability is None
            or price is None
            or int(capability.model_config_id) != int(quote.model_config_id)
            or int(price.model_config_id) != int(quote.model_config_id)
            or pricing.get("price_version_id") != int(price.id)
            or pricing.get("price_version") != int(price.version)
        ):
            _invalid_quote_snapshot("素材解锁报价引用的价格版本不存在或不一致")
        expected_price_snapshot = {
            "base_cost_credits": int(price.base_cost_credits or 0),
            "unlock_cost_credits": int(price.unlock_cost_credits or 0),
            "pricing": deepcopy(price.pricing or {}),
        }
        extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
        if (
            pricing.get("price_version_snapshot") != expected_price_snapshot
            or int(price.unlock_cost_credits or 0) != amount
            or snapshot.get("model_config_id") != int(quote.model_config_id)
            or snapshot.get("capability_version_id") != int(capability.id)
            or snapshot.get("capability_version") != int(capability.version)
            or snapshot.get("price_version_id") != int(price.id)
            or snapshot.get("price_version") != int(price.version)
            or int(snapshot.get("cost_credits") or 0) != int(price.base_cost_credits or 0)
            or int(snapshot.get("unlock_cost") or 0) != amount
            or deepcopy(extra.get("capabilities") or {})
            != deepcopy(capability.capabilities or {})
            or deepcopy(extra.get("credit_pricing") or {}) != deepcopy(price.pricing or {})
        ):
            _invalid_quote_snapshot("素材解锁报价的能力或价格快照已被篡改")
    elif source == "task_model_snapshot":
        task_snapshot = (
            deepcopy((task.params or {}).get("_model_snapshot") or {})
            if task is not None and isinstance(task.params, dict)
            else {}
        )
        if (
            task is None
            or pricing.get("model_snapshot_fingerprint") != _json_fingerprint(task_snapshot)
            or _without_action_fingerprints(snapshot) != task_snapshot
        ):
            _invalid_quote_snapshot("素材任务的历史模型快照已变化，请重新报价")
        price_id = pricing.get("price_version_id")
        price = db.get(ModelPriceVersion, int(price_id)) if price_id is not None else None
        expected_price_snapshot = (
            {
                "base_cost_credits": int(price.base_cost_credits or 0),
                "unlock_cost_credits": int(price.unlock_cost_credits or 0),
                "pricing": deepcopy(price.pricing or {}),
            }
            if price is not None
            else None
        )
        if (
            quote.price_version_id != price_id
            or pricing.get("price_version_snapshot") != expected_price_snapshot
            or (price_id is not None and price is None)
        ):
            _invalid_quote_snapshot("素材任务的历史价格版本已变化，请重新报价")
    else:
        _invalid_quote_snapshot("素材解锁报价价格来源非法，请重新报价")
    return amount
