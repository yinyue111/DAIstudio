"""Queue dispatch and lifecycle transitions for reverse operations."""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ReverseOperation
from ..schemas import ReverseIn
from . import credits
from .config_store import ModelConfigResolutionError, resolve_model_config
from .model_capabilities import ModelCapabilityError, assert_reverse_capability
from .model_gateway_config import runtime_config_for_model
from .reverse_batches import sync_batch_for_operation
from .reverse_operation_records import _get_owned
from .reverse_quotes import (
    BIZ_TYPE,
    CONFIRMATION_TTL,
    TERMINAL_STATUSES,
    ReverseOperationConflict,
    ReverseOperationInvalid,
    ReverseOperationNotFound,
    _assert_supported_vision_runtime,
    _aware,
    _log_operation_event,
    _model_snapshot,
    _reverse_cost_for_model,
    _template_snapshot,
    utcnow,
)
from .video_analysis import normalize_video_analysis_preset

log = logging.getLogger("reverse_operations")


def enqueue_operation(operation_id: int) -> str:
    from ..tasks import enqueue_with_request_context, reverse_operation_task

    celery_id = ""
    status = None
    phase = None
    db = SessionLocal()
    try:
        operation = db.execute(
            select(ReverseOperation)
            .where(ReverseOperation.id == int(operation_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if operation is None:
            raise ReverseOperationNotFound("反推任务不存在")
        celery_id = str(operation.celery_task_id or "")
        if operation.status != "queued":
            db.rollback()
            if celery_id:
                return celery_id
            raise ReverseOperationConflict("当前反推任务不在待投递状态")
        if not celery_id:
            celery_id = str(uuid4())
            operation.celery_task_id = celery_id
            operation.updated_at = utcnow()
            db.commit()
        else:
            db.rollback()
        status = operation.status
        phase = operation.phase
    finally:
        db.close()

    result = enqueue_with_request_context(
        reverse_operation_task,
        int(operation_id),
        task_id=celery_id,
    )
    returned_id = str(result.id)
    if returned_id != celery_id:
        raise RuntimeError("Celery 返回了与预留 task ID 不一致的结果")
    _log_operation_event(
        "reverse_operation_enqueued",
        operation_id=int(operation_id),
        status=status,
        phase=phase,
        celery_task_id=celery_id,
    )
    return celery_id


def _refund_locked(db: Session, operation: ReverseOperation, *, note: str) -> None:
    frozen = int(operation.cost_frozen or 0)
    if frozen:
        credits.refund(
            db,
            operation.user_id,
            frozen,
            operation.id,
            biz_type=BIZ_TYPE,
            commit=False,
        )
        operation.cost_frozen = 0
    legacy = int(operation.charged_credits or 0)
    if legacy:
        credits.refund_consumed(
            db,
            operation.user_id,
            legacy,
            biz_type="reverse",
            biz_ref=operation.id,
            note=note,
            commit=False,
        )
        operation.charged_credits = 0


def fail_operation(operation_id: int, *, code: str, error: str) -> bool:
    db = SessionLocal()
    try:
        operation = db.execute(
            select(ReverseOperation)
            .where(ReverseOperation.id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if operation is None or operation.status in TERMINAL_STATUSES:
            db.rollback()
            return False
        _refund_locked(db, operation, note=f"failed code={code}")
        canceled = bool(operation.cancel_requested)
        operation.status = "canceled" if canceled else "failed"
        operation.phase = None
        operation.progress = 100
        operation.error_code = "CANCELED" if canceled else code[:64]
        operation.error = None if canceled else str(error)[:2000]
        operation.finished_at = utcnow()
        operation.confirmation_expires_at = None
        operation.updated_at = utcnow()
        db.commit()
        sync_batch_for_operation(db, operation_id)
        _log_operation_event(
            "reverse_operation_closed",
            operation_id=operation_id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
            level=logging.WARNING,
        )
        return True
    except Exception:
        db.rollback()
        log.exception("failed to close reverse operation %s", operation_id)
        raise
    finally:
        db.close()


def fail_queued_submission(operation_id: int, *, code: str, error: str) -> bool:
    """Fail and refund only while a broker-owned operation is queued."""
    db = SessionLocal()
    now = utcnow()
    try:
        claimed = db.execute(
            update(ReverseOperation)
            .where(
                ReverseOperation.id == operation_id,
                ReverseOperation.status == "queued",
            )
            .values(
                status="failed",
                phase=None,
                progress=100,
                error_code=code[:64],
                error=str(error)[:2000],
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
        _refund_locked(db, operation, note=f"failed queue submission code={code}")
        db.commit()
        sync_batch_for_operation(db, operation_id)
        _log_operation_event(
            "reverse_operation_queue_submission_failed",
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


def request_cancel(db: Session, *, operation_id: int, user_id: int) -> ReverseOperation:
    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id, ReverseOperation.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None:
        raise ReverseOperationNotFound("反推任务不存在")
    if operation.status in TERMINAL_STATUSES:
        db.rollback()
        return _get_owned(db, operation_id, user_id)
    if operation.status == "running":
        operation.cancel_requested = True
        operation.updated_at = utcnow()
        db.commit()
        sync_batch_for_operation(db, operation_id)
        db.refresh(operation)
        _log_operation_event(
            "reverse_operation_cancel_requested",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
        )
        return operation

    _refund_locked(db, operation, note="user canceled reverse operation")
    operation.status = "canceled"
    operation.phase = None
    operation.progress = 100
    operation.cancel_requested = True
    operation.error_code = "CANCELED"
    operation.error = None
    operation.finished_at = utcnow()
    operation.confirmation_expires_at = None
    operation.updated_at = utcnow()
    db.commit()
    sync_batch_for_operation(db, operation_id)
    db.refresh(operation)
    _log_operation_event(
        "reverse_operation_canceled",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
    )
    return operation


def _expire_cover_confirmation(
    db: Session,
    operation: ReverseOperation,
    *,
    now: Any,
) -> None:
    _refund_locked(db, operation, note="cover confirmation expired")
    operation.status = "canceled"
    operation.phase = None
    operation.progress = 100
    operation.error_code = "CONFIRMATION_EXPIRED"
    operation.error = "封面降级确认已过期,积分已全额退回"
    operation.finished_at = now
    operation.confirmation_expires_at = None
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


def confirm_cover(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    fallback_image: str | None,
) -> ReverseOperation:
    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id, ReverseOperation.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None:
        raise ReverseOperationNotFound("反推任务不存在")
    if operation.status != "needs_confirmation":
        raise ReverseOperationConflict("当前反推任务不需要封面确认")
    now = utcnow()
    expires_at = _aware(operation.confirmation_expires_at)
    if expires_at is not None and expires_at <= now:
        _expire_cover_confirmation(db, operation, now=now)
        raise ReverseOperationConflict("封面降级确认已过期,积分已全额退回")
    context = dict(operation.request_context or {})
    effective_fallback = (fallback_image or context.get("fallback_image") or "").strip()
    if not effective_fallback:
        raise ReverseOperationInvalid("请先提供用于单帧分析的封面图")
    try:
        model = resolve_model_config(
            db,
            "vision",
            getattr(operation, "model_config_id", None),
            require_enabled=False,
        )
        assert_reverse_capability(model, target=operation.target, source_type="image")
    except (ModelConfigResolutionError, ModelCapabilityError) as exc:
        raise ReverseOperationInvalid(str(exc)) from exc
    context["fallback_image"] = effective_fallback
    context["cover_confirmed"] = True
    context["cover_confirmed_at"] = now.isoformat()
    if operation.celery_task_id:
        context["previous_celery_task_id"] = operation.celery_task_id
    operation.request_context = context
    operation.model_config_id = model.id
    operation.status = "queued"
    operation.phase = "queued"
    operation.progress = 0
    operation.celery_task_id = None
    operation.confirmation_expires_at = None
    operation.error_code = None
    operation.error = None
    operation.updated_at = now
    db.commit()
    db.refresh(operation)
    _log_operation_event(
        "reverse_operation_cover_confirmed",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
    )
    return operation


def transition_legacy_to_confirmation(
    db: Session,
    *,
    operation_id: int,
    body: ReverseIn,
    video_analysis: dict[str, Any] | None,
    reason: str,
) -> ReverseOperation:
    """Convert a pre-v2 synchronous charge into an async frozen reservation."""
    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None or operation.status != "running":
        raise ReverseOperationConflict("反推请求已被其他恢复流程关闭")
    try:
        model = resolve_model_config(
            db,
            "vision",
            getattr(operation, "model_config_id", None)
            or getattr(body, "model_config_id", None),
            require_enabled=False,
        )
    except ModelConfigResolutionError as exc:
        raise ReverseOperationInvalid(str(exc)) from exc
    runtime = runtime_config_for_model(model, "vision")
    _assert_supported_vision_runtime(runtime)
    charged = int(operation.charged_credits or 0)
    if charged:
        credits.refund_consumed(
            db,
            operation.user_id,
            charged,
            biz_type="reverse",
            biz_ref=operation.id,
            note="convert legacy reverse to async confirmation",
            commit=False,
        )
        credits.freeze(
            db,
            operation.user_id,
            charged,
            operation.id,
            biz_type=BIZ_TYPE,
            commit=False,
        )
    preset = normalize_video_analysis_preset(body.video_analysis_preset)
    context = body.model_dump(mode="json", exclude_none=True)
    context["video_analysis_preset"] = preset
    context["cover_confirmation_required"] = True
    context["cover_confirmation_required_at"] = utcnow().isoformat()
    operation.request_context = context
    operation.model_snapshot = _model_snapshot(model, runtime)
    operation.template_snapshot = _template_snapshot(body.target)
    operation.pricing_snapshot = {
        "version": 1,
        "preset": preset,
        "frozen": charged,
        "single_image_cost": _reverse_cost_for_model(model, "image"),
    }
    operation.charged_credits = 0
    operation.cost_frozen = charged
    operation.cost_settled = 0
    operation.status = "needs_confirmation"
    operation.phase = "awaiting_cover_confirmation"
    operation.progress = 25
    operation.result = {"video_analysis": video_analysis or {"analysis_mode": "unavailable"}}
    operation.error_code = "VIDEO_FRAMES_UNAVAILABLE"
    operation.error = reason[:2000]
    operation.confirmation_expires_at = utcnow() + CONFIRMATION_TTL
    operation.updated_at = utcnow()
    db.commit()
    db.refresh(operation)
    return operation
