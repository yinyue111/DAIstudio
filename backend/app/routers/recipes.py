"""Versioned creation recipes restored into the shared Studio workflow."""
from __future__ import annotations

import secrets
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import (
    CreationRecipe,
    CreationRecipeShare,
    CreationRecipeUsageEvent,
    CreationRecipeVersion,
    GenTask,
    ReverseOperation,
    User,
)
from ..schemas import (
    CreationRecipeIn,
    CreationRecipeOut,
    CreationRecipeSharedOut,
    CreationRecipeShareIn,
    CreationRecipeShareOut,
    CreationRecipeUsageEventOut,
    CreationRecipeUsageIn,
    CreationRecipeUsageSummaryOut,
    CreationRecipeVersionIn,
    CreationRecipeVersionOut,
    validate_creation_recipe_payload,
)
from ..services import audit  # noqa: F401 - legacy monkeypatch surface
from ..services import recipe_lifecycle as _lifecycle
from ..services.content_safety import assert_text_allowed

router = APIRouter(prefix="/api/recipes", tags=["recipes"])

# Compatibility aliases: callers and tests historically imported these helpers
# from the router module. Their implementation now lives in the service layer.
_MAX_PUBLIC_COLLECTION_ITEMS = _lifecycle._MAX_PUBLIC_COLLECTION_ITEMS
_MAX_PUBLIC_PAYLOAD_DEPTH = _lifecycle._MAX_PUBLIC_PAYLOAD_DEPTH
_MAX_PUBLIC_PAYLOAD_NODES = _lifecycle._MAX_PUBLIC_PAYLOAD_NODES
_MAX_PUBLIC_STRING_LENGTH = _lifecycle._MAX_PUBLIC_STRING_LENGTH
_apply_recipe_version_metadata = _lifecycle._apply_recipe_version_metadata
_audit_recipe_change = _lifecycle._audit_recipe_change
_contains_url_uri_or_secret = _lifecycle._contains_url_uri_or_secret
_current_version = _lifecycle._current_version
_invalidate_moderation = _lifecycle._invalidate_moderation
_moderation_is_current = _lifecycle._moderation_is_current
_normalize_cover_url = _lifecycle._normalize_cover_url
_owned_recipe = _lifecycle._owned_recipe
_public_recipe = _lifecycle._public_recipe
_public_recipe_payload = _lifecycle._public_recipe_payload
_recipe_audit_snapshot = _lifecycle._recipe_audit_snapshot
_recipe_version_metadata = _lifecycle._recipe_version_metadata
_record_usage = _lifecycle._record_usage
_resolve_usage_access = _lifecycle._resolve_usage_access
_revoke_active_shares = _lifecycle._revoke_active_shares
_serialize = _lifecycle._serialize
_serialize_share = _lifecycle._serialize_share
_share_by_slug = _lifecycle._share_by_slug
_usage_stats_map = _lifecycle._usage_stats_map
_version = _lifecycle._version
_with_catalog_version_snapshot = _lifecycle._with_catalog_version_snapshot

class CreationRecipeMetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=128)
    visibility: Literal["private", "public"] | None = None
    cover_asset_url: str | None = Field(default=None, max_length=4096)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("配方标题不能为空")
        return normalized

    @field_validator("cover_asset_url")
    @classmethod
    def _cover(cls, value: str | None) -> str | None:
        return _normalize_cover_url(value)

    @model_validator(mode="after")
    def _has_update(self):
        if not self.model_fields_set:
            raise ValueError("请至少提交一个可修改字段")
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("配方标题不能为空")
        if "visibility" in self.model_fields_set and self.visibility is None:
            raise ValueError("配方可见性不能为空")
        return self


class CreationRecipeCloneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, gt=0)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    share_slug: str | None = Field(
        default=None,
        min_length=20,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("配方标题不能为空")
        return normalized

    @field_validator("share_slug")
    @classmethod
    def _share_slug(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


@router.get("", response_model=list[CreationRecipeOut])
def list_recipes(
    category: str | None = None,
    favorite: bool | None = None,
    q: str = Query(default="", max_length=128),
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(CreationRecipe).where(
        CreationRecipe.user_id == user.id,
        CreationRecipe.deleted_at.is_(None),
    )
    if category in {"image", "video"}:
        query = query.where(CreationRecipe.category == category)
    if favorite is not None:
        query = query.where(CreationRecipe.favorite.is_(bool(favorite)))
    search = q.strip()
    if search:
        query = query.where(CreationRecipe.title.ilike(f"%{search}%"))
    rows = list(db.execute(
        query.order_by(
            CreationRecipe.favorite.desc(),
            CreationRecipe.updated_at.desc(),
            CreationRecipe.id.desc(),
        )
        .limit(min(max(int(limit), 1), 200))
        .offset(max(int(offset), 0))
    ).scalars())
    usage_stats = _usage_stats_map(db, [int(row.id) for row in rows])
    return [_serialize(db, row, usage=usage_stats[int(row.id)]) for row in rows]


@router.post("", response_model=CreationRecipeOut)
def create_recipe(
    body: CreationRecipeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    payload = _with_catalog_version_snapshot(db, body.payload)
    try:
        cover_asset_url = _normalize_cover_url(body.cover_asset_url)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    assert_text_allowed(db, body.title, cover_asset_url, payload)
    if body.source_operation_id is not None:
        operation = db.get(ReverseOperation, body.source_operation_id)
        if operation is None or int(operation.user_id) != int(user.id):
            raise HTTPException(404, "来源反推任务不存在")
        if operation.status != "succeeded":
            raise HTTPException(409, "来源反推任务尚未成功")
        if operation.target in {"image", "video"} and operation.target != body.category:
            raise HTTPException(422, "配方类型与来源反推任务不一致")
    row = CreationRecipe(
        user_id=user.id,
        source_operation_id=body.source_operation_id,
        title=body.title,
        category=body.category,
        visibility=body.visibility,
        moderation_status="draft",
        favorite=body.favorite,
        current_version=1,
        cover_asset_url=cover_asset_url,
    )
    db.add(row)
    db.flush()
    db.add(CreationRecipeVersion(
        recipe_id=row.id,
        version=1,
        schema_version="creation-recipe.v1",
        payload=payload,
        metadata_snapshot=_recipe_version_metadata(row),
    ))
    _audit_recipe_change(
        db,
        request,
        user,
        "create_creation_recipe",
        row,
        detail={"version": 1},
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.get("/public", response_model=list[CreationRecipeOut])
def list_public_recipes(
    category: str | None = None,
    q: str = Query(default="", max_length=128),
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = select(CreationRecipe).where(
        CreationRecipe.deleted_at.is_(None),
        CreationRecipe.visibility == "public",
        CreationRecipe.moderation_status == "approved",
        CreationRecipe.approved_version == CreationRecipe.current_version,
    )
    if category in {"image", "video"}:
        query = query.where(CreationRecipe.category == category)
    search = q.strip()
    if search:
        query = query.where(CreationRecipe.title.ilike(f"%{search}%"))
    rows = list(db.execute(
        query.order_by(CreationRecipe.updated_at.desc(), CreationRecipe.id.desc())
        .limit(min(max(int(limit), 1), 200))
        .offset(max(int(offset), 0))
    ).scalars())
    usage_stats = _usage_stats_map(db, [int(row.id) for row in rows])
    return [
        _serialize(db, row, public=True, usage=usage_stats[int(row.id)])
        for row in rows
    ]


@router.get("/public/{recipe_id}", response_model=CreationRecipeOut)
def get_public_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
):
    return _serialize(db, _public_recipe(db, recipe_id), public=True)


@router.get("/shared/{slug}", response_model=CreationRecipeSharedOut)
def get_shared_recipe(
    slug: str,
    db: Session = Depends(get_db),
):
    share = _share_by_slug(db, slug)
    row = db.get(CreationRecipe, share.recipe_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, "配方分享不存在或已失效")
    version = _version(db, row, int(share.version))
    return {
        "share": _serialize_share(share),
        "recipe": _serialize(db, row, public=True, version=version),
    }


@router.get("/{recipe_id}", response_model=CreationRecipeOut)
def get_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _serialize(db, _owned_recipe(db, recipe_id, user.id))


@router.patch("/{recipe_id}", response_model=CreationRecipeOut)
def update_recipe_metadata(
    recipe_id: int,
    body: CreationRecipeMetadataPatch,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    before = _recipe_audit_snapshot(row)
    before_metadata = _recipe_version_metadata(row)
    title = body.title if "title" in body.model_fields_set else row.title
    visibility = (
        body.visibility if "visibility" in body.model_fields_set else row.visibility
    )
    cover_asset_url = (
        body.cover_asset_url
        if "cover_asset_url" in body.model_fields_set
        else row.cover_asset_url
    )
    if visibility == "public":
        version_row = _current_version(db, row)
        if version_row is None:
            raise HTTPException(409, "当前配方版本不存在，无法公开")
        assert_text_allowed(db, title, cover_asset_url, version_row.payload)
    else:
        assert_text_allowed(db, title, cover_asset_url)
    changed_fields = sorted(
        field
        for field in body.model_fields_set
        if {
            "title": title,
            "visibility": visibility,
            "cover_asset_url": cover_asset_url,
        }[field]
        != before_metadata[field]
    )
    if not changed_fields:
        return _serialize(db, row)
    row.title = title
    row.visibility = visibility
    row.cover_asset_url = cover_asset_url
    current = _current_version(db, row)
    if current is None:
        raise HTTPException(409, "当前配方版本不存在，无法修改元数据")
    latest_version = db.scalar(
        select(func.max(CreationRecipeVersion.version)).where(
            CreationRecipeVersion.recipe_id == row.id,
        )
    )
    next_version = int(latest_version or 0) + 1
    db.add(CreationRecipeVersion(
        recipe_id=row.id,
        version=next_version,
        schema_version=current.schema_version,
        payload=deepcopy(current.payload),
        metadata_snapshot=_recipe_version_metadata(row),
    ))
    row.current_version = next_version
    revoked_share_count = _invalidate_moderation(db, row)
    row.updated_at = datetime.now(timezone.utc)
    _audit_recipe_change(
        db,
        request,
        user,
        "update_creation_recipe_metadata",
        row,
        detail={
            "changed_fields": changed_fields,
            "before": before,
            "version": next_version,
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.post("/{recipe_id}/submit-review", response_model=CreationRecipeOut)
def submit_recipe_review(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    if row.visibility != "public":
        raise HTTPException(409, "请先将创作配方设为公开候选")
    if row.moderation_status == "pending" or _moderation_is_current(row):
        return _serialize(db, row)
    version = _current_version(db, row)
    if version is None:
        raise HTTPException(409, "当前配方版本不存在，无法提交审核")
    assert_text_allowed(db, row.title, row.cover_asset_url, version.payload)
    now = datetime.now(timezone.utc)
    row.moderation_status = "pending"
    row.approved_version = None
    row.submitted_at = now
    row.reviewed_at = None
    row.reviewed_by = None
    row.review_note = None
    row.updated_at = now
    revoked_share_count = _revoke_active_shares(db, int(row.id), now=now)
    _audit_recipe_change(
        db,
        request,
        user,
        "submit_creation_recipe_review",
        row,
        detail={
            "version": int(row.current_version),
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.get("/{recipe_id}/shares", response_model=list[CreationRecipeShareOut])
def list_recipe_shares(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    shares = list(
        db.execute(
            select(CreationRecipeShare)
            .where(CreationRecipeShare.recipe_id == row.id)
            .order_by(CreationRecipeShare.id.desc())
        ).scalars()
    )
    return [_serialize_share(share) for share in shares]


@router.post("/{recipe_id}/shares", response_model=CreationRecipeShareOut)
def create_recipe_share(
    recipe_id: int,
    body: CreationRecipeShareIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    if not _moderation_is_current(row):
        raise HTTPException(409, "配方当前版本尚未审核通过，无法创建公开分享")
    version = int(body.version or row.current_version)
    if (
        version != int(row.current_version)
        or version != int(row.approved_version or 0)
    ):
        raise HTTPException(409, "仅可分享当前已审核通过的配方版本")
    version_row = _version(db, row, version)
    assert_text_allowed(db, row.title, row.cover_asset_url, version_row.payload)
    slug = secrets.token_urlsafe(24)
    share = CreationRecipeShare(
        recipe_id=row.id,
        owner_user_id=user.id,
        version=version,
        slug=slug,
        status="active",
        expires_at=body.expires_at,
    )
    db.add(share)
    db.flush()
    _audit_recipe_change(
        db,
        request,
        user,
        "create_creation_recipe_share",
        row,
        detail={
            "share_id": int(share.id),
            "version": int(share.version),
            "expires": share.expires_at is not None,
        },
    )
    db.commit()
    db.refresh(share)
    return _serialize_share(share)


@router.delete(
    "/{recipe_id}/shares/{share_id}",
    response_model=CreationRecipeShareOut,
)
def revoke_recipe_share(
    recipe_id: int,
    share_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    share = db.execute(
        select(CreationRecipeShare).where(
            CreationRecipeShare.id == share_id,
            CreationRecipeShare.recipe_id == row.id,
            CreationRecipeShare.owner_user_id == user.id,
        )
    ).scalar_one_or_none()
    if share is None:
        raise HTTPException(404, "配方分享不存在")
    if share.status == "active":
        share.status = "revoked"
        share.revoked_at = datetime.now(timezone.utc)
        _audit_recipe_change(
            db,
            request,
            user,
            "revoke_creation_recipe_share",
            row,
            detail={"share_id": int(share.id), "version": int(share.version)},
        )
        db.commit()
        db.refresh(share)
    return _serialize_share(share)


@router.post("/{recipe_id}/usage", response_model=CreationRecipeUsageEventOut)
def record_recipe_usage(
    recipe_id: int,
    body: CreationRecipeUsageIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(CreationRecipe, recipe_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, "创作配方不存在")
    version, source, share_id = _resolve_usage_access(db, row, user, body)
    if body.generation_task_id is not None:
        task = db.get(GenTask, body.generation_task_id)
        if task is None or int(task.user_id) != int(user.id):
            raise HTTPException(404, "关联生成任务不存在")
        if task.category != row.category:
            raise HTTPException(422, "生成任务类型与配方不一致")
    return _record_usage(
        db,
        recipe_id=int(row.id),
        recipe_version=int(version.version),
        user_id=int(user.id),
        event_type=body.event_type,
        source=source,
        share_id=share_id,
        generation_task_id=body.generation_task_id,
        client_event_id=body.client_event_id,
        context=body.context,
    )


@router.get("/{recipe_id}/usage", response_model=CreationRecipeUsageSummaryOut)
def recipe_usage_summary(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    grouped = db.execute(
        select(
            CreationRecipeUsageEvent.event_type,
            func.count(CreationRecipeUsageEvent.id),
        )
        .where(CreationRecipeUsageEvent.recipe_id == row.id)
        .group_by(CreationRecipeUsageEvent.event_type)
    ).all()
    total, unique_users, last_used_at = db.execute(
        select(
            func.count(CreationRecipeUsageEvent.id),
            func.count(func.distinct(CreationRecipeUsageEvent.user_id)),
            func.max(CreationRecipeUsageEvent.created_at),
        ).where(CreationRecipeUsageEvent.recipe_id == row.id)
    ).one()
    return {
        "recipe_id": int(row.id),
        "total": int(total or 0),
        "unique_users": int(unique_users or 0),
        "by_event": {str(event_type): int(count) for event_type, count in grouped},
        "last_used_at": last_used_at,
    }


@router.get(
    "/{recipe_id}/versions",
    response_model=list[CreationRecipeVersionOut],
)
def list_recipe_versions(
    recipe_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    return list(db.execute(
        select(CreationRecipeVersion)
        .where(CreationRecipeVersion.recipe_id == row.id)
        .order_by(CreationRecipeVersion.version.desc())
    ).scalars())


@router.get(
    "/{recipe_id}/versions/{version}",
    response_model=CreationRecipeVersionOut,
)
def get_recipe_version(
    recipe_id: int,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id)
    return _version(db, row, version)


@router.post("/{recipe_id}/versions", response_model=CreationRecipeVersionOut)
def create_recipe_version(
    recipe_id: int,
    body: CreationRecipeVersionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    payload = _with_catalog_version_snapshot(db, body.payload)
    try:
        validate_creation_recipe_payload(payload, category=row.category)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    assert_text_allowed(db, payload)
    latest_version = db.execute(
        select(func.max(CreationRecipeVersion.version)).where(
            CreationRecipeVersion.recipe_id == row.id,
        )
    ).scalar_one()
    next_version = int(latest_version or 0) + 1
    version_row = CreationRecipeVersion(
        recipe_id=row.id,
        version=next_version,
        schema_version="creation-recipe.v1",
        payload=payload,
        metadata_snapshot=_recipe_version_metadata(row),
    )
    db.add(version_row)
    row.current_version = next_version
    revoked_share_count = _invalidate_moderation(db, row)
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    _audit_recipe_change(
        db,
        request,
        user,
        "create_creation_recipe_version",
        row,
        detail={
            "version": next_version,
            "version_id": int(version_row.id),
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(version_row)
    return version_row


@router.post(
    "/{recipe_id}/versions/{version}/activate",
    response_model=CreationRecipeOut,
)
def activate_recipe_version(
    recipe_id: int,
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    previous_version = int(row.current_version)
    version_row = _version(db, row, version)
    _apply_recipe_version_metadata(row, version_row)
    if row.visibility == "public":
        assert_text_allowed(db, row.title, row.cover_asset_url, version_row.payload)
    row.current_version = version_row.version
    revoked_share_count = _invalidate_moderation(db, row)
    row.updated_at = datetime.now(timezone.utc)
    _audit_recipe_change(
        db,
        request,
        user,
        "activate_creation_recipe_version",
        row,
        detail={
            "previous_version": previous_version,
            "activated_version": int(version_row.version),
            "version_id": int(version_row.id),
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.post("/{recipe_id}/clone", response_model=CreationRecipeOut)
def clone_recipe(
    recipe_id: int,
    body: CreationRecipeCloneIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.execute(
        select(CreationRecipe)
        .where(
            CreationRecipe.id == recipe_id,
            CreationRecipe.deleted_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(404, "创作配方不存在")
    is_owner = int(source.user_id) == int(user.id)
    share: CreationRecipeShare | None = None
    if body.share_slug:
        share = _share_by_slug(db, body.share_slug, recipe_id=int(source.id))
    selected_version = int(body.version or (share.version if share else source.current_version))
    if share is not None and selected_version != int(share.version):
        raise HTTPException(404, "分享的配方版本不存在")
    if not is_owner and share is None:
        if not _moderation_is_current(source):
            raise HTTPException(404, "创作配方不存在")
        if selected_version != int(source.current_version):
            raise HTTPException(404, "公开创作配方版本不存在")
    source_version = _version(db, source, selected_version)
    payload = (
        deepcopy(source_version.payload)
        if is_owner
        else _public_recipe_payload(source_version.payload)
    )
    payload["derived_from_recipe_id"] = int(source.id)
    payload["derived_from_recipe_version"] = int(source_version.version)
    title = body.title or f"{source.title} 副本"
    if len(title) > 128:
        title = f"{source.title[:123].rstrip()} 副本"
    cover_asset_url = (
        source.cover_asset_url
        if is_owner
        else None
    )
    assert_text_allowed(db, title, cover_asset_url, payload)
    cloned = CreationRecipe(
        user_id=user.id,
        source_operation_id=None,
        title=title,
        category=source.category,
        visibility="private",
        moderation_status="draft",
        favorite=False,
        current_version=1,
        cover_asset_url=cover_asset_url,
    )
    db.add(cloned)
    db.flush()
    db.add(CreationRecipeVersion(
        recipe_id=cloned.id,
        version=1,
        schema_version=source_version.schema_version,
        payload=payload,
        metadata_snapshot=_recipe_version_metadata(cloned),
    ))
    _record_usage(
        db,
        recipe_id=int(source.id),
        recipe_version=int(source_version.version),
        user_id=int(user.id),
        event_type="clone",
        source=("owner" if is_owner and share is None else "share" if share else "public"),
        share_id=int(share.id) if share else None,
        derived_recipe_id=int(cloned.id),
        context={"derived_title": title},
        commit=False,
    )
    _audit_recipe_change(
        db,
        request,
        user,
        "clone_creation_recipe",
        cloned,
        detail={
            "source_recipe_id": int(source.id),
            "source_version": int(source_version.version),
            "source": (
                "owner" if is_owner and share is None else "share" if share else "public"
            ),
            "share_id": int(share.id) if share else None,
        },
    )
    db.commit()
    db.refresh(cloned)
    return _serialize(db, cloned)


@router.post("/{recipe_id}/favorite", response_model=CreationRecipeOut)
def toggle_recipe_favorite(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    previous_favorite = bool(row.favorite)
    row.favorite = not bool(row.favorite)
    row.updated_at = datetime.now(timezone.utc)
    _audit_recipe_change(
        db,
        request,
        user,
        "toggle_creation_recipe_favorite",
        row,
        detail={
            "before": previous_favorite,
            "after": bool(row.favorite),
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize(db, row)


@router.delete("/{recipe_id}")
def delete_recipe(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_recipe(db, recipe_id, user.id, for_update=True)
    version_count = int(db.scalar(
        select(func.count(CreationRecipeVersion.id)).where(
            CreationRecipeVersion.recipe_id == row.id
        )
    ) or 0)
    share_count = int(db.scalar(
        select(func.count(CreationRecipeShare.id)).where(
            CreationRecipeShare.recipe_id == row.id
        )
    ) or 0)
    usage_count = int(db.scalar(
        select(func.count(CreationRecipeUsageEvent.id)).where(
            CreationRecipeUsageEvent.recipe_id == row.id
        )
    ) or 0)
    revoked_share_count = _revoke_active_shares(db, int(row.id))
    row.deleted_at = datetime.now(timezone.utc)
    row.updated_at = row.deleted_at
    _audit_recipe_change(
        db,
        request,
        user,
        "delete_creation_recipe",
        row,
        detail={
            "version_count": version_count,
            "share_count": share_count,
            "usage_count": usage_count,
            "revoked_share_count": revoked_share_count,
        },
    )
    db.commit()
    return {"ok": True}
