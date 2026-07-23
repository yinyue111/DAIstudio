"""Video generation polling/download flow helpers."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import update

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
    60,
    VIDEO_POLL_INTERVAL * 6,
)
POLL_MAX_CONSEC_ERRORS = 3
VIDEO_DOWNLOAD_MAX_ATTEMPTS = max(1, int(settings.video_download_max_attempts or 1))
VIDEO_DOWNLOAD_LIVENESS_TTL = max(
    POLL_LIVENESS_TTL,
    180,
    min(int(settings.video_download_timeout_seconds), 300),
)
_DELETE_IF_VALUE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def unlink_keys(keys) -> None:
    """Best-effort remove stored media files after a losing finalizer race."""
    for k in keys or []:
        try:
            storage.delete(k)
        except Exception:  # noqa: BLE001
            log.warning("failed to unlink stale media key %s", k, exc_info=True)


def download_lock_key(task_id: int) -> str:
    return f"video:download:lock:{task_id}"


def _redis_value_matches(value, expected: str) -> bool:
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return False
    return value == expected


def _delete_if_value(key: str, expected: str) -> None:
    try:
        redis_client.eval(_DELETE_IF_VALUE_LUA, 1, key, expected)
        return
    except Exception:  # noqa: BLE001
        pass
    for _attempt in range(3):
        pipe = None
        try:
            pipe = redis_client.pipeline()
            pipe.watch(key)
            if not _redis_value_matches(pipe.get(key), expected):
                pipe.reset()
                return
            pipe.multi()
            pipe.delete(key)
            pipe.execute()
            return
        except Exception:  # noqa: BLE001
            if pipe is not None:
                try:
                    pipe.reset()
                except Exception:  # noqa: BLE001
                    pass


def _generation_liveness_key(
    prefix: str,
    task_id: int,
    external_task_id: str | None = None,
) -> str:
    if external_task_id is None:
        return f"{prefix}:{task_id}"
    digest = hashlib.sha256(str(external_task_id).encode()).hexdigest()[:32]
    return f"{prefix}:{task_id}:{digest}"


def _poll_error_key(task_id: int, external_task_id: str | None = None) -> str:
    return _generation_liveness_key("video:poll:err", task_id, external_task_id)


def mark_poll_alive(task_id: int, external_task_id: str | None = None) -> None:
    try:
        redis_client.set(
            _generation_liveness_key("video:poll:alive", task_id, external_task_id),
            str(external_task_id) if external_task_id is not None else "1",
            ex=POLL_LIVENESS_TTL,
        )
    except Exception:  # noqa: BLE001
        log.warning("failed to mark video poll alive task=%s", task_id, exc_info=True)


def clear_poll_alive(task_id: int, expected_external_task_id: str | None = None) -> None:
    try:
        key = _generation_liveness_key(
            "video:poll:alive",
            task_id,
            expected_external_task_id,
        )
        if expected_external_task_id is None:
            redis_client.delete(key)
        else:
            _delete_if_value(key, str(expected_external_task_id))
    except Exception:  # noqa: BLE001
        log.warning("failed to clear video poll alive task=%s", task_id, exc_info=True)


def mark_video_download_alive(task_id: int, external_task_id: str | None = None) -> None:
    try:
        redis_client.set(
            _generation_liveness_key("video:download:alive", task_id, external_task_id),
            str(external_task_id) if external_task_id is not None else "1",
            ex=VIDEO_DOWNLOAD_LIVENESS_TTL,
        )
    except Exception:  # noqa: BLE001
        log.warning("failed to mark video download alive task=%s", task_id, exc_info=True)


def clear_video_download_alive(
    task_id: int,
    expected_external_task_id: str | None = None,
) -> None:
    try:
        key = _generation_liveness_key(
            "video:download:alive",
            task_id,
            expected_external_task_id,
        )
        if expected_external_task_id is None:
            redis_client.delete(key)
        else:
            _delete_if_value(key, str(expected_external_task_id))
    except Exception:  # noqa: BLE001
        log.warning("failed to clear video download alive task=%s", task_id, exc_info=True)


def bump_poll_errors(task_id: int, external_task_id: str | None = None) -> int:
    try:
        n = incr_window(_poll_error_key(task_id, external_task_id), POLL_LIVENESS_TTL)
        return int(n)
    except Exception:  # noqa: BLE001
        log.warning("failed to bump video poll errors task=%s", task_id, exc_info=True)
        return 0


def reset_poll_errors(task_id: int, external_task_id: str | None = None) -> None:
    try:
        redis_client.delete(_poll_error_key(task_id, external_task_id))
    except Exception:  # noqa: BLE001
        log.warning("failed to reset video poll errors task=%s", task_id, exc_info=True)


def poll_chain_alive(task_id: int, expected_external_task_id: str | None = None) -> bool:
    try:
        value = redis_client.get(
            _generation_liveness_key(
                "video:poll:alive",
                task_id,
                expected_external_task_id,
            )
        )
        if expected_external_task_id is None:
            return bool(value)
        return _redis_value_matches(value, str(expected_external_task_id))
    except Exception:  # noqa: BLE001
        log.warning("failed to read video poll alive task=%s", task_id, exc_info=True)
        return False


def video_download_alive(
    task_id: int,
    expected_external_task_id: str | None = None,
) -> bool:
    try:
        value = redis_client.get(
            _generation_liveness_key(
                "video:download:alive",
                task_id,
                expected_external_task_id,
            )
        )
        if expected_external_task_id is None:
            return bool(value)
        return _redis_value_matches(value, str(expected_external_task_id))
    except Exception:  # noqa: BLE001
        log.warning("failed to read video download alive task=%s", task_id, exc_info=True)
        return False


def local_worker_shutting_down() -> bool:
    try:
        from ..celery_app import is_worker_shutting_down

        return is_worker_shutting_down(check_redis=False)
    except Exception:  # noqa: BLE001
        log.warning("failed to read local worker shutdown flag", exc_info=True)
        return False


def enqueue_poll(task_id: int, expected_external_task_id: str | None = None) -> None:
    """Schedule one poll tick; each tick re-enqueues itself until terminal."""
    from ..tasks import enqueue_with_request_context, poll_video_task
    if local_worker_shutting_down():
        clear_poll_alive(task_id, expected_external_task_id)
        raise RuntimeError("worker is shutting down; video poll will be resumed by recovery")
    try:
        enqueue_with_request_context(poll_video_task, task_id, countdown=VIDEO_POLL_INTERVAL)
    except Exception:
        clear_poll_alive(task_id, expected_external_task_id)
        log.exception("failed to enqueue video poll for task %s", task_id)
        raise


def has_video_download_result(task: GenTask) -> bool:
    params = task.params or {}
    return bool(params.get("_video_result_url") or params.get("_video_result_mock"))


def video_task_action(task: GenTask | None) -> str | None:
    """Return the only safe queue action for a persisted video task state."""
    if (
        task is None
        or task.status != "running"
        or task.category != "video"
        or not task.external_task_id
    ):
        return None
    if task.phase == "polling":
        return "poll"
    if task.phase == "downloading" and has_video_download_result(task):
        return "download"
    return None


def enqueue_video_download(
    task_id: int,
    *,
    countdown: int = 0,
    external_task_id: str | None = None,
) -> None:
    """Schedule video result persistence on the dedicated download queue."""
    from ..tasks import download_video_task, enqueue_with_request_context
    if not external_task_id:
        raise ValueError("video download enqueue requires external_task_id")
    if local_worker_shutting_down():
        clear_video_download_alive(task_id, external_task_id)
        raise RuntimeError("worker is shutting down; video download will be resumed by recovery")
    try:
        enqueue_with_request_context(
            download_video_task,
            task_id,
            external_task_id,
            countdown=countdown,
        )
    except Exception:
        clear_video_download_alive(task_id, external_task_id)
        log.exception("failed to enqueue video download for task %s", task_id)
        raise


def persist_video_download_result(db, task: GenTask, result: dict) -> bool:
    params = dict(task.params or {})
    params["_video_download_started_at"] = datetime.now(timezone.utc).isoformat()
    if result.get("url"):
        params["_video_result_url"] = result["url"]
    provider_usage = usage_from_response(result)
    if provider_usage:
        params["_video_result_usage"] = provider_usage
    if result.get("mock"):
        params["_video_result_mock"] = True
    else:
        params.pop("_video_result_mock", None)
    transitioned = db.execute(
        update(GenTask)
        .where(
            GenTask.id == task.id,
            GenTask.status == "running",
            GenTask.phase == "polling",
            GenTask.external_task_id == task.external_task_id,
        )
        .values(params=params, status="running", phase="downloading")
        .execution_options(synchronize_session=False)
    ).rowcount
    if (transitioned or 0) != 1:
        db.rollback()
        return False
    db.commit()
    clear_poll_alive(task.id, task.external_task_id)
    set_progress(task.id, 90, "running")
    return True


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
