"""Celery task entrypoints. Thin wrappers around services.generation so the
orchestration logic stays import-safe and unit-testable."""
from __future__ import annotations

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

from .celery_app import celery_app
from .config import settings
from .models import GenTask, ParseRecord
from .observability import current_request_id
from .services import generation

_LOCK_RETRY_COUNTDOWN_SECONDS = 30
_LOCK_RETRY_MAX = max(
    20,
    int((settings.celery_task_time_limit_seconds + 360) / _LOCK_RETRY_COUNTDOWN_SECONDS) + 1,
)


def enqueue_with_request_context(task, *args, countdown: int | None = None, **kwargs):
    """Enqueue a Celery task and propagate the current HTTP request id."""
    options = {"headers": {"x-request-id": current_request_id()}}
    if countdown is not None:
        options["countdown"] = countdown
    return task.apply_async(args=args, kwargs=kwargs, **options)


def _soft_timeout_public_error(category: str) -> str:
    if category == "image":
        return "图片生成超时，已退回冻结积分，请稍后重试"
    if category == "video":
        return "视频生成超时，已退回冻结积分，请稍后重试"
    return "任务执行超时，请稍后重试"


def _fail_generation_soft_timeout(task_id: int, *, category: str) -> None:
    from .db import SessionLocal
    from .services.generation_common import fail_and_refund

    db = SessionLocal()
    try:
        message = _soft_timeout_public_error(category)
        if category == "video":
            task = db.get(GenTask, task_id)
            params = dict((task.params or {}) if task else {})
            request_id = params.get("_video_request_id") or params.get("request_id")
            if task and task.external_task_id:
                generation._hold_video_poll_for_reconciliation(db, task_id, message)
                return
            if task and request_id:
                generation._hold_video_submit_unknown_for_reconciliation(
                    db,
                    task_id,
                    message,
                    params_update=params,
                )
                return
        fail_and_refund(db, task_id, message, public_error=message)
    finally:
        db.close()


def _fail_parse_soft_timeout(parse_id: int) -> None:
    from .db import SessionLocal

    db = SessionLocal()
    try:
        db.execute(
            update(ParseRecord)
            .where(ParseRecord.id == parse_id, ParseRecord.status.in_(("queued", "running")))
            .values(status="failed", error="抓取任务执行超时,请重新提交链接")
        )
        db.commit()
    finally:
        db.close()


@celery_app.task(name="generate.image", bind=True, max_retries=_LOCK_RETRY_MAX)
def generate_image_task(self, task_id: int) -> None:
    try:
        generation.run_image_task(task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="image")
    except generation.TaskLockedError as e:
        raise self.retry(exc=e, countdown=_LOCK_RETRY_COUNTDOWN_SECONDS, max_retries=_LOCK_RETRY_MAX)


@celery_app.task(name="generate.video", bind=True, max_retries=_LOCK_RETRY_MAX)
def generate_video_task(self, task_id: int) -> None:
    # submit only; a self-re-enqueuing poll task drives it to completion so the
    # worker is never blocked across a long render.
    try:
        generation.start_video_task(task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="video")
    except generation.TaskLockedError as e:
        raise self.retry(exc=e, countdown=_LOCK_RETRY_COUNTDOWN_SECONDS, max_retries=_LOCK_RETRY_MAX)


@celery_app.task(name="poll.video", bind=True, max_retries=0)
def poll_video_task(self, task_id: int) -> None:
    try:
        generation.poll_video_once(task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="video")


@celery_app.task(name="download.video", bind=True, max_retries=0)
def download_video_task(
    self,
    task_id: int,
    expected_external_task_id: str | None = None,
) -> None:
    try:
        generation.run_video_download_task(task_id, expected_external_task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="video")


@celery_app.task(name="cleanup.resume_videos")
def resume_stuck_videos_task() -> int:
    """Re-attach polling to in-flight video renders whose poll chain died (worker
    crash). Run frequently via beat so submitted external jobs aren't stranded."""
    from .db import SessionLocal
    from .services import generation as gen

    db = SessionLocal()
    try:
        return gen.resume_stuck_videos(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.expired_assets")
def cleanup_expired_assets_task() -> dict:
    """Purge expired assets + old tasks/parse-records/audit-logs. Schedule via
    celery beat or cron (see scripts/cleanup_expired.py)."""
    from .db import SessionLocal
    from .services import retention

    db = SessionLocal()
    try:
        return retention.purge_all(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.reap_stuck")
def reap_stuck_tasks_task() -> int:
    """Fail + refund tasks stuck past the timeout (worker crash safety net)."""
    from .db import SessionLocal
    from .services import retention

    db = SessionLocal()
    try:
        return retention.reap_stuck_tasks(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.reap_parse")
def reap_stuck_parse_records_task() -> int:
    """Fail queued parse records whose worker task disappeared."""
    from .db import SessionLocal
    from .services import retention

    db = SessionLocal()
    try:
        return retention.reap_stuck_parse_records(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.reap_reverse")
def reap_stuck_reverse_operations_task() -> int:
    """Refund reverse calls abandoned by an API process crash."""
    from .db import SessionLocal
    from .services import retention

    db = SessionLocal()
    try:
        return retention.reap_stuck_reverse_operations(db)
    finally:
        db.close()


@celery_app.task(name="payments.reconcile")
def payment_reconcile_task() -> dict:
    """Backstop missed/late payment notifications by querying live providers."""
    from .db import SessionLocal
    from .services import payments

    db = SessionLocal()
    try:
        return payments.reconcile_pending_orders(db)
    finally:
        db.close()


@celery_app.task(name="parse.url")
def parse_url_task(parse_id: int) -> None:
    """Fetch and localize link assets outside the API request path."""
    from .routers import parse

    try:
        parse.run_parse_record(parse_id)
    except SoftTimeLimitExceeded:
        _fail_parse_soft_timeout(parse_id)
