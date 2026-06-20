"""Tiny Redis locks for task idempotency (prevent double-charge on duplicate
delivery / retries)."""
from __future__ import annotations

import secrets
import time

from ..config import settings
from ..redis_client import redis_client

_RELEASE_IF_OWNER_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""


def acquire(key: str, ttl: int | None = None) -> str | None:
    """Best-effort lock. Returns an owner token if acquired."""
    ttl = ttl or settings.celery_task_time_limit_seconds + 300
    token = secrets.token_urlsafe(24)
    return token if redis_client.set(key, token, nx=True, ex=ttl) else None


def release(key: str, token: str | None = None) -> None:
    if not token:
        try:
            redis_client.delete(key)
        except Exception:
            pass
        return
    for attempt in range(3):
        try:
            redis_client.eval(_RELEASE_IF_OWNER_LUA, 1, key, token)
            return
        except Exception:
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    for attempt in range(3):
        pipe = None
        try:
            pipe = redis_client.pipeline()
            pipe.watch(key)
            if pipe.get(key) != token:
                pipe.reset()
                return
            pipe.multi()
            pipe.delete(key)
            pipe.execute()
            return
        except Exception:
            if pipe is not None:
                try:
                    pipe.reset()
                except Exception:
                    pass
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))


class RedisSemaphore:
    def __init__(
        self,
        key: str,
        limit: int,
        ttl: int = 900,
        *,
        wait_timeout: float = 30.0,
        poll_interval: float = 0.2,
    ):
        self.key = key
        self.limit = max(1, int(limit or 1))
        self.ttl = max(1, int(ttl or 1))
        self.wait_timeout = max(0.0, float(wait_timeout or 0.0))
        self.poll_interval = max(0.05, float(poll_interval or 0.05))
        self.member = f"{time.time_ns()}:{secrets.token_urlsafe(8)}"
        self.acquired = False

    def __enter__(self):
        deadline = time.monotonic() + self.wait_timeout
        while True:
            now = time.time()
            pipe = redis_client.pipeline()
            pipe.zremrangebyscore(self.key, 0, now - self.ttl)
            pipe.zadd(self.key, {self.member: now})
            pipe.zrank(self.key, self.member)
            pipe.expire(self.key, self.ttl)
            _removed, _added, rank, _expire = pipe.execute()
            if rank is not None and int(rank) < self.limit:
                self.acquired = True
                return self
            redis_client.zrem(self.key, self.member)
            if time.monotonic() >= deadline:
                break
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise TimeoutError("全局图片生成并发已满")

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            try:
                redis_client.zrem(self.key, self.member)
            except Exception:
                pass
