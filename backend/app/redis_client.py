"""Shared Redis connection (verification codes, rate limits, task progress)."""
from __future__ import annotations

import redis

from .config import settings

redis_client = redis.from_url(settings.redis_url, decode_responses=True)
