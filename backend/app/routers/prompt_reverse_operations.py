"""Reverse-prompt operation lifecycle and legacy HTTP routes."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import ReverseOperation, User
from ..redis_client import redis_client
from ..schemas import (
    ReverseIn,
    ReverseOperationConfirm,
    ReverseOperationCreate,
    ReverseOperationOut,
    ReverseOperationRetryIn,
    ReverseOut,
)
from ..services import credits, locks, project_collection, reverse_operations
from ..services.config_store import get_setting
from ..services.product_edition import feature_enabled
from .prompt_shared import (
    _REVERSE_STATUSES,
    _REVERSE_WS_TICKET_TTL_SECONDS,
    SsrfError,
    _acquire_reverse_quote_lock,
    _assert_text_allowed,
    _deprecated_reverse_endpoint,
    _existing_reverse_operation_replay,
    _legacy_cover_confirmation_required,
    _rate_limit,
    _reverse_operation_response,
    _reverse_request_fingerprint,
    _validate_local_video_selection,
    _validate_reverse_asset_request,
    assert_safe_user_asset_url,
    incr_window,
    log,
)

router = APIRouter()
lifecycle_router = APIRouter()
legacy_router = APIRouter()


@router.post(
    "/reverse-operations",
    response_model=ReverseOperationOut,
    status_code=202,
)
def create_reverse_operation(
    body: ReverseOperationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.project_id is not None and not feature_enabled("projects_enabled"):
        raise HTTPException(404, "not found")
    lock_key, lock_token = _acquire_reverse_quote_lock(user.id, body.quote_id)
    try:
        try:
            replay = reverse_operations.find_quoted_idempotent_operation(
                db,
                user_id=user.id,
                body=body,
            )
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        if replay is not None:
            return reverse_operations.serialize_operation(replay)

        if body.project_id is not None:
            try:
                project_collection.require_owned_project(db, user.id, body.project_id)
            except project_collection.ProjectNotFound as exc:
                raise HTTPException(404, str(exc)) from exc
        for source in body.sources:
            _validate_reverse_asset_request(
                db,
                asset_url=source.asset_url,
                fallback_image=body.fallback_image if source.role == "primary" else None,
                target=body.target,
                source_type=source.source_type,
            )
        _validate_local_video_selection(body, db, user)
        if not get_setting(db, "reverse_prompt_enabled", True):
            raise HTTPException(403, "反推功能已被管理员关闭")
        _rate_limit(user.id)
        try:
            operation, created = reverse_operations.create_quoted_operation(
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
        try:
            reverse_operations.enqueue_operation(int(operation.id))
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to enqueue reverse operation %s", operation.id)
            if reverse_operations.fail_queued_submission(
                int(operation.id),
                code="BROKER_UNAVAILABLE",
                error="反推任务队列暂时不可用,已退回冻结积分",
            ):
                raise HTTPException(503, "反推任务队列暂时不可用,请稍后重试") from exc
            db.expire_all()
            advanced = db.get(ReverseOperation, int(operation.id))
            if advanced is None:
                raise HTTPException(
                    503,
                    "反推任务队列状态不确定,请使用原 client_request_id 重试",
                ) from exc
            operation = advanced
            log.warning(
                "reverse operation %s advanced to %s after enqueue raised; "
                "returning operation",
                operation.id,
                operation.status,
            )
    db.expire_all()
    operation = db.get(ReverseOperation, int(operation.id)) or operation
    return reverse_operations.serialize_operation(operation)


@router.get("/reverse-operations", response_model=list[ReverseOperationOut])
def list_reverse_operations(
    status: str | None = None,
    target: str | None = None,
    source_type: str | None = None,
    analysis_focus: str | None = None,
    include_audio: bool | None = None,
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if status is not None and status not in _REVERSE_STATUSES:
        raise HTTPException(422, "反推任务状态非法")
    rows = reverse_operations.list_owned_operations(
        db,
        user_id=user.id,
        status=status,
        target=target,
        source_type=source_type,
        analysis_focus=analysis_focus,
        include_audio=include_audio,
        limit=limit,
        offset=offset,
    )
    return [reverse_operations.serialize_operation(row) for row in rows]


@router.get("/reverse-operations/{operation_id}", response_model=ReverseOperationOut)
def get_reverse_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        operation = reverse_operations.get_owned_operation(db, operation_id, user.id)
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_operation(operation)


@lifecycle_router.post(
    "/reverse-operations/{operation_id}/retry",
    response_model=ReverseOperationOut,
    status_code=202,
)
def retry_reverse_operation(
    operation_id: int,
    body: ReverseOperationRetryIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    lock_key, lock_token = _acquire_reverse_quote_lock(user.id, body.quote_id)
    try:
        previous, retry_body = reverse_operations.build_retry_operation_body(
            db,
            operation_id=operation_id,
            user_id=user.id,
            client_request_id=body.client_request_id,
            model_config_id=body.model_config_id,
            quote_id=body.quote_id,
        )
        replay = reverse_operations.find_quoted_idempotent_operation(
            db,
            user_id=user.id,
            body=retry_body,
            retry_of_operation_id=int(previous.id),
        )
        if replay is not None:
            return reverse_operations.serialize_operation(replay)
        context = dict(previous.request_context or {})
        sources = context.get("sources") if isinstance(context.get("sources"), list) else []
        if sources:
            for source in sources:
                _validate_reverse_asset_request(
                    db,
                    asset_url=str(source.get("asset_url") or ""),
                    fallback_image=(
                        context.get("fallback_image")
                        if source.get("role") == "primary"
                        else None
                    ),
                    target=previous.target,
                    source_type=source.get("source_type"),
                )
        else:
            _validate_reverse_asset_request(
                db,
                asset_url=previous.asset_url,
                fallback_image=context.get("fallback_image"),
                target=previous.target,
                source_type=context.get("source_type"),
            )
        operation, created = reverse_operations.retry_operation(
            db,
            operation_id=operation_id,
            user_id=user.id,
            client_request_id=body.client_request_id,
            quote_id=body.quote_id,
            model_config_id=body.model_config_id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
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
        try:
            reverse_operations.enqueue_operation(int(operation.id))
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to enqueue reverse retry %s", operation.id)
            if reverse_operations.fail_queued_submission(
                int(operation.id),
                code="BROKER_UNAVAILABLE",
                error="反推任务队列暂时不可用,已退回冻结积分",
            ):
                raise HTTPException(503, "反推任务队列暂时不可用,请稍后重试") from exc
    db.expire_all()
    return reverse_operations.serialize_operation(
        db.get(ReverseOperation, int(operation.id)) or operation
    )


@lifecycle_router.post(
    "/reverse-operations/{operation_id}/confirm-cover",
    response_model=ReverseOperationOut,
    status_code=202,
)
def confirm_reverse_operation_cover(
    operation_id: int,
    body: ReverseOperationConfirm,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.fallback_image:
        _assert_text_allowed(db, body.fallback_image)
        try:
            assert_safe_user_asset_url(body.fallback_image)
        except SsrfError as exc:
            raise HTTPException(400, f"封面链接被安全策略拦截:{exc}") from exc
    try:
        operation = reverse_operations.confirm_cover(
            db,
            operation_id=operation_id,
            user_id=user.id,
            fallback_image=body.fallback_image,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        reverse_operations.enqueue_operation(operation_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to enqueue confirmed reverse operation %s", operation_id)
        if reverse_operations.fail_queued_submission(
            operation_id,
            code="BROKER_UNAVAILABLE",
            error="反推任务队列暂时不可用,已退回冻结积分",
        ):
            raise HTTPException(503, "反推任务队列暂时不可用,请稍后重试") from exc
        db.expire_all()
        advanced = db.get(ReverseOperation, operation_id)
        if advanced is None:
            raise HTTPException(503, "反推任务队列状态不确定,请稍后查询任务状态") from exc
        operation = advanced
        log.warning(
            "confirmed reverse operation %s advanced to %s after enqueue raised; "
            "returning operation",
            operation.id,
            operation.status,
        )
    db.expire_all()
    operation = db.get(ReverseOperation, operation_id) or operation
    return reverse_operations.serialize_operation(operation)


@lifecycle_router.post(
    "/reverse-operations/{operation_id}/cancel",
    response_model=ReverseOperationOut,
)
def cancel_reverse_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        operation = reverse_operations.request_cancel(
            db,
            operation_id=operation_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_operation(operation)


@lifecycle_router.post("/reverse-operations/{operation_id}/ws-ticket")
def create_reverse_operation_ws_ticket(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    operation = db.get(ReverseOperation, operation_id)
    if operation is None or operation.user_id != user.id:
        raise HTTPException(404, "反推任务不存在")
    count = incr_window(f"ws:ticket-rate:reverse:{user.id}", 60)
    if count > max(1, int(settings.ws_ticket_rate_per_minute or 1)):
        raise HTTPException(429, "WebSocket 连接过于频繁,请稍后再试")
    ticket = secrets.token_urlsafe(32)
    redis_client.setex(
        f"ws:reverse-ticket:{ticket}",
        _REVERSE_WS_TICKET_TTL_SECONDS,
        f"{user.id}:{operation_id}:{user.token_version}",
    )
    return {"ticket": ticket, "expires_in": _REVERSE_WS_TICKET_TTL_SECONDS}


@legacy_router.post("/reverse", response_model=ReverseOut, deprecated=True)
@_deprecated_reverse_endpoint
def reverse(
    body: ReverseIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    response: Response = None,
):
    if body.project_id is not None and not feature_enabled("projects_enabled"):
        raise HTTPException(404, "not found")
    if body.project_id is not None:
        try:
            project_collection.require_owned_project(db, user.id, body.project_id)
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    replay = _existing_reverse_operation_replay(db, user_id=user.id, body=body)
    if replay is not None:
        return replay
    _validate_reverse_asset_request(
        db,
        asset_url=body.asset_url,
        fallback_image=body.fallback_image,
        target=body.target,
        source_type=body.source_type,
    )
    _validate_local_video_selection(body, db, user)
    if not get_setting(db, "reverse_prompt_enabled", True):
        raise HTTPException(403, "反推功能已被管理员关闭")
    _rate_limit(user.id)
    try:
        operation, created = reverse_operations.create_legacy_operation(
            db,
            user_id=user.id,
            body=body,
            fingerprint=_reverse_request_fingerprint(body),
        )
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        status_code = 403 if str(exc) == "反推功能已被管理员关闭" else 400
        raise HTTPException(status_code, str(exc)) from exc
    except project_collection.ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except credits.InsufficientCredits as exc:
        raise HTTPException(400, str(exc)) from exc

    if not created:
        if operation.status == "succeeded":
            return _reverse_operation_response(operation)
        if operation.status == "needs_confirmation":
            raise _legacy_cover_confirmation_required(operation)
        if operation.status == "running":
            raise HTTPException(409, "反推请求仍在处理中,请稍后重试")
        if operation.status in {"failed", "canceled"}:
            raise HTTPException(409, "该反推请求已关闭,请重新发起")

    reverse_operations.run_operation(int(operation.id))
    db.expire_all()
    operation = db.get(ReverseOperation, int(operation.id))
    if operation is None:
        raise HTTPException(502, "反推任务状态丢失,请稍后重试")
    if operation.status == "succeeded":
        return _reverse_operation_response(operation)
    if operation.status == "needs_confirmation":
        raise _legacy_cover_confirmation_required(operation)
    if operation.status == "canceled":
        raise HTTPException(409, "反推任务已取消,积分已退回")

    detail = operation.error or "反推失败,请稍后重试"
    if operation.error_code in {
        "CONTENT_SAFETY_BLOCKED",
        "INVALID_OPERATION",
        "REQUEST_REJECTED",
    }:
        raise HTTPException(400, detail)
    if operation.error_code == "ASSET_NOT_FOUND":
        raise HTTPException(404, detail)
    if operation.error_code == "MODEL_CONFIG_CHANGED":
        raise HTTPException(409, detail)
    raise HTTPException(502, detail)
