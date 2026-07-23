"""Transactional workflow dispatch outbox and broker reconciliation."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import WorkflowDispatch, WorkflowRun

log = logging.getLogger("workflow.dispatch")
MAX_PUBLISH_ATTEMPTS = 8
MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class DispatchOutcome:
    status: str
    reconciliation_required: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _backoff(attempt: int) -> int:
    return min(MAX_BACKOFF_SECONDS, 2 ** min(max(1, attempt), 8))


def ensure_orchestrator_dispatch(
    db: Session,
    *,
    run: WorkflowRun,
    scheduled_for: datetime | None = None,
    dedupe_suffix: str | None = None,
) -> WorkflowDispatch:
    suffix = dedupe_suffix or f"revision:{int(run.revision or 0)}"
    dedupe_key = f"workflow:{int(run.id)}:orchestrate:{suffix}"[:128]
    existing = db.scalar(
        select(WorkflowDispatch).where(WorkflowDispatch.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return existing
    row = WorkflowDispatch(
        workflow_run_id=int(run.id),
        node_run_id=None,
        kind="orchestrate",
        dedupe_key=dedupe_key,
        celery_task_id=f"pending-{uuid4().hex}",
        status="pending",
        publish_attempts=0,
        scheduled_for=scheduled_for,
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.flush()
    row.celery_task_id = f"workflow-run-{int(run.id)}-dispatch-{int(row.id)}"
    db.flush()
    return row


def create_node_dispatch(
    db: Session,
    *,
    run_id: int,
    node_id: int,
    attempt_number: int,
    dispatch_token: str,
) -> WorkflowDispatch:
    dedupe_key = f"workflow-node:{int(node_id)}:attempt:{int(attempt_number)}"
    existing = db.scalar(
        select(WorkflowDispatch).where(WorkflowDispatch.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return existing
    row = WorkflowDispatch(
        workflow_run_id=int(run_id),
        node_run_id=int(node_id),
        kind="node",
        dedupe_key=dedupe_key,
        celery_task_id=f"pending-{uuid4().hex}",
        dispatch_token=dispatch_token,
        status="pending",
        publish_attempts=0,
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.flush()
    row.celery_task_id = f"workflow-node-{int(node_id)}-dispatch-{int(row.id)}"
    db.flush()
    return row


def mark_dispatch_delivered(dispatch_id: int | None) -> None:
    if dispatch_id is None:
        return
    db = SessionLocal()
    try:
        row = db.scalar(
            select(WorkflowDispatch)
            .where(WorkflowDispatch.id == int(dispatch_id))
            .with_for_update()
        )
        if row is None or row.status == "completed":
            db.rollback()
            return
        now = utcnow()
        row.status = "completed"
        row.completed_at = now
        row.next_attempt_at = None
        row.last_error_code = None
        row.last_error = None
        row.updated_at = now
        db.commit()
    finally:
        db.close()


def publish_dispatch(db: Session, dispatch_id: int) -> DispatchOutcome:
    row = db.get(WorkflowDispatch, int(dispatch_id))
    if row is None:
        raise RuntimeError("workflow dispatch is missing")
    if row.status == "completed":
        return DispatchOutcome("completed", False)
    if row.status in {"failed", "needs_review"}:
        return DispatchOutcome(row.status, False)

    now = utcnow()
    row.status = "publishing"
    row.publish_attempts = int(row.publish_attempts or 0) + 1
    row.next_attempt_at = now + timedelta(seconds=_backoff(row.publish_attempts))
    row.updated_at = now
    db.commit()

    scheduled_for = _aware(row.scheduled_for)
    countdown = (
        max(0, int((scheduled_for - utcnow()).total_seconds()) + 1)
        if scheduled_for is not None
        else None
    )
    try:
        from ..tasks import (
            enqueue_with_request_context,
            workflow_node_task,
            workflow_run_task,
        )

        if row.kind == "node":
            if row.node_run_id is None or not row.dispatch_token:
                raise RuntimeError("node dispatch is incomplete")
            result = enqueue_with_request_context(
                workflow_node_task,
                int(row.workflow_run_id),
                int(row.node_run_id),
                row.dispatch_token,
                int(row.id),
                countdown=countdown,
                task_id=row.celery_task_id,
            )
        else:
            result = enqueue_with_request_context(
                workflow_run_task,
                int(row.workflow_run_id),
                int(row.id),
                countdown=countdown,
                task_id=row.celery_task_id,
            )
        if str(result.id) != row.celery_task_id:
            raise RuntimeError("Celery 返回了与预留 task ID 不一致的结果")
    except Exception as exc:  # noqa: BLE001 - ambiguous broker acknowledgement is durable
        db.rollback()
        row = db.get(WorkflowDispatch, int(dispatch_id), populate_existing=True)
        if row is None:
            raise
        if row.status == "completed":
            return DispatchOutcome("completed", False)
        row.status = "unknown"
        row.last_error_code = "BROKER_ACK_UNKNOWN"
        row.last_error = str(exc)[:2000]
        row.next_attempt_at = utcnow() + timedelta(seconds=_backoff(row.publish_attempts))
        row.updated_at = utcnow()
        db.commit()
        log.warning("workflow dispatch acknowledgement unknown dispatch=%s", row.id)
        return DispatchOutcome("unknown", True)

    db.expire_all()
    row = db.get(WorkflowDispatch, int(dispatch_id))
    if row is None:
        raise RuntimeError("workflow dispatch disappeared after publish")
    if row.status == "completed":
        return DispatchOutcome("completed", False)
    row.status = "published"
    row.published_at = utcnow()
    row.next_attempt_at = None
    row.last_error_code = None
    row.last_error = None
    row.updated_at = utcnow()
    db.commit()
    return DispatchOutcome("published", False)


def reconcile_dispatches(db: Session, *, limit: int = 100) -> dict[str, int]:
    now = utcnow()
    rows = list(
        db.scalars(
            select(WorkflowDispatch)
            .where(
                WorkflowDispatch.status.in_(("pending", "publishing", "unknown")),
                or_(
                    WorkflowDispatch.next_attempt_at.is_(None),
                    WorkflowDispatch.next_attempt_at <= now,
                ),
            )
            .order_by(WorkflowDispatch.next_attempt_at, WorkflowDispatch.id)
            .limit(max(1, min(int(limit), 500)))
        )
    )
    counts = {
        "scanned": len(rows),
        "published": 0,
        "completed": 0,
        "unknown": 0,
        "needs_review": 0,
    }
    for row in rows:
        if int(row.publish_attempts or 0) >= MAX_PUBLISH_ATTEMPTS:
            row.status = "needs_review"
            row.last_error_code = "BROKER_ACK_UNRESOLVED"
            row.last_error = "工作流任务发布状态持续未知"
            row.next_attempt_at = None
            row.updated_at = now
            db.commit()
            counts["needs_review"] += 1
            continue
        outcome = publish_dispatch(db, int(row.id))
        counts[outcome.status if outcome.status in counts else "published"] += 1
    return counts
