"""Transactional generation dispatch outbox and broker reconciliation."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..models import GenerationDispatch, GenTask
from . import credits
from .generation_state import TERMINAL_STATUSES

log = logging.getLogger("generation.dispatch")

MAX_PUBLISH_ATTEMPTS = 8
MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class DispatchOutcome:
    status: str
    reconciliation_required: bool


class PrePublishError(RuntimeError):
    """Explicit proof that a publisher rejected before contacting the broker."""


def validate_dispatch_request(category: str) -> None:
    """Run deterministic validation before task, quote, or credit mutation."""
    if category not in {"image", "video"}:
        raise ValueError("unsupported generation dispatch category")
    json.dumps({"args": [0], "task": task_name_for_category(category)})


def task_name_for_category(category: str) -> str:
    return "generate.image" if category == "image" else "generate.video"


def deterministic_celery_task_id(
    task_id: int,
    attempt: int,
    *,
    dispatch_id: int | None = None,
) -> str:
    prefix = f"generation-{int(task_id)}-attempt-{int(attempt)}"
    return f"{prefix}-dispatch-{int(dispatch_id)}" if dispatch_id is not None else prefix


def create_dispatch_intent(
    db: Session,
    task: GenTask,
    *,
    attempt: int | None = None,
) -> GenerationDispatch:
    if attempt is None:
        highest = db.scalar(
            select(func.max(GenerationDispatch.attempt)).where(
                GenerationDispatch.task_id == task.id
            )
        )
        attempt = int(highest or 0) + 1
    dispatch = GenerationDispatch(
        task_id=int(task.id),
        attempt=int(attempt),
        task_name=task_name_for_category(task.category),
        # A temporary unique value lets the row obtain its durable identity.
        # The final value below is deterministic and survives every re-publish.
        celery_task_id=f"pending-{uuid.uuid4().hex}",
        status="pending",
        publish_attempts=0,
        next_attempt_at=datetime.now(timezone.utc),
    )
    db.add(dispatch)
    db.flush()
    dispatch.celery_task_id = deterministic_celery_task_id(
        task.id,
        attempt,
        dispatch_id=dispatch.id,
    )
    db.flush()
    return dispatch


def latest_dispatch(db: Session, task_id: int) -> GenerationDispatch | None:
    return db.scalar(
        select(GenerationDispatch)
        .where(GenerationDispatch.task_id == task_id)
        .order_by(GenerationDispatch.attempt.desc())
        .limit(1)
    )


def _publish_backoff(attempt: int) -> int:
    return min(MAX_BACKOFF_SECONDS, 2 ** min(max(1, attempt), 8))


def _definitely_not_published(exc: Exception) -> bool:
    # Once the broker call begins, transport and wrapper exceptions are
    # ambiguous: the message may have been accepted before the caller observed
    # an error. Only an explicit publisher contract can authorize compensation.
    return isinstance(exc, PrePublishError)


def _terminal(task: GenTask | None) -> bool:
    return bool(task and task.status in TERMINAL_STATUSES)


def _delivered(task: GenTask | None) -> bool:
    # The worker's queued -> running CAS is durable proof that the broker
    # delivered at least one copy. Re-publishing after that point only creates
    # duplicate deliveries and can falsely exhaust reconciliation attempts.
    return bool(task and (task.status == "running" or _terminal(task)))


def _mark_completed(db: Session, dispatch: GenerationDispatch) -> None:
    now = datetime.now(timezone.utc)
    dispatch.status = "completed"
    dispatch.completed_at = now
    dispatch.next_attempt_at = None
    dispatch.last_error_code = None
    db.commit()


def _compensate_definite_failure(
    db: Session,
    dispatch: GenerationDispatch,
    task: GenTask,
) -> None:
    now = datetime.now(timezone.utc)
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task.id, GenTask.status == "queued")
        .values(
            status="failed",
            phase=None,
            error="任务入队失败,冻结积分已退回",
            finished_at=now,
        )
    ).rowcount
    if (claimed or 0) == 1 and int(task.cost_frozen or 0) > 0:
        credits.refund(
            db,
            task.user_id,
            int(task.cost_frozen),
            biz_ref=task.id,
            commit=False,
        )
    dispatch.status = "failed"
    dispatch.last_error_code = "BROKER_REJECTED"
    dispatch.last_error_at = now
    dispatch.next_attempt_at = None
    db.commit()


def _mark_unknown(
    db: Session,
    dispatch: GenerationDispatch,
    task: GenTask,
) -> DispatchOutcome:
    now = datetime.now(timezone.utc)
    dispatch.status = "unknown"
    dispatch.last_error_code = "BROKER_ACK_UNKNOWN"
    dispatch.last_error_at = now
    dispatch.next_attempt_at = now + timedelta(
        seconds=_publish_backoff(int(dispatch.publish_attempts or 0))
    )
    if task.status == "queued":
        task.phase = "reconciling"
    db.commit()
    return DispatchOutcome(status="unknown", reconciliation_required=True)


def _claim_dispatch_needs_review(db: Session, task_id: int, now: datetime) -> bool:
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status == "queued")
        .values(
            status="needs_review",
            phase="reconciling",
            error="任务发布状态持续未知,冻结积分保留待人工核对",
            finished_at=now,
        )
    ).rowcount
    return (claimed or 0) == 1


def publish_dispatch(db: Session, dispatch_id: int) -> DispatchOutcome:
    dispatch = db.get(GenerationDispatch, dispatch_id)
    if dispatch is None:
        raise RuntimeError("generation dispatch is missing")
    task = db.get(GenTask, dispatch.task_id)
    if task is None:
        dispatch.status = "failed"
        dispatch.last_error_code = "TASK_MISSING"
        dispatch.next_attempt_at = None
        db.commit()
        return DispatchOutcome(status="failed", reconciliation_required=False)
    if _delivered(task):
        _mark_completed(db, dispatch)
        return DispatchOutcome(status="completed", reconciliation_required=False)

    dispatch.status = "publishing"
    dispatch.publish_attempts = int(dispatch.publish_attempts or 0) + 1
    dispatch.next_attempt_at = datetime.now(timezone.utc) + timedelta(
        seconds=_publish_backoff(dispatch.publish_attempts)
    )
    db.commit()

    try:
        from ..tasks import enqueue_with_request_context, generate_image_task, generate_video_task

        celery_task = generate_image_task if task.category == "image" else generate_video_task
        enqueue_with_request_context(
            celery_task,
            int(task.id),
            task_id=dispatch.celery_task_id,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        dispatch = db.get(GenerationDispatch, dispatch_id, populate_existing=True)
        task = db.get(GenTask, dispatch.task_id, populate_existing=True) if dispatch else None
        if dispatch is None or task is None:
            raise
        if _delivered(task):
            _mark_completed(db, dispatch)
            return DispatchOutcome(status="completed", reconciliation_required=False)
        if _definitely_not_published(exc):
            _compensate_definite_failure(db, dispatch, task)
            raise HTTPException(
                503,
                detail={
                    "code": "GENERATION_DISPATCH_REJECTED",
                    "message": "任务未能发布,冻结积分已退回",
                    "task_id": int(task.id),
                    "dispatch_status": "failed",
                },
            ) from exc
        log.warning(
            "generation dispatch acknowledgement unknown task=%s dispatch=%s",
            task.id,
            dispatch.id,
        )
        return _mark_unknown(db, dispatch, task)

    db.expire_all()
    dispatch = db.get(GenerationDispatch, dispatch_id)
    task = db.get(GenTask, dispatch.task_id) if dispatch else None
    if dispatch is None or task is None:
        raise RuntimeError("generation dispatch disappeared after publish")
    if _delivered(task):
        _mark_completed(db, dispatch)
        return DispatchOutcome(status="completed", reconciliation_required=False)
    dispatch.status = "published"
    dispatch.published_at = datetime.now(timezone.utc)
    dispatch.next_attempt_at = None
    dispatch.last_error_code = None
    db.commit()
    return DispatchOutcome(status="published", reconciliation_required=False)


def reconcile_dispatches(db: Session, *, limit: int = 50) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    rows = list(
        db.scalars(
            select(GenerationDispatch)
            .where(
                GenerationDispatch.status.in_(("pending", "publishing", "unknown")),
                or_(
                    GenerationDispatch.next_attempt_at.is_(None),
                    GenerationDispatch.next_attempt_at <= now,
                ),
            )
            .order_by(GenerationDispatch.next_attempt_at, GenerationDispatch.id)
            .limit(max(1, min(int(limit), 200)))
        )
    )
    counts = {"scanned": len(rows), "published": 0, "unknown": 0, "completed": 0, "needs_review": 0, "failed": 0}
    for row in rows:
        task = db.get(GenTask, row.task_id)
        if _delivered(task):
            _mark_completed(db, row)
            counts["completed"] += 1
            continue
        if int(row.publish_attempts or 0) >= MAX_PUBLISH_ATTEMPTS:
            if task is not None and task.status == "queued" and not _claim_dispatch_needs_review(
                db,
                int(task.id),
                now,
            ):
                # A worker can win queued -> running after our SELECT but before
                # the escalation CAS. Reload before changing the dispatch so a
                # stale ORM object can never overwrite the worker's claim.
                db.rollback()
                task = db.get(GenTask, row.task_id, populate_existing=True)
                row = db.get(GenerationDispatch, row.id, populate_existing=True)
                if row is None:
                    continue
                if _delivered(task):
                    _mark_completed(db, row)
                    counts["completed"] += 1
                    continue
            row.status = "needs_review"
            row.last_error_code = "BROKER_ACK_UNRESOLVED"
            row.last_error_at = now
            row.next_attempt_at = None
            db.commit()
            counts["needs_review"] += 1
            continue
        try:
            outcome = publish_dispatch(db, row.id)
        except HTTPException:
            counts["failed"] += 1
            continue
        counts[outcome.status if outcome.status in counts else "published"] += 1
    return counts
