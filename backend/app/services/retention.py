"""Asset retention: generated media is kept for N days (default 30), then the
files + DB rows are purged. Expiry is computed from gen_assets.created_at.

Used both lazily (filter/purge a user's expired assets on gallery load) and by a
scheduled cleanup job (scripts/cleanup_expired.py / celery task)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AuditLog, GenAsset, GenTask, ParseRecord, UploadedAsset
from ..redis_client import redis_client
from . import credits, generation, storage
from .config_store import get_setting

log = logging.getLogger("retention")


def _video_poll_alive(task_id: int) -> bool:
    try:
        return bool(
            redis_client.get(f"video:poll:alive:{task_id}")
            or redis_client.get(f"video:download:alive:{task_id}")
        )
    except Exception:  # noqa: BLE001
        return False


def get_retention_days(db: Session) -> int:
    try:
        return int(get_setting(db, "asset_retention_days", 30))
    except Exception:
        return 30


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expiry_of(created_at: datetime | None, days: int) -> datetime | None:
    created = _aware(created_at)
    return created + timedelta(days=days) if created else None


def days_left(created_at: datetime | None, days: int) -> int | None:
    exp = expiry_of(created_at, days)
    if not exp:
        return None
    return max(0, (exp - datetime.now(timezone.utc)).days)


def is_expired(created_at: datetime | None, days: int) -> bool:
    exp = expiry_of(created_at, days)
    if not exp:
        return False
    return datetime.now(timezone.utc) >= exp


def _delete_files(asset: GenAsset) -> None:
    for url in (asset.preview_url, asset.hd_url):
        key = storage.key_from_url(url or "")
        if not key:
            continue
        try:
            storage.local_path(key).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            log.warning("failed to delete file for asset %s", asset.id)


def purge_expired(db: Session, user_id: int | None = None, limit: int = 1000) -> int:
    """Delete expired assets (files + rows). Returns how many were removed."""
    days = get_retention_days(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(GenAsset).where(GenAsset.created_at < cutoff)
    if user_id is not None:
        q = q.where(GenAsset.user_id == user_id)
    q = q.limit(limit)
    rows = list(db.execute(q).scalars())
    for a in rows:
        _delete_files(a)
        db.delete(a)
    if rows:
        db.commit()
        log.info("purged %s expired assets (user=%s)", len(rows), user_id)
    return len(rows)


def _purge_table_older_than(db: Session, model, cutoff: datetime, extra=None) -> int:
    stmt = delete(model).where(model.created_at < cutoff)
    if extra is not None:
        stmt = stmt.where(extra)
    res = db.execute(stmt)
    db.commit()
    return res.rowcount or 0


def purge_parsed_previews(db: Session, cutoff: datetime, limit: int = 1000) -> int:
    """Delete localized parse preview files + UploadedAsset rows older than cutoff."""
    rows = list(
        db.execute(
            select(UploadedAsset)
            .where(
                UploadedAsset.original_filename == "parsed-preview.png",
                UploadedAsset.created_at < cutoff,
            )
            .limit(limit)
        ).scalars()
    )
    removed = 0
    for row in rows:
        if _delete_uploaded_asset_row(db, row.key):
            removed += 1
        if row.key.startswith("preview/"):
            model_ref_key = row.key.replace("preview/", "model_ref/", 1)
            if _delete_uploaded_asset_row(db, model_ref_key):
                removed += 1
    if removed:
        db.commit()
    return removed


def _referenced_upload_urls(db: Session, cutoff: datetime) -> set[str]:
    refs: set[str] = set()
    rows = db.execute(
        select(GenTask.source_asset_url, GenTask.params).where(GenTask.created_at >= cutoff)
    ).all()
    for source_asset_url, params in rows:
        if source_asset_url:
            refs.add(source_asset_url)
        task_params = params or {}
        if not isinstance(task_params, dict):
            continue
        for key in ("reference_image_url", "first_frame_image"):
            value = task_params.get(key)
            if value:
                refs.add(value)
    return refs


def _upload_preview_key(upload_key: str) -> str | None:
    if not upload_key.startswith("upload/"):
        return None
    return "upload_preview/" + upload_key.split("/", 1)[1].rsplit(".", 1)[0] + ".png"


def _upload_model_ref_key(upload_key: str) -> str | None:
    if not upload_key.startswith("upload/"):
        return None
    return "upload_model_ref/" + upload_key.split("/", 1)[1].rsplit(".", 1)[0] + ".png"


def _delete_uploaded_asset_row(db: Session, key: str) -> bool:
    row = db.get(UploadedAsset, key)
    if not row:
        return False
    try:
        storage.local_path(row.key).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.warning("failed to delete upload %s", row.key)
    db.delete(row)
    return True


def purge_uploaded_assets(db: Session, cutoff: datetime, limit: int = 1000) -> int:
    """Delete stale user uploads not referenced by unexpired tasks."""
    rows = list(
        db.execute(
            select(UploadedAsset)
            .where(
                UploadedAsset.created_at < cutoff,
                UploadedAsset.key.like("upload/%"),
                or_(
                    UploadedAsset.original_filename.is_(None),
                    UploadedAsset.original_filename != "parsed-preview.png",
                ),
            )
            .limit(limit)
        ).scalars()
    )
    removed = 0
    referenced_urls = _referenced_upload_urls(db, cutoff)
    for row in rows:
        if storage.upload_api_url(row.key) in referenced_urls:
            continue
        if _delete_uploaded_asset_row(db, row.key):
            removed += 1
        preview_key = _upload_preview_key(row.key)
        if preview_key and _delete_uploaded_asset_row(db, preview_key):
            removed += 1
        model_ref_key = _upload_model_ref_key(row.key)
        if model_ref_key and _delete_uploaded_asset_row(db, model_ref_key):
            removed += 1
    if removed:
        db.commit()
    return removed


def purge_all(db: Session) -> dict:
    """Master cleanup across all time-bounded tables. Run on a daily schedule."""
    now = datetime.now(timezone.utc)
    asset_days = get_retention_days(db)
    audit_days = int(get_setting(db, "audit_retention_days", 90))
    parse_days = int(settings.parse_retention_days)

    result = {"assets": purge_expired(db)}

    # GenTask rows are accounting/reporting ledger state: they include frozen
    # and settled credits, reconciliation status, provider request ids, and
    # usage-report inputs. Media retention deletes files/assets, not task rows.
    result["tasks"] = 0
    # ephemeral link-parse records
    result["parse_records"] = _purge_table_older_than(
        db, ParseRecord, now - timedelta(days=parse_days)
    )
    result["parsed_previews"] = purge_parsed_previews(db, now - timedelta(days=parse_days))
    result["uploads"] = purge_uploaded_assets(db, now - timedelta(days=asset_days))
    # audit logs (kept longer for accountability)
    result["audit_logs"] = _purge_table_older_than(
        db, AuditLog, now - timedelta(days=audit_days)
    )
    log.info("purge_all: %s", result)
    return result


def reap_stuck_tasks(db: Session, max_minutes: int = 60) -> int:
    """Fail tasks stuck in queued/running past max_minutes (worker crash etc.)
    and refund their frozen credits. Run frequently (e.g. every 10 min)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max_minutes)
    rows = list(
        db.execute(
            select(GenTask).where(
                GenTask.status.in_(("queued", "running")),
                GenTask.created_at < cutoff,
            )
        ).scalars()
    )
    reaped = 0
    video_window = timedelta(seconds=int(settings.video_poll_max_seconds))
    for t in rows:
        # Don't reap a video still inside its valid poll window or actively being
        # polled — its lifecycle uses external_submitted_at, not created_at.
        if t.category == "video":
            if t.phase == "submitting" and not t.external_task_id:
                generation._hold_for_reconciliation(
                    db,
                    t.id,
                    "视频提交进程在写入外部任务号前中断,需人工确认上游是否已接受任务",
                )
                reaped += 1
                continue
            sub = _aware(t.external_submitted_at)
            if (sub and (now - sub) < video_window) or _video_poll_alive(t.id):
                continue
        # Atomic claim: skip if a worker finalized the task between our SELECT
        # and now, so the reaper can't double-refund alongside the worker.
        res = db.execute(
            update(GenTask)
            .where(
                GenTask.id == t.id,
                GenTask.status.not_in(("succeeded", "failed", generation.NEEDS_REVIEW)),
            )
            .values(status="failed", error="任务超时,已自动失败并退回额度",
                    finished_at=now)
        )
        if (res.rowcount or 0) != 1:
            continue
        if t.cost_frozen and t.cost_settled == 0:
            try:
                credits.refund(db, t.user_id, t.cost_frozen,
                               biz_ref=t.id, commit=False)
            except Exception:  # noqa: BLE001
                log.exception("refund failed reaping task %s", t.id)
        reaped += 1
    if reaped:
        db.commit()
        log.info("reaped %s stuck tasks", reaped)
    return reaped
