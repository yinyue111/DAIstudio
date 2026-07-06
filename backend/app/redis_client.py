"""Shared Redis connection (verification codes, rate limits, task progress)."""
from __future__ import annotations

import redis

from .config import settings


def redis_connection_kwargs() -> dict:
    redis_settings = settings.redis
    return {
        "decode_responses": True,
        "max_connections": int(redis_settings.max_connections),
        "socket_connect_timeout": float(redis_settings.socket_connect_timeout_seconds),
        "socket_timeout": float(redis_settings.socket_timeout_seconds),
        "health_check_interval": int(redis_settings.health_check_interval_seconds),
        "retry_on_timeout": True,
    }


redis_client = redis.from_url(settings.redis.url, **redis_connection_kwargs())
blocking_redis_client = redis.from_url(
    settings.redis.url,
    **{
        **redis_connection_kwargs(),
        "max_connections": max(2, min(10, int(settings.redis.max_connections))),
    },
)
