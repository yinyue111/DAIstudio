"""Task progress in Redis, read by the WS/polling endpoints.

Progress is best-effort telemetry: a Redis hiccup here must NEVER turn an
already-succeeded (committed, charged) task into a failure. So both writes and
reads swallow Redis errors and degrade gracefully.
"""
from __future__ import annotations

import logging

from ..redis_client import blocking_redis_client, redis_client

log = logging.getLogger("progress")


def _k(task_id: int) -> str:
    return f"task:progress:{task_id}"


def _stream_k(task_id: int) -> str:
    return f"task:progress-stream:{task_id}"


PROGRESS_TTL_SECONDS = 3600


def set_progress(task_id: int, percent: int, status: str | None = None) -> None:
    mapping = {"percent": int(percent)}
    if status:
        mapping["status"] = status
    try:
        redis_client.hset(_k(task_id), mapping=mapping)
        redis_client.expire(_k(task_id), PROGRESS_TTL_SECONDS)
        redis_client.xadd(
            _stream_k(task_id),
            {"percent": int(percent), "status": status or ""},
            maxlen=200,
            approximate=True,
        )
        redis_client.expire(_stream_k(task_id), PROGRESS_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — telemetry only, never break the caller
        log.warning("set_progress failed for task %s (ignored)", task_id, exc_info=True)


def get_progress(task_id: int) -> dict:
    try:
        data = redis_client.hgetall(_k(task_id))
    except Exception:  # noqa: BLE001
        log.warning("get_progress failed for task %s (ignored)", task_id, exc_info=True)
        return {"percent": 0, "status": None}
    if not data:
        return {"percent": 0, "status": None}
    return {"percent": int(data.get("percent", 0)), "status": data.get("status")}


def wait_progress_event(task_id: int, last_id: str = "$", *, block_ms: int = 25000) -> tuple[str, dict | None]:
    """Wait for one progress event. Returns (new_last_id, event-or-None)."""
    try:
        rows = blocking_redis_client.xread(
            {_stream_k(task_id): last_id or "$"},
            block=max(0, int(block_ms)),
            count=1,
        )
    except Exception:  # noqa: BLE001
        log.warning("wait_progress_event failed for task %s (ignored)", task_id, exc_info=True)
        return last_id or "$", None
    for _name, entries in rows or []:
        for event_id, fields in entries:
            status = fields.get("status") if isinstance(fields, dict) else None
            percent = fields.get("percent") if isinstance(fields, dict) else 0
            try:
                percent_int = int(percent or 0)
            except (TypeError, ValueError):
                percent_int = 0
            return str(event_id), {"percent": percent_int, "status": status or None}
    return last_id or "$", None
