"""Admin moderation queue for public Creation Recipes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import CreationRecipe, User
from ..schemas import AdminCreationRecipeReviewIn, CreationRecipeOut
from ..services import audit
from ..services.content_safety import assert_text_allowed
from .recipes import _current_version, _serialize

router = APIRouter(prefix="/recipes", tags=["admin-recipes"])


@router.get("/reviews", response_model=list[CreationRecipeOut])
def list_recipe_reviews(
    status: str = "pending",
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if status not in {"draft", "pending", "approved", "rejected", "all"}:
        raise HTTPException(400, "配方审核状态非法")
    query = select(CreationRecipe).where(CreationRecipe.deleted_at.is_(None))
    if status != "all":
        query = query.where(CreationRecipe.moderation_status == status)
    rows = list(
        db.execute(
            query.order_by(
                CreationRecipe.submitted_at.desc(),
                CreationRecipe.updated_at.desc(),
                CreationRecipe.id.desc(),
            )
            .limit(min(max(int(limit), 1), 200))
            .offset(max(int(offset), 0))
        ).scalars()
    )
    return [_serialize(db, row) for row in rows]


@router.post("/{recipe_id}/review", response_model=CreationRecipeOut)
def review_recipe(
    recipe_id: int,
    body: AdminCreationRecipeReviewIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    row = db.execute(
        select(CreationRecipe)
        .where(
            CreationRecipe.id == recipe_id,
            CreationRecipe.deleted_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "创作配方不存在")
    if row.moderation_status != "pending":
        raise HTTPException(409, "创作配方不在待审核状态")
    version = _current_version(db, row)
    if version is None:
        raise HTTPException(409, "当前配方版本不存在")
    now = datetime.now(timezone.utc)
    if body.action == "approve":
        if row.visibility != "public":
            raise HTTPException(409, "私有配方不能审核为公开")
        assert_text_allowed(db, row.title, row.cover_asset_url, version.payload)
        row.moderation_status = "approved"
        row.approved_version = int(row.current_version)
    else:
        row.moderation_status = "rejected"
        row.approved_version = None
    row.reviewed_by = admin.id
    row.reviewed_at = now
    row.review_note = body.note
    row.updated_at = now
    audit.log_required(
        db,
        user_id=admin.id,
        action=f"{body.action}_creation_recipe",
        biz_type="creation_recipe",
        biz_id=row.id,
        ip=get_client_ip(request),
        detail={
            "owner_user_id": row.user_id,
            "version": row.current_version,
            "moderation_status": row.moderation_status,
            "note": body.note,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)
