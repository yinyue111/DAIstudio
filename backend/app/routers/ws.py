"""WebSocket progress push: WS /ws/tasks/{id}?ticket=one-time-ticket.

The worker writes progress into Redis; this endpoint relays it until the task
reaches a terminal state. Falls back gracefully to DB status.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..db import SessionLocal
from ..deps import resolve_token_user
from ..models import GenTask
from ..redis_client import redis_client
from ..services.generation import is_terminal_status
from ..services.progress import get_progress

router = APIRouter(tags=["ws"])

_AUTH_RECHECK_EVERY = 15  # re-validate token revocation/disable roughly every 15s
_DB_STATUS_RECHECK_EVERY = 2  # keep UI close to the committed task state
_TICKET_PREFIX = "ws:task-ticket:"


def _consume_ws_ticket(ticket: str) -> tuple[int, int, int] | None:
    if not ticket:
        return None
    key = f"{_TICKET_PREFIX}{ticket}"
    try:
        raw = redis_client.getdel(key)
    except AttributeError:
        raw = redis_client.get(key)
        if raw is not None:
            redis_client.delete(key)
    if not raw:
        return None
    try:
        user_id, ticket_task_id, tv = (int(p) for p in str(raw).split(":", 2))
        return user_id, ticket_task_id, tv
    except (TypeError, ValueError):
        return None


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

    db = SessionLocal()
    try:
        # enforce token revocation / disabled-account at connect, like the REST guard
        if resolve_token_user(db, user_id, tv) is None:
            await websocket.close(code=4401)
            return
        task = db.get(GenTask, task_id)
        if not task or task.user_id != user_id:
            await websocket.close(code=4404)
            return
    finally:
        db.close()

    await websocket.accept()
    ticks = 0
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
                db = SessionLocal()
                try:
                    if resolve_token_user(db, user_id, tv) is None:
                        await websocket.close(code=4401)
                        return
                    task = db.get(GenTask, task_id)
                    db_status = task.status if task else "failed"
                    if is_terminal_status(db_status) or status is None or is_terminal_status(status):
                        status = db_status
                        error = task.error if task else None
                    phase = task.phase if task else None
                finally:
                    db.close()
            if is_terminal_status(status):
                percent = 100
            await websocket.send_json(
                {"task_id": task_id, "status": status, "percent": percent,
                 "phase": phase, "error": error}
            )
            if is_terminal_status(status):
                break
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
