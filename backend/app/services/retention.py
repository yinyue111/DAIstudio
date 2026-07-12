"""Asset retention: generated media is kept for N days (default 30), then the
files + DB rows are purged. Expiry is computed from gen_assets.created_at.

Used both lazily (filter/purge a user's expired assets on gallery load) and by a
scheduled cleanup job (scripts/cleanup_expired.py / celery task)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, null, or_, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AssetReport,
    AuditLog,
    GenAsset,
    GenTask,
    ParseRecord,
    ReverseOperation,
    UploadedAsset,
    UserDraft,
)
from . import credits, generation, storage
from .config_store import get_setting
from .generation_state import TERMINAL_STATUSES
from .generation_video_flow import poll_chain_alive as _video_poll_alive
from .generation_video_flow import video_download_alive as _video_download_alive
from .media_sidecars import collect_unreferenced_asset_keys, unlink_keys

log = logging.getLogger("retention")


def _video_waiting_for_download(task: GenTask) -> bool:
    params = task.params or {}
    return task.phase == "downloading" and bool(
        params.get("_video_result_url") or params.get("_video_result_mock")
    )


def _image_reap_window() -> timedelta:
    # Image renders are synchronous gateway calls. A live worker should either
    # finalize or fail/refund after the render timeout plus any result download
    # timeout. Do not key this to the global Celery time limit: that limit also
    # covers long video lifecycles and would leave orphaned image tasks looking
    # "generating" for hours after a worker crash.
    seconds = (
        int(settings.image_gateway_timeout_seconds or 0)
        + int(settings.image_download_timeout_seconds or 0)
    )
    return timedelta(seconds=seconds + 300)


def _video_download_reap_window() -> timedelta:
    seconds = (
        int(settings.video_download_timeout_seconds or 0)
        * max(1, int(settings.video_download_max_attempts or 1))
    )
    return timedelta(seconds=seconds + 300)


def _video_submit_reap_window() -> timedelta:
    seconds = int(settings.video_submit_timeout_seconds or 0)
    return timedelta(seconds=max(300, seconds + 120))


def _reverse_reap_window() -> timedelta:
    # Fine video analysis can spend several minutes extracting keyframes before
    # the synchronous vision request starts. Keep a conservative floor while
    # still expanding with any operator-configured gateway timeout.
    seconds = max(30 * 60, int(settings.reverse_gateway_timeout_seconds or 0) + 5 * 60)
    return timedelta(seconds=seconds)


def _image_inside_reap_window(task: GenTask, now: datetime) -> bool:
    anchor = _aware(task.created_at)
    return bool(anchor and now - anchor < _image_reap_window())


def _image_render_started_at(params: dict | None, fallback: datetime | None) -> datetime | None:
    raw = (params or {}).get("_image_render_started_at")
    if raw:
        try:
            return _aware(datetime.fromisoformat(str(raw)))
        except ValueError:
            return _aware(fallback)
    return _aware(fallback)


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


def purge_expired(db: Session, user_id: int | None = None, limit: int = 1000) -> int:
    """Delete expired assets (files + rows). Returns how many were removed."""
    days = get_retention_days(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(GenAsset).where(GenAsset.created_at < cutoff)
    if user_id is not None:
        q = q.where(GenAsset.user_id == user_id)
    q = q.limit(limit)
    rows = list(db.execute(q).scalars())
    unlink_after_commit: list[str] = []
    for a in rows:
        unlink_after_commit.extend(collect_unreferenced_asset_keys(db, a))
        detach_asset_reports(db, a.id)
        db.delete(a)
    if rows:
        db.commit()
        unlink_keys(unlink_after_commit)
        log.info("purged %s expired assets (user=%s)", len(rows), user_id)
    return len(rows)


def _purge_table_older_than(db: Session, model, cutoff: datetime, extra=None) -> int:
    stmt = delete(model).where(model.created_at < cutoff)
    if extra is not None:
        stmt = stmt.where(extra)
    res = db.execute(stmt)
    db.commit()
    return res.rowcount or 0


def _tombstone_named_reverse_operations(db: Session, cutoff: datetime) -> int:
    """Redact expired caller-keyed rows without changing idempotent replay."""
    failed = db.execute(
        update(ReverseOperation)
        .where(
            ReverseOperation.created_at < cutoff,
            ReverseOperation.status == "failed",
            ReverseOperation.client_request_id.is_not(None),
            or_(
                ReverseOperation.asset_url != "",
                ReverseOperation.error.is_not(None),
                ReverseOperation.result.is_not(None),
            ),
        )
        .values(asset_url="", error=None, result=null())
        .execution_options(synchronize_session=False)
    )
    succeeded = db.execute(
        update(ReverseOperation)
        .where(
            ReverseOperation.created_at < cutoff,
            ReverseOperation.status == "succeeded",
            ReverseOperation.client_request_id.is_not(None),
            or_(
                ReverseOperation.asset_url != "",
                ReverseOperation.error.is_not(None),
            ),
        )
        # The successful result is the public idempotent replay payload.
        .values(asset_url="", error=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return (failed.rowcount or 0) + (succeeded.rowcount or 0)


def detach_asset_reports(db: Session, asset_id: int) -> int:
    """Preserve report history when an asset row is deleted."""
    res = db.execute(
        update(AssetReport)
        .where(AssetReport.asset_id == asset_id)
        .values(asset_id=None)
        .execution_options(synchronize_session=False)
    )
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
    unlink_after_commit: list[str] = []
    for row in rows:
        if _delete_uploaded_asset_row(db, row.key, unlink_after_commit):
            removed += 1
        if row.key.startswith("preview/"):
            model_ref_png_key = row.key.replace("preview/", "model_ref/", 1)
            for model_ref_key in (model_ref_png_key.rsplit(".", 1)[0] + ".jpg", model_ref_png_key):
                if _delete_uploaded_asset_row(db, model_ref_key, unlink_after_commit):
                    removed += 1
    if removed:
        db.commit()
        unlink_keys(unlink_after_commit)
    return removed


_TASK_UPLOAD_PARAM_FIELDS = (
    "reference_image_url",
    "first_frame_image",
    "last_frame_image",
    "style_reference_image",
    "character_reference_image",
    "mask_image_url",
)
_REFERENCE_QUERY_CHUNK_SIZE = 500


def _chunks(values: set[str], size: int = _REFERENCE_QUERY_CHUNK_SIZE):
    ordered = sorted(values)
    for start in range(0, len(ordered), size):
        yield ordered[start : start + size]


def _referenced_upload_urls(
    db: Session,
    cutoff: datetime,
    candidate_urls: set[str],
) -> set[str]:
    """Find task references only among this purge batch's candidate URLs."""
    if not candidate_urls:
        return set()
    refs: set[str] = set()
    active = or_(
        GenTask.created_at >= cutoff,
        GenTask.status.in_(("queued", "running", generation.NEEDS_REVIEW)),
    )
    for chunk in _chunks(candidate_urls):
        refs.update(
            value
            for value in db.execute(
                select(GenTask.source_asset_url).where(
                    active,
                    GenTask.source_asset_url.in_(chunk),
                )
            ).scalars()
            if value
        )
        for field in _TASK_UPLOAD_PARAM_FIELDS:
            value_expr = GenTask.params[field].as_string()
            refs.update(
                value
                for value in db.execute(
                    select(value_expr).where(active, value_expr.in_(chunk))
                ).scalars()
                if value
            )
    return refs


_STUDIO_ASSET_FIELDS = ("assets", "selected", "productAsset", "variationSource")
_STUDIO_ASSET_URL_FIELDS = (
    "url",
    "thumb",
    "preview_url",
    "original_url",
    "original_thumb",
)


def _studio_draft_upload_urls(db: Session, user_ids: set[int]) -> set[tuple[int, str]]:
    """Return only known Studio asset URLs, scoped to the draft owner."""
    if not user_ids:
        return set()
    refs: set[tuple[int, str]] = set()
    rows = db.execute(
        select(UserDraft.user_id, UserDraft.payload).where(
            UserDraft.key == "studio",
            UserDraft.user_id.in_(user_ids),
        )
    ).all()
    for user_id, payload in rows:
        workspaces = payload.get("workspaces") if isinstance(payload, dict) else None
        if not isinstance(workspaces, dict):
            continue
        for workspace in workspaces.values():
            if not isinstance(workspace, dict):
                continue
            for field in _STUDIO_ASSET_FIELDS:
                value = workspace.get(field)
                assets = value if field == "assets" and isinstance(value, list) else [value]
                for asset in assets:
                    if not isinstance(asset, dict):
                        continue
                    for url_field in _STUDIO_ASSET_URL_FIELDS:
                        url = asset.get(url_field)
                        if isinstance(url, str) and url:
                            refs.add((int(user_id), url))
    return refs


def _upload_preview_key(upload_key: str) -> str | None:
    if not upload_key.startswith("upload/"):
        return None
    return "upload_preview/" + upload_key.split("/", 1)[1].rsplit(".", 1)[0] + ".png"


def _upload_model_ref_keys(upload_key: str) -> list[str]:
    if not upload_key.startswith("upload/"):
        return []
    stem = upload_key.split("/", 1)[1].rsplit(".", 1)[0]
    return [f"upload_model_ref/{stem}.jpg", f"upload_model_ref/{stem}.png"]


def _upload_model_ref_key(upload_key: str) -> str | None:
    keys = _upload_model_ref_keys(upload_key)
    return keys[0] if keys else None


def _upload_video_preview_key(upload_key: str) -> str | None:
    if not upload_key.startswith("upload_video/"):
        return None
    return "upload_video_preview/" + upload_key.split("/", 1)[1].rsplit(".", 1)[0] + ".jpg"


def _upload_related_keys(upload_key: str) -> list[str]:
    keys = [upload_key]
    preview_key = _upload_preview_key(upload_key)
    if preview_key:
        keys.append(preview_key)
    keys.extend(_upload_model_ref_keys(upload_key))
    video_preview_key = _upload_video_preview_key(upload_key)
    if video_preview_key:
        keys.append(video_preview_key)
    return keys


def _upload_group_is_referenced(
    row: UploadedAsset,
    referenced_urls: set[str],
    studio_draft_urls: set[tuple[int, str]],
) -> bool:
    for key in _upload_related_keys(row.key):
        url = storage.upload_api_url(key)
        if url in referenced_urls or (int(row.user_id), url) in studio_draft_urls:
            return True
    return False


def _delete_uploaded_asset_row(db: Session, key: str, unlink_after_commit: list[str]) -> bool:
    row = db.get(UploadedAsset, key)
    if not row:
        return False
    unlink_after_commit.append(row.key)
    db.delete(row)
    return True


def purge_uploaded_assets(db: Session, cutoff: datetime, limit: int = 1000) -> int:
    """Delete stale user uploads not referenced by unexpired tasks."""
    rows = list(
        db.execute(
            select(UploadedAsset)
            .where(
                UploadedAsset.created_at < cutoff,
                or_(
                    UploadedAsset.key.like("upload/%"),
                    UploadedAsset.key.like("upload_video/%"),
                ),
                or_(
                    UploadedAsset.original_filename.is_(None),
                    UploadedAsset.original_filename != "parsed-preview.png",
                ),
            )
            .limit(limit)
        ).scalars()
    )
    removed = 0
    unlink_after_commit: list[str] = []
    candidate_urls = {
        storage.upload_api_url(key)
        for row in rows
        for key in _upload_related_keys(row.key)
    }
    referenced_urls = _referenced_upload_urls(db, cutoff, candidate_urls)
    studio_draft_urls = _studio_draft_upload_urls(db, {int(row.user_id) for row in rows})
    for row in rows:
        if _upload_group_is_referenced(row, referenced_urls, studio_draft_urls):
            continue
        if _delete_uploaded_asset_row(db, row.key, unlink_after_commit):
            removed += 1
        preview_key = _upload_preview_key(row.key)
        if preview_key and _delete_uploaded_asset_row(db, preview_key, unlink_after_commit):
            removed += 1
        for model_ref_key in _upload_model_ref_keys(row.key):
            if _delete_uploaded_asset_row(db, model_ref_key, unlink_after_commit):
                removed += 1
        video_preview_key = _upload_video_preview_key(row.key)
        if video_preview_key and _delete_uploaded_asset_row(db, video_preview_key, unlink_after_commit):
            removed += 1
    if removed:
        db.commit()
        unlink_keys(unlink_after_commit)
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
    reverse_cutoff = now - timedelta(days=audit_days)
    # Anonymous calls have no public replay key, so terminal rows can expire.
    # Caller-keyed rows retain their compact replay/conflict state indefinitely.
    result["reverse_operations"] = _purge_table_older_than(
        db,
        ReverseOperation,
        reverse_cutoff,
        ReverseOperation.status.in_(("succeeded", "failed"))
        & ReverseOperation.client_request_id.is_(None),
    )
    result["reverse_operation_tombstones"] = _tombstone_named_reverse_operations(
        db, reverse_cutoff
    )
    # audit logs (kept longer for accountability)
    result["audit_logs"] = _purge_table_older_than(
        db, AuditLog, now - timedelta(days=audit_days)
    )
    log.info("purge_all: %s", result)
    return result


def reap_stuck_parse_records(db: Session, max_minutes: int | None = None) -> int:
    """Fail queued/running parse jobs that outlived the pending window.

    Parse jobs are fire-and-forget Celery tasks. If a worker dies or the broker
    drops a message, the API would otherwise keep reusing a stale active row and
    count it against the user's pending limit until retention purges it.
    """
    minutes = max(1, int(max_minutes or settings.parse_pending_max_age_minutes))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    res = db.execute(
        update(ParseRecord)
        .where(ParseRecord.status.in_(("queued", "running")), ParseRecord.created_at < cutoff)
        .values(status="failed", error="抓取任务超时,请重新提交链接")
        .execution_options(synchronize_session=False)
    )
    count = res.rowcount or 0
    if count:
        db.commit()
        log.info("reaped %s stuck parse record(s)", count)
    return count


def reap_stuck_reverse_operations(db: Session, max_minutes: int | None = None) -> int:
    """Fail and refund reverse calls abandoned by an API process crash."""
    window = (
        timedelta(minutes=max(1, int(max_minutes)))
        if max_minutes is not None
        else _reverse_reap_window()
    )
    now = datetime.now(timezone.utc)
    cutoff = now - window
    operation_ids = list(
        db.execute(
            select(ReverseOperation.id).where(
                ReverseOperation.status == "running",
                ReverseOperation.updated_at < cutoff,
            )
        ).scalars()
    )
    reaped = 0
    for operation_id in operation_ids:
        try:
            claimed = db.execute(
                update(ReverseOperation)
                .where(
                    ReverseOperation.id == int(operation_id),
                    ReverseOperation.status == "running",
                    ReverseOperation.updated_at < cutoff,
                )
                .values(
                    status="failed",
                    error="反推任务超时,已自动失败并退回积分",
                    updated_at=now,
                )
                .returning(
                    ReverseOperation.user_id,
                    ReverseOperation.charged_credits,
                )
                .execution_options(synchronize_session=False)
            ).first()
            if claimed is None:
                db.rollback()
                continue
            user_id, charged_credits = claimed
            charged = int(charged_credits or 0)
            if charged:
                db.execute(
                    update(ReverseOperation)
                    .where(ReverseOperation.id == int(operation_id))
                    .values(charged_credits=0)
                    .execution_options(synchronize_session=False)
                )
                credits.refund_consumed(
                    db,
                    int(user_id),
                    charged,
                    biz_type="reverse",
                    biz_ref=int(operation_id),
                    note="stale reverse operation refund",
                    commit=False,
                )
            db.commit()
            reaped += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("failed to reap reverse operation %s", operation_id)
    if reaped:
        log.info("reaped %s stale reverse operation(s)", reaped)
    return reaped


def reap_stuck_tasks(db: Session, max_minutes: int = 60) -> int:
    """Fail tasks stuck in queued/running past max_minutes (worker crash etc.)
    and refund their frozen credits. Run frequently (e.g. every 10 min)."""
    now = datetime.now(timezone.utc)
    generic_window = timedelta(minutes=max_minutes)
    rows = list(
        db.execute(
            select(
                GenTask.id,
                GenTask.user_id,
                GenTask.category,
                GenTask.status,
                GenTask.phase,
                GenTask.external_task_id,
                GenTask.external_submitted_at,
                GenTask.created_at,
                GenTask.cost_frozen,
                GenTask.cost_settled,
                GenTask.params,
            ).where(GenTask.status.in_(("queued", "running")))
        ).mappings()
    )
    reaped = 0
    video_window = timedelta(seconds=int(settings.video_poll_max_seconds))
    for row in rows:
        task_id = int(row["id"])
        created_at = _aware(row["created_at"])
        age = now - created_at if created_at else None
        error_message = "任务超时,已自动失败并退回额度"
        hold_for_review = False
        review_message = ""
        review_params = None
        if row["phase"] == "reconciling":
            continue
        # Don't reap a video still inside its valid poll window or actively being
        # polled — its lifecycle uses external_submitted_at, not created_at.
        if row["category"] == "video":
            params = row["params"] or {}
            if row["phase"] == "submitting" and not row["external_task_id"]:
                if age is not None and age < _video_submit_reap_window():
                    continue
                request_id = params.get("_video_request_id") or params.get("request_id")
                if request_id:
                    res = db.execute(
                        update(GenTask)
                        .where(
                            GenTask.id == task_id,
                            GenTask.status.not_in(TERMINAL_STATUSES),
                        )
                        .values(
                            status=generation.NEEDS_REVIEW,
                            phase="reconciling",
                            error=(
                                "视频提交状态未知,上游可能已接受任务,冻结积分暂不退回。"
                                f"request_id={str(request_id)[:120]}"
                            ),
                            finished_at=now,
                        )
                    )
                    if res.rowcount or 0:
                        reaped += 1
                    continue
                error_message = "视频提交超时且未记录请求号,已自动失败并退回额度"
            else:
                sub = _aware(row["external_submitted_at"])
                if _video_poll_alive(task_id, row["external_task_id"]):
                    continue
                if row["phase"] == "downloading" and bool(
                    params.get("_video_result_url") or params.get("_video_result_mock")
                ):
                    if _video_download_alive(task_id, row["external_task_id"]):
                        continue
                    download_started_at = None
                    raw_download_started_at = params.get("_video_download_started_at")
                    if raw_download_started_at:
                        try:
                            download_started_at = datetime.fromisoformat(str(raw_download_started_at))
                        except ValueError:
                            download_started_at = None
                    anchor = _aware(download_started_at) or _aware(row["created_at"])
                    if anchor and now - anchor < _video_download_reap_window():
                        continue
                    hold_for_review = True
                    review_message = (
                        "视频结果下载超时,上游已返回结果,冻结积分暂不退回。"
                        f"external_task_id={row['external_task_id'] or 'unknown'}"
                    )
                    review_params = {**params, "_video_download_state_unknown": True}
                elif row["phase"] == "downloading":
                    if row["external_task_id"]:
                        hold_for_review = True
                        review_message = (
                            "视频结果下载状态未知,上游可能已生成结果,冻结积分暂不退回。"
                            f"external_task_id={row['external_task_id'] or 'unknown'}"
                        )
                        review_params = {**params, "_video_download_state_unknown": True}
                    else:
                        error_message = "视频结果下载状态未知,已自动失败并退回额度"
                elif sub and (now - sub) < video_window:
                    continue
                elif row["external_task_id"]:
                    hold_for_review = True
                    review_message = (
                        "视频渲染超时且上游任务状态未知,冻结积分暂不退回。"
                        f"external_task_id={row['external_task_id'] or 'unknown'}"
                    )
                    review_params = {**params, "_video_poll_state_unknown": True}
                elif age is not None and age < generic_window:
                    continue
        elif row["category"] == "image":
            params = row["params"] or {}
            if row["status"] == "queued":
                if age is not None and age < generic_window:
                    continue
                error_message = "任务排队超时,已自动失败并退回额度"
            else:
                anchor = _image_render_started_at(params, row["created_at"])
                if not anchor or now - anchor < _image_reap_window():
                    continue
                error_message = "图片生成任务超时,已自动失败并退回额度"
        elif age is not None and age < generic_window:
            continue
        if hold_for_review:
            values = {
                "status": generation.NEEDS_REVIEW,
                "phase": "reconciling",
                "error": review_message,
                "finished_at": now,
            }
            if review_params is not None:
                values["params"] = review_params
            res = db.execute(
                update(GenTask)
                .where(
                    GenTask.id == task_id,
                    GenTask.status.not_in(TERMINAL_STATUSES),
                )
                .values(**values)
            )
            if res.rowcount or 0:
                reaped += 1
            continue
        # Atomic claim: skip if a worker finalized the task between our SELECT
        # and now, so the reaper can't double-refund alongside the worker.
        res = db.execute(
            update(GenTask)
            .where(
                GenTask.id == task_id,
                GenTask.status.not_in(TERMINAL_STATUSES),
            )
            .values(status="failed", error=error_message, finished_at=now)
        )
        if (res.rowcount or 0) != 1:
            continue
        cost_frozen = int(row["cost_frozen"] or 0)
        cost_settled = int(row["cost_settled"] or 0)
        if cost_frozen and cost_settled == 0:
            try:
                credits.refund(db, int(row["user_id"]), cost_frozen,
                               biz_ref=task_id, commit=False)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                log.exception("refund failed reaping task %s", task_id)
                review_res = db.execute(
                    update(GenTask)
                    .where(
                        GenTask.id == task_id,
                        GenTask.status.not_in(TERMINAL_STATUSES),
                    )
                    .values(
                        status=generation.NEEDS_REVIEW,
                        phase="reconciling",
                        error=f"任务超时且自动退款失败,请联系管理员处理:{str(e)[:300]}",
                        finished_at=now,
                    )
                )
                if review_res.rowcount or 0:
                    reaped += 1
                continue
        reaped += 1
    if reaped:
        db.commit()
        log.info("reaped %s stuck tasks", reaped)
    return reaped
