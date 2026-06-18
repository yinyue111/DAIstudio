"""Persist per-call gateway usage for real-cost accounting.

Best-effort: a logging failure must never break the generation/reverse flow.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import GatewayCall

log = logging.getLogger("usage")


def record_call(db: Session, *, kind: str, model_id: str | None = None,
                user_id: int | None = None, task_id: int | None = None,
                status: str = "ok", latency_ms: int | None = None,
                usage: dict | None = None, detail: dict | None = None) -> None:
    u = usage or {}
    try:
        db.add(GatewayCall(
            user_id=user_id,
            task_id=task_id,
            kind=kind,
            model_id=model_id,
            status=status,
            latency_ms=latency_ms,
            prompt_tokens=u.get("prompt_tokens"),
            completion_tokens=u.get("completion_tokens"),
            total_tokens=u.get("total_tokens"),
            detail=detail,
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("failed to record gateway call (kind=%s)", kind)
