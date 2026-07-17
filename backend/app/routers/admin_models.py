"""Admin model provider configuration endpoints."""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import GenTask, ModelConfig, ReverseOperation, User
from ..schemas import (
    ModelCatalogImportIn,
    ModelConfigIn,
    ModelConfigPatchIn,
    ModelProbeIn,
)
from ..services import audit, gateway
from ..services.config_store import get_model_config
from ..services.model_discovery import annotate_discovered_models
from ..services.model_gateway_config import (
    PROVIDER_PRESETS,
    ModelGatewayConfigError,
    apply_model_gateway_update,
    apply_saved_model_gateway,
    encrypted_key_present,
    model_to_admin_dict,
    normalise_base_url,
    runtime_config_from_probe,
)
from .admin_helpers import gateway_update_changes_runtime as _gateway_update_changes_runtime
from .admin_helpers import model_audit_snapshot as _model_audit_snapshot
from .admin_helpers import model_gateway_update_values as _model_gateway_update_values

router = APIRouter()

_ACTIVE_TASK_STATUSES = ("queued", "running", "needs_review")
_ACTIVE_REVERSE_STATUSES = ("queued", "running", "needs_confirmation")
_RUNTIME_PATCH_FIELDS = {
    "model_id",
    "provider",
    "base_url",
    "api_key",
    "api_key_clear",
    "gateway_format",
    "enabled",
    "extra",
}


def _admin_model_dict(row: ModelConfig) -> dict:
    return {
        "id": row.id,
        "display_name": row.display_name or row.model_id,
        "is_default": bool(row.is_default),
        "sort_order": int(row.sort_order or 0),
        **model_to_admin_dict(row),
    }


def _provider_connection_dict(row: ModelConfig) -> dict:
    provider = row.provider or "custom_openai"
    preset = PROVIDER_PRESETS.get(provider, {})
    return {
        "id": row.id,
        "provider": provider,
        "label": preset.get("label") or provider,
        "gateway_format": row.gateway_format or preset.get("gateway_format") or "openai",
        "source_model_id": row.model_id,
        "source_display_name": row.display_name or row.model_id,
    }


def _saved_provider_connection(db: Session, provider_config_id: int | None) -> ModelConfig | None:
    if provider_config_id is None:
        return None
    row = db.get(ModelConfig, provider_config_id)
    if row is None:
        raise HTTPException(404, "已有供应商连接不存在")
    if not row.base_url or not encrypted_key_present(row):
        raise HTTPException(400, "所选已有供应商未完整配置 Base URL 和 API Key")
    return row


def _audit_snapshot(row: ModelConfig | None) -> dict:
    snapshot = _model_audit_snapshot(row)
    if row is not None:
        snapshot.update(
            {
                "id": row.id,
                "display_name": row.display_name or row.model_id,
                "is_default": bool(row.is_default),
                "sort_order": int(row.sort_order or 0),
            }
        )
    return snapshot


def _assert_no_active_model_config_tasks(db: Session, row: ModelConfig) -> None:
    task_predicates = [GenTask.model_config_id == row.id]
    if row.is_default:
        task_predicates.append(
            (GenTask.model_config_id.is_(None)) & (GenTask.model_use == row.use)
        )
    active_task = db.execute(
        select(GenTask.id)
        .where(GenTask.status.in_(_ACTIVE_TASK_STATUSES), *task_predicates[:1])
        .limit(1)
    ).scalar_one_or_none()
    if active_task is None and len(task_predicates) > 1:
        active_task = db.execute(
            select(GenTask.id)
            .where(GenTask.status.in_(_ACTIVE_TASK_STATUSES), task_predicates[1])
            .limit(1)
        ).scalar_one_or_none()
    active_reverse = db.execute(
        select(ReverseOperation.id)
        .where(
            ReverseOperation.status.in_(_ACTIVE_REVERSE_STATUSES),
            ReverseOperation.model_config_id == row.id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if active_reverse is None and row.is_default and row.use == "vision":
        active_reverse = db.execute(
            select(ReverseOperation.id)
            .where(
                ReverseOperation.status.in_(_ACTIVE_REVERSE_STATUSES),
                ReverseOperation.model_config_id.is_(None),
            )
            .limit(1)
        ).scalar_one_or_none()
    if active_task is not None or active_reverse is not None:
        raise HTTPException(
            409,
            "所选模型仍有排队、运行中或待处理任务,请等待任务结束后再修改运行配置",
        )


def _set_default(db: Session, row: ModelConfig) -> None:
    # Lock the use group on PostgreSQL and rely on the partial unique index as
    # the final concurrency guard.
    list(
        db.execute(
            select(ModelConfig.id)
            .where(ModelConfig.use == row.use)
            .with_for_update()
        ).scalars()
    )
    db.execute(
        update(ModelConfig)
        .where(ModelConfig.use == row.use, ModelConfig.id != row.id)
        .values(is_default=False)
    )
    row.is_default = True


def _promote_enabled_default(db: Session, use: str, *, exclude_id: int | None = None) -> bool:
    query = select(ModelConfig).where(ModelConfig.use == use, ModelConfig.enabled.is_(True))
    if exclude_id is not None:
        query = query.where(ModelConfig.id != exclude_id)
    candidate = db.execute(
        query.order_by(ModelConfig.sort_order, ModelConfig.id).limit(1)
    ).scalar_one_or_none()
    if candidate is not None:
        _set_default(db, candidate)
        return True
    return False


def _assert_supported_model_protocol(
    use: str,
    provider: str | None,
    gateway_format: str | None,
) -> None:
    if use == "vision" and (provider == "anthropic" or gateway_format == "anthropic"):
        raise HTTPException(
            400,
            "视觉反推不支持 Anthropic 原生协议,请使用 OpenAI-compatible 视觉网关",
        )


def _commit_catalog_change(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "模型目录发生并发更新,请刷新后重试") from exc


@router.get("/models")
def get_models(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = list(
        db.execute(
            select(ModelConfig).order_by(
                ModelConfig.use,
                ModelConfig.is_default.desc(),
                ModelConfig.sort_order,
                ModelConfig.id,
            )
        ).scalars()
    )
    return {
        "providers": PROVIDER_PRESETS,
        "provider_connections": [
            _provider_connection_dict(row)
            for row in rows
            if row.base_url and encrypted_key_present(row)
        ],
        "models": [_admin_model_dict(r) for r in rows],
    }


@router.post("/models", status_code=201)
def create_model(
    body: ModelConfigIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    provider_connection = _saved_provider_connection(db, body.provider_config_id)
    effective_provider = provider_connection.provider if provider_connection is not None else body.provider
    effective_gateway_format = (
        provider_connection.gateway_format if provider_connection is not None else body.gateway_format
    )
    _assert_supported_model_protocol(body.use, effective_provider, effective_gateway_format)
    if body.is_default is True and not body.enabled:
        raise HTTPException(400, "默认模型必须处于启用状态")
    duplicate = db.execute(
        select(ModelConfig.id).where(
            ModelConfig.use == body.use,
            ModelConfig.model_id == body.model_id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(409, "该用途下已存在相同模型标识")
    has_default = db.execute(
        select(ModelConfig.id)
        .where(ModelConfig.use == body.use, ModelConfig.is_default.is_(True))
        .limit(1)
    ).scalar_one_or_none()
    row = ModelConfig(
        use=body.use,
        model_id=body.model_id,
        display_name=body.display_name or body.model_id,
        is_default=False,
        sort_order=body.sort_order,
        cost_credits=body.cost_credits,
        unlock_cost=body.unlock_cost,
        enabled=body.enabled,
        extra=body.extra,
    )
    db.add(row)
    try:
        if provider_connection is not None:
            apply_saved_model_gateway(row, provider_connection)
        else:
            apply_model_gateway_update(
                row,
                provider=body.provider,
                base_url=body.base_url,
                api_key=body.api_key,
                api_key_clear=body.api_key_clear,
                gateway_format=body.gateway_format,
            )
    except ModelGatewayConfigError as e:
        raise HTTPException(400, str(e)) from e
    db.flush()
    if body.is_default is True or has_default is None:
        _set_default(db, row)
    _commit_catalog_change(db)
    db.refresh(row)
    audit.log(
        db,
        user_id=admin.id,
        action="create_model",
        biz_type="admin",
        biz_id=row.id,
        ip=get_client_ip(request) if request else None,
        detail={
            "after": _audit_snapshot(row),
            "provider_config_id": body.provider_config_id,
            "api_key_changed": bool(body.api_key),
        },
    )
    return {"ok": True, "model": _admin_model_dict(row)}


@router.put("/models")
def upsert_model(
    body: ModelConfigIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    provider_connection = _saved_provider_connection(db, body.provider_config_id)
    effective_provider = provider_connection.provider if provider_connection is not None else body.provider
    effective_gateway_format = (
        provider_connection.gateway_format if provider_connection is not None else body.gateway_format
    )
    _assert_supported_model_protocol(body.use, effective_provider, effective_gateway_format)
    if body.is_default is True and not body.enabled:
        raise HTTPException(400, "默认模型必须处于启用状态")
    row = get_model_config(db, body.use)
    before = _audit_snapshot(row)
    if row is None:
        row = ModelConfig(
            use=body.use,
            display_name=body.display_name or body.model_id,
            is_default=True,
            sort_order=body.sort_order,
        )
        db.add(row)
    elif body.provider_config_id is not None or _gateway_update_changes_runtime(row, body):
        _assert_no_active_model_config_tasks(db, row)
    if row.is_default and not body.enabled:
        raise HTTPException(409, "默认模型不能直接停用,请先将同用途的其他模型设为默认")
    try:
        if provider_connection is not None:
            apply_saved_model_gateway(row, provider_connection)
        else:
            provider, base_url, gateway_format = _model_gateway_update_values(row, body)
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
    if body.display_name is not None:
        row.display_name = body.display_name
    elif not row.display_name:
        row.display_name = body.model_id
    row.sort_order = body.sort_order
    row.cost_credits = body.cost_credits
    row.unlock_cost = body.unlock_cost
    row.enabled = body.enabled
    row.extra = body.extra
    if body.is_default is True or not row.is_default:
        _set_default(db, row)
    _commit_catalog_change(db)
    db.refresh(row)
    after = _audit_snapshot(row)
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


@router.patch("/models/{model_config_id}")
def patch_model(
    model_config_id: int,
    body: ModelConfigPatchIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    row = db.get(ModelConfig, model_config_id)
    if row is None:
        raise HTTPException(404, "模型配置不存在")
    fields = body.model_fields_set
    effective_provider = body.provider if "provider" in fields else row.provider
    effective_gateway_format = (
        body.gateway_format if "gateway_format" in fields else row.gateway_format
    )
    _assert_supported_model_protocol(row.use, effective_provider, effective_gateway_format)
    if fields & _RUNTIME_PATCH_FIELDS:
        _assert_no_active_model_config_tasks(db, row)
    if row.is_default and body.enabled is False:
        raise HTTPException(409, "默认模型不能直接停用,请先将同用途的其他模型设为默认")
    effective_enabled = body.enabled if "enabled" in fields else row.enabled
    if body.is_default is True and not effective_enabled:
        raise HTTPException(400, "默认模型必须处于启用状态")
    before = _audit_snapshot(row)
    provider = effective_provider
    base_url = body.base_url if "base_url" in fields else row.base_url
    gateway_format = effective_gateway_format
    try:
        apply_model_gateway_update(
            row,
            provider=provider,
            base_url=base_url,
            api_key=body.api_key if "api_key" in fields else None,
            api_key_clear=body.api_key_clear if "api_key_clear" in fields else False,
            gateway_format=gateway_format,
        )
    except ModelGatewayConfigError as e:
        raise HTTPException(400, str(e)) from e
    for field in (
        "model_id",
        "display_name",
        "cost_credits",
        "unlock_cost",
        "enabled",
        "sort_order",
        "extra",
    ):
        if field in fields:
            setattr(row, field, getattr(body, field))
    if body.is_default is True:
        _set_default(db, row)
    elif body.is_default is False and row.is_default:
        if not _promote_enabled_default(db, row.use, exclude_id=row.id):
            raise HTTPException(409, "每个模型用途必须保留一个已启用的默认模型")
    elif body.enabled is True:
        has_default = db.execute(
            select(ModelConfig.id)
            .where(ModelConfig.use == row.use, ModelConfig.is_default.is_(True))
            .limit(1)
        ).scalar_one_or_none()
        if has_default is None:
            _set_default(db, row)
    _commit_catalog_change(db)
    db.refresh(row)
    audit.log(
        db,
        user_id=admin.id,
        action="update_model",
        biz_type="admin",
        biz_id=row.id,
        ip=get_client_ip(request) if request else None,
        detail={
            "before": before,
            "after": _audit_snapshot(row),
            "api_key_changed": bool("api_key" in fields or body.api_key_clear),
        },
    )
    return {"ok": True, "model": _admin_model_dict(row)}


@router.post("/models/probe")
def probe_models(
    body: ModelProbeIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    fallback = None
    if body.provider_config_id is not None:
        fallback = _saved_provider_connection(db, body.provider_config_id)
    elif body.model_config_id is not None:
        fallback = db.get(ModelConfig, body.model_config_id)
        if fallback is None:
            raise HTTPException(404, "模型配置不存在")
        if body.use and body.use != fallback.use:
            raise HTTPException(400, "探测用途与模型配置不一致")
    elif body.use:
        fallback = get_model_config(db, body.use)
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
        models = annotate_discovered_models(
            gateway.list_models(cfg),
            provider=cfg.provider,
            gateway_format=cfg.gateway_format,
        )
    except (ModelGatewayConfigError, gateway.GatewayError) as e:
        raise HTTPException(400, str(e)) from e
    return {
        "provider": cfg.provider,
        "base_url": cfg.base_url,
        "gateway_format": cfg.gateway_format,
        "models": models,
    }


@router.post("/models/import", status_code=201)
def import_models(
    body: ModelCatalogImportIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    requested_keys = [(item.use, item.model_id) for item in body.models]
    if len(requested_keys) != len(set(requested_keys)):
        raise HTTPException(400, "批量导入中存在重复的用途和模型 ID")
    existing_keys = set(db.execute(select(ModelConfig.use, ModelConfig.model_id)).all())
    conflicts = [f"{use}:{model_id}" for use, model_id in requested_keys if (use, model_id) in existing_keys]
    if conflicts:
        raise HTTPException(409, f"模型已存在: {', '.join(conflicts[:8])}")

    default_uses = set(
        db.execute(
            select(ModelConfig.use).where(ModelConfig.is_default.is_(True))
        ).scalars()
    )
    rows: list[ModelConfig] = []
    try:
        for item in body.models:
            _assert_supported_model_protocol(item.use, body.provider, body.gateway_format)
            row = ModelConfig(
                use=item.use,
                model_id=item.model_id,
                display_name=item.display_name or item.model_id,
                is_default=False,
                sort_order=item.sort_order,
                cost_credits=item.cost_credits,
                unlock_cost=item.unlock_cost,
                enabled=item.enabled,
                extra=item.extra,
            )
            apply_model_gateway_update(
                row,
                provider=body.provider,
                base_url=body.base_url,
                api_key=body.api_key,
                api_key_clear=False,
                gateway_format=body.gateway_format,
            )
            db.add(row)
            rows.append(row)
        db.flush()
        for row in rows:
            if row.use not in default_uses and row.enabled:
                _set_default(db, row)
                default_uses.add(row.use)
        _commit_catalog_change(db)
    except ModelGatewayConfigError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise

    audit.log(
        db,
        user_id=admin.id,
        action="import_models",
        biz_type="admin",
        ip=get_client_ip(request) if request else None,
        detail={
            "provider": body.provider,
            "count": len(rows),
            "models": [
                {"id": row.id, "use": row.use, "model_id": row.model_id}
                for row in rows
            ],
            "api_key_changed": True,
        },
    )
    return {"ok": True, "models": [_admin_model_dict(row) for row in rows]}
