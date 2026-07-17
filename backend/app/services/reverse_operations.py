"""Asynchronous reverse-prompt orchestration and credit lifecycle."""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import ReverseOperation, User
from ..schemas import ReverseIn, ReverseOperationCreate
from . import credits, gateway, usage
from .config_store import (
    ModelConfigResolutionError,
    get_setting,
    resolve_model_config,
)
from .content_safety import assert_text_allowed
from .gateway_prompting import (
    ReverseResultValidationError,
    reverse_template,
)
from .gateway_prompting import (
    validate_reverse_result as validate_reverse_contract,
)
from .generation_pricing import reverse_cost
from .model_capabilities import ModelCapabilityError, assert_reverse_capability
from .model_gateway_config import gateway_key_fingerprint, runtime_config_for_model
from .prompt_history import remember_prompt
from .video_analysis import (
    frame_count_for_duration,
    max_frame_count,
    normalize_video_analysis_preset,
)

log = logging.getLogger("reverse_operations")

BIZ_TYPE = "reverse_operation"
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
CONFIRMATION_TTL = timedelta(minutes=15)
REPUBLISH_AFTER = timedelta(minutes=2)


def _log_operation_event(
    event: str,
    *,
    operation_id: int,
    status: str | None,
    phase: str | None,
    error_code: str | None = None,
    level: int = logging.INFO,
    **detail: Any,
) -> None:
    """Emit one machine-readable lifecycle event without request payloads."""
    payload = {
        "event": event,
        "operation_id": int(operation_id),
        "status": status,
        "phase": phase,
        "error_code": error_code,
        **detail,
    }
    log.log(
        level,
        "reverse_operation_event=%s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
    )


class ReverseOperationConflict(Exception):
    pass


class ReverseOperationInvalid(Exception):
    pass


class ReverseOperationNotFound(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _source_type_for_request(body: ReverseIn | ReverseOperationCreate) -> str:
    if body.source_type in {"image", "video"}:
        return body.source_type
    path = str(body.asset_url or "").split("?", 1)[0].lower()
    return "video" if path.endswith((".mp4", ".webm", ".mov", ".m3u8")) else "image"


_SNAPSHOT_FIELDS = {
    "version",
    "creation_mode",
    "subject_mode",
    "source_signature",
    "target",
    "selected",
    "product_asset",
    "assets",
    "video_analysis_preset",
    "subject_profile",
    "product_profile",
    "portrait_profile",
    "model_selections",
}
_ASSET_FIELDS = {
    "id",
    "type",
    "url",
    "thumb",
    "preview_url",
    "original_url",
    "original_thumb",
    "source_page_url",
    "source_captured_at",
    "width",
    "height",
    "thumb_width",
    "thumb_height",
    "retention_expires_at",
    "expired",
    "available",
}
_SECRET_KEY_PARTS = {
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "credential",
    "cookie",
    "password",
    "secret",
}


def _assert_snapshot_has_no_secrets(value: Any, path: str = "workspace_snapshot_v2") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                raise ReverseOperationInvalid(f"{path} 不得包含密钥或凭据字段: {key}")
            _assert_snapshot_has_no_secrets(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_snapshot_has_no_secrets(nested, f"{path}[{index}]")


def _clean_asset(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    cleaned = {key: value[key] for key in _ASSET_FIELDS if value.get(key) not in (None, "")}
    return cleaned or None


def sanitize_workspace_snapshot(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReverseOperationInvalid("workspace_snapshot_v2 必须是 JSON 对象")
    _assert_snapshot_has_no_secrets(value)
    cleaned = {key: value[key] for key in _SNAPSHOT_FIELDS if key in value}
    cleaned["version"] = 2
    if "source_signature" in cleaned:
        signature = cleaned["source_signature"]
        if not isinstance(signature, str) or len(signature) > 512:
            raise ReverseOperationInvalid("workspace_snapshot_v2.source_signature 必须是不超过 512 字符的字符串")
    for key in ("selected", "product_asset"):
        if key in cleaned:
            cleaned[key] = _clean_asset(cleaned[key])
    if "assets" in cleaned:
        assets = cleaned["assets"] if isinstance(cleaned["assets"], list) else []
        cleaned["assets"] = [asset for item in assets[:12] if (asset := _clean_asset(item))]
    for key in ("subject_profile", "product_profile", "portrait_profile"):
        if key in cleaned and not isinstance(cleaned[key], dict):
            cleaned[key] = None
    return cleaned


def _body_dict(body: ReverseOperationCreate) -> dict[str, Any]:
    payload = body.model_dump(mode="json", exclude_none=True)
    payload["video_analysis_preset"] = normalize_video_analysis_preset(
        body.video_analysis_preset
    )
    if "workspace_snapshot_v2" in payload:
        payload["workspace_snapshot_v2"] = sanitize_workspace_snapshot(
            payload["workspace_snapshot_v2"]
        )
    return payload


def request_fingerprint(body: ReverseOperationCreate) -> str:
    payload = _body_dict(body)
    payload.pop("client_request_id", None)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _model_snapshot(model, runtime) -> dict[str, Any]:
    endpoint_fingerprint = hashlib.sha256(str(runtime.base_url or "").encode()).hexdigest()
    return {
        "model_config_id": int(model.id) if getattr(model, "id", None) is not None else None,
        "model_name": str(getattr(model, "display_name", None) or model.model_id),
        "use": "vision",
        "model_id": model.model_id,
        "provider": runtime.provider,
        "gateway_endpoint_fingerprint": endpoint_fingerprint,
        "gateway_format": runtime.gateway_format,
        "source": runtime.source,
        "gateway_key_fingerprint": gateway_key_fingerprint(runtime),
    }


def _reverse_cost_for_model(model, target: str, *, preset: str | None = None) -> int:
    pricing = (model.extra or {}).get("reverse_pricing") if isinstance(model.extra, dict) else None
    if isinstance(pricing, dict):
        if target == "video":
            preset_costs = pricing.get("video_preset_costs")
            if isinstance(preset_costs, dict):
                value = preset_costs.get((preset or "standard").strip().lower())
                if value is not None:
                    return max(0, int(value))
        else:
            value = pricing.get("image_cost")
            if value is not None:
                return max(0, int(value))
    return reverse_cost(target, preset=preset)


def _template_snapshot(target: str) -> dict[str, Any]:
    contract_targets = [target]
    if target == "video":
        contract_targets.append("image_to_video")
    return {
        "version": 2,
        "target": target,
        "templates": {
            contract_target: reverse_template(contract_target)
            for contract_target in contract_targets
        },
    }


def _assert_supported_vision_runtime(runtime) -> None:
    if runtime.provider == "anthropic" or runtime.gateway_format == "anthropic":
        raise ReverseOperationInvalid(
            "视觉反推不支持 Anthropic 原生协议,请使用 OpenAI-compatible 视觉网关"
        )


def serialize_operation(op: ReverseOperation) -> dict[str, Any]:
    result = dict(op.result) if isinstance(op.result, dict) else None
    if result is not None:
        result.setdefault("reference_count", max(1, int(op.reference_count or 1)))
        result.setdefault("charged_credits", int(op.cost_settled or op.charged_credits or 0))
    context = dict(op.request_context) if isinstance(op.request_context, dict) else {}
    return {
        "id": int(op.id),
        "model_config_id": getattr(op, "model_config_id", None),
        "model_name": (op.model_snapshot or {}).get("model_name") if isinstance(op.model_snapshot, dict) else None,
        "model_id": (op.model_snapshot or {}).get("model_id") if isinstance(op.model_snapshot, dict) else None,
        "target": op.target,
        "source_type": context.get("source_type"),
        "status": op.status,
        "phase": op.phase,
        "progress": int(op.progress or 0),
        "result": result,
        "video_analysis": (
            result.get("video_analysis")
            if isinstance(result, dict) and isinstance(result.get("video_analysis"), dict)
            else None
        ),
        "request_context": context or None,
        "workspace_snapshot_v2": context.get("workspace_snapshot_v2"),
        "reference_count": max(1, int(op.reference_count or 1)),
        "charged_credits": int(op.cost_settled or op.charged_credits or 0),
        "cost_frozen": int(op.cost_frozen or 0),
        "cost_settled": int(op.cost_settled or 0),
        "confirmation_expires_at": op.confirmation_expires_at,
        "cancel_requested": bool(op.cancel_requested),
        "error_code": op.error_code,
        "error": op.error,
        "created_at": op.created_at,
        "updated_at": op.updated_at,
        "started_at": op.started_at,
        "finished_at": op.finished_at,
    }


def create_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseOperationCreate,
) -> tuple[ReverseOperation, bool]:
    """Create and freeze an operation atomically; return (operation, created)."""
    client_request_id = body.client_request_id.strip()
    fingerprint = request_fingerprint(body)
    return _create_operation_record(
        db,
        user_id=user_id,
        body=body,
        client_request_id=client_request_id,
        fingerprint=fingerprint,
    )


def find_idempotent_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseOperationCreate,
) -> ReverseOperation | None:
    """Return an exact request replay without charging or rate-limiting it."""
    client_request_id = body.client_request_id.strip()
    existing = db.execute(
        select(ReverseOperation).where(
            ReverseOperation.user_id == user_id,
            ReverseOperation.client_request_id == client_request_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    if existing.request_fingerprint != request_fingerprint(body):
        raise ReverseOperationConflict("client_request_id 已用于不同反推请求")
    return existing


def create_legacy_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseIn,
    fingerprint: str,
) -> tuple[ReverseOperation, bool]:
    """Create a synchronously executed compatibility operation.

    The legacy HTTP endpoint keeps its optional idempotency key, while using
    the same frozen-credit lifecycle and worker-safe execution core as v2.
    """
    return _create_operation_record(
        db,
        user_id=user_id,
        body=body,
        client_request_id=(body.client_request_id or "").strip() or None,
        fingerprint=fingerprint,
    )


def _create_operation_record(
    db: Session,
    *,
    user_id: int,
    body: ReverseIn | ReverseOperationCreate,
    client_request_id: str | None,
    fingerprint: str,
) -> tuple[ReverseOperation, bool]:
    existing = db.execute(
        select(ReverseOperation).where(
            ReverseOperation.user_id == user_id,
            ReverseOperation.client_request_id == client_request_id,
        )
    ).scalar_one_or_none() if client_request_id else None
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ReverseOperationConflict("client_request_id 已用于不同反推请求")
        return existing, False

    if not get_setting(db, "reverse_prompt_enabled", True):
        raise ReverseOperationInvalid("反推功能已被管理员关闭")
    try:
        model = resolve_model_config(db, "vision", getattr(body, "model_config_id", None))
    except ModelConfigResolutionError as exc:
        raise ReverseOperationInvalid(str(exc)) from exc
    try:
        assert_reverse_capability(
            model,
            target=body.target,
            source_type=_source_type_for_request(body),
        )
    except ModelCapabilityError as exc:
        raise ReverseOperationInvalid(str(exc)) from exc
    runtime = runtime_config_for_model(model, "vision")
    _assert_supported_vision_runtime(runtime)

    preset = normalize_video_analysis_preset(body.video_analysis_preset)
    frozen = _reverse_cost_for_model(model, body.target, preset=preset)
    context = (
        _body_dict(body)
        if isinstance(body, ReverseOperationCreate)
        else body.model_dump(mode="json", exclude_none=True)
    )
    context["video_analysis_preset"] = preset
    operation = ReverseOperation(
        user_id=user_id,
        model_config_id=model.id,
        client_request_id=client_request_id,
        request_fingerprint=fingerprint,
        target=body.target,
        asset_url=body.asset_url,
        status="queued",
        phase="queued",
        progress=0,
        request_context=context,
        model_snapshot=_model_snapshot(model, runtime),
        template_snapshot=_template_snapshot(body.target),
        pricing_snapshot={
            "version": 1,
            "preset": preset,
            "frozen": frozen,
            "single_image_cost": _reverse_cost_for_model(model, "image"),
        },
        cost_frozen=frozen,
        cost_settled=0,
        charged_credits=0,
    )
    try:
        db.add(operation)
        db.flush()
        credits.freeze(
            db,
            user_id,
            frozen,
            int(operation.id),
            biz_type=BIZ_TYPE,
            commit=False,
        )
        db.commit()
        db.refresh(operation)
        _log_operation_event(
            "reverse_operation_created",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            target=operation.target,
            cost_frozen=int(operation.cost_frozen or 0),
        )
        return operation, True
    except IntegrityError as exc:
        db.rollback()
        existing = db.execute(
            select(ReverseOperation).where(
                ReverseOperation.user_id == user_id,
                ReverseOperation.client_request_id == client_request_id,
            )
        ).scalar_one_or_none() if client_request_id else None
        if existing is None:
            raise
        if existing.request_fingerprint != fingerprint:
            raise ReverseOperationConflict("client_request_id 已用于不同反推请求") from exc
        return existing, False


def _get_owned(db: Session, operation_id: int, user_id: int) -> ReverseOperation:
    operation = db.get(ReverseOperation, operation_id)
    if operation is None or int(operation.user_id) != int(user_id):
        raise ReverseOperationNotFound("反推任务不存在")
    return operation


def get_owned_operation(db: Session, operation_id: int, user_id: int) -> ReverseOperation:
    return _get_owned(db, operation_id, user_id)


def list_owned_operations(
    db: Session,
    *,
    user_id: int,
    status: str | None,
    limit: int,
    offset: int,
) -> list[ReverseOperation]:
    stmt = select(ReverseOperation).where(ReverseOperation.user_id == user_id)
    if status:
        stmt = stmt.where(ReverseOperation.status == status)
    return list(
        db.execute(
            stmt.order_by(ReverseOperation.id.desc())
            .limit(min(max(int(limit), 1), 100))
            .offset(max(int(offset), 0))
        ).scalars()
    )


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

    # Persist the identity before touching the broker. Once apply_async returns
    # there is no fallible database write that could misclassify an accepted
    # message as a broker failure. Re-publish uses the same id; the database
    # claim remains the authoritative duplicate-execution guard.
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
    # Legacy synchronous rows used consume/refund rather than freeze/refund.
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
    """Fail/refund only while the broker-owned operation is still queued.

    A publish call can raise after the broker accepted the message. The worker
    and API race through this conditional transition; whichever claims the
    queued row first owns the outcome.
    """
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
    db.refresh(operation)
    _log_operation_event(
        "reverse_operation_canceled",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
    )
    return operation


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
            getattr(operation, "model_config_id", None) or getattr(body, "model_config_id", None),
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


def validate_reverse_result(result: Any, target: str) -> dict[str, Any]:
    """Revalidate an adapter result against the selected target contract.

    ``gateway.reverse_prompt`` normally returns a normalized object after its
    own provider-output validation. This boundary check is still required:
    tests, wrappers, and future adapters can replace that function, and no
    result may reach credit settlement based only on a shallow shape check.
    """
    if not isinstance(result, dict):
        raise gateway.GatewayError(
            "视觉模型返回结果不是 JSON 对象",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )
    structured = result.get("structured")
    final_text = result.get("final_text")
    provider_final_text = result.get("provider_final_text", final_text)
    if not isinstance(structured, dict):
        raise gateway.GatewayError(
            "视觉模型返回的 structured 类型错误",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )
    if not isinstance(final_text, str) or not final_text.strip():
        raise gateway.GatewayError(
            "视觉模型返回的 final_text 必须是非空字符串",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )
    if not isinstance(provider_final_text, str) or not provider_final_text.strip():
        raise gateway.GatewayError(
            "视觉模型返回的 provider_final_text 必须是非空字符串",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        )

    contract_payload = dict(structured)
    contract_payload["final_text"] = provider_final_text
    if "shots" in result:
        contract_payload["shots"] = result.get("shots")
    try:
        validated = validate_reverse_contract(contract_payload, target)
    except ReverseResultValidationError as exc:
        raise gateway.GatewayError(
            f"视觉模型返回结果不符合 {target} 契约: {str(exc)[:500]}",
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        ) from exc

    normalized = dict(result)
    normalized.update(validated)
    normalized["provider_final_text"] = provider_final_text
    return normalized


def _claim_operation(db: Session, operation_id: int) -> ReverseOperation | None:
    now = utcnow()
    claimed = db.execute(
        update(ReverseOperation)
        .where(
            ReverseOperation.id == operation_id,
            ReverseOperation.status == "queued",
        )
        .values(
            status="running",
            phase="resolving_asset",
            progress=5,
            attempt_count=func.coalesce(ReverseOperation.attempt_count, 0) + 1,
            started_at=func.coalesce(ReverseOperation.started_at, now),
            updated_at=now,
        )
        .returning(ReverseOperation.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if claimed is None:
        db.rollback()
        return None
    db.commit()
    operation = db.get(ReverseOperation, operation_id)
    if operation is not None:
        _log_operation_event(
            "reverse_operation_claimed",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
            attempt_count=int(operation.attempt_count or 0),
        )
    return operation


def _set_phase(db: Session, operation_id: int, phase: str, progress: int) -> bool:
    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None or operation.status != "running":
        db.rollback()
        return False
    if operation.cancel_requested:
        _refund_locked(db, operation, note="cooperative reverse cancellation")
        operation.status = "canceled"
        operation.phase = None
        operation.progress = 100
        operation.error_code = "CANCELED"
        operation.finished_at = utcnow()
        operation.updated_at = utcnow()
        db.commit()
        _log_operation_event(
            "reverse_operation_canceled",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
        )
        return False
    operation.phase = phase
    operation.progress = min(max(int(progress), 0), 99)
    operation.updated_at = utcnow()
    db.commit()
    _log_operation_event(
        "reverse_operation_phase_changed",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
        progress=int(operation.progress or 0),
    )
    return True


def _pause_for_cover(
    db: Session,
    operation_id: int,
    *,
    video_analysis: dict[str, Any] | None,
    reason: str,
) -> None:
    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None or operation.status != "running":
        db.rollback()
        return
    if operation.cancel_requested:
        _refund_locked(db, operation, note="canceled before cover confirmation")
        operation.status = "canceled"
        operation.progress = 100
        operation.phase = None
        operation.error_code = "CANCELED"
        operation.finished_at = utcnow()
    else:
        context = dict(operation.request_context or {})
        context["cover_confirmation_required"] = True
        context["cover_confirmation_required_at"] = utcnow().isoformat()
        operation.request_context = context
        operation.status = "needs_confirmation"
        operation.phase = "awaiting_cover_confirmation"
        operation.progress = 25
        operation.result = {"video_analysis": video_analysis or {"analysis_mode": "unavailable"}}
        operation.error_code = "VIDEO_FRAMES_UNAVAILABLE"
        operation.error = reason[:2000]
        operation.confirmation_expires_at = utcnow() + CONFIRMATION_TTL
    operation.updated_at = utcnow()
    db.commit()
    _log_operation_event(
        (
            "reverse_operation_canceled"
            if operation.status == "canceled"
            else "reverse_operation_needs_confirmation"
        ),
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
    )


def _gateway_accepts(name: str) -> bool:
    try:
        signature = inspect.signature(gateway.reverse_prompt)
    except (TypeError, ValueError):
        return True
    return name in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _history_title(target: str) -> tuple[str, str]:
    if target == "video":
        return "视频反推提示词", "video"
    if target == "portrait_profile":
        return "人物身份档案", "image"
    if target == "product_profile":
        return "产品身份档案", "image"
    return "图片反推提示词", "image"


def _merge_video_analysis_result(
    result: dict[str, Any],
    collected_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Preserve gateway evidence while attaching normalized shots and gaps."""
    gateway_analysis = result.pop("video_analysis", None)
    gateway_analysis = gateway_analysis if isinstance(gateway_analysis, dict) else {}
    merged = {**collected_analysis, **gateway_analysis}
    collected_source = collected_analysis.get("source")
    gateway_source = gateway_analysis.get("source")
    if isinstance(collected_source, dict) or isinstance(gateway_source, dict):
        merged["source"] = {
            **(collected_source if isinstance(collected_source, dict) else {}),
            **(gateway_source if isinstance(gateway_source, dict) else {}),
        }
    if "shots" in result:
        merged["shots"] = result.pop("shots")
    if "analysis_gaps" in result:
        merged["analysis_gaps"] = result.pop("analysis_gaps")
    return merged


def _remember_history(operation: ReverseOperation, result: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        title, category = _history_title(operation.target)
        context = dict(operation.request_context or {})
        workspace = (
            dict(context.get("workspace_snapshot_v2"))
            if isinstance(context.get("workspace_snapshot_v2"), dict)
            else {}
        )
        snapshot = {
            **workspace,
            "version": 2,
            "target": operation.target,
            "request_context": context,
            "workspace_snapshot_v2": workspace or None,
            "structured": result.get("structured"),
            "final_text": result.get("final_text"),
            "video_analysis": result.get("video_analysis"),
            "reference_count": max(1, int(operation.reference_count or 1)),
        }
        if operation.target in {"product_profile", "portrait_profile"}:
            profile_structured = (
                dict(result.get("structured"))
                if isinstance(result.get("structured"), dict)
                else {}
            )
            profile = {
                "structured": profile_structured,
                "final_text": str(result.get("final_text") or ""),
            }
            profile_key = operation.target
            snapshot["subject_mode"] = (
                "portrait" if operation.target == "portrait_profile" else "product"
            )
            snapshot["subject_profile"] = profile
            snapshot[profile_key] = profile
            if not isinstance(snapshot.get("product_asset"), dict):
                selected = snapshot.get("selected")
                snapshot["product_asset"] = (
                    dict(selected)
                    if isinstance(selected, dict)
                    else {"type": "image", "url": operation.asset_url}
                )
        remember_prompt(
            db,
            user_id=operation.user_id,
            prompt=result.get("final_text"),
            title=title,
            category=category,
            source="reverse",
            params={
                "asset_url": operation.asset_url,
                "reference_count": max(1, int(operation.reference_count or 1)),
                "workspace_snapshot_v2": workspace or None,
                "reverse_snapshot_v2": snapshot,
            },
            commit=True,
        )
    except Exception:
        db.rollback()
        log.exception("failed to remember async reverse history operation=%s", operation.id)
    finally:
        db.close()


def _record_gateway_failure(
    db: Session,
    *,
    operation_id: int,
    model_id: str | None,
    user_id: int,
    target: str,
    contract_target: str,
    preset: str,
    cost_credits: int,
    phase: str,
    error_code: str,
    error: str,
    repair_attempted: bool,
    result: dict[str, Any] | None = None,
) -> None:
    operation = db.get(ReverseOperation, operation_id)
    usage.record_call(
        db,
        kind="reverse",
        model_id=model_id,
        user_id=user_id,
        model_config_id=getattr(operation, "model_config_id", None),
        status="failed",
        latency_ms=(result or {}).get("latency_ms"),
        usage=(result or {}).get("usage"),
        detail={
            "operation_id": operation_id,
            "target": target,
            "contract_target": contract_target,
            "phase": phase,
            "error_code": error_code,
            "preset": preset,
            "cost_credits": max(0, int(cost_credits)),
            "repair_attempted": bool(repair_attempted),
            "error": str(error)[:500],
        },
    )


def _finish_success(
    db: Session,
    operation_id: int,
    *,
    result: dict[str, Any],
    reference_count: int,
    real_cost: int,
) -> ReverseOperation | None:
    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None or operation.status != "running":
        db.rollback()
        return None
    if operation.cancel_requested:
        _refund_locked(db, operation, note="canceled after reverse gateway response")
        operation.status = "canceled"
        operation.phase = None
        operation.progress = 100
        operation.error_code = "CANCELED"
        operation.finished_at = utcnow()
        operation.updated_at = utcnow()
        db.commit()
        _log_operation_event(
            "reverse_operation_canceled",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
        )
        return None
    reserved = int(operation.cost_frozen or 0)
    real_cost = min(max(int(real_cost), 0), reserved)
    credits.settle(
        db,
        operation.user_id,
        reserved,
        real_cost,
        operation.id,
        biz_type=BIZ_TYPE,
        commit=False,
    )
    operation.cost_frozen = 0
    operation.cost_settled = real_cost
    operation.charged_credits = real_cost
    operation.reference_count = max(1, int(reference_count))
    result = dict(result)
    result["reference_count"] = operation.reference_count
    result["charged_credits"] = real_cost
    operation.result = result
    operation.status = "succeeded"
    operation.phase = None
    operation.progress = 100
    operation.error_code = None
    operation.error = None
    operation.finished_at = utcnow()
    operation.updated_at = utcnow()
    db.commit()
    db.refresh(operation)
    _log_operation_event(
        "reverse_operation_succeeded",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
        target=operation.target,
        cost_settled=int(operation.cost_settled or 0),
    )
    return operation


def run_operation(operation_id: int) -> None:
    """Run one operation. Redelivery is harmless because claiming is conditional."""
    from ..routers import prompt as prompt_router

    db = SessionLocal()
    operation: ReverseOperation | None = None
    user: User | None = None
    model = None
    body: ReverseIn | None = None
    preset = "standard"
    gateway_target = "image"
    real_cost = 0
    gateway_invoked = False
    gateway_call_recorded = False
    gateway_result: dict[str, Any] | None = None
    try:
        operation = _claim_operation(db, operation_id)
        if operation is None:
            return
        context = dict(operation.request_context or {})
        body = ReverseIn.model_validate(context)
        user = db.get(User, operation.user_id)
        if user is None:
            fail_operation(operation_id, code="USER_NOT_FOUND", error="用户不存在")
            return
        try:
            model = resolve_model_config(
                db,
                "vision",
                getattr(operation, "model_config_id", None),
                require_enabled=False,
            )
        except ModelConfigResolutionError as exc:
            fail_operation(operation_id, code="MODEL_UNAVAILABLE", error=str(exc))
            return
        runtime = runtime_config_for_model(model, "vision")
        _assert_supported_vision_runtime(runtime)
        expected_snapshot = dict(operation.model_snapshot or {})
        current_snapshot = _model_snapshot(model, runtime)
        for key in (
            "model_config_id",
            "model_id",
            "provider",
            "gateway_endpoint_fingerprint",
            "gateway_format",
            "gateway_key_fingerprint",
        ):
            if key in expected_snapshot and expected_snapshot.get(key) != current_snapshot.get(key):
                fail_operation(
                    operation_id,
                    code="MODEL_CONFIG_CHANGED",
                    error="反推提交后视觉模型配置已变更,请重新发起",
                )
                return
        # Older queued operations stored the endpoint directly. Keep those
        # recoverable while new snapshots retain only a one-way fingerprint.
        if (
            "gateway_endpoint_fingerprint" not in expected_snapshot
            and "base_url" in expected_snapshot
            and expected_snapshot.get("base_url") != runtime.base_url
        ):
            fail_operation(
                operation_id,
                code="MODEL_CONFIG_CHANGED",
                error="反推提交后视觉模型配置已变更,请重新发起",
            )
            return

        is_video_source = prompt_router._is_video_source(body)
        preset = normalize_video_analysis_preset(body.video_analysis_preset)
        video_analysis = None
        gateway_target = body.target
        pricing_snapshot = dict(operation.pricing_snapshot or {})
        real_cost = int(
            pricing_snapshot.get("frozen")
            or _reverse_cost_for_model(model, body.target, preset=preset)
        )
        single_image_cost = int(
            pricing_snapshot.get("single_image_cost") or _reverse_cost_for_model(model, "image")
        )

        if is_video_source and body.target != "video":
            fail_operation(operation_id, code="INVALID_SOURCE", error="视频素材仅支持视频反推")
            return
        if not _set_phase(db, operation_id, "extracting_frames" if is_video_source else "resolving_asset", 15):
            return

        cover_confirmed = bool(context.get("cover_confirmed"))
        if is_video_source and cover_confirmed:
            fallback = str(context.get("fallback_image") or "").strip()
            ref = prompt_router._gateway_ref(db, user, fallback)
            refs = [ref] if ref else []
            previous = operation.result if isinstance(operation.result, dict) else {}
            previous_analysis = previous.get("video_analysis") if isinstance(previous, dict) else None
            video_analysis = dict(previous_analysis) if isinstance(previous_analysis, dict) else {}
            video_analysis.update(
                {
                    "analysis_mode": "cover_fallback",
                    "degraded_reason": "用户已确认改用封面单帧进行运动设计",
                }
            )
            gateway_target = "image_to_video"
            real_cost = single_image_cost
        else:
            frame_budget = 1
            if is_video_source:
                duration = prompt_router._video_duration_for_reverse(body, db, user)
                frame_budget = frame_count_for_duration(duration, preset)
                frame_budget = min(frame_budget, max_frame_count(preset))
            try:
                collected_refs = prompt_router._collect_refs(
                    body,
                    db,
                    user,
                    frame_budget=frame_budget,
                    video_preset=preset,
                    gateway_mock=(
                        settings.mock_mode
                        or (runtime.source == "env" and settings.effective_mock_mode)
                    ),
                )
                if isinstance(collected_refs, tuple) and len(collected_refs) == 2:
                    refs, video_analysis = collected_refs
                else:
                    refs, video_analysis = collected_refs, None
            except HTTPException as exc:
                if is_video_source and exc.status_code == 400:
                    _pause_for_cover(
                        db,
                        operation_id,
                        video_analysis=None,
                        reason=str(exc.detail),
                    )
                    return
                raise
            if (
                is_video_source
                and isinstance(video_analysis, dict)
                and video_analysis.get("analysis_mode") == "cover_fallback"
            ):
                _pause_for_cover(
                    db,
                    operation_id,
                    video_analysis=video_analysis,
                    reason=str(video_analysis.get("degraded_reason") or "视频抽帧不可用"),
                )
                return
            if body.target == "video" and not is_video_source:
                gateway_target = "image_to_video"
                real_cost = single_image_cost

        if not refs:
            raise ReverseOperationInvalid("素材解析后没有可用参考帧")
        if not _set_phase(db, operation_id, "calling_model", 45):
            return
        kwargs: dict[str, Any] = {"target": gateway_target}
        if _gateway_accepts("gateway_config"):
            kwargs["gateway_config"] = runtime
        if video_analysis is not None and _gateway_accepts("video_analysis"):
            kwargs["video_analysis"] = video_analysis
        templates = (
            (operation.template_snapshot or {}).get("templates")
            if isinstance(operation.template_snapshot, dict)
            else None
        )
        if (
            isinstance(templates, dict)
            and isinstance(templates.get(gateway_target), str)
            and _gateway_accepts("template_override")
        ):
            kwargs["template_override"] = templates[gateway_target]
        if _gateway_accepts("before_repair"):
            kwargs["before_repair"] = lambda: _set_phase(
                db, operation_id, "repairing", 70
            )
        if _gateway_accepts("audit_context"):
            kwargs["audit_context"] = {
                "operation_id": operation_id,
                "preset": preset,
                "cost_credits": real_cost,
            }
        gateway_invoked = True
        result = gateway.reverse_prompt(refs, model.model_id, **kwargs)
        gateway_result = result if isinstance(result, dict) else None
        result = validate_reverse_result(result, gateway_target)
        if video_analysis is not None:
            result["video_analysis"] = _merge_video_analysis_result(result, video_analysis)
        assert_text_allowed(db, result.get("structured"), result.get("final_text"))
        usage.record_call(
            db,
            kind="reverse",
            model_id=model.model_id,
            user_id=operation.user_id,
            model_config_id=getattr(operation, "model_config_id", None),
            status="ok",
            latency_ms=(gateway_result or {}).get("latency_ms"),
            usage=(gateway_result or {}).get("usage"),
            detail={
                "operation_id": operation_id,
                "target": body.target,
                "contract_target": gateway_target,
                "preset": preset,
                "frames": len(refs),
                "cost_credits": real_cost,
                "phase": "gateway_completed",
                "error_code": None,
                "repair_attempted": bool((gateway_result or {}).get("repair_attempted")),
            },
        )
        gateway_call_recorded = True
        if not _set_phase(db, operation_id, "settling", 90):
            return
        finished = _finish_success(
            db,
            operation_id,
            result=result,
            reference_count=len(refs),
            real_cost=real_cost,
        )
        if finished is None:
            return
        _remember_history(finished, result)
    except HTTPException as exc:
        status_code = int(exc.status_code)
        upstream_failure = gateway_invoked and status_code >= 500
        if upstream_failure:
            error_code = "GATEWAY_ERROR"
            phase = "calling_model"
        elif gateway_invoked:
            error_code = "CONTENT_SAFETY_BLOCKED"
            phase = "validating_output"
        elif status_code == 404:
            error_code = "ASSET_NOT_FOUND"
            phase = "resolving_asset"
        else:
            error_code = "REQUEST_REJECTED"
            phase = "resolving_asset"
        if (
            gateway_invoked
            and not gateway_call_recorded
            and operation is not None
            and user is not None
        ):
            _record_gateway_failure(
                db,
                operation_id=operation_id,
                model_id=getattr(model, "model_id", None),
                user_id=user.id,
                target=body.target if body is not None else operation.target,
                contract_target=gateway_target,
                preset=preset,
                cost_credits=real_cost,
                phase=phase,
                error_code=error_code,
                error=str(exc.detail),
                repair_attempted=bool((gateway_result or {}).get("repair_attempted")),
                result=gateway_result,
            )
        fail_operation(operation_id, code=error_code, error=str(exc.detail))
    except gateway.GatewayError as exc:
        error_code = str(exc.error_code or "GATEWAY_ERROR")
        phase = str(exc.phase or "calling_model")
        if (
            gateway_invoked
            and not gateway_call_recorded
            and operation is not None
            and user is not None
        ):
            _record_gateway_failure(
                db,
                operation_id=operation_id,
                model_id=getattr(model, "model_id", None),
                user_id=user.id,
                target=body.target if body is not None else operation.target,
                contract_target=gateway_target,
                preset=preset,
                cost_credits=real_cost,
                phase=phase,
                error_code=error_code,
                error=str(exc),
                repair_attempted=(
                    phase == "repairing"
                    or bool((gateway_result or {}).get("repair_attempted"))
                ),
                result=gateway_result,
            )
        fail_operation(operation_id, code=error_code, error=str(exc))
    except ReverseOperationInvalid as exc:
        fail_operation(operation_id, code="INVALID_OPERATION", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        _log_operation_event(
            "reverse_operation_crashed",
            operation_id=operation_id,
            status=getattr(operation, "status", None),
            phase=getattr(operation, "phase", None),
            error_code="INTERNAL_ERROR",
            level=logging.ERROR,
        )
        log.exception("reverse operation crash traceback operation_id=%s", operation_id)
        if (
            gateway_invoked
            and not gateway_call_recorded
            and operation is not None
            and user is not None
        ):
            phase = str(getattr(exc, "reverse_phase", None) or "calling_model")
            _record_gateway_failure(
                db,
                operation_id=operation_id,
                model_id=getattr(model, "model_id", None),
                user_id=user.id,
                target=body.target if body is not None else operation.target,
                contract_target=gateway_target,
                preset=preset,
                cost_credits=real_cost,
                phase=phase,
                error_code="INTERNAL_ERROR",
                error=str(exc),
                repair_attempted=phase == "repairing",
                result=gateway_result,
            )
        fail_operation(operation_id, code="INTERNAL_ERROR", error=str(exc))
    finally:
        db.close()


def _fail_stale_running(
    operation_id: int,
    *,
    cutoff: datetime,
    now: datetime,
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
        _refund_locked(db, operation, note="stale reverse operation timed out")
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


def reap_operations() -> dict[str, int]:
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
            _refund_locked(db, operation, note="cover confirmation expired")
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
