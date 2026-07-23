"""Server-authoritative creation-recipe access and usage attribution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    CreationRecipe,
    CreationRecipeShare,
    CreationRecipeUsageEvent,
    CreationRecipeVersion,
)

RecipeUsageSource = Literal["owner", "public", "share"]


@dataclass(frozen=True)
class RecipeAttribution:
    recipe_id: int
    recipe_version: int
    source: RecipeUsageSource
    share_id: int | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "recipe_version": self.recipe_version,
            "source": self.source,
            "share_id": self.share_id,
        }


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def attribution_from_snapshot(value: object) -> RecipeAttribution | None:
    if not isinstance(value, dict):
        return None
    recipe_id = _positive_id(value.get("recipe_id"))
    recipe_version = _positive_id(value.get("recipe_version"))
    source = value.get("source")
    share_id = _positive_id(value.get("share_id"))
    if recipe_id is None or recipe_version is None or source not in {"owner", "public", "share"}:
        return None
    if (source == "share") != (share_id is not None):
        return None
    return RecipeAttribution(
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        source=source,
        share_id=share_id,
    )


def share_is_active(row: CreationRecipeShare) -> bool:
    if row.status != "active" or row.revoked_at is not None:
        return False
    expires_at = row.expires_at
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    return expires_at > datetime.now(timezone.utc)


def share_by_slug(
    db: Session,
    slug: str,
    *,
    recipe_id: int | None = None,
    version: int | None = None,
) -> CreationRecipeShare:
    query = select(CreationRecipeShare).where(CreationRecipeShare.slug == slug)
    if recipe_id is not None:
        query = query.where(CreationRecipeShare.recipe_id == recipe_id)
    if version is not None:
        query = query.where(CreationRecipeShare.version == version)
    row = db.execute(query).scalar_one_or_none()
    if row is None or not share_is_active(row):
        raise HTTPException(404, "配方分享不存在或已失效")
    recipe = db.get(CreationRecipe, int(row.recipe_id))
    if (
        recipe is None
        or recipe.deleted_at is not None
        or not moderation_is_current(recipe)
        or int(row.version) != int(recipe.current_version)
        or int(row.version) != int(recipe.approved_version or 0)
    ):
        # Historical shares created before moderation enforcement fail closed.
        # Backfilled public recipes remain compatible because migration 0054
        # records their current version as approved.
        raise HTTPException(404, "配方分享不存在或已失效")
    return row


def moderation_is_current(row: CreationRecipe) -> bool:
    return (
        row.deleted_at is None
        and row.visibility == "public"
        and row.moderation_status == "approved"
        and row.approved_version is not None
        and int(row.approved_version) == int(row.current_version)
    )


def recipe_version(
    db: Session,
    row: CreationRecipe,
    version: int | None,
) -> CreationRecipeVersion:
    version_row = db.execute(
        select(CreationRecipeVersion).where(
            CreationRecipeVersion.recipe_id == row.id,
            CreationRecipeVersion.version == version,
        )
    ).scalar_one_or_none()
    if version_row is None:
        raise HTTPException(404, "创作配方版本不存在")
    return version_row


def resolve_recipe_attribution(
    db: Session,
    *,
    user_id: int,
    recipe_id: int,
    version: int,
    share_slug: str | None = None,
    expected_category: str | None = None,
) -> tuple[RecipeAttribution, CreationRecipeVersion]:
    row = db.get(CreationRecipe, recipe_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, "创作配方不存在")
    if expected_category is not None and row.category != expected_category:
        raise HTTPException(422, "生成类型与创作配方不一致")

    if share_slug:
        share = share_by_slug(db, share_slug, recipe_id=int(row.id))
        selected_version = int(version or share.version)
        if selected_version != int(share.version):
            raise HTTPException(404, "分享的配方版本不存在")
        version_row = recipe_version(db, row, selected_version)
        return (
            RecipeAttribution(
                recipe_id=int(row.id),
                recipe_version=int(version_row.version),
                source="share",
                share_id=int(share.id),
            ),
            version_row,
        )

    if int(row.user_id) == int(user_id):
        version_row = recipe_version(db, row, int(version or row.current_version))
        return (
            RecipeAttribution(
                recipe_id=int(row.id),
                recipe_version=int(version_row.version),
                source="owner",
            ),
            version_row,
        )

    selected_version = int(version or row.current_version)
    if not moderation_is_current(row) or selected_version != int(row.current_version):
        raise HTTPException(404, "公开创作配方不存在")
    version_row = recipe_version(db, row, selected_version)
    return (
        RecipeAttribution(
            recipe_id=int(row.id),
            recipe_version=int(version_row.version),
            source="public",
        ),
        version_row,
    )


def _usage_event_matches(
    event: CreationRecipeUsageEvent,
    *,
    recipe_id: int,
    recipe_version: int,
    event_type: str,
    source: str,
    share_id: int | None,
    generation_task_id: int | None,
    context: dict[str, Any] | None,
) -> bool:
    return (
        event.recipe_id == recipe_id
        and int(event.recipe_version) == recipe_version
        and event.event_type == event_type
        and event.source == source
        and event.share_id == share_id
        and event.generation_task_id == generation_task_id
        and event.context == context
    )


def record_usage(
    db: Session,
    *,
    recipe_id: int,
    recipe_version: int,
    user_id: int,
    event_type: str,
    source: str,
    share_id: int | None = None,
    derived_recipe_id: int | None = None,
    generation_task_id: int | None = None,
    client_event_id: str | None = None,
    context: dict[str, Any] | None = None,
    commit: bool = True,
) -> CreationRecipeUsageEvent:
    if client_event_id:
        existing = db.execute(
            select(CreationRecipeUsageEvent).where(
                CreationRecipeUsageEvent.user_id == user_id,
                CreationRecipeUsageEvent.client_event_id == client_event_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not _usage_event_matches(
                existing,
                recipe_id=recipe_id,
                recipe_version=recipe_version,
                event_type=event_type,
                source=source,
                share_id=share_id,
                generation_task_id=generation_task_id,
                context=context,
            ):
                raise HTTPException(409, "配方使用事件幂等键已用于其他请求")
            return existing

    event = CreationRecipeUsageEvent(
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        user_id=user_id,
        event_type=event_type,
        source=source,
        share_id=share_id,
        derived_recipe_id=derived_recipe_id,
        generation_task_id=generation_task_id,
        client_event_id=client_event_id,
        context=context,
    )
    db.add(event)
    if not commit:
        db.flush()
        return event
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not client_event_id:
            raise
        existing = db.execute(
            select(CreationRecipeUsageEvent).where(
                CreationRecipeUsageEvent.user_id == user_id,
                CreationRecipeUsageEvent.client_event_id == client_event_id,
            )
        ).scalar_one_or_none()
        if existing is None or not _usage_event_matches(
            existing,
            recipe_id=recipe_id,
            recipe_version=recipe_version,
            event_type=event_type,
            source=source,
            share_id=share_id,
            generation_task_id=generation_task_id,
            context=context,
        ):
            raise HTTPException(409, "配方使用事件幂等键冲突") from exc
        return existing
    db.refresh(event)
    return event


def record_generation_prepare(
    db: Session,
    *,
    attribution: RecipeAttribution | None,
    user_id: int,
    quote_id: int,
    category: str,
) -> CreationRecipeUsageEvent | None:
    if attribution is None:
        return None
    return record_usage(
        db,
        recipe_id=attribution.recipe_id,
        recipe_version=attribution.recipe_version,
        user_id=user_id,
        event_type="generation_prepare",
        source=attribution.source,
        share_id=attribution.share_id,
        client_event_id=f"recipe-prepare-quote-{quote_id}",
        context={"quote_id": quote_id, "category": category},
        commit=False,
    )


def record_generation_submit(
    db: Session,
    *,
    attribution: RecipeAttribution | None,
    user_id: int,
    quote_id: int,
    task_id: int,
    category: str,
) -> CreationRecipeUsageEvent | None:
    if attribution is None:
        return None
    return record_usage(
        db,
        recipe_id=attribution.recipe_id,
        recipe_version=attribution.recipe_version,
        user_id=user_id,
        event_type="generation_submit",
        source=attribution.source,
        share_id=attribution.share_id,
        generation_task_id=task_id,
        client_event_id=f"recipe-submit-task-{task_id}",
        context={"quote_id": quote_id, "category": category},
        commit=False,
    )
