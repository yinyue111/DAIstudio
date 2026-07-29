"""Tool-definition administration endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    ToolDefinition,
    ToolRun,
    ToolVersion,
    User,
)
from ..schemas import (
    ToolDefinitionCreateIn,
    ToolDefinitionPatchIn,
)
from ..services import audit
from ..services.catalog_metadata import (
    tool_metadata_snapshot,
)
from .admin_catalog_shared import (
    _admin_tool_detail,
    _commit,
    _disable_active_tool_version,
    _lock_tool,
    _locked_tool_versions,
    _publish_tool_metadata_change,
)

router = APIRouter()


@router.get("/tools")
def admin_tools(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = list(
        db.scalars(
            select(ToolDefinition)
            .where(ToolDefinition.deleted_at.is_(None))
            .order_by(
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
        select(ToolDefinition)
        .where(
            ToolDefinition.id == tool_id,
            ToolDefinition.deleted_at.is_(None),
        )
        .with_for_update()
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


@router.delete("/tools/{tool_id}")
def delete_tool(
    tool_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """软删工具：历史运行记录保留，slug 立即释放供重建使用。"""
    tool = _lock_tool(db, tool_id)
    active_run = db.scalar(
        select(ToolRun.id)
        .where(
            ToolRun.tool_definition_id == int(tool.id),
            ToolRun.status.in_(
                ("queued", "running", "waiting_review", "compensating")
            ),
        )
        .limit(1)
    )
    if active_run is not None:
        raise HTTPException(409, "该工具还有进行中的任务,请等待任务结束后再删除")
    versions = _locked_tool_versions(db, tool)
    now = datetime.now(timezone.utc)
    _disable_active_tool_version(versions, now=now)
    tool.enabled = False
    tool.featured = False
    tool.deleted_at = now
    audit.log_required(
        db,
        user_id=admin.id,
        action="delete_tool_definition",
        biz_type="tool_definition",
        biz_id=tool.id,
        ip=get_client_ip(request),
        detail={"slug": tool.slug, "soft_deleted": True},
    )
    _commit(db, "工具删除发生并发冲突,请刷新后重试")
    return {"ok": True}
