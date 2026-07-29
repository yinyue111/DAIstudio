"""Reverse-operation timeout, expiry, and queue recovery."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import case, select, update

from ..config import settings
from ..db import SessionLocal
from ..models import ReverseOperation
from .reverse_quotes import REPUBLISH_AFTER, _log_operation_event, utcnow

log = logging.getLogger("reverse_operations")


def _fail_stale_running(
    operation_id: int,
    *,
    cutoff: datetime,
    now: datetime,
    refund_locked: Callable,
) -> bool:
    """Timeout a still-stale run and refund it in the same transaction."""
    db = SessionLocal()
    try:
        claimed = db.execute(
            update(ReverseOperation)
            .where(
                ReverseOperation.id == operation_id,
                ReverseOperation.status == "running",
                ReverseOperation.updated_at < cutoff,
                ReverseOperation.cost_frozen > 0,
            )
            .values(
                status=case(
                    (ReverseOperation.cancel_requested.is_(True), "canceled"),
                    else_="failed",
                ),
                phase=None,
                progress=100,
                error_code=case(
                    (ReverseOperation.cancel_requested.is_(True), "CANCELED"),
                    else_="OPERATION_TIMEOUT",
                ),
                error=case(
                    (ReverseOperation.cancel_requested.is_(True), None),
                    else_="反推任务超时,已自动失败并退回积分",
                ),
                confirmation_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
            .returning(ReverseOperation.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if claimed is None:
            db.rollback()
            return False
        operation = db.execute(
            select(ReverseOperation)
            .where(ReverseOperation.id == operation_id)
            .execution_options(populate_existing=True)
        ).scalar_one()
        refund_locked(db, operation, note="stale reverse operation timed out")
        db.commit()
        _log_operation_event(
            (
                "reverse_operation_canceled"
                if operation.status == "canceled"
                else "reverse_operation_timed_out"
            ),
            operation_id=operation_id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
            level=logging.WARNING,
        )
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reap_operations(
    *,
    refund_locked: Callable,
    enqueue_operation: Callable[[int], str],
) -> dict[str, int]:
    """Expire confirmations, fail stale runs, and republish stale queued rows."""
    now = utcnow()
    stale_running_cutoff = now - timedelta(
        seconds=max(30 * 60, int(settings.reverse_gateway_timeout_seconds or 0) + 5 * 60)
    )
    db = SessionLocal()
    try:
        expired = list(
            db.execute(
                select(ReverseOperation.id).where(
                    ReverseOperation.status == "needs_confirmation",
                    ReverseOperation.confirmation_expires_at <= now,
                )
            ).scalars()
        )
        stale_running = list(
            db.execute(
                select(ReverseOperation.id).where(
                    ReverseOperation.status == "running",
                    ReverseOperation.updated_at < stale_running_cutoff,
                    ReverseOperation.cost_frozen > 0,
                )
            ).scalars()
        )
        stale_queued = list(
            db.execute(
                select(ReverseOperation.id).where(
                    ReverseOperation.status == "queued",
                    ReverseOperation.updated_at < now - REPUBLISH_AFTER,
                    ReverseOperation.cost_frozen > 0,
                )
            ).scalars()
        )
    finally:
        db.close()

    counts = {"expired": 0, "failed": 0, "republished": 0, "legacy_failed": 0}
    for operation_id in expired:
        db = SessionLocal()
        try:
            operation = db.execute(
                select(ReverseOperation)
                .where(ReverseOperation.id == int(operation_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if operation is None or operation.status != "needs_confirmation":
                db.rollback()
                continue
            refund_locked(db, operation, note="cover confirmation expired")
            operation.status = "canceled"
            operation.phase = None
            operation.progress = 100
            operation.error_code = "CONFIRMATION_EXPIRED"
            operation.error = "封面降级确认已过期,积分已全额退回"
            operation.confirmation_expires_at = None
            operation.finished_at = now
            operation.updated_at = now
            db.commit()
            _log_operation_event(
                "reverse_operation_confirmation_expired",
                operation_id=operation.id,
                status=operation.status,
                phase=operation.phase,
                error_code=operation.error_code,
                level=logging.WARNING,
            )
            counts["expired"] += 1
        finally:
            db.close()
    for operation_id in stale_running:
        if _fail_stale_running(
            int(operation_id),
            cutoff=stale_running_cutoff,
            now=now,
            refund_locked=refund_locked,
        ):
            counts["failed"] += 1
    for operation_id in stale_queued:
        try:
            enqueue_operation(int(operation_id))
            counts["republished"] += 1
        except Exception:  # noqa: BLE001
            log.exception("failed to republish reverse operation %s", operation_id)
    # Keep upgrade compatibility for pre-0035 rows that consumed credits
    # synchronously and therefore have no frozen reservation.
    from . import retention

    legacy_db = SessionLocal()
    try:
        counts["legacy_failed"] = retention.reap_stuck_reverse_operations(legacy_db)
    finally:
        legacy_db.close()
    return counts
