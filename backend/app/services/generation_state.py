"""Shared generation task status helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update

from ..models import GenTask

NEEDS_REVIEW = "needs_review"
CANCELED = "canceled"
TERMINAL_STATUSES = ("succeeded", "failed", NEEDS_REVIEW, CANCELED)


class VideoResultValidationError(RuntimeError):
    """The provider returned a terminal result, but the persisted media is bad."""


class LocalVideoSettlementError(RuntimeError):
    """The provider result was persisted, but local credit settlement failed."""


def claim_terminal(
    db,
    task_id: int,
    status: str,
    *,
    error: str | None = None,
    cost_settled: int | None = None,
    expected_status: str | None = None,
    expected_phase: str | None = None,
    expected_external_task_id: str | None = None,
    expected_params: dict | None = None,
) -> bool:
    """Atomically move a task into a terminal state.

    Returns True iff this call performed the transition, which keeps settlement
    and refund idempotent when workers, recovery, or duplicate delivery race.
    """
    values: dict = {"status": status, "finished_at": datetime.now(timezone.utc)}
    if status == "succeeded":
        values["phase"] = None
    if error is not None:
        values["error"] = error[:1000]
    if cost_settled is not None:
        values["cost_settled"] = cost_settled
    # Success settlement is allowed to recover a needs_review task during admin
    # reconciliation, but it must never revive a user-canceled task.
    blocked_statuses = ("succeeded", "failed", CANCELED) if status == "succeeded" else TERMINAL_STATUSES
    conditions = [GenTask.id == task_id, GenTask.status.not_in(blocked_statuses)]
    if expected_status is not None:
        conditions.append(GenTask.status == expected_status)
    if expected_phase is not None:
        conditions.append(GenTask.phase == expected_phase)
    if expected_external_task_id is not None:
        conditions.append(GenTask.external_task_id == expected_external_task_id)
    if expected_params is not None:
        conditions.append(GenTask.params == expected_params)
    res = db.execute(update(GenTask).where(*conditions).values(**values))
    claimed = (res.rowcount or 0) == 1
    if claimed and status == "succeeded":
        from .project_collection import collect_generation_outputs

        collect_generation_outputs(db, task_id)
    return claimed


def is_terminal_status(status: str | None) -> bool:
    return status in TERMINAL_STATUSES


def cancel_requested(task: GenTask | None) -> bool:
    params = getattr(task, "params", None) or {}
    return bool(isinstance(params, dict) and params.get("_cancel_requested"))
