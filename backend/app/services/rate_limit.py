"""Atomic Redis fixed-window counters used by lightweight API limits."""
from __future__ import annotations

import time

from ..redis_client import redis_client

_INCR_WINDOW_LUA = """
local current = redis.call("incr", KEYS[1])
if current == 1 then
  redis.call("expire", KEYS[1], ARGV[1])
end
return current
"""


def incr_window(key: str, window_seconds: int) -> int:
    """Increment ``key`` and ensure the TTL is installed atomically.

    Redis ``INCR`` followed by ``EXPIRE`` can leave permanent counters if the
    worker dies between the two commands. Lua is the production path; the
    pipeline fallback keeps fakeredis/older test doubles working.
    """
    seconds = max(1, int(window_seconds or 1))
    for attempt in range(2):
        try:
            return int(redis_client.eval(_INCR_WINDOW_LUA, 1, key, seconds) or 0)
        except Exception:
            if attempt == 0:
                time.sleep(0.01)
    pipe = redis_client.pipeline(transaction=True)
    pipe.incr(key)
    pipe.ttl(key)
    current, ttl = pipe.execute()
    if int(ttl or 0) < 0:
        redis_client.expire(key, seconds)
    return int(current or 0)
