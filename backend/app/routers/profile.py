"""Personal profile: user info + a gallery of the user's generated assets,
with 30-day (configurable) retention. Expired assets are filtered out and
best-effort purged on load."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import GenAsset, GenTask, User
from ..redis_client import redis_client
from ..schemas import AssetOut, UserOut
from ..services import retention
from ..services.asset_output import to_asset_out

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _page(limit: int, offset: int, cap: int) -> tuple[int, int]:
    return min(max(int(limit), 1), cap), max(int(offset), 0)


def _maybe_purge(db: Session, user_id: int) -> None:
    """Purge a user's expired assets at most once per hour (the query below
    already hides expired rows, and a daily job does the bulk cleanup — so we
    don't need to write/delete files on every gallery load)."""
    try:
        if redis_client.set(f"purge:assets:{user_id}", "1", nx=True, ex=3600):
            retention.purge_expired(db, user_id=user_id)
    except Exception:  # noqa: BLE001
        db.rollback()


@router.get("")
def profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _maybe_purge(db, user.id)
    days = retention.get_retention_days(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    base = (
        select(func.count())
        .select_from(GenAsset)
        .where(GenAsset.user_id == user.id, GenAsset.created_at >= cutoff)
    )
    images = db.execute(base.where(GenAsset.type == "image")).scalar() or 0
    videos = db.execute(base.where(GenAsset.type == "video")).scalar() or 0
    unlocked = db.execute(base.where(GenAsset.unlocked.is_(True))).scalar() or 0
    tasks = db.execute(
        select(func.count()).select_from(GenTask).where(GenTask.user_id == user.id)
    ).scalar() or 0
    return {
        "user": UserOut.model_validate(user),
        "retention_days": days,
        "stats": {
            "images": int(images),
            "videos": int(videos),
            "unlocked": int(unlocked),
            "total_assets": int(images) + int(videos),
            "tasks": int(tasks),
        },
    }


@router.get("/assets", response_model=list[AssetOut])
def my_assets(type: str = "all", favorite: bool = False,
              limit: int = 60, offset: int = 0,
              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # best-effort, throttled: drop this user's expired assets (rate-limited so a
    # gallery refresh isn't deleting files on every request)
    _maybe_purge(db, user.id)

    days = retention.get_retention_days(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = (
        select(GenAsset, GenTask)
        .join(GenTask, GenTask.id == GenAsset.task_id)
        .where(GenAsset.user_id == user.id, GenAsset.created_at >= cutoff)
        .order_by(GenAsset.id.desc())
    )
    if type in ("image", "video"):
        q = q.where(GenAsset.type == type)
    if favorite:
        q = q.where(GenAsset.favorite.is_(True))
    limit, offset = _page(limit, offset, 200)
    q = q.limit(limit).offset(offset)

    out: list[AssetOut] = []
    for asset, task in db.execute(q).all():
        ao = to_asset_out(db, asset, task=task)
        ao.expires_at = retention.expiry_of(asset.created_at, days)
        ao.days_left = retention.days_left(asset.created_at, days)
        ao.category = task.category
        out.append(ao)
    return out
