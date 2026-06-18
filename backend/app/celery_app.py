"""Celery app (Redis as broker + result backend)."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from .config import settings

celery_app = Celery(
    "ai_studio",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.celery_task_time_limit_seconds,
    timezone="UTC",
    beat_schedule={
        # daily retention cleanup at 03:00 UTC (run worker with -B to enable)
        "cleanup-expired-daily": {
            "task": "cleanup.expired_assets",
            "schedule": crontab(hour=3, minute=0),
        },
        # reap stuck tasks every 10 minutes (last-resort backstop)
        "reap-stuck-tasks": {
            "task": "cleanup.reap_stuck",
            "schedule": crontab(minute="*/10"),
        },
        # resume in-flight video renders whose poll chain died (worker crash)
        "resume-stuck-videos": {
            "task": "cleanup.resume_videos",
            "schedule": crontab(minute="*/2"),
        },
    },
)

# ensure task functions are registered
from . import tasks  # noqa: E402,F401
