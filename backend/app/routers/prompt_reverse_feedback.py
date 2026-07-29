"""Reverse-operation feedback HTTP routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import ReverseOperationFeedbackIn, ReverseOperationFeedbackOut
from ..services import reverse_operations
from .prompt_shared import _assert_text_allowed

router = APIRouter()


@router.get(
    "/reverse-operations/{operation_id}/feedback",
    response_model=ReverseOperationFeedbackOut | None,
)
def get_reverse_operation_feedback(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.get_feedback(
            db,
            operation_id=operation_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put(
    "/reverse-operations/{operation_id}/feedback",
    response_model=ReverseOperationFeedbackOut,
)
def save_reverse_operation_feedback(
    operation_id: int,
    body: ReverseOperationFeedbackIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _assert_text_allowed(db, body.note)
    try:
        return reverse_operations.upsert_feedback(
            db,
            operation_id=operation_id,
            user_id=user.id,
            rating=body.rating,
            issue_types=body.issue_types,
            note=body.note,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
