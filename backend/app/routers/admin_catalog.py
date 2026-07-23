"""Admin version history, rollback, and configurable tool catalog endpoints."""
from __future__ import annotations

import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    ModelConfig,
    ModelRoute,
    ModelRouteHealthEvent,
    ToolDefinition,
    ToolVersion,
    User,
)
from ..schemas import (
    ModelRouteCreateIn,
    ModelRoutePatchIn,
    ModelVersionActivateIn,
    ModelVersionDraftCreateIn,
    ModelVersionDraftPatchIn,
    ToolDefinitionCreateIn,
    ToolDefinitionPatchIn,
    ToolVersionPayloadIn,
)
from ..services import audit, gateway
from ..services.catalog import model_catalog_detail
from ..services.catalog_metadata import (
    TOOL_METADATA_FIELDS,
    TOOL_METADATA_SCHEMA_VERSION,
    recorded_snapshot,
    snapshot_fields,
    tool_metadata_snapshot,
)
from ..services.model_gateway_config import ModelGatewayConfigError, runtime_config_for_model
from ..services.model_routes import (
    active_route_version,
    apply_route_gateway_update,
    error_counts_toward_circuit,
    publish_route_projection,
    reset_route_health,
    retire_route_version,
    rollback_route_version,
    route_admin_dict,
    route_runtime_model,
    route_version_history,
    serialize_route_version,
    validate_route_extra,
)
from ..services.model_versions import (
    create_model_version_draft,
    disable_model_version,
    publish_model_version,
    retire_model_version,
    rollback_model_version,
    update_model_version_draft,
)
from .admin_models import _assert_no_active_model_config_tasks

router = APIRouter()


def _commit(db: Session, message: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, message) from exc


def _route_for_model(db: Session, model_config_id: int, route_id: int) -> tuple[ModelConfig, ModelRoute]:
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    route = db.scalar(
        select(ModelRoute).where(
            ModelRoute.id == route_id,
            ModelRoute.model_config_id == model.id,
        )
    )
    if route is None:
        raise HTTPException(404, "模型路由不存在")
    return model, route


def _route_probe_error_code(value: str | None) -> str | None:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())[:64]
    return normalized or None


def _route_probe_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_route_probe_outcome(
    db: Session,
    route: ModelRoute,
    *,
    success: bool,
    latency_ms: int,
    error_code: str | None = None,
    counts_toward_circuit: bool = True,
) -> None:
    now = datetime.now(timezone.utc)
    latency = max(0, int(latency_ms))
    safe_error_code = _route_probe_error_code(error_code)
    db.add(
        ModelRouteHealthEvent(
            route_id=int(route.id),
            operation="probe",
            outcome="success"
            if success
            else ("failure" if counts_toward_circuit else "ignored"),
            counts_toward_circuit=bool(not success and counts_toward_circuit),
            latency_ms=latency,
            error_code=safe_error_code,
        )
    )
    route.last_probe_at = now
    previous = route.latency_ema_ms
    route.latency_ema_ms = latency if previous is None else round(previous * 0.8 + latency * 0.2)
    if success:
        route.last_success_at = now
        route.health_status = "closed"
        route.window_started_at = now
        route.window_requests = 1
        route.window_failures = 0
        route.consecutive_failures = 0
        route.opened_at = None
        route.cooldown_until = None
        route.half_open_claimed_until = None
        route.last_error_code = None
    elif counts_toward_circuit:
        window_started = _route_probe_utc(route.window_started_at)
        if (
            window_started is None
            or window_started + timedelta(seconds=int(route.window_seconds)) <= now
        ):
            route.window_started_at = now
            route.window_requests = 0
            route.window_failures = 0
        route.window_requests = int(route.window_requests or 0) + 1
        route.window_failures = int(route.window_failures or 0) + 1
        route.consecutive_failures = int(route.consecutive_failures or 0) + 1
        route.last_failure_at = now
        route.last_error_code = safe_error_code
        should_open = (
            route.health_status == "half_open"
            or int(route.window_failures) >= int(route.failure_threshold)
            or int(route.consecutive_failures) >= int(route.failure_threshold)
        )
        if should_open:
            route.health_status = "open"
            route.opened_at = now
            route.cooldown_until = now + timedelta(seconds=int(route.cooldown_seconds))
            route.half_open_claimed_until = None
    db.flush()


_TOOL_VERSION_PAYLOAD_FIELDS = (
    "schema_version",
    "input_schema",
    "workflow",
    "pricing_policy",
    "capabilities",
)


def _validated_tool_metadata(row: ToolVersion) -> dict:
    values = snapshot_fields(row.metadata_snapshot, TOOL_METADATA_FIELDS)
    if set(values) != set(TOOL_METADATA_FIELDS):
        raise HTTPException(409, "工具版本缺少完整目录元数据快照")
    required_text = ("slug", "name", "category", "renderer", "entry_path")
    if any(not isinstance(values[field], str) or not values[field].strip() for field in required_text):
        raise HTTPException(409, "工具版本的目录文本元数据无效")
    if not values["entry_path"].startswith("/") or values["entry_path"].startswith("//"):
        raise HTTPException(409, "工具版本的站内入口无效")
    if not isinstance(values["enabled"], bool) or not isinstance(values["featured"], bool):
        raise HTTPException(409, "工具版本的目录状态元数据无效")
    if isinstance(values["sort_order"], bool) or not isinstance(values["sort_order"], int):
        raise HTTPException(409, "工具版本的目录排序元数据无效")
    return values


def _apply_tool_metadata(tool: ToolDefinition, row: ToolVersion) -> None:
    for field, value in _validated_tool_metadata(row).items():
        setattr(tool, field, deepcopy(value))


def _serialize_tool_version(row: ToolVersion) -> dict:
    return {
        "id": int(row.id),
        "version": int(row.version),
        "schema_version": row.schema_version,
        "input_schema": deepcopy(row.input_schema or {}),
        "workflow": deepcopy(row.workflow or {}),
        "pricing_policy": deepcopy(row.pricing_policy or {}),
        "capabilities": deepcopy(row.capabilities or {}),
        "metadata_snapshot": deepcopy(row.metadata_snapshot or {}),
        "status": row.status,
        "is_active": bool(row.is_active),
        "source_version_id": row.source_version_id,
        "activated_at": row.activated_at,
        "disabled_at": row.disabled_at,
        "retired_at": row.retired_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _admin_tool_detail(
    db: Session,
    tool: ToolDefinition,
    *,
    history: bool = False,
) -> dict:
    query = select(ToolVersion).where(ToolVersion.tool_definition_id == int(tool.id))
    if not history:
        query = query.where(ToolVersion.is_active.is_(True))
    versions = list(db.scalars(query.order_by(ToolVersion.version.desc())))
    active = next((row for row in versions if row.is_active), None)
    return {
        "id": int(tool.id),
        "slug": tool.slug,
        "name": tool.name,
        "description": tool.description,
        "category": tool.category,
        "renderer": tool.renderer,
        "entry_path": tool.entry_path,
        "icon": tool.icon,
        "sort_order": int(tool.sort_order or 0),
        "enabled": bool(tool.enabled),
        "featured": bool(tool.featured),
        "active_version": _serialize_tool_version(active) if active is not None else None,
        "created_at": tool.created_at,
        "updated_at": tool.updated_at,
        **(
            {"versions": [_serialize_tool_version(row) for row in versions]}
            if history
            else {}
        ),
    }


def _lock_tool(db: Session, tool_id: int) -> ToolDefinition:
    tool = db.scalar(
        select(ToolDefinition).where(ToolDefinition.id == int(tool_id)).with_for_update()
    )
    if tool is None:
        raise HTTPException(404, "工具不存在")
    return tool


def _locked_tool_versions(db: Session, tool: ToolDefinition) -> list[ToolVersion]:
    return list(
        db.scalars(
            select(ToolVersion)
            .where(ToolVersion.tool_definition_id == int(tool.id))
            .order_by(ToolVersion.version)
            .with_for_update()
        )
    )


def _tool_version_row(
    versions: list[ToolVersion],
    version_number: int,
) -> ToolVersion:
    target = next((row for row in versions if int(row.version) == int(version_number)), None)
    if target is None:
        raise HTTPException(404, "工具版本不存在")
    return target


def _disable_active_tool_version(
    versions: list[ToolVersion],
    *,
    now: datetime,
    except_id: int | None = None,
) -> None:
    for row in versions:
        if row.is_active and (except_id is None or int(row.id) != int(except_id)):
            row.is_active = False
            row.status = "disabled"
            row.disabled_at = now


def _publish_tool_version_draft(
    db: Session,
    tool: ToolDefinition,
    versions: list[ToolVersion],
    target: ToolVersion,
) -> ToolVersion:
    if target.status != "draft" or target.is_active:
        raise HTTPException(409, "只有草稿工具版本可以发布")
    now = datetime.now(timezone.utc)
    _disable_active_tool_version(versions, now=now, except_id=int(target.id))
    db.flush()
    target.status = "published"
    target.is_active = True
    target.activated_at = now
    target.disabled_at = None
    target.retired_at = None
    _apply_tool_metadata(tool, target)
    db.flush()
    return target


def _rollback_tool_version_copy(
    db: Session,
    tool: ToolDefinition,
    versions: list[ToolVersion],
    source: ToolVersion,
) -> ToolVersion:
    if source.status != "disabled" or source.is_active:
        raise HTTPException(409, "只能回滚到已停用且未退役的历史工具版本")
    now = datetime.now(timezone.utc)
    _disable_active_tool_version(versions, now=now)
    db.flush()
    target = ToolVersion(
        tool_definition_id=int(tool.id),
        version=max((int(row.version) for row in versions), default=0) + 1,
        **{
            field: deepcopy(getattr(source, field))
            for field in _TOOL_VERSION_PAYLOAD_FIELDS
        },
        metadata_snapshot=recorded_snapshot(
            source.metadata_snapshot,
            schema_version=TOOL_METADATA_SCHEMA_VERSION,
            fields=TOOL_METADATA_FIELDS,
        ),
        status="published",
        is_active=True,
        source_version_id=int(source.id),
        activated_at=now,
    )
    db.add(target)
    db.flush()
    _apply_tool_metadata(tool, target)
    db.flush()
    return target


def _publish_tool_metadata_change(
    db: Session,
    tool: ToolDefinition,
    versions: list[ToolVersion],
) -> ToolVersion:
    source = next((row for row in versions if row.is_active), None)
    if source is None:
        source = next(
            (
                row
                for row in reversed(versions)
                if row.status in {"published", "disabled"} and not row.is_active
            ),
            None,
        )
    now = datetime.now(timezone.utc)
    _disable_active_tool_version(versions, now=now)
    db.flush()
    target = ToolVersion(
        tool_definition_id=int(tool.id),
        version=max((int(row.version) for row in versions), default=0) + 1,
        schema_version=source.schema_version if source is not None else "tool.v1",
        input_schema=deepcopy(source.input_schema or {}) if source is not None else {},
        workflow=deepcopy(source.workflow or {}) if source is not None else {},
        pricing_policy=deepcopy(source.pricing_policy or {}) if source is not None else {},
        capabilities=deepcopy(source.capabilities or {}) if source is not None else {},
        metadata_snapshot=tool_metadata_snapshot(tool),
        status="published",
        is_active=True,
        source_version_id=int(source.id) if source is not None else None,
        activated_at=now,
    )
    db.add(target)
    db.flush()
    return target


@router.get("/models/{model_config_id}/routes")
def model_routes(
    model_config_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    rows = list(
        db.scalars(
            select(ModelRoute)
            .where(ModelRoute.model_config_id == model.id)
            .order_by(ModelRoute.priority, ModelRoute.id)
        )
    )
    versions = [active_route_version(db, row, create=False) for row in rows]
    items = [
        route_admin_dict(row, version)
        for row, version in zip(rows, versions, strict=True)
    ]
    return {"items": items, "total": len(rows)}


@router.post("/models/{model_config_id}/routes", status_code=201)
def create_model_route(
    model_config_id: int,
    body: ModelRouteCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model = db.scalar(
        select(ModelConfig).where(ModelConfig.id == model_config_id).with_for_update()
    )
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    row = ModelRoute(
        model_config_id=int(model.id),
        route_key=body.route_key,
        name=body.name,
        model_id=body.model_id,
        extra=validate_route_extra(body.extra),
        priority=body.priority,
        enabled=body.enabled,
        managed_by_model_config=False,
        failure_threshold=body.failure_threshold,
        window_seconds=body.window_seconds,
        cooldown_seconds=body.cooldown_seconds,
    )
    try:
        apply_route_gateway_update(
            row,
            model_use=model.use,
            provider=body.provider,
            base_url=body.base_url,
            api_key=body.api_key,
            api_key_clear=False,
            gateway_format=body.gateway_format,
        )
    except (ModelGatewayConfigError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    db.add(row)
    db.flush()
    version = publish_route_projection(db, row)
    audit.log_required(
        db,
        user_id=admin.id,
        action="create_model_route",
        biz_type="model_route",
        biz_id=row.id,
        ip=get_client_ip(request),
        detail={
            "model_config_id": model.id,
            "route_key": row.route_key,
            "published_version": version.version,
        },
    )
    _commit(db, "模型路由标识已存在或发生并发更新")
    db.refresh(row)
    return route_admin_dict(row, version)


@router.patch("/models/{model_config_id}/routes/{route_id}")
def patch_model_route(
    model_config_id: int,
    route_id: int,
    body: ModelRoutePatchIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    route = db.scalar(
        select(ModelRoute)
        .where(ModelRoute.id == route_id, ModelRoute.model_config_id == model.id)
        .with_for_update()
    )
    if route is None:
        raise HTTPException(404, "模型路由不存在")
    _assert_no_active_model_config_tasks(db, model)
    fields = set(body.model_fields_set)
    gateway_fields = {"provider", "base_url", "api_key", "api_key_clear", "gateway_format"}
    custom_config_fields = gateway_fields | {"model_id", "extra"}
    if body.managed_by_model_config is True:
        if route.route_key != "legacy-default":
            raise HTTPException(400, "只有默认兼容路由可以重新绑定模型配置")
        if fields.intersection(custom_config_fields):
            raise HTTPException(400, "重新绑定模型配置时不能同时覆盖路由运行参数")
        route.managed_by_model_config = True
        route.model_id = None
        route.extra = {}
        for key, value in {
            "provider": model.provider,
            "base_url": model.base_url,
            "api_key_encrypted": model.api_key_encrypted,
            "gateway_format": model.gateway_format,
        }.items():
            setattr(route, key, value)
    else:
        if fields.intersection(gateway_fields):
            try:
                apply_route_gateway_update(
                    route,
                    model_use=model.use,
                    provider=body.provider if "provider" in fields else route.provider,
                    base_url=body.base_url if "base_url" in fields else route.base_url,
                    api_key=body.api_key if "api_key" in fields else "__keep__",
                    api_key_clear=bool(body.api_key_clear),
                    gateway_format=(
                        body.gateway_format
                        if "gateway_format" in fields
                        else route.gateway_format
                    ),
                )
            except ModelGatewayConfigError as exc:
                raise HTTPException(400, str(exc)) from exc
        if "model_id" in fields:
            route.model_id = body.model_id
        if "extra" in fields:
            route.extra = validate_route_extra(body.extra)
        if fields.intersection(custom_config_fields):
            route.managed_by_model_config = False
        elif body.managed_by_model_config is not None:
            route.managed_by_model_config = body.managed_by_model_config

    for field in (
        "name",
        "priority",
        "enabled",
        "failure_threshold",
        "window_seconds",
        "cooldown_seconds",
    ):
        if field in fields:
            setattr(route, field, getattr(body, field))
    version = publish_route_projection(db, route)
    audit.log_required(
        db,
        user_id=admin.id,
        action="update_model_route",
        biz_type="model_route",
        biz_id=route.id,
        ip=get_client_ip(request),
        detail={"fields": sorted(fields), "published_version": version.version},
    )
    _commit(db, "模型路由发生并发更新")
    db.refresh(route)
    return route_admin_dict(route, version)


@router.get("/models/{model_config_id}/routes/{route_id}/versions")
def model_route_versions(
    model_config_id: int,
    route_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    _model, route = _route_for_model(db, model_config_id, route_id)
    active = active_route_version(db, route, create=False)
    rows = route_version_history(db, route)
    return {
        "route": route_admin_dict(route, active),
        "versions": [serialize_route_version(row) for row in rows],
        "total": len(rows),
    }


@router.post("/models/{model_config_id}/routes/{route_id}/versions/{version}/rollback")
def rollback_model_route_catalog_version(
    model_config_id: int,
    route_id: int,
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    route = db.scalar(
        select(ModelRoute)
        .where(ModelRoute.id == route_id, ModelRoute.model_config_id == model.id)
        .with_for_update()
    )
    if route is None:
        raise HTTPException(404, "模型路由不存在")
    _assert_no_active_model_config_tasks(db, model)
    try:
        published = rollback_route_version(db, route, version=version)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit.log_required(
        db,
        user_id=admin.id,
        action="rollback_model_route_version",
        biz_type="model_route",
        biz_id=route.id,
        ip=get_client_ip(request),
        detail={"source_version": version, "published_version": published.version},
    )
    _commit(db, "模型路由版本回滚发生并发冲突，请刷新后重试")
    return route_admin_dict(route, published)


@router.post("/models/{model_config_id}/routes/{route_id}/versions/{version}/retire")
def retire_model_route_catalog_version(
    model_config_id: int,
    route_id: int,
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    _model, route = _route_for_model(db, model_config_id, route_id)
    try:
        retired = retire_route_version(db, route, version=version)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit.log_required(
        db,
        user_id=admin.id,
        action="retire_model_route_version",
        biz_type="model_route",
        biz_id=route.id,
        ip=get_client_ip(request),
        detail={"version": version},
    )
    _commit(db, "模型路由版本退役发生并发冲突，请刷新后重试")
    return serialize_route_version(retired)


@router.post("/models/{model_config_id}/routes/{route_id}/reset-health")
def reset_model_route_health(
    model_config_id: int,
    route_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model, route = _route_for_model(db, model_config_id, route_id)
    reset_route_health(route)
    audit.log_required(
        db,
        user_id=admin.id,
        action="reset_model_route_health",
        biz_type="model_route",
        biz_id=route.id,
        ip=get_client_ip(request),
        detail={"model_config_id": model.id},
    )
    _commit(db, "模型路由健康状态重置发生并发冲突,请刷新后重试")
    return route_admin_dict(route, active_route_version(db, route))


@router.post("/models/{model_config_id}/routes/{route_id}/probe")
def probe_model_route(
    model_config_id: int,
    route_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model, route = _route_for_model(db, model_config_id, route_id)
    runtime_model = route_runtime_model(model, route)
    started = time.monotonic()
    try:
        config = runtime_config_for_model(runtime_model, model.use)
        models = gateway.list_models(config)
    except (ModelGatewayConfigError, gateway.GatewayError) as exc:
        route = db.scalar(
            select(ModelRoute).where(ModelRoute.id == route.id).with_for_update()
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        error_code = getattr(exc, "error_code", None) or type(exc).__name__
        counts_toward_circuit = error_counts_toward_circuit(exc)
        _record_route_probe_outcome(
            db,
            route,
            success=False,
            latency_ms=latency_ms,
            error_code=error_code,
            counts_toward_circuit=counts_toward_circuit,
        )
        audit.log_required(
            db,
            user_id=admin.id,
            action="probe_model_route",
            biz_type="model_route",
            biz_id=route.id,
            ip=get_client_ip(request),
            detail={
                "model_config_id": model.id,
                "ok": False,
                "latency_ms": latency_ms,
                "error_code": _route_probe_error_code(error_code),
                "counts_toward_circuit": counts_toward_circuit,
            },
        )
        _commit(db, "模型路由探测结果发生并发冲突,请刷新后重试")
        raise HTTPException(400, str(exc)) from exc
    route = db.scalar(
        select(ModelRoute).where(ModelRoute.id == route.id).with_for_update()
    )
    latency_ms = round((time.monotonic() - started) * 1000)
    _record_route_probe_outcome(
        db,
        route,
        success=True,
        latency_ms=latency_ms,
    )
    audit.log_required(
        db,
        user_id=admin.id,
        action="probe_model_route",
        biz_type="model_route",
        biz_id=route.id,
        ip=get_client_ip(request),
        detail={
            "model_config_id": model.id,
            "ok": True,
            "latency_ms": latency_ms,
            "model_count": len(models),
        },
    )
    _commit(db, "模型路由探测结果发生并发冲突,请刷新后重试")
    return {"ok": True, "route_id": route.id, "models": models}


@router.get("/models/{model_config_id}/routes/{route_id}/health-events")
def model_route_health_events(
    model_config_id: int,
    route_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    _model, route = _route_for_model(db, model_config_id, route_id)
    rows = list(
        db.scalars(
            select(ModelRouteHealthEvent)
            .where(ModelRouteHealthEvent.route_id == route.id)
            .order_by(ModelRouteHealthEvent.id.desc())
            .limit(max(1, min(int(limit), 200)))
        )
    )
    return {
        "items": [
            {
                "id": row.id,
                "operation": row.operation,
                "outcome": row.outcome,
                "counts_toward_circuit": row.counts_toward_circuit,
                "latency_ms": row.latency_ms,
                "error_code": row.error_code,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/models/{model_config_id}/versions")
def model_versions(
    model_config_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = db.get(ModelConfig, model_config_id)
    if row is None:
        raise HTTPException(404, "模型配置不存在")
    return model_catalog_detail(db, row, history=True)


@router.post("/models/{model_config_id}/versions/activate")
def activate_model_version(
    model_config_id: int,
    body: ModelVersionActivateIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    _assert_no_active_model_config_tasks(db, model)
    try:
        target = rollback_model_version(
            db,
            model,
            kind=body.kind,
            version=body.version,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit.log_required(
        db,
        user_id=admin.id,
        action="rollback_model_catalog_version",
        biz_type="model_config",
        biz_id=model.id,
        ip=get_client_ip(request),
        detail={
            "kind": body.kind,
            "source_version": body.version,
            "published_version": target.version,
        },
    )
    _commit(db, "模型版本回滚发生并发冲突，请刷新后重试")
    db.refresh(model)
    return model_catalog_detail(db, model, history=True)


@router.post("/models/{model_config_id}/versions", status_code=201)
def create_model_version(
    model_config_id: int,
    body: ModelVersionDraftCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    try:
        row = create_model_version_draft(
            db,
            model,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit.log_required(
        db,
        user_id=admin.id,
        action="create_model_catalog_version_draft",
        biz_type="model_config",
        biz_id=model.id,
        ip=get_client_ip(request),
        detail={"kind": body.kind, "version": row.version},
    )
    _commit(db, "模型版本草稿创建发生并发冲突，请刷新后重试")
    db.refresh(model)
    return model_catalog_detail(db, model, history=True)


@router.patch("/models/{model_config_id}/versions/{kind}/{version}")
def patch_model_version(
    model_config_id: int,
    kind: Literal["capability", "price"],
    version: int,
    body: ModelVersionDraftPatchIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    try:
        row = update_model_version_draft(
            db,
            model,
            kind=kind,
            version=version,
            values=body.model_dump(exclude_unset=True),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit.log_required(
        db,
        user_id=admin.id,
        action="update_model_catalog_version_draft",
        biz_type="model_config",
        biz_id=model.id,
        ip=get_client_ip(request),
        detail={"kind": kind, "version": row.version},
    )
    _commit(db, "模型版本草稿更新发生并发冲突，请刷新后重试")
    db.refresh(model)
    return model_catalog_detail(db, model, history=True)


def _model_version_action(
    *,
    model_config_id: int,
    kind: Literal["capability", "price"],
    version: int,
    action: Literal["publish", "disable", "retire", "rollback"],
    request: Request,
    db: Session,
    admin: User,
):
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    if action in {"publish", "disable", "rollback"}:
        _assert_no_active_model_config_tasks(db, model)
    operation = {
        "publish": publish_model_version,
        "disable": disable_model_version,
        "retire": retire_model_version,
        "rollback": rollback_model_version,
    }[action]
    try:
        row = operation(db, model, kind=kind, version=version)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    labels = {
        "publish": "发布",
        "disable": "停用",
        "retire": "退役",
        "rollback": "回滚",
    }
    audit.log_required(
        db,
        user_id=admin.id,
        action=f"{action}_model_catalog_version",
        biz_type="model_config",
        biz_id=model.id,
        ip=get_client_ip(request),
        detail={
            "kind": kind,
            "source_version": version,
            "result_version": row.version,
        },
    )
    _commit(db, f"模型版本{labels[action]}发生并发冲突，请刷新后重试")
    db.refresh(model)
    return model_catalog_detail(db, model, history=True)


@router.post("/models/{model_config_id}/versions/{kind}/{version}/publish")
def publish_model_catalog_version(
    model_config_id: int,
    kind: Literal["capability", "price"],
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _model_version_action(
        model_config_id=model_config_id,
        kind=kind,
        version=version,
        action="publish",
        request=request,
        db=db,
        admin=admin,
    )


@router.post("/models/{model_config_id}/versions/{kind}/{version}/disable")
def disable_model_catalog_version(
    model_config_id: int,
    kind: Literal["capability", "price"],
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _model_version_action(
        model_config_id=model_config_id,
        kind=kind,
        version=version,
        action="disable",
        request=request,
        db=db,
        admin=admin,
    )


@router.post("/models/{model_config_id}/versions/{kind}/{version}/retire")
def retire_model_catalog_version(
    model_config_id: int,
    kind: Literal["capability", "price"],
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _model_version_action(
        model_config_id=model_config_id,
        kind=kind,
        version=version,
        action="retire",
        request=request,
        db=db,
        admin=admin,
    )


@router.post("/models/{model_config_id}/versions/{kind}/{version}/rollback")
def rollback_model_catalog_version(
    model_config_id: int,
    kind: Literal["capability", "price"],
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _model_version_action(
        model_config_id=model_config_id,
        kind=kind,
        version=version,
        action="rollback",
        request=request,
        db=db,
        admin=admin,
    )


@router.get("/tools")
def admin_tools(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = list(
        db.scalars(
            select(ToolDefinition).order_by(
                ToolDefinition.category,
                ToolDefinition.sort_order,
                ToolDefinition.id,
            )
        )
    )
    return {"items": [_admin_tool_detail(db, row) for row in rows], "total": len(rows)}


@router.post("/tools", status_code=201)
def create_tool(
    body: ToolDefinitionCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    values = body.model_dump(exclude={"initial_version"})
    row = ToolDefinition(**values)
    db.add(row)
    try:
        db.flush()
        version = ToolVersion(
            tool_definition_id=row.id,
            version=1,
            **body.initial_version.model_dump(),
            metadata_snapshot=tool_metadata_snapshot(row),
            status="published",
            is_active=True,
            activated_at=datetime.now(timezone.utc),
        )
        db.add(version)
        audit.log_required(
            db,
            user_id=admin.id,
            action="create_tool_definition",
            biz_type="tool_definition",
            biz_id=row.id,
            ip=get_client_ip(request),
            detail={"slug": row.slug, "version": 1},
        )
        _commit(db, "工具标识已存在或目录发生并发更新")
    except HTTPException:
        raise
    db.refresh(row)
    return _admin_tool_detail(db, row, history=True)


@router.patch("/tools/{tool_id}")
def patch_tool(
    tool_id: int,
    body: ToolDefinitionPatchIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = db.scalar(
        select(ToolDefinition).where(ToolDefinition.id == tool_id).with_for_update()
    )
    if row is None:
        raise HTTPException(404, "工具不存在")
    values = body.model_dump(exclude_unset=True)
    before = {key: getattr(row, key) for key in values}
    for key, value in values.items():
        setattr(row, key, value)
    changed = any(before[key] != getattr(row, key) for key in values)
    published = None
    if changed:
        versions = _locked_tool_versions(db, row)
        published = _publish_tool_metadata_change(db, row, versions)
    audit.log_required(
        db,
        user_id=admin.id,
        action="update_tool_definition",
        biz_type="tool_definition",
        biz_id=row.id,
        ip=get_client_ip(request),
        detail={
            "before": before,
            "after": values,
            "published_version": int(published.version) if published is not None else None,
        },
    )
    _commit(db, "工具标识已存在或目录发生并发更新")
    db.refresh(row)
    return _admin_tool_detail(db, row, history=True)


@router.get("/tools/{tool_id}/versions")
def tool_versions(
    tool_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = db.get(ToolDefinition, tool_id)
    if row is None:
        raise HTTPException(404, "工具不存在")
    return _admin_tool_detail(db, row, history=True)


@router.post("/tools/{tool_id}/versions", status_code=201)
def create_tool_version(
    tool_id: int,
    body: ToolVersionPayloadIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    tool = _lock_tool(db, tool_id)
    versions = _locked_tool_versions(db, tool)
    next_version = max((int(row.version) for row in versions), default=0) + 1
    version = ToolVersion(
        tool_definition_id=tool.id,
        version=next_version,
        **body.model_dump(),
        metadata_snapshot=tool_metadata_snapshot(tool),
        status="draft",
        is_active=False,
        activated_at=None,
    )
    db.add(version)
    audit.log_required(
        db,
        user_id=admin.id,
        action="create_tool_version_draft",
        biz_type="tool_definition",
        biz_id=tool.id,
        ip=get_client_ip(request),
        detail={"version": next_version},
    )
    _commit(db, "工具版本发生并发更新，请刷新后重试")
    db.refresh(tool)
    return _admin_tool_detail(db, tool, history=True)


@router.patch("/tools/{tool_id}/versions/{version_number}")
def patch_tool_version(
    tool_id: int,
    version_number: int,
    body: ToolVersionPayloadIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    tool = _lock_tool(db, tool_id)
    versions = _locked_tool_versions(db, tool)
    target = _tool_version_row(versions, version_number)
    if target.status != "draft" or target.is_active:
        raise HTTPException(409, "只有草稿工具版本可以编辑")
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(400, "至少提供一个工具版本字段")
    for field, value in values.items():
        setattr(target, field, deepcopy(value))
    audit.log_required(
        db,
        user_id=admin.id,
        action="update_tool_version_draft",
        biz_type="tool_definition",
        biz_id=tool.id,
        ip=get_client_ip(request),
        detail={"version": int(target.version), "fields": sorted(values)},
    )
    _commit(db, "工具版本草稿更新发生并发冲突，请刷新后重试")
    db.refresh(tool)
    return _admin_tool_detail(db, tool, history=True)


def _tool_version_action(
    *,
    tool_id: int,
    version_number: int,
    action: Literal["publish", "disable", "retire", "rollback"],
    request: Request,
    db: Session,
    admin: User,
):
    tool = _lock_tool(db, tool_id)
    versions = _locked_tool_versions(db, tool)
    target = _tool_version_row(versions, version_number)
    published: ToolVersion | None = None
    if action == "publish":
        published = _publish_tool_version_draft(db, tool, versions, target)
    elif action == "disable":
        if target.status != "published" or not target.is_active:
            raise HTTPException(409, "只有当前发布的工具版本可以停用")
        if tool.enabled:
            raise HTTPException(409, "请先在工具管理中停用该工具，再停用当前版本")
        target.is_active = False
        target.status = "disabled"
        target.disabled_at = datetime.now(timezone.utc)
        db.flush()
    elif action == "retire":
        if target.is_active:
            raise HTTPException(409, "当前工具版本不能退役")
        if target.status == "retired":
            pass
        elif target.status in {"draft", "disabled"}:
            target.status = "retired"
            target.retired_at = datetime.now(timezone.utc)
            db.flush()
        else:
            raise HTTPException(409, "只有草稿或已停用工具版本可以退役")
    else:
        published = _rollback_tool_version_copy(db, tool, versions, target)

    audit.log_required(
        db,
        user_id=admin.id,
        action=f"{action}_tool_version",
        biz_type="tool_definition",
        biz_id=tool.id,
        ip=get_client_ip(request),
        detail={
            "source_version": int(target.version),
            "published_version": int(published.version) if published is not None else None,
        },
    )
    _commit(db, f"工具版本{action}发生并发冲突，请刷新后重试")
    db.refresh(tool)
    return _admin_tool_detail(db, tool, history=True)


@router.post("/tools/{tool_id}/versions/{version_number}/publish")
def publish_tool_catalog_version(
    tool_id: int,
    version_number: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _tool_version_action(
        tool_id=tool_id,
        version_number=version_number,
        action="publish",
        request=request,
        db=db,
        admin=admin,
    )


@router.post("/tools/{tool_id}/versions/{version_number}/disable")
def disable_tool_catalog_version(
    tool_id: int,
    version_number: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _tool_version_action(
        tool_id=tool_id,
        version_number=version_number,
        action="disable",
        request=request,
        db=db,
        admin=admin,
    )


@router.post("/tools/{tool_id}/versions/{version_number}/retire")
def retire_tool_catalog_version(
    tool_id: int,
    version_number: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _tool_version_action(
        tool_id=tool_id,
        version_number=version_number,
        action="retire",
        request=request,
        db=db,
        admin=admin,
    )


@router.post("/tools/{tool_id}/versions/{version_number}/rollback")
def rollback_tool_catalog_version(
    tool_id: int,
    version_number: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return _tool_version_action(
        tool_id=tool_id,
        version_number=version_number,
        action="rollback",
        request=request,
        db=db,
        admin=admin,
    )


@router.post("/tools/{tool_id}/versions/{version_number}/activate")
def activate_tool_version(
    tool_id: int,
    version_number: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    tool = _lock_tool(db, tool_id)
    versions = _locked_tool_versions(db, tool)
    target = _tool_version_row(versions, version_number)
    if target.status == "published" and target.is_active:
        published = target
        _apply_tool_metadata(tool, published)
    elif target.status == "draft":
        published = _publish_tool_version_draft(db, tool, versions, target)
    elif target.status == "disabled" and not target.is_active:
        published = _rollback_tool_version_copy(db, tool, versions, target)
    else:
        raise HTTPException(409, "已退役工具版本不能激活")
    audit.log_required(
        db,
        user_id=admin.id,
        action="activate_tool_version",
        biz_type="tool_definition",
        biz_id=tool.id,
        ip=get_client_ip(request),
        detail={
            "source_version": version_number,
            "published_version": int(published.version),
        },
    )
    _commit(db, "工具版本回滚发生并发冲突，请刷新后重试")
    db.refresh(tool)
    return _admin_tool_detail(db, tool, history=True)
