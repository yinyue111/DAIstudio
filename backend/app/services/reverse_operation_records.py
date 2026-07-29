"""Reverse-operation persistence, idempotency, and read models."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    GenerationQuote,
    ReverseOperation,
    ReverseOperationBatch,
    ReverseOperationBatchItem,
)
from ..schemas import ReverseIn, ReverseOperationCreate
from . import credits, generation_image_evidence, generation_quotes, project_collection
from .config_store import ModelConfigResolutionError, get_setting, resolve_model_config
from .content_safety import assert_text_allowed
from .model_capabilities import ModelCapabilityError, assert_reverse_capability
from .model_gateway_config import runtime_config_for_model
from .reverse_batches import _assert_quote_replay_binding, sync_batch_state
from .reverse_quotes import (
    BIZ_TYPE,
    IMAGE_EVIDENCE_TARGETS,
    ReverseOperationConflict,
    ReverseOperationInvalid,
    ReverseOperationNotFound,
    _assert_supported_vision_runtime,
    _log_operation_event,
    _model_snapshot,
    _primary_asset_url,
    _provider_cost_policy_for_model,
    _reverse_cost_for_model,
    _source_ranges_payload,
    _source_type_for_request,
    _template_snapshot,
    quote_request_fingerprint,
    request_fingerprint,
    reverse_pricing_snapshot,
    reverse_request_snapshot,
    reverse_template_snapshot,
)
from .video_analysis import normalize_video_analysis_preset


def serialize_operation(op: ReverseOperation) -> dict[str, Any]:
    normalized = getattr(op, "normalized_result", None)
    result_source = normalized if isinstance(normalized, dict) else op.result
    result = dict(result_source) if isinstance(result_source, dict) else None
    if result is not None:
        result.setdefault("reference_count", max(1, int(op.reference_count or 1)))
        result.setdefault("charged_credits", int(op.cost_settled or op.charged_credits or 0))
        if op.target in IMAGE_EVIDENCE_TARGETS:
            result.setdefault("image_evidence", [])
        result["image_mask_readiness"] = (
            generation_image_evidence.image_mask_readiness_for_operation(
                op, supported=op.target in IMAGE_EVIDENCE_TARGETS
            )
        )
    context = dict(op.request_context) if isinstance(op.request_context, dict) else {}
    source_ranges = getattr(op, "source_ranges", None)
    if not isinstance(source_ranges, list):
        source_ranges = context.get("source_ranges")
    if not isinstance(source_ranges, list):
        legacy_range = getattr(op, "source_range", None) or context.get("source_range")
        source_ranges = [legacy_range] if isinstance(legacy_range, dict) else []
    source_range = source_ranges[0] if len(source_ranges) == 1 else None
    return {
        "id": int(op.id),
        "quote_id": int(op.quote_id) if getattr(op, "quote_id", None) is not None else None,
        "model_config_id": getattr(op, "model_config_id", None),
        "model_name": (
            (op.model_snapshot or {}).get("model_name")
            if isinstance(op.model_snapshot, dict)
            else None
        ),
        "model_id": (
            (op.model_snapshot or {}).get("model_id")
            if isinstance(op.model_snapshot, dict)
            else None
        ),
        "target": op.target,
        "source_type": context.get("source_type"),
        "analysis_focus": (
            getattr(op, "analysis_focus", None)
            or context.get("analysis_focus")
            or "comprehensive"
        ),
        "analysis_precision": (
            getattr(op, "analysis_precision", None)
            or context.get("analysis_precision")
            or context.get("video_analysis_preset")
            or "standard"
        ),
        "output_purpose": (
            getattr(op, "output_purpose", None)
            or context.get("output_purpose")
            or "generation"
        ),
        "include_audio": bool(
            getattr(op, "include_audio", False) or context.get("include_audio")
        ),
        "source_range": source_range,
        "source_ranges": source_ranges,
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
        "workspace_snapshot_v3": context.get("workspace_snapshot_v3"),
        "result_schema_version": getattr(op, "result_schema_version", None) or "reverse.v2",
        "applied_result_version": getattr(op, "applied_result_version", None),
        "retry_of_operation_id": getattr(op, "retry_of_operation_id", None),
        "reference_count": max(1, int(op.reference_count or 1)),
        "charged_credits": int(op.cost_settled or op.charged_credits or 0),
        "cost_frozen": int(op.cost_frozen or 0),
        "cost_settled": int(op.cost_settled or 0),
        "confirmation_expires_at": op.confirmation_expires_at,
        "cancel_requested": bool(op.cancel_requested),
        "error_code": op.error_code,
        "error": op.error,
        "expired": op.error_code == "RESULT_EXPIRED",
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
    commit: bool = True,
) -> tuple[ReverseOperation, bool]:
    """Create and freeze an operation atomically; return (operation, created)."""
    client_request_id = body.client_request_id.strip()
    return _create_operation_record(
        db,
        user_id=user_id,
        body=body,
        client_request_id=client_request_id,
        fingerprint=request_fingerprint(body),
        commit=commit,
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
    if body.quote_id is not None and int(existing.quote_id or 0) != int(body.quote_id):
        raise ReverseOperationConflict("client_request_id 已绑定其他反推报价")
    if existing.error_code == "RESULT_EXPIRED":
        raise ReverseOperationConflict("该反推结果已超过保留期，请使用新的 client_request_id 重新发起")
    return existing


def _assert_operation_quote_replay(
    quote: GenerationQuote | None,
    operation: ReverseOperation,
    *,
    user_id: int,
    body: ReverseOperationCreate,
    retry_of_operation_id: int | None,
) -> None:
    if body.quote_id is None or int(operation.quote_id or 0) != int(body.quote_id):
        raise ReverseOperationConflict("client_request_id 已绑定其他反推报价")
    operation_retry_id = (
        int(operation.retry_of_operation_id)
        if operation.retry_of_operation_id is not None
        else None
    )
    normalized_retry_id = (
        int(retry_of_operation_id) if retry_of_operation_id is not None else None
    )
    if operation_retry_id != normalized_retry_id:
        raise ReverseOperationConflict("反推重试来源与幂等任务不一致")
    _assert_quote_replay_binding(
        quote,
        user_id=user_id,
        kind="reverse",
        ref_type="reverse_operation",
        ref_id=int(operation.id),
        message="反推报价与幂等任务绑定不一致",
    )
    subject = (
        quote.subject_snapshot
        if quote is not None and isinstance(quote.subject_snapshot, dict)
        else {}
    )
    quoted_retry_id = subject.get("retry_of_operation_id")
    try:
        quoted_retry_id = int(quoted_retry_id) if quoted_retry_id is not None else None
    except (TypeError, ValueError) as exc:
        raise ReverseOperationConflict("反推报价绑定的重试来源非法") from exc
    if quoted_retry_id != retry_of_operation_id:
        raise ReverseOperationConflict("反推报价绑定的重试来源不一致")
    if (
        quote is None
        or quote.client_request_id != body.client_request_id
        or quote.request_fingerprint
        != quote_request_fingerprint(body, retry_of_operation_id=retry_of_operation_id)
    ):
        raise ReverseOperationConflict("反推报价与幂等请求不一致")


def find_quoted_idempotent_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseOperationCreate,
    retry_of_operation_id: int | None = None,
) -> ReverseOperation | None:
    if body.quote_id is None:
        raise ReverseOperationInvalid("反推执行前必须确认有效报价")
    existing = find_idempotent_operation(db, user_id=user_id, body=body)
    if existing is None:
        return None
    quote = db.get(GenerationQuote, int(body.quote_id))
    _assert_operation_quote_replay(
        quote,
        existing,
        user_id=user_id,
        body=body,
        retry_of_operation_id=retry_of_operation_id,
    )
    return existing


def _create_quoted_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseOperationCreate,
    retry_of_operation_id: int | None = None,
    batch_item: ReverseOperationBatchItem | None = None,
) -> tuple[ReverseOperation, bool]:
    if body.quote_id is None:
        raise ReverseOperationInvalid("反推执行前必须确认有效报价")
    quote = generation_quotes.lock_execution_quote(
        db,
        quote_id=int(body.quote_id),
        user_id=user_id,
        kind="reverse",
        allow_consumed=True,
    )
    execution_snapshot = generation_quotes.validate_reverse_quote(
        db,
        quote,
        body=body,
        retry_of_operation_id=retry_of_operation_id,
    )
    existing = find_idempotent_operation(db, user_id=user_id, body=body)
    if existing is not None:
        _assert_operation_quote_replay(
            quote,
            existing,
            user_id=user_id,
            body=body,
            retry_of_operation_id=retry_of_operation_id,
        )
        db.commit()
        db.refresh(existing)
        return existing, False
    if quote.status == "consumed":
        db.rollback()
        raise ReverseOperationConflict("反推报价已被其他任务使用")

    try:
        operation, created = _create_operation_record(
            db,
            user_id=user_id,
            body=body,
            client_request_id=body.client_request_id,
            fingerprint=request_fingerprint(body),
            commit=False,
            execution_snapshot=execution_snapshot,
            quote_id=int(quote.id),
        )
        if not created:
            raise ReverseOperationConflict("反推任务发生幂等冲突")
        operation.retry_of_operation_id = retry_of_operation_id
        if batch_item is not None:
            batch = db.get(ReverseOperationBatch, int(batch_item.batch_id))
            if batch is not None and not batch.cancel_requested:
                batch_item.operation_id = int(operation.id)
                db.flush()
                sync_batch_state(db, batch, commit=False)
        generation_quotes.consume_execution_quote(
            quote,
            ref_type="reverse_operation",
            ref_id=int(operation.id),
        )
        db.commit()
        db.refresh(operation)
    except IntegrityError as exc:
        db.rollback()
        replay = find_quoted_idempotent_operation(
            db,
            user_id=user_id,
            body=body,
            retry_of_operation_id=retry_of_operation_id,
        )
        if replay is not None:
            return replay, False
        raise ReverseOperationConflict("反推请求发生并发幂等冲突") from exc
    except Exception:
        db.rollback()
        raise

    _log_operation_event(
        "reverse_operation_created",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        target=operation.target,
        cost_frozen=int(operation.cost_frozen or 0),
        quote_id=int(quote.id),
        retry_of_operation_id=retry_of_operation_id,
    )
    return operation, True


def create_quoted_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseOperationCreate,
) -> tuple[ReverseOperation, bool]:
    return _create_quoted_operation(db, user_id=user_id, body=body)


def create_legacy_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseIn,
    fingerprint: str,
) -> tuple[ReverseOperation, bool]:
    """Create a synchronously executed compatibility operation."""
    return _create_operation_record(
        db,
        user_id=user_id,
        body=body,
        client_request_id=(body.client_request_id or "").strip() or None,
        fingerprint=fingerprint,
    )


def _operation_snapshots(
    db: Session,
    body: ReverseIn | ReverseOperationCreate,
    execution_snapshot: dict[str, Any] | None,
) -> tuple[int, int, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    if execution_snapshot is not None:
        model_snapshot = deepcopy(execution_snapshot.get("model_snapshot") or {})
        template_snapshot = deepcopy(execution_snapshot.get("template_snapshot") or {})
        pricing_snapshot = deepcopy(execution_snapshot.get("pricing_snapshot") or {})
        try:
            model_config_id = int(model_snapshot["model_config_id"])
            frozen = int(pricing_snapshot["frozen"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReverseOperationInvalid("反推报价执行快照不完整") from exc
        if frozen < 0 or not template_snapshot:
            raise ReverseOperationInvalid("反推报价执行快照非法")
        preset = normalize_video_analysis_preset(
            pricing_snapshot.get("preset")
            or getattr(body, "analysis_precision", None)
            or body.video_analysis_preset
        )
        return (
            model_config_id,
            frozen,
            preset,
            model_snapshot,
            template_snapshot,
            pricing_snapshot,
        )

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
    if isinstance(body, ReverseOperationCreate):
        model_extra = model.extra if isinstance(model.extra, dict) else {}
        pricing_snapshot = reverse_pricing_snapshot(
            body,
            model_extra.get("reverse_pricing"),
        )
        preset = str(pricing_snapshot["preset"])
    else:
        preset = normalize_video_analysis_preset(body.video_analysis_preset)
        visual_cost = _reverse_cost_for_model(model, body.target, preset=preset)
        pricing_snapshot = {
            "version": 3,
            "preset": preset,
            "frozen": visual_cost,
            "visual_cost": visual_cost,
            "audio_surcharge": 0,
            "single_image_cost": _reverse_cost_for_model(model, "image"),
            "provider_cost_credits": deepcopy(_provider_cost_policy_for_model(model)),
        }
    return (
        int(model.id),
        int(pricing_snapshot["frozen"]),
        preset,
        _model_snapshot(model, runtime),
        (
            reverse_template_snapshot(body)
            if isinstance(body, ReverseOperationCreate)
            else _template_snapshot(body.target)
        ),
        pricing_snapshot,
    )


def _operation_context(
    body: ReverseIn | ReverseOperationCreate,
    *,
    model_config_id: int,
    preset: str,
    freeze_resolved_model: bool,
) -> dict[str, Any]:
    context = (
        reverse_request_snapshot(body)
        if isinstance(body, ReverseOperationCreate)
        else body.model_dump(mode="json", exclude_none=True)
    )
    if freeze_resolved_model:
        context["model_config_id"] = model_config_id
    context["video_analysis_preset"] = preset
    context["analysis_precision"] = preset
    return context


def _attach_operation_project_inputs(
    db: Session,
    *,
    project: Any,
    operation: ReverseOperation,
    body: ReverseIn | ReverseOperationCreate,
    user_id: int,
) -> None:
    if project is None:
        return
    project_collection.attach_task(
        db,
        project=project,
        task_kind="reverse",
        task_id=int(operation.id),
    )
    if isinstance(body, ReverseOperationCreate):
        input_urls = [
            (source.asset_url, "source" if source.role == "primary" else source.role)
            for source in body.sources
        ]
    else:
        input_urls = [(body.asset_url, "source")]
    fallback_image = getattr(body, "fallback_image", None)
    if fallback_image:
        input_urls.append((fallback_image, "fallback"))
    project_collection.attach_input_urls(
        db,
        project=project,
        user_id=user_id,
        inputs=input_urls,
    )
    db.flush()


def _create_operation_record(
    db: Session,
    *,
    user_id: int,
    body: ReverseIn | ReverseOperationCreate,
    client_request_id: str | None,
    fingerprint: str,
    commit: bool = True,
    execution_snapshot: dict[str, Any] | None = None,
    quote_id: int | None = None,
) -> tuple[ReverseOperation, bool]:
    existing = (
        db.execute(
            select(ReverseOperation).where(
                ReverseOperation.user_id == user_id,
                ReverseOperation.client_request_id == client_request_id,
            )
        ).scalar_one_or_none()
        if client_request_id
        else None
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ReverseOperationConflict("client_request_id 已用于不同反推请求")
        if quote_id is not None and int(existing.quote_id or 0) != int(quote_id):
            raise ReverseOperationConflict("client_request_id 已绑定其他反推报价")
        return existing, False

    project_id = getattr(body, "project_id", None)
    project = (
        project_collection.require_owned_project(db, user_id, project_id)
        if project_id is not None
        else None
    )
    if not get_setting(db, "reverse_prompt_enabled", True):
        raise ReverseOperationInvalid("反推功能已被管理员关闭")
    (
        model_config_id,
        frozen,
        preset,
        model_snapshot,
        template_snapshot,
        pricing_snapshot,
    ) = _operation_snapshots(db, body, execution_snapshot)
    context = _operation_context(
        body,
        model_config_id=model_config_id,
        preset=preset,
        freeze_resolved_model=execution_snapshot is not None,
    )
    custom_instruction = str(getattr(body, "custom_instruction", None) or "").strip() or None
    if custom_instruction:
        assert_text_allowed(db, custom_instruction)
    source_ranges = _source_ranges_payload(body)
    source_range = source_ranges[0] if len(source_ranges) == 1 else None
    operation = ReverseOperation(
        user_id=user_id,
        model_config_id=model_config_id,
        quote_id=quote_id,
        client_request_id=client_request_id,
        request_fingerprint=fingerprint,
        target=body.target,
        asset_url=_primary_asset_url(body),
        analysis_focus=str(getattr(body, "analysis_focus", None) or "comprehensive"),
        analysis_precision=preset,
        output_purpose=str(getattr(body, "output_purpose", None) or "generation"),
        custom_instruction=custom_instruction,
        include_audio=bool(getattr(body, "include_audio", False)),
        source_range=source_range,
        source_ranges=source_ranges,
        status="queued",
        phase="queued",
        progress=0,
        request_context=context,
        model_snapshot=model_snapshot,
        template_snapshot=template_snapshot,
        result_schema_version=(
            "reverse.v3" if isinstance(body, ReverseOperationCreate) else "reverse.v2"
        ),
        pricing_snapshot=pricing_snapshot,
        cost_frozen=frozen,
        cost_settled=0,
        charged_credits=0,
    )
    try:
        db.add(operation)
        db.flush()
        _attach_operation_project_inputs(
            db,
            project=project,
            operation=operation,
            body=body,
            user_id=user_id,
        )
        credits.freeze(
            db,
            user_id,
            frozen,
            int(operation.id),
            biz_type=BIZ_TYPE,
            commit=False,
        )
        if commit:
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
        if not commit:
            raise
        existing = (
            db.execute(
                select(ReverseOperation).where(
                    ReverseOperation.user_id == user_id,
                    ReverseOperation.client_request_id == client_request_id,
                )
            ).scalar_one_or_none()
            if client_request_id
            else None
        )
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
    target: str | None = None,
    source_type: str | None = None,
    analysis_focus: str | None = None,
    include_audio: bool | None = None,
) -> list[ReverseOperation]:
    stmt = select(ReverseOperation).where(ReverseOperation.user_id == user_id)
    if status:
        stmt = stmt.where(ReverseOperation.status == status)
    if target:
        stmt = stmt.where(ReverseOperation.target == target)
    if analysis_focus:
        stmt = stmt.where(ReverseOperation.analysis_focus == analysis_focus)
    if include_audio is not None:
        stmt = stmt.where(ReverseOperation.include_audio.is_(bool(include_audio)))
    if source_type:
        rows = list(
            db.execute(
                stmt.order_by(ReverseOperation.id.desc()).limit(100)
            ).scalars()
        )
        filtered = [
            row
            for row in rows
            if str((row.request_context or {}).get("source_type") or "") == source_type
        ]
        start = max(int(offset), 0)
        return filtered[start : start + min(max(int(limit), 1), 100)]
    return list(
        db.execute(
            stmt.order_by(ReverseOperation.id.desc())
            .limit(min(max(int(limit), 1), 100))
            .offset(max(int(offset), 0))
        ).scalars()
    )
