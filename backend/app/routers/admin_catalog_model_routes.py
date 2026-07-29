"""Model-route administration endpoints."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    ModelConfig,
    ModelRoute,
    ModelRouteHealthEvent,
    User,
)
from ..schemas import (
    ModelRouteCreateIn,
    ModelRoutePatchIn,
)
from ..services import audit, gateway
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
from .admin_catalog_shared import (
    _commit,
    _record_route_probe_outcome,
    _route_for_model,
    _route_probe_error_code,
)
from .admin_models import _assert_no_active_model_config_tasks

router = APIRouter()


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
            .where(
                ModelRoute.model_config_id == model.id,
                ModelRoute.deleted_at.is_(None),
            )
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
        .where(
            ModelRoute.id == route_id,
            ModelRoute.model_config_id == model.id,
            ModelRoute.deleted_at.is_(None),
        )
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


@router.delete("/models/{model_config_id}/routes/{route_id}")
def delete_model_route(
    model_config_id: int,
    route_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """软删模型路由：保留历史引用，同时释放 route_key 供重建使用。"""
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    route = db.scalar(
        select(ModelRoute)
        .where(
            ModelRoute.id == route_id,
            ModelRoute.model_config_id == model.id,
            ModelRoute.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if route is None:
        raise HTTPException(404, "模型路由不存在")
    if route.route_key == "legacy-default":
        raise HTTPException(409, "默认兼容路由不能删除,请在模型配置中停用该模型")
    _assert_no_active_model_config_tasks(db, model)
    route.enabled = False
    route.deleted_at = datetime.now(timezone.utc)
    audit.log_required(
        db,
        user_id=admin.id,
        action="delete_model_route",
        biz_type="model_route",
        biz_id=route.id,
        ip=get_client_ip(request),
        detail={
            "model_config_id": model.id,
            "route_key": route.route_key,
            "soft_deleted": True,
        },
    )
    _commit(db, "模型路由删除发生并发冲突,请刷新后重试")
    return {"ok": True}


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
        .where(
            ModelRoute.id == route_id,
            ModelRoute.model_config_id == model.id,
            ModelRoute.deleted_at.is_(None),
        )
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
