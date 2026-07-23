"""Studio-only prompt optimization endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..prompt_optimization_schemas import (
    PromptOptimizationStatus,
    StudioPromptOptimizationDecisionIn,
    StudioPromptOptimizationDecisionOut,
    StudioPromptOptimizationDetailOut,
    StudioPromptOptimizationIn,
    StudioPromptOptimizationOut,
    StudioPromptOptimizationPageOut,
)
from ..services import prompt_optimization

router = APIRouter(prefix="/api/studio/prompt-optimizations", tags=["studio"])


def _raise_http(exc: prompt_optimization.PromptOptimizationError):
    raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("", response_model=StudioPromptOptimizationPageOut)
def list_prompt_optimizations(
    proposal_status: PromptOptimizationStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return prompt_optimization.list_proposals(
        db,
        user_id=user.id,
        status=proposal_status,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=StudioPromptOptimizationOut, status_code=status.HTTP_201_CREATED)
def create_prompt_optimization(
    body: StudioPromptOptimizationIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return prompt_optimization.create_proposal(db, user_id=user.id, body=body)
    except prompt_optimization.PromptOptimizationError as exc:
        _raise_http(exc)


@router.get("/{proposal_id}", response_model=StudioPromptOptimizationDetailOut)
def get_prompt_optimization(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return prompt_optimization.get_proposal(
            db,
            user_id=user.id,
            proposal_id=proposal_id,
        )
    except prompt_optimization.PromptOptimizationError as exc:
        _raise_http(exc)


@router.post("/{proposal_id}/accept", response_model=StudioPromptOptimizationDecisionOut)
def accept_prompt_optimization(
    proposal_id: int,
    body: StudioPromptOptimizationDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return prompt_optimization.decide_proposal(
            db,
            user_id=user.id,
            proposal_id=proposal_id,
            proposal_version=body.proposal_version,
            idempotency_key=body.idempotency_key,
            accepted_segment_ids=body.accepted_segment_ids,
            rejected_segment_ids=body.rejected_segment_ids,
        )
    except prompt_optimization.PromptOptimizationError as exc:
        _raise_http(exc)


@router.post("/{proposal_id}/reject", response_model=StudioPromptOptimizationDecisionOut)
def reject_prompt_optimization(
    proposal_id: int,
    body: StudioPromptOptimizationDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return prompt_optimization.decide_proposal(
            db,
            user_id=user.id,
            proposal_id=proposal_id,
            proposal_version=body.proposal_version,
            idempotency_key=body.idempotency_key,
            accepted_segment_ids=[],
            rejected_segment_ids=[],
            reject_all=True,
        )
    except prompt_optimization.PromptOptimizationError as exc:
        _raise_http(exc)
