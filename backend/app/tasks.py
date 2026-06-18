"""Celery task entrypoints. Thin wrappers around services.generation so the
orchestration logic stays import-safe and unit-testable."""
from __future__ import annotations

from .celery_app import celery_app
from .services import generation


@celery_app.task(name="generate.image", bind=True, max_retries=0)
def generate_image_task(self, task_id: int) -> None:
    generation.run_image_task(task_id)


@celery_app.task(name="generate.video", bind=True, max_retries=0)
def generate_video_task(self, task_id: int) -> None:
    # submit only; a self-re-enqueuing poll task drives it to completion so the
    # worker is never blocked across a long render.
    generation.start_video_task(task_id)


@celery_app.task(name="poll.video", bind=True, max_retries=0)
def poll_video_task(self, task_id: int) -> None:
    generation.poll_video_once(task_id)


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
