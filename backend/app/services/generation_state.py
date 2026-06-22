"""Shared generation task status helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update

from ..models import GenTask

NEEDS_REVIEW = "needs_review"
TERMINAL_STATUSES = ("succeeded", "failed", NEEDS_REVIEW)


class VideoResultValidationError(RuntimeError):
    """The provider returned a terminal result, but the persisted media is bad."""


def claim_terminal(db, task_id: int, status: str, *, error: str | None = None,
                   cost_settled: int | None = None) -> bool:
    """Atomically move a task into a terminal state.

    Returns True iff this call performed the transition, which keeps settlement
    and refund idempotent when workers, recovery, or duplicate delivery race.
    """
    values: dict = {"status": status, "finished_at": datetime.now(timezone.utc)}
    if error is not None:
        values["error"] = error[:1000]
    if cost_settled is not None:
        values["cost_settled"] = cost_settled
    res = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status.not_in(TERMINAL_STATUSES))
        .values(**values)
    )
    return (res.rowcount or 0) == 1


def is_terminal_status(status: str | None) -> bool:
    return status in TERMINAL_STATUSES
