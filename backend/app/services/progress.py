"""Task progress in Redis, read by the WS/polling endpoints.

Progress is best-effort telemetry: a Redis hiccup here must NEVER turn an
already-succeeded (committed, charged) task into a failure. So both writes and
reads swallow Redis errors and degrade gracefully.
"""
from __future__ import annotations

import logging

from ..redis_client import redis_client

log = logging.getLogger("progress")


def _k(task_id: int) -> str:
    return f"task:progress:{task_id}"


def set_progress(task_id: int, percent: int, status: str | None = None) -> None:
    mapping = {"percent": int(percent)}
    if status:
        mapping["status"] = status
    try:
        redis_client.hset(_k(task_id), mapping=mapping)
        redis_client.expire(_k(task_id), 3600)
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
