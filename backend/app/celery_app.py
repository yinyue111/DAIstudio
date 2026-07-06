"""Celery app (Redis as broker + result backend)."""
from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, task_postrun, task_prerun, worker_init, worker_shutting_down
from kombu import Queue

from .config import settings
from .db import SessionLocal
from .observability import reset_request_id, set_request_id
from .redis_client import redis_client, redis_connection_kwargs
from .runtime_config import validate_model_gateway_rows, validate_runtime_config
from .services.config_store import seed_from_yaml

logger = logging.getLogger(__name__)

_celery_settings = settings.celery
_redis_settings = settings.redis
_redis_kwargs = redis_connection_kwargs()
_redis_transport_options = {
    "socket_connect_timeout": _redis_kwargs["socket_connect_timeout"],
    "socket_timeout": _redis_kwargs["socket_timeout"],
    "health_check_interval": _redis_kwargs["health_check_interval"],
    "retry_on_timeout": _redis_kwargs["retry_on_timeout"],
    "visibility_timeout": _celery_settings.visibility_timeout_seconds,
}
_worker_shutting_down = False

celery_app = Celery(
    "ai_studio",
    broker=_redis_settings.url,
    backend=_redis_settings.url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_pool_limit=_redis_kwargs["max_connections"],
    broker_transport_options=_redis_transport_options,
    result_backend_transport_options=_redis_transport_options,
    redis_max_connections=_redis_kwargs["max_connections"],
    redis_socket_connect_timeout=_redis_kwargs["socket_connect_timeout"],
    redis_socket_timeout=_redis_kwargs["socket_timeout"],
    redis_retry_on_timeout=_redis_kwargs["retry_on_timeout"],
    redis_backend_health_check_interval=_redis_kwargs["health_check_interval"],
    task_time_limit=_celery_settings.task_time_limit_seconds,
    task_soft_time_limit=_celery_settings.task_soft_time_limit_seconds,
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


def mark_worker_shutting_down(*, reason: str = "signal") -> None:
    """Set the local and shared shutdown flag for cooperative long-running tasks."""
    global _worker_shutting_down
    _worker_shutting_down = True
    try:
        redis_client.setex(
            _celery_settings.shutdown_flag_key,
            max(60, int(_celery_settings.worker_shutdown_grace_seconds)),
            reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to publish celery shutdown flag: %s", exc)


def is_worker_shutting_down(*, check_redis: bool = True) -> bool:
    """Return true once this worker has begun graceful shutdown.

    Long tasks can call this between network polls/download chunks and checkpoint
    or stop scheduling follow-up work when shutdown has started.
    """
    if _worker_shutting_down:
        return True
    if not check_redis:
        return False
    try:
        return bool(redis_client.get(_celery_settings.shutdown_flag_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read celery shutdown flag: %s", exc)
        return False


def _validate_worker_runtime_config() -> None:
    validate_runtime_config()
    db = SessionLocal()
    try:
        seed_from_yaml(db)
        validate_model_gateway_rows(db)
    finally:
        db.close()


@worker_init.connect
def _validate_worker_runtime_config_signal(**_kwargs) -> None:
    _validate_worker_runtime_config()


@beat_init.connect
def _validate_beat_runtime_config(**_kwargs) -> None:
    _validate_worker_runtime_config()


@worker_shutting_down.connect
def _mark_worker_shutting_down_signal(sig=None, how=None, exitcode=None, **_kwargs) -> None:
    mark_worker_shutting_down(reason=f"signal:{sig or 'unknown'}:{how or 'unknown'}:{exitcode}")


@task_prerun.connect
def _bind_task_request_id(task=None, task_id=None, **_kwargs) -> None:
    headers = getattr(getattr(task, "request", None), "headers", None) or {}
    request_id = headers.get("x-request-id") or headers.get("request_id") or task_id
    token = set_request_id(str(request_id or "-"))
    if task is not None:
        task.request._request_id_token = token


@task_postrun.connect
def _reset_task_request_id(task=None, **_kwargs) -> None:
    token = getattr(getattr(task, "request", None), "_request_id_token", None)
    if token is not None:
        reset_request_id(token)


# ensure task functions are registered
from . import tasks  # noqa: E402,F401
