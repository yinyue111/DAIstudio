"""Celery app (Redis as broker + result backend)."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_init
from kombu import Queue

from .config import settings
from .db import SessionLocal
from .runtime_config import validate_model_gateway_rows, validate_runtime_config

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
    task_default_queue="default",
    task_queues=(
        Queue("default"),
        Queue("image"),
        Queue("video_submit"),
        Queue("video_poll"),
        Queue("video_download"),
        Queue("parse"),
        Queue("cleanup"),
        Queue("payment"),
    ),
    task_routes={
        "generate.image": {"queue": "image"},
        "generate.video": {"queue": "video_submit"},
        "poll.video": {"queue": "video_poll"},
        "download.video": {"queue": "video_download"},
        "parse.*": {"queue": "parse"},
        "cleanup.*": {"queue": "cleanup"},
        "payments.*": {"queue": "payment"},
    },
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
        # fail parse jobs whose worker message disappeared
        "reap-stuck-parse-records": {
            "task": "cleanup.reap_parse",
            "schedule": crontab(minute="*/10"),
        },
        # resume in-flight video renders whose poll chain died (worker crash)
        "resume-stuck-videos": {
            "task": "cleanup.resume_videos",
            "schedule": crontab(minute="*/2"),
        },
        # query live payment providers for missed or delayed notifications
        "payment-reconcile": {
            "task": "payments.reconcile",
            "schedule": crontab(minute=f"*/{max(1, int(settings.payment_reconcile_interval_minutes))}"),
        },
    },
)


def _validate_worker_runtime_config() -> None:
    validate_runtime_config()
    db = SessionLocal()
    try:
        validate_model_gateway_rows(db)
    finally:
        db.close()


@worker_init.connect
def _validate_worker_runtime_config_signal(**_kwargs) -> None:
    _validate_worker_runtime_config()


@beat_init.connect
def _validate_beat_runtime_config(**_kwargs) -> None:
    _validate_worker_runtime_config()


# ensure task functions are registered
from . import tasks  # noqa: E402,F401
