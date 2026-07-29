"""Reverse-prompt batch HTTP routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import ReverseBatchCreate, ReverseBatchOut
from ..services import credits, locks, project_collection, reverse_operations
from ..services.config_store import get_setting
from .prompt_shared import (
    _acquire_reverse_quote_lock,
    _rate_limit,
    _validate_local_video_selection,
    _validate_reverse_asset_request,
    log,
)

router = APIRouter()


@router.post(
    "/reverse-batches",
    response_model=ReverseBatchOut,
    status_code=202,
)
def create_reverse_batch(
    body: ReverseBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    lock_key, lock_token = _acquire_reverse_quote_lock(user.id, body.quote_id)
    try:
        try:
            replay = reverse_operations.find_quoted_idempotent_batch(
                db,
                user_id=user.id,
                body=body,
            )
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        if replay is not None:
            return reverse_operations.serialize_batch(db, replay, include_items=True)

        if body.project_id is not None:
            try:
                project_collection.require_owned_project(db, user.id, body.project_id)
            except project_collection.ProjectNotFound as exc:
                raise HTTPException(404, str(exc)) from exc
        operation_bodies = reverse_operations.batch_operation_bodies(body)
        for operation_body in operation_bodies:
            for source in operation_body.sources:
                _validate_reverse_asset_request(
                    db,
                    asset_url=source.asset_url,
                    fallback_image=(
                        operation_body.fallback_image
                        if source.role == "primary"
                        else None
                    ),
                    target=operation_body.target,
                    source_type=source.source_type,
                )
            _validate_local_video_selection(operation_body, db, user)
        if not get_setting(db, "reverse_prompt_enabled", True):
            raise HTTPException(403, "反推功能已被管理员关闭")
        for _ in operation_bodies:
            _rate_limit(user.id)
        try:
            batch, created, operation_ids = reverse_operations.create_quoted_batch(
                db,
                user_id=user.id,
                body=body,
            )
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except credits.InsufficientCredits as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        locks.release(lock_key, lock_token)

    if created:
        for operation_id in operation_ids:
            try:
                reverse_operations.enqueue_operation(operation_id)
            except Exception:  # noqa: BLE001
                log.exception("failed to enqueue reverse batch item %s", operation_id)
                reverse_operations.fail_queued_submission(
                    operation_id,
                    code="BROKER_UNAVAILABLE",
                    error="反推任务队列暂时不可用,该批次单项已退回冻结积分",
                )
    db.expire_all()
    batch = reverse_operations.get_owned_batch(
        db,
        batch_id=int(batch.id),
        user_id=user.id,
    )
    return reverse_operations.serialize_batch(db, batch, include_items=True)


@router.get("/reverse-batches", response_model=list[ReverseBatchOut])
def list_reverse_batches(
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    batches = reverse_operations.list_owned_batches(
        db,
        user_id=user.id,
        limit=limit,
        offset=offset,
    )
    return [
        reverse_operations.serialize_batch(db, batch, include_items=False)
        for batch in batches
    ]


@router.get("/reverse-batches/{batch_id}", response_model=ReverseBatchOut)
def get_reverse_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        batch = reverse_operations.get_owned_batch(
            db,
            batch_id=batch_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_batch(db, batch, include_items=True)


@router.post("/reverse-batches/{batch_id}/cancel", response_model=ReverseBatchOut)
def cancel_reverse_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        batch = reverse_operations.request_batch_cancel(
            db,
            batch_id=batch_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_batch(db, batch, include_items=True)
