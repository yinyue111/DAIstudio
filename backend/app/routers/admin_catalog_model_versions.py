"""Model capability and price-version administration endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    ModelConfig,
    User,
)
from ..schemas import (
    ModelVersionActivateIn,
    ModelVersionDraftCreateIn,
    ModelVersionDraftPatchIn,
)
from ..services import audit
from ..services.catalog import model_catalog_detail
from ..services.model_versions import (
    create_model_version_draft,
    disable_model_version,
    publish_model_version,
    retire_model_version,
    rollback_model_version,
    update_model_version_draft,
)
from .admin_catalog_shared import _commit
from .admin_models import _assert_no_active_model_config_tasks

router = APIRouter()


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
