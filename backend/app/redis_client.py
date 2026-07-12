"""Shared Redis connection (verification codes, rate limits, task progress)."""
from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit, urlunsplit

import redis

from .config import settings


def redis_connection_kwargs() -> dict:
    redis_settings = settings.redis
    kwargs = {
        "decode_responses": True,
        "max_connections": int(redis_settings.max_connections),
        "socket_connect_timeout": float(redis_settings.socket_connect_timeout_seconds),
        "socket_timeout": float(redis_settings.socket_timeout_seconds),
        "health_check_interval": int(redis_settings.health_check_interval_seconds),
        "retry_on_timeout": True,
    }
    if redis_settings.password:
        kwargs["password"] = redis_settings.password
    return kwargs


def celery_redis_url() -> str:
    """Return a broker URL with the separately supplied password encoded safely."""
    redis_settings = settings.redis
    if not redis_settings.password:
        return redis_settings.url
    parsed = urlsplit(redis_settings.url)
    host = parsed.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    username = quote(unquote(parsed.username or ""), safe="")
    auth = f"{username}:{quote(redis_settings.password, safe='')}@"
    return urlunsplit((parsed.scheme, f"{auth}{host}{port}", parsed.path, parsed.query, parsed.fragment))


redis_client = redis.from_url(settings.redis.url, **redis_connection_kwargs())
blocking_redis_client = redis.from_url(
    settings.redis.url,
    **{
        **redis_connection_kwargs(),
        "max_connections": max(2, min(10, int(settings.redis.max_connections))),
    },
)
