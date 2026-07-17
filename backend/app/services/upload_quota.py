"""Per-user media storage quota helpers."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenAsset, UploadedAsset, User

USER_MEDIA_STORAGE_PREFIXES = (
    "upload/",
    "upload_preview/",
    "upload_model_ref/",
    "upload_video/",
    "upload_video_preview/",
    "preview/",
    "model_ref/",
)


def user_media_usage_bytes(db: Session, user_id: int) -> int:
    uploaded_total = db.execute(
        select(func.coalesce(func.sum(UploadedAsset.bytes), 0)).where(
            UploadedAsset.user_id == user_id,
            or_(*(UploadedAsset.key.startswith(prefix) for prefix in USER_MEDIA_STORAGE_PREFIXES)),
        )
    ).scalar_one()
    retained_generated_total = db.execute(
        select(func.coalesce(func.sum(GenAsset.bytes), 0)).where(
            GenAsset.user_id == user_id,
            GenAsset.retained_at.is_not(None),
        )
    ).scalar_one()
    return int(uploaded_total or 0) + int(retained_generated_total or 0)


def _assert_user_media_quota(db: Session, user_id: int, incoming_bytes: int) -> None:
    quota = int(settings.user_upload_storage_quota_bytes or 0)
    if quota <= 0:
        return
    incoming = max(0, int(incoming_bytes or 0))
    used = user_media_usage_bytes(db, user_id)
    if used + incoming > quota:
        raise HTTPException(
            413,
            f"上传空间不足,当前已用 {used // 1024 // 1024}MB,"
            f"本次 {incoming // 1024 // 1024}MB,上限 {quota // 1024 // 1024}MB",
        )


def preflight_user_media_quota(db: Session, user_id: int, incoming_bytes: int) -> None:
    """Cheap early rejection before large uploads hit disk/ffmpeg."""
    _assert_user_media_quota(db, user_id, incoming_bytes)


def ensure_user_media_quota(db: Session, user_id: int, incoming_bytes: int) -> None:
    # In production PostgreSQL this locks the user's row until caller commits
    # the media rows, so concurrent writes for one account do not race past quota.
    db.get(User, user_id, with_for_update=True)
    _assert_user_media_quota(db, user_id, incoming_bytes)
