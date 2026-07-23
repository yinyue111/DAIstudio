"""Authenticated public model and tool catalogs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import ModelConfig, ToolDefinition, User
from ..services.catalog import app_navigation_catalog, model_catalog_detail, tool_catalog_detail
from ..services.config_store import get_setting

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/navigation")
def get_navigation(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return app_navigation_catalog(
        is_admin=bool(user.is_admin),
        navigation_states=get_setting(db, "navigation_states", {}),
    )


@router.get("/models")
def list_models(
    use: str | None = Query(default=None, pattern="^(vision|image|video|prompt)$"),
    q: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = select(ModelConfig).where(ModelConfig.enabled.is_(True))
    if use:
        query = query.where(ModelConfig.use == use)
    search = str(q or "").strip()
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                ModelConfig.display_name.ilike(pattern),
                ModelConfig.model_id.ilike(pattern),
                ModelConfig.provider.ilike(pattern),
            )
        )
    rows = list(
        db.scalars(
            query.order_by(
                ModelConfig.use,
                ModelConfig.is_default.desc(),
                ModelConfig.sort_order,
                ModelConfig.id,
            )
        )
    )
    return {"items": [model_catalog_detail(db, row) for row in rows], "total": len(rows)}


@router.get("/models/{model_config_id}")
def get_model(
    model_config_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row = db.get(ModelConfig, model_config_id)
    if row is None or not row.enabled:
        raise HTTPException(404, "模型不存在或未启用")
    return model_catalog_detail(db, row)


@router.get("/tools")
def list_tools(
    category: str | None = Query(
        default=None,
        pattern="^(image|video|workflow|utility)$",
    ),
    featured: bool | None = None,
    q: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = select(ToolDefinition).where(ToolDefinition.enabled.is_(True))
    if category:
        query = query.where(ToolDefinition.category == category)
    if featured is not None:
        query = query.where(ToolDefinition.featured.is_(featured))
    search = str(q or "").strip()
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                ToolDefinition.name.ilike(pattern),
                ToolDefinition.slug.ilike(pattern),
                ToolDefinition.description.ilike(pattern),
            )
        )
    rows = list(
        db.scalars(
            query.order_by(
                ToolDefinition.featured.desc(),
                ToolDefinition.sort_order,
                ToolDefinition.id,
            )
        )
    )
    return {"items": [tool_catalog_detail(db, row) for row in rows], "total": len(rows)}


@router.get("/tools/{slug}")
def get_tool(
    slug: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row = db.scalar(
        select(ToolDefinition).where(
            ToolDefinition.slug == slug,
            ToolDefinition.enabled.is_(True),
        )
    )
    if row is None:
        raise HTTPException(404, "工具不存在或未启用")
    return tool_catalog_detail(db, row)
