"""Best-effort per-user event stream for UI notifications.

Redis Stream is used only as delivery acceleration. Callers must never depend
on this module for accounting or task state: publish failures are swallowed so
payment, credits, and generation remain the source of truth.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..redis_client import blocking_redis_client, redis_client

log = logging.getLogger("user_events")

STREAM_TTL_SECONDS = 6 * 3600
STREAM_MAXLEN = 500


def _key(user_id: int) -> str:
    return f"user:events:{int(user_id)}"


def publish_user_event(user_id: int, event_type: str, payload: dict[str, Any] | None = None) -> None:
    try:
        redis_client.xadd(
            _key(user_id),
            {
                "type": str(event_type),
                "payload": json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
            },
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        redis_client.expire(_key(user_id), STREAM_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        log.warning("publish_user_event failed for user %s type %s", user_id, event_type, exc_info=True)


def read_user_events(user_id: int, last_id: str = "0", *, block_ms: int = 25000, count: int = 20) -> tuple[str, list[dict]]:
    """Return (new_last_id, events). Empty list means timeout/no events."""
    stream_key = _key(user_id)
    try:
        rows = blocking_redis_client.xread({stream_key: last_id or "0"}, block=max(0, int(block_ms)), count=max(1, int(count)))
    except Exception:  # noqa: BLE001
        log.warning("read_user_events failed for user %s", user_id, exc_info=True)
        return last_id or "0", []
    events: list[dict] = []
    next_last_id = last_id or "0"
    for _name, entries in rows or []:
        for event_id, fields in entries:
            next_last_id = str(event_id)
            payload = {}
            raw_payload = fields.get("payload") if isinstance(fields, dict) else None
            if raw_payload:
                try:
                    parsed = json.loads(raw_payload)
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            events.append({
                "id": str(event_id),
                "type": str(fields.get("type") or "event") if isinstance(fields, dict) else "event",
                "payload": payload,
            })
    return next_last_id, events
