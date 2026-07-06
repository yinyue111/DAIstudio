"""Shared generation task settlement and notification helpers."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models import GenTask
from . import credits
from .generation_pricing import generation_cost_from_snapshot
from .generation_state import (
    NEEDS_REVIEW,
    TERMINAL_STATUSES,
    cancel_requested,
    claim_terminal,
)
from .progress import set_progress
from .user_events import publish_user_event

log = logging.getLogger("generation")


class TaskLockedError(RuntimeError):
    pass


class TaskCanceled(RuntimeError):
    pass


def publish_task_update(task: GenTask | None, status: str) -> None:
    if task is None:
        return
    publish_user_event(
        task.user_id,
        "task_updated",
        {"task_id": task.id, "status": status, "category": task.category, "stage": task.stage},
    )


def raise_if_cancel_requested(db, task: GenTask | None) -> None:
    if cancel_requested(task):
        raise TaskCanceled("用户已取消任务")


def settlement_cost(task: GenTask, model, *, image_count: int | None = None) -> int:
    """Settled credit cost capped by the amount frozen for this task."""
    n = int(image_count if image_count is not None else (task.params or {}).get("n") or 1)
    snapshot = (task.params or {}).get("_model_snapshot") or {}
    if not snapshot:
        snapshot = {
            "cost_credits": int(getattr(model, "cost_credits", 0) or 0),
            "extra": getattr(model, "extra", None) or {},
        }
    cost = generation_cost_from_snapshot(
        snapshot,
        category=task.category,
        stage=task.stage,
        params=task.params or {},
        n=n,
        source_type=task.source_type,
    )
    return max(0, min(int(cost or 0), int(task.cost_frozen or 0)))


def fail_and_refund(db, task_id: int, error: str, *, public_error: str | None = None) -> None:
    try:
        db.rollback()
        task = db.get(GenTask, task_id, populate_existing=True)
        if not task:
            return
        if not claim_terminal(db, task_id, "failed", error=public_error or error):
            db.rollback()
            return
        if task.cost_frozen and int(task.cost_settled or 0) == 0:
            credits.refund(db, task.user_id, task.cost_frozen, biz_ref=task.id, commit=False)
        db.commit()
        set_progress(task_id, 100, "failed")
        publish_task_update(task, "failed")
    except Exception:
        log.exception("fail handler errored for task %s", task_id)
        db.rollback()
        _hold_for_refund_review(db, task_id, f"自动退款失败，请人工确认: {public_error or error}")


def cancel_and_refund(db, task_id: int, error: str = "用户已取消任务") -> None:
    try:
        db.rollback()
        task = db.get(GenTask, task_id, populate_existing=True)
        if not task:
            return
        if not claim_terminal(db, task_id, "canceled", error=error):
            db.rollback()
            return
        if task.cost_frozen and int(task.cost_settled or 0) == 0:
            credits.refund(db, task.user_id, task.cost_frozen, biz_ref=task.id, commit=False)
        db.commit()
        set_progress(task_id, 100, "canceled")
        publish_task_update(task, "canceled")
    except Exception:
        log.exception("cancel handler errored for task %s", task_id)
        db.rollback()
        _hold_for_refund_review(db, task_id, f"自动取消退款失败，请人工确认: {error}")


def _hold_for_refund_review(db, task_id: int, error: str) -> None:
    try:
        task = db.get(GenTask, task_id, populate_existing=True)
        if not task:
            return
        if task.status in TERMINAL_STATUSES:
            return
        task.status = NEEDS_REVIEW
        task.phase = "reconciling"
        task.error = error[:1000]
        task.finished_at = datetime.now(timezone.utc)
        db.commit()
        set_progress(task_id, 100, NEEDS_REVIEW)
        publish_task_update(task, NEEDS_REVIEW)
    except Exception:
        log.exception("refund review handler errored for task %s", task_id)
        db.rollback()


def mark_needs_review(
    db,
    task_id: int,
    error: str,
    *,
    params_update: dict | None = None,
    rollback: bool = True,
) -> None:
    try:
        if rollback:
            db.rollback()
        task = db.get(GenTask, task_id)
        if not task:
            return
        if task.status in TERMINAL_STATUSES:
            return
        if params_update:
            task.params = {**(task.params or {}), **params_update}
        task.status = NEEDS_REVIEW
        task.phase = "reconciling"
        task.error = error[:1000]
        task.finished_at = datetime.now(timezone.utc)
        db.commit()
        set_progress(task_id, 100, NEEDS_REVIEW)
        publish_task_update(task, NEEDS_REVIEW)
    except Exception:
        log.exception("needs-review handler errored for task %s", task_id)
        db.rollback()
