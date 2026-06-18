"""Tiny Redis locks for task idempotency (prevent double-charge on duplicate
delivery / retries)."""
from __future__ import annotations

from ..config import settings
from ..redis_client import redis_client


def acquire(key: str, ttl: int | None = None) -> bool:
    """Best-effort lock. Returns True if acquired."""
    ttl = ttl or settings.celery_task_time_limit_seconds + 300
    return bool(redis_client.set(key, "1", nx=True, ex=ttl))


def release(key: str) -> None:
    try:
        redis_client.delete(key)
    except Exception:
        pass
