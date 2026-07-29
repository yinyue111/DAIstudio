"""Tool-version administration endpoints."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    ToolDefinition,
    ToolVersion,
    User,
)
from ..schemas import (
    ToolVersionPayloadIn,
)
from ..services import audit
from ..services.catalog_metadata import (
    tool_metadata_snapshot,
)
from .admin_catalog_shared import (
    _admin_tool_detail,
    _apply_tool_metadata,
    _commit,
    _lock_tool,
    _locked_tool_versions,
    _publish_tool_version_draft,
    _rollback_tool_version_copy,
    _tool_version_row,
)

router = APIRouter()


@router.get("/tools/{tool_id}/versions")
def tool_versions(
    tool_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = db.get(ToolDefinition, tool_id)
    if row is None or row.deleted_at is not None:
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
