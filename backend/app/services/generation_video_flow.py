"""Video generation polling/download flow helpers."""
from __future__ import annotations

import logging
from datetime import timezone

from ..config import settings
from ..models import GenTask
from ..redis_client import redis_client
from . import storage
from .model_pricing import usage_from_response
from .progress import set_progress
from .rate_limit import incr_window

log = logging.getLogger("generation")

VIDEO_POLL_MAX_SECONDS = settings.video_poll_max_seconds
VIDEO_POLL_INTERVAL = settings.video_poll_interval_seconds
POLL_LIVENESS_TTL = max(
    180,
    VIDEO_POLL_INTERVAL * 3,
    min(VIDEO_POLL_MAX_SECONDS, 1800),
)
POLL_MAX_CONSEC_ERRORS = 3
VIDEO_DOWNLOAD_MAX_ATTEMPTS = max(1, int(settings.video_download_max_attempts or 1))
VIDEO_DOWNLOAD_LIVENESS_TTL = max(
    POLL_LIVENESS_TTL,
    int(settings.video_download_timeout_seconds) * VIDEO_DOWNLOAD_MAX_ATTEMPTS + 60,
)


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def unlink_keys(keys) -> None:
    """Best-effort remove stored media files after a losing finalizer race."""
    for k in keys or []:
        try:
            storage.local_path(k).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def download_lock_key(task_id: int) -> str:
    return f"video:download:lock:{task_id}"


def mark_poll_alive(task_id: int) -> None:
    try:
        redis_client.set(f"video:poll:alive:{task_id}", "1", ex=POLL_LIVENESS_TTL)
    except Exception:  # noqa: BLE001
        pass


def clear_poll_alive(task_id: int) -> None:
    try:
        redis_client.delete(f"video:poll:alive:{task_id}")
    except Exception:  # noqa: BLE001
        pass


def mark_video_download_alive(task_id: int) -> None:
    try:
        redis_client.set(f"video:download:alive:{task_id}", "1", ex=VIDEO_DOWNLOAD_LIVENESS_TTL)
    except Exception:  # noqa: BLE001
        pass


def clear_video_download_alive(task_id: int) -> None:
    try:
        redis_client.delete(f"video:download:alive:{task_id}")
    except Exception:  # noqa: BLE001
        pass


def bump_poll_errors(task_id: int) -> int:
    try:
        n = incr_window(f"video:poll:err:{task_id}", POLL_LIVENESS_TTL)
        return int(n)
    except Exception:  # noqa: BLE001
        return 0


def reset_poll_errors(task_id: int) -> None:
    try:
        redis_client.delete(f"video:poll:err:{task_id}")
    except Exception:  # noqa: BLE001
        pass


def poll_chain_alive(task_id: int) -> bool:
    try:
        return bool(redis_client.get(f"video:poll:alive:{task_id}"))
    except Exception:  # noqa: BLE001
        return False


def video_download_alive(task_id: int) -> bool:
    try:
        return bool(redis_client.get(f"video:download:alive:{task_id}"))
    except Exception:  # noqa: BLE001
        return False


def enqueue_poll(task_id: int) -> None:
    """Schedule one poll tick; each tick re-enqueues itself until terminal."""
    from ..tasks import poll_video_task
    try:
        poll_video_task.apply_async((task_id,), countdown=VIDEO_POLL_INTERVAL)
    except Exception:
        clear_poll_alive(task_id)
        log.exception("failed to enqueue video poll for task %s", task_id)
        raise


def has_video_download_result(task: GenTask) -> bool:
    params = task.params or {}
    return bool(params.get("_video_result_url") or params.get("_video_result_mock"))


def enqueue_video_download(task_id: int, *, countdown: int = 0) -> None:
    """Schedule video result persistence on the dedicated download queue."""
    from ..tasks import download_video_task
    try:
        download_video_task.apply_async((task_id,), countdown=countdown)
    except Exception:
        clear_video_download_alive(task_id)
        log.exception("failed to enqueue video download for task %s", task_id)
        raise


def persist_video_download_result(db, task: GenTask, result: dict) -> None:
    params = dict(task.params or {})
    if result.get("url"):
        params["_video_result_url"] = result["url"]
    provider_usage = usage_from_response(result)
    if provider_usage:
        params["_video_result_usage"] = provider_usage
    if result.get("mock"):
        params["_video_result_mock"] = True
    else:
        params.pop("_video_result_mock", None)
    task.params = params
    task.status = "running"
    task.phase = "downloading"
    db.commit()
    set_progress(task.id, 90, "running")


def download_result_from_task(task: GenTask) -> dict:
    params = dict(task.params or {})
    result: dict = {"status": "succeeded"}
    if params.get("_video_result_url"):
        result["url"] = params["_video_result_url"]
    if params.get("_video_result_usage"):
        result["usage"] = params["_video_result_usage"]
    if params.get("_video_result_mock"):
        result["mock"] = True
    return result
