"""Short-lived server-authoritative generation quotes."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    GenAsset,
    GenerationQuote,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    ToolDefinition,
    ToolVersion,
    User,
)
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from ..schemas import ReverseBatchCreate, ReverseOperationCreate
from ..workflow_schemas import ToolRunCreateIn, WorkflowSpec
from .asset_output import unlock_cost_for_asset
from .catalog_metadata import resolved_tool_metadata_snapshot
from .config_store import get_model_config
from .gateway_config_errors import raise_gateway_config_http
from .generation_model_runtime import (
    FrozenAdapterConfigError,
    ModelSnapshotMismatchError,
    model_from_persisted_snapshot,
    validate_frozen_adapter_snapshot,
)
from .generation_model_runtime import (
    model_snapshot as runtime_model_snapshot,
)
from .generation_pricing import image_quality_tier, is_edit_or_subject_task
from .model_versions import sync_model_versions
from .recipe_usage import attribution_from_snapshot

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


def _expired_quote_needs_reissue(quote: GenerationQuote | None) -> bool:
    return bool(
        quote is not None
        and quote.status == "expired"
        and quote.task_id is None
        and quote.consumed_ref_id is None
    )


def _quote_mismatch(message: str) -> None:
    raise HTTPException(409, detail={"code": "QUOTE_MISMATCH", "message": message})


def create_execution_quote(
    db: Session,
    *,
    user_id: int,
    kind: str,
    client_request_id: str | None,
    request_fingerprint: str,
    category: str,
    stage: str,
    request_snapshot: dict[str, Any],
    model_snapshot: dict[str, Any] | None,
    pricing_snapshot: dict[str, Any] | None,
    price_breakdown: dict[str, Any] | None,
    estimated_credits: int,
    model_config_id: int | None = None,
    capability_version_id: int | None = None,
    price_version_id: int | None = None,
    tool_version_id: int | None = None,
    subject_snapshot: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> GenerationQuote:
    if kind not in QUOTE_KINDS:
        raise ValueError(f"unsupported quote kind: {kind}")
    total = max(0, int(estimated_credits))
    now = datetime.now(timezone.utc)
    quote = GenerationQuote(
        user_id=int(user_id),
        kind=kind,
        client_request_id=(str(client_request_id).strip() if client_request_id else None),
        model_config_id=model_config_id,
        capability_version_id=capability_version_id,
        price_version_id=price_version_id,
        tool_version_id=tool_version_id,
        request_fingerprint=request_fingerprint,
        category=category,
        stage=stage,
        request_snapshot=deepcopy(request_snapshot),
        model_snapshot=deepcopy(model_snapshot or {}),
        pricing_snapshot=deepcopy(pricing_snapshot or {}),
        price_breakdown=normalized_price_breakdown(
            price_breakdown,
            total_credits=total,
        ),
        subject_snapshot=deepcopy(subject_snapshot or {}),
        warnings=deepcopy(warnings or []),
        estimated_credits=total,
        status="active",
        expires_at=now + timedelta(seconds=int(settings.generation_quote_ttl_seconds)),
    )
    db.add(quote)
    db.flush()
    return quote


def find_idempotent_quote(
    db: Session,
    *,
    user_id: int,
    kind: str,
    client_request_id: str | None,
    request_fingerprint: str,
) -> GenerationQuote | None:
    if not client_request_id:
        return None
    quote = db.scalar(
        select(GenerationQuote)
        .where(
            GenerationQuote.user_id == int(user_id),
            GenerationQuote.kind == kind,
            GenerationQuote.client_request_id == client_request_id,
        )
        .order_by(GenerationQuote.id.desc())
        .limit(1)
    )
    if quote is None:
        return None
    if quote.request_fingerprint != request_fingerprint:
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_IDEMPOTENCY_CONFLICT",
                "message": "client_request_id 已用于不同的报价请求",
            },
        )
    consumed = (
        quote.status == "consumed"
        or quote.task_id is not None
        or quote.consumed_ref_id is not None
    )
    if consumed:
        return quote
    now = datetime.now(timezone.utc)
    if quote.status == "active" and _utc(quote.expires_at) <= now:
        quote.status = "expired"
        db.flush()
        return None
    if quote.status == "active":
        return quote
    # An expired or canceled quote which never reached execution is historical
    # evidence, not a reusable quote. Keeping the same request id lets clients
    # safely obtain a fresh quote after timeout or explicit cancellation.
    return None


def lock_execution_quote(
    db: Session,
    *,
    quote_id: int,
    user_id: int,
    kind: str,
    allow_consumed: bool = False,
) -> GenerationQuote:
    quote = db.scalar(
        select(GenerationQuote)
        .where(
            GenerationQuote.id == int(quote_id),
            GenerationQuote.user_id == int(user_id),
        )
        .with_for_update()
    )
    if quote is None:
        raise HTTPException(404, detail={"code": "QUOTE_NOT_FOUND", "message": "报价不存在"})
    if quote.kind != kind:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价类型与执行请求不匹配"},
        )
    now = datetime.now(timezone.utc)
    if quote.status == "active" and _utc(quote.expires_at) <= now:
        quote.status = "expired"
        db.flush()
    if quote.status == "expired":
        raise HTTPException(409, detail={"code": "QUOTE_EXPIRED", "message": "报价已过期，请重新报价"})
    if quote.status == "consumed" and allow_consumed:
        return quote
    if quote.status != "active" or quote.consumed_ref_id is not None or quote.task_id is not None:
        raise HTTPException(409, detail={"code": "QUOTE_CONSUMED", "message": "报价已被使用"})
    return quote


def consume_execution_quote(
    quote: GenerationQuote,
    *,
    ref_type: str,
    ref_id: int,
) -> None:
    quote.status = "consumed"
    quote.consumed_ref_type = str(ref_type)
    quote.consumed_ref_id = int(ref_id)
    if quote.kind == "generation" and ref_type == "gen_task":
        quote.task_id = int(ref_id)
    quote.consumed_at = datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def workflow_request_fingerprint(
    *,
    tool_version_id: int,
    payload: dict[str, Any],
    project_id: int | None = None,
) -> str:
    raw = json.dumps(
        {
            "tool_version_id": int(tool_version_id),
            "project_id": int(project_id) if project_id is not None else None,
            "input": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _active_tool_version(db: Session, slug: str) -> tuple[ToolDefinition, ToolVersion]:
    row = db.execute(
        select(ToolDefinition, ToolVersion)
        .join(ToolVersion, ToolVersion.tool_definition_id == ToolDefinition.id)
        .where(
            ToolDefinition.slug == slug,
            ToolDefinition.enabled.is_(True),
            ToolVersion.is_active.is_(True),
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            404,
            detail={"code": "TOOL_NOT_FOUND", "message": "工具不存在或没有可用版本"},
        )
    return row[0], row[1]


def _workflow_request_snapshot(body: ToolRunCreateIn) -> dict[str, Any]:
    return {
        "tool_slug": body.tool_slug,
        "project_id": int(body.project_id) if body.project_id is not None else None,
        "client_request_id": body.client_request_id,
        "input": deepcopy(body.input),
    }


def _workflow_subject_snapshot(
    tool: ToolDefinition,
    version: ToolVersion,
    spec: WorkflowSpec,
) -> dict[str, Any]:
    metadata = resolved_tool_metadata_snapshot(tool, version.metadata_snapshot)
    return {
        "tool_definition_id": int(tool.id),
        "tool_slug": metadata["slug"],
        "tool_name": metadata["name"],
        "tool_metadata": metadata,
        "tool_version_id": int(version.id),
        "tool_version": int(version.version),
        "schema_version": version.schema_version,
        "input_schema": deepcopy(version.input_schema or {}),
        "workflow": spec.model_dump(mode="json"),
        "pricing_policy": deepcopy(version.pricing_policy or {}),
        "capabilities": deepcopy(version.capabilities or {}),
    }


def _find_workflow_quote_replay(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> GenerationQuote | None:
    quote = db.scalar(
        select(GenerationQuote)
        .where(
            GenerationQuote.user_id == int(user_id),
            GenerationQuote.kind == "workflow",
            GenerationQuote.client_request_id == body.client_request_id,
        )
        .order_by(GenerationQuote.id.desc())
        .limit(1)
    )
    if quote is None:
        return None
    if deepcopy(quote.request_snapshot or {}) != _workflow_request_snapshot(body):
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_IDEMPOTENCY_CONFLICT",
                "message": "client_request_id 已用于不同的报价请求",
            },
        )
    consumed = (
        quote.status == "consumed"
        or quote.task_id is not None
        or quote.consumed_ref_id is not None
    )
    if consumed:
        return quote
    if quote.status == "active" and _utc(quote.expires_at) <= datetime.now(timezone.utc):
        quote.status = "expired"
        db.flush()
        return None
    return quote if quote.status == "active" else None


def create_workflow_quote(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> GenerationQuote:
    replay = _find_workflow_quote_replay(db, user_id=user_id, body=body)
    if replay is not None:
        return replay
    tool, version = _active_tool_version(db, body.tool_slug)
    try:
        spec = WorkflowSpec.model_validate(version.workflow or {})
    except Exception as exc:
        raise HTTPException(
            409,
            detail={"code": "WORKFLOW_INVALID", "message": "当前工具版本不是可执行工作流"},
        ) from exc
    fingerprint = workflow_request_fingerprint(
        tool_version_id=int(version.id),
        payload=body.input,
        project_id=body.project_id,
    )
    pricing = deepcopy(version.pricing_policy or {})
    try:
        total = max(0, int(pricing.get("credits") or 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            409,
            detail={"code": "WORKFLOW_PRICING_INVALID", "message": "工具版本计价策略非法"},
        ) from exc
    snapshot = _workflow_subject_snapshot(tool, version, spec)
    return create_execution_quote(
        db,
        user_id=user_id,
        kind="workflow",
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
        category="workflow",
        stage="final",
        request_snapshot=_workflow_request_snapshot(body),
        model_snapshot={},
        pricing_snapshot=pricing,
        price_breakdown={
            "items": [
                quote_item(
                    code="workflow_orchestration",
                    label="工作流编排",
                    credits=total,
                )
            ]
        },
        estimated_credits=total,
        tool_version_id=int(version.id),
        subject_snapshot=snapshot,
        warnings=_balance_warning(db, user_id=user_id, total=total),
    )


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

    # ModelConfig is still the admin write surface. Synchronize it before the
    # quote pins a catalog version so a stale active row cannot be quoted.
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


def lock_generation_quote(db: Session, *, quote_id: int, user_id: int) -> GenerationQuote:
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
    snapshot = validate_quote_snapshot_integrity(db, quote)
    try:
        model_from_persisted_snapshot(db, snapshot, model, str(quote.category or model.use))
    except ModelSnapshotMismatchError as exc:
        raise_gateway_config_http(exc)
    return snapshot


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


def validate_workflow_quote(
    db: Session,
    quote: GenerationQuote,
    *,
    body: ToolRunCreateIn,
) -> tuple[ToolDefinition, ToolVersion, WorkflowSpec]:
    if quote.kind != "workflow":
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价不是工作流类型"},
        )
    if str(quote.client_request_id or "") != body.client_request_id:
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_MISMATCH",
                "message": "工作流报价与执行请求标识不一致，请重新报价",
            },
        )
    if quote.tool_version_id is None:
        _invalid_quote_snapshot("工作流报价缺少工具版本，请重新报价")
    version = db.get(ToolVersion, int(quote.tool_version_id))
    subject = quote.subject_snapshot if isinstance(quote.subject_snapshot, dict) else {}
    if version is None:
        _invalid_quote_snapshot("工作流报价引用的工具版本不存在")
    tool = db.get(ToolDefinition, int(version.tool_definition_id))
    if tool is None:
        _invalid_quote_snapshot("工作流报价引用的工具定义不存在")
    metadata = resolved_tool_metadata_snapshot(tool, version.metadata_snapshot)
    if metadata["slug"] != body.tool_slug:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_MISMATCH", "message": "工作流工具已变化，请重新报价"},
        )
    fingerprint = workflow_request_fingerprint(
        tool_version_id=int(version.id),
        payload=body.input,
        project_id=body.project_id,
    )
    if quote.request_fingerprint != fingerprint:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_MISMATCH", "message": "工作流输入已变化，请重新报价"},
        )
    expected_request = _workflow_request_snapshot(body)
    if deepcopy(quote.request_snapshot or {}) != expected_request:
        _invalid_quote_snapshot("工作流报价请求快照已被篡改，请重新报价")
    try:
        spec = WorkflowSpec.model_validate(version.workflow or {})
    except Exception as exc:
        _invalid_quote_snapshot(f"工作流报价 DAG 非法: {exc}")
    expected = _workflow_subject_snapshot(tool, version, spec)
    if subject != expected or deepcopy(quote.pricing_snapshot or {}) != expected["pricing_policy"]:
        _invalid_quote_snapshot("工作流报价版本快照已被篡改，请重新报价")
    return tool, version, spec


def _invalid_quote_snapshot(message: str) -> None:
    raise HTTPException(
        409,
        detail={"code": "QUOTE_SNAPSHOT_INVALID", "message": message},
    )


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


def quoted_model_snapshot(
    quote: GenerationQuote,
    _current_snapshot: dict | None = None,
) -> dict:
    """Return the quote snapshot verbatim; current catalog fields are irrelevant."""
    return deepcopy(quote.model_snapshot or {})


def consume_generation_quote(quote: GenerationQuote, *, task_id: int) -> None:
    consume_execution_quote(quote, ref_type="gen_task", ref_id=task_id)


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
