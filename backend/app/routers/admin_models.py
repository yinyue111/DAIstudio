"""Admin model provider configuration endpoints."""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import ModelConfig, User
from ..schemas import ModelConfigIn, ModelProbeIn
from ..services import audit, gateway
from ..services.model_gateway_config import (
    PROVIDER_PRESETS,
    ModelGatewayConfigError,
    apply_model_gateway_update,
    model_to_admin_dict,
    normalise_base_url,
    runtime_config_from_probe,
)
from .admin_helpers import assert_no_active_model_tasks as _assert_no_active_model_tasks
from .admin_helpers import gateway_update_changes_runtime as _gateway_update_changes_runtime
from .admin_helpers import model_audit_snapshot as _model_audit_snapshot
from .admin_helpers import model_gateway_update_values as _model_gateway_update_values

router = APIRouter()


@router.get("/models")
def get_models(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = list(db.execute(select(ModelConfig).order_by(ModelConfig.use)).scalars())
    return {
        "providers": PROVIDER_PRESETS,
        "models": [model_to_admin_dict(r) for r in rows],
    }


@router.put("/models")
def upsert_model(
    body: ModelConfigIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    row = db.execute(select(ModelConfig).where(ModelConfig.use == body.use)).scalar_one_or_none()
    before = _model_audit_snapshot(row)
    if row is None:
        row = ModelConfig(use=body.use)
        db.add(row)
    elif _gateway_update_changes_runtime(row, body):
        _assert_no_active_model_tasks(db, body.use)
    provider, base_url, gateway_format = _model_gateway_update_values(row, body)
    try:
        apply_model_gateway_update(
            row,
            provider=provider,
            base_url=base_url,
            api_key=body.api_key,
            api_key_clear=body.api_key_clear,
            gateway_format=gateway_format,
        )
    except ModelGatewayConfigError as e:
        raise HTTPException(400, str(e)) from e
    row.model_id = body.model_id
    row.cost_credits = body.cost_credits
    row.unlock_cost = body.unlock_cost
    row.enabled = body.enabled
    row.extra = body.extra
    db.commit()
    db.refresh(row)
    after = _model_audit_snapshot(row)
    audit.log(
        db,
        user_id=admin.id,
        action="update_model",
        biz_type="admin",
        ip=get_client_ip(request) if request else None,
        detail={
            "use": body.use,
            "before": before,
            "after": after,
            "api_key_changed": bool(body.api_key or body.api_key_clear),
        },
    )
    return {"ok": True}


@router.post("/models/probe")
def probe_models(
    body: ModelProbeIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    fallback = None
    if body.use:
        fallback = db.execute(select(ModelConfig).where(ModelConfig.use == body.use)).scalar_one_or_none()
    requested_base = normalise_base_url(body.base_url)
    requested_host = (urlparse(requested_base).hostname or "").lower() if requested_base else ""
    if requested_host and requested_host in app_config.trusted_egress_host_list:
        raise HTTPException(400, "不能临时探测受信任内网网关,请保存配置后再探测")
    if requested_base and requested_base.lower().startswith("http://"):
        raise HTTPException(400, "不能临时探测非 HTTPS 网关,请使用 HTTPS Base URL")
    try:
        cfg = runtime_config_from_probe(
            use=body.use,
            provider=body.provider,
            base_url=body.base_url,
            api_key=body.api_key,
            gateway_format=body.gateway_format,
            fallback_row=fallback,
        )
        models = gateway.list_models(cfg)
    except (ModelGatewayConfigError, gateway.GatewayError) as e:
        raise HTTPException(400, str(e)) from e
    return {
        "provider": cfg.provider,
        "base_url": cfg.base_url,
        "gateway_format": cfg.gateway_format,
        "models": models,
    }
