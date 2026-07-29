"""Quality feedback for completed reverse operations."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReverseOperationFeedback
from .reverse_operation_records import _get_owned
from .reverse_quotes import ReverseOperationConflict, utcnow


def get_feedback(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
) -> ReverseOperationFeedback | None:
    _get_owned(db, operation_id, user_id)
    return db.execute(
        select(ReverseOperationFeedback).where(
            ReverseOperationFeedback.operation_id == operation_id
        )
    ).scalar_one_or_none()


def upsert_feedback(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    rating: str,
    issue_types: list[str],
    note: str | None,
) -> ReverseOperationFeedback:
    operation = _get_owned(db, operation_id, user_id)
    if operation.status != "succeeded":
        raise ReverseOperationConflict("反推任务成功后才能提交质量反馈")
    row = db.execute(
        select(ReverseOperationFeedback).where(
            ReverseOperationFeedback.operation_id == operation_id
        )
    ).scalar_one_or_none()
    if row is None:
        row = ReverseOperationFeedback(operation_id=operation_id, user_id=user_id)
        db.add(row)
    row.rating = rating
    row.issue_types = list(dict.fromkeys(issue_types))
    row.note = str(note or "").strip() or None
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return row
