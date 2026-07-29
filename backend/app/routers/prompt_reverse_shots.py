"""Reverse result revisions, shot editing, and shot generation routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import ReverseOperation, User
from ..schemas import (
    ReverseOperationOut,
    ReverseResultApplyIn,
    ReverseResultApplyOut,
    ReverseResultRevisionIn,
    ReverseResultRevisionOut,
    ReverseShotGenerationPrepareIn,
    ReverseShotGenerationPrepareOut,
    ReverseShotReanalyzeIn,
    ReverseShotTimelineEditIn,
)
from ..services import credits, reverse_operations
from ..services.generation_image_evidence import (
    ReviewedEvidenceMaskError,
    validate_saved_reviewed_image_evidence,
)
from .prompt_shared import _CLIENT_REVISION_SOURCES, _assert_text_allowed, _rate_limit

router = APIRouter()


@router.get(
    "/reverse-operations/{operation_id}/revisions",
    response_model=list[ReverseResultRevisionOut],
)
def list_reverse_operation_revisions(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.list_result_revisions(
            db,
            operation_id=operation_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/revisions",
    response_model=ReverseResultRevisionOut,
)
def create_reverse_operation_revision(
    operation_id: int,
    body: ReverseResultRevisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.source not in _CLIENT_REVISION_SOURCES:
        raise HTTPException(422, "客户端只能创建 user_edit 或 applied 反推结果版本")
    try:
        operation = reverse_operations.get_owned_operation(db, operation_id, user.id)
        validate_saved_reviewed_image_evidence(
            operation,
            body.payload,
            require_all_confirmed=body.source == "applied",
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewedEvidenceMaskError as exc:
        raise HTTPException(422, f"图片证据审阅数据无效：{exc}") from exc
    _assert_text_allowed(db, body.payload)
    try:
        return reverse_operations.create_result_revision(
            db,
            operation_id=operation_id,
            user_id=user.id,
            source=body.source,
            payload=body.payload,
            parent_revision_id=body.parent_revision_id,
            clear_image_evidence=body.clear_image_evidence,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/apply",
    response_model=ReverseResultApplyOut,
)
def apply_reverse_operation_result(
    operation_id: int,
    body: ReverseResultApplyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _assert_text_allowed(db, body.payload)
    try:
        edited, applied = reverse_operations.apply_result_revision(
            db,
            operation_id=operation_id,
            user_id=user.id,
            payload=body.payload,
            parent_revision_id=body.parent_revision_id,
            clear_image_evidence=body.clear_image_evidence,
        )
        return {"user_edit": edited, "applied": applied}
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/shots/edit",
    response_model=ReverseResultRevisionOut,
)
def edit_reverse_operation_shots(
    operation_id: int,
    body: ReverseShotTimelineEditIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.edit_shot_timeline(
            db,
            operation_id=operation_id,
            user_id=user.id,
            body=body,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/shots/reanalyze",
    response_model=ReverseOperationOut,
    status_code=202,
)
def reanalyze_reverse_operation_shot(
    operation_id: int,
    body: ReverseShotReanalyzeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _rate_limit(user.id)
        operation_body, reanalysis_context = reverse_operations.shot_reanalysis_body(
            db,
            operation_id=operation_id,
            user_id=user.id,
            client_request_id=body.client_request_id,
            shot_id=body.shot_id,
            analysis_precision=body.analysis_precision,
            include_audio=body.include_audio,
        )
        operation, created = reverse_operations.create_operation(
            db,
            user_id=user.id,
            body=operation_body,
        )
        operation = reverse_operations.attach_shot_reanalysis_context(
            db,
            operation=operation,
            user_id=user.id,
            context=reanalysis_context,
        )
        if created:
            try:
                reverse_operations.enqueue_operation(int(operation.id))
            except Exception as exc:  # noqa: BLE001
                if reverse_operations.fail_queued_submission(
                    int(operation.id),
                    code="BROKER_UNAVAILABLE",
                    error="单镜头重分析任务队列暂时不可用,已退回冻结积分",
                ):
                    raise HTTPException(503, "单镜头重分析任务队列暂时不可用") from exc
                db.expire_all()
                operation = db.get(ReverseOperation, int(operation.id)) or operation
        return reverse_operations.serialize_operation(operation)
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except credits.InsufficientCredits as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/shots/prepare-generation",
    response_model=ReverseShotGenerationPrepareOut,
)
def prepare_reverse_operation_shot_generation(
    operation_id: int,
    body: ReverseShotGenerationPrepareIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.prepare_shot_generation(
            db,
            operation_id=operation_id,
            user_id=user.id,
            shot_id=body.shot_id,
            revision_id=body.revision_id,
            client_request_id=body.client_request_id,
            model_config_id=body.model_config_id,
            params=body.params,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
