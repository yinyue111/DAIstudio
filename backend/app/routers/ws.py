"""WebSocket progress push: WS /ws/tasks/{id}?ticket=one-time-ticket.

The worker writes progress into Redis streams; this endpoint relays events
until the task reaches a terminal state. Falls back gracefully to DB status.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import ResponseError

from ..config import settings
from ..db import SessionLocal
from ..deps import resolve_token_user
from ..models import GenTask, ReverseOperation
from ..redis_client import redis_client
from ..services.generation import is_terminal_status
from ..services.progress import get_progress, wait_progress_event
from ..services.rate_limit import incr_window
from ..services.user_events import read_user_events

router = APIRouter(tags=["ws"])
log = logging.getLogger("ws")

_AUTH_RECHECK_EVERY = 15  # re-validate token revocation/disable roughly every 15s
_DB_STATUS_RECHECK_EVERY = 2  # keep UI close to the committed task state
_PROGRESS_BLOCK_MS = 5000
_EVENTS_BLOCK_MS = 5000
_TICKET_PREFIX = "ws:task-ticket:"
_EVENT_TICKET_PREFIX = "ws:event-ticket:"
_REVERSE_TICKET_PREFIX = "ws:reverse-ticket:"
_CONNECT_RATE_WINDOW_SECONDS = 60
_ATOMIC_GETDEL_LUA = """
local value = redis.call('GET', KEYS[1])
if value then
  redis.call('DEL', KEYS[1])
end
return value
"""


def _atomic_getdel(key: str) -> str | None:
    """Consume a Redis value exactly once, including on Redis before 6.2."""
    try:
        return redis_client.getdel(key)
    except (AttributeError, ResponseError):
        try:
            return redis_client.eval(_ATOMIC_GETDEL_LUA, 1, key)
        except (AttributeError, ResponseError):
            log.warning("ws_ticket_atomic_consume_unavailable", extra={"ticket_key": key})
            return None


def _ws_connect_allowed(user_id: int, scope: str) -> bool:
    limit = max(1, int(settings.ws_connect_rate_per_minute or 1))
    n = incr_window(f"ws:connect-rate:{scope}:{user_id}", _CONNECT_RATE_WINDOW_SECONDS)
    return n <= limit


def _consume_ws_ticket(ticket: str) -> tuple[int, int, int] | None:
    if not ticket:
        return None
    key = f"{_TICKET_PREFIX}{ticket}"
    raw = _atomic_getdel(key)
    if not raw:
        return None
    try:
        user_id, ticket_task_id, tv = (int(p) for p in str(raw).split(":", 2))
        return user_id, ticket_task_id, tv
    except (TypeError, ValueError):
        return None


def _consume_event_ticket(ticket: str) -> tuple[int, int] | None:
    if not ticket:
        return None
    key = f"{_EVENT_TICKET_PREFIX}{ticket}"
    raw = _atomic_getdel(key)
    if not raw:
        return None
    try:
        user_id, tv = (int(p) for p in str(raw).split(":", 1))
        return user_id, tv
    except (TypeError, ValueError):
        return None


def _consume_reverse_ticket(ticket: str) -> tuple[int, int, int] | None:
    if not ticket:
        return None
    key = f"{_REVERSE_TICKET_PREFIX}{ticket}"
    raw = _atomic_getdel(key)
    if not raw:
        return None
    try:
        user_id, operation_id, token_version = (int(part) for part in str(raw).split(":", 2))
        return user_id, operation_id, token_version
    except (TypeError, ValueError):
        return None


def _validate_task_ws_access(user_id: int, token_version: int, task_id: int) -> bool:
    db = SessionLocal()
    try:
        if resolve_token_user(db, user_id, token_version) is None:
            return False
        task = db.get(GenTask, task_id)
        return bool(task and task.user_id == user_id)
    finally:
        db.close()


def _validate_event_ws_access(user_id: int, token_version: int) -> bool:
    db = SessionLocal()
    try:
        return resolve_token_user(db, user_id, token_version) is not None
    finally:
        db.close()


def _reverse_operation_state(
    user_id: int,
    token_version: int,
    operation_id: int,
) -> tuple[bool, dict | None]:
    db = SessionLocal()
    try:
        if resolve_token_user(db, user_id, token_version) is None:
            return False, None
        operation = db.get(ReverseOperation, operation_id)
        if operation is None or operation.user_id != user_id:
            return True, None
        result = operation.result if isinstance(operation.result, dict) else None
        return True, {
            "id": int(operation.id),
            "status": operation.status,
            "phase": operation.phase,
            "progress": int(operation.progress or 0),
            "result": result,
            "video_analysis": (
                result.get("video_analysis")
                if isinstance(result, dict) and isinstance(result.get("video_analysis"), dict)
                else None
            ),
            "cost_frozen": int(operation.cost_frozen or 0),
            "cost_settled": int(operation.cost_settled or 0),
            "confirmation_expires_at": (
                operation.confirmation_expires_at.isoformat()
                if operation.confirmation_expires_at else None
            ),
            "cancel_requested": bool(operation.cancel_requested),
            "error_code": operation.error_code,
            "error": operation.error,
        }
    finally:
        db.close()


def _task_db_status(user_id: int, token_version: int, task_id: int) -> tuple[bool, str, str | None, str | None]:
    db = SessionLocal()
    try:
        if resolve_token_user(db, user_id, token_version) is None:
            return False, "failed", None, None
        task = db.get(GenTask, task_id)
        if not task or task.user_id != user_id:
            return True, "failed", None, None
        return True, task.status, task.error, task.phase
    finally:
        db.close()


@router.websocket("/ws/tasks/{task_id}")
async def task_progress(websocket: WebSocket, task_id: int, ticket: str = ""):
    ticket_payload = _consume_ws_ticket(ticket)
    if not ticket_payload:
        await websocket.close(code=4401)
        return
    user_id, ticket_task_id, tv = ticket_payload
    if ticket_task_id != task_id:
        await websocket.close(code=4404)
        return

    # Enforce token revocation / disabled-account at connect, like the REST
    # guard, without blocking the event loop on synchronous SQLAlchemy I/O.
    if not await asyncio.to_thread(_validate_task_ws_access, user_id, tv, task_id):
        await websocket.close(code=4404)
        return
    if not await asyncio.to_thread(_ws_connect_allowed, user_id, "task"):
        await websocket.close(code=4408)
        return

    await websocket.accept()
    ticks = 0
    last_progress_id = "0"
    try:
        while True:
            ticks += 1
            # Fast path: the worker writes percent+status into Redis. Still
            # re-check DB frequently because terminal status is committed there
            # before/independently of best-effort Redis progress telemetry.
            prog = get_progress(task_id)
            status = prog["status"]
            percent = prog["percent"]
            error = None
            phase = None
            # Open a DB session when we need authoritative status (queued/terminal)
            # OR on the periodic auth re-check (honours mid-stream logout/disable).
            recheck_auth = ticks % _AUTH_RECHECK_EVERY == 0
            recheck_task = ticks % _DB_STATUS_RECHECK_EVERY == 0
            if status is None or is_terminal_status(status) or recheck_auth or recheck_task:
                ok, db_status, db_error, db_phase = await asyncio.to_thread(_task_db_status, user_id, tv, task_id)
                if not ok:
                    await websocket.close(code=4401)
                    return
                if is_terminal_status(db_status) or status is None or is_terminal_status(status):
                    status = db_status
                    error = db_error
                phase = db_phase
            if is_terminal_status(status):
                percent = 100
            if not await asyncio.to_thread(_validate_task_ws_access, user_id, tv, task_id):
                await websocket.close(code=4401)
                return
            await websocket.send_json(
                {"task_id": task_id, "status": status, "percent": percent,
                 "phase": phase, "error": error}
            )
            if is_terminal_status(status):
                break
            last_progress_id, event = await asyncio.to_thread(
                wait_progress_event,
                task_id,
                last_progress_id,
                block_ms=_PROGRESS_BLOCK_MS,
            )
            if event is not None:
                # Ensure the next loop sends the freshest hash/DB-backed state.
                continue
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            log.debug("task websocket close failed", exc_info=True)


@router.websocket("/ws/prompt/reverse-operations/{operation_id}")
async def reverse_operation_progress(
    websocket: WebSocket,
    operation_id: int,
    ticket: str = "",
):
    ticket_payload = _consume_reverse_ticket(ticket)
    if not ticket_payload:
        await websocket.close(code=4401)
        return
    user_id, ticket_operation_id, token_version = ticket_payload
    if ticket_operation_id != operation_id:
        await websocket.close(code=4404)
        return
    ok, state = await asyncio.to_thread(
        _reverse_operation_state,
        user_id,
        token_version,
        operation_id,
    )
    if not ok:
        await websocket.close(code=4401)
        return
    if state is None:
        await websocket.close(code=4404)
        return
    if not await asyncio.to_thread(_ws_connect_allowed, user_id, "reverse"):
        await websocket.close(code=4408)
        return

    await websocket.accept()
    last_state: dict | None = None
    heartbeat_ticks = 0
    try:
        while True:
            ok, state = await asyncio.to_thread(
                _reverse_operation_state,
                user_id,
                token_version,
                operation_id,
            )
            if not ok:
                await websocket.close(code=4401)
                return
            if state is None:
                await websocket.close(code=4404)
                return
            heartbeat_ticks += 1
            if state != last_state or heartbeat_ticks >= 10:
                await websocket.send_json(state)
                last_state = state
                heartbeat_ticks = 0
            if state["status"] in {"succeeded", "failed", "canceled"}:
                break
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            log.debug("reverse operation websocket close failed", exc_info=True)


@router.websocket("/ws/events")
async def user_events(websocket: WebSocket, ticket: str = "", last_id: str = "$"):
    ticket_payload = _consume_event_ticket(ticket)
    if not ticket_payload:
        await websocket.close(code=4401)
        return
    user_id, tv = ticket_payload
    if not await asyncio.to_thread(_validate_event_ws_access, user_id, tv):
        await websocket.close(code=4401)
        return
    if not await asyncio.to_thread(_ws_connect_allowed, user_id, "events"):
        await websocket.close(code=4408)
        return

    await websocket.accept()
    cursor = last_id or "$"
    ticks = 0
    try:
        while True:
            ticks += 1
            cursor, events = await asyncio.to_thread(
                read_user_events,
                user_id,
                cursor,
                block_ms=_EVENTS_BLOCK_MS,
                count=20,
            )
            if not await asyncio.to_thread(_validate_event_ws_access, user_id, tv):
                await websocket.close(code=4401)
                return
            if events:
                await websocket.send_json({"events": events, "last_id": cursor})
            else:
                await websocket.send_json({"events": [], "last_id": cursor, "heartbeat": True})
            if ticks % _AUTH_RECHECK_EVERY == 0:
                if not await asyncio.to_thread(_validate_event_ws_access, user_id, tv):
                    await websocket.close(code=4401)
                    return
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            log.debug("event websocket close failed", exc_info=True)
