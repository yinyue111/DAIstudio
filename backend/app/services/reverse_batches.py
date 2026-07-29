"""Batch reverse-operation creation, idempotency, state-sync and serialization."""
from __future__ import annotations

import hashlib
import json
import logging
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
from ..schemas import ReverseBatchCreate, ReverseOperationCreate
from . import generation_quotes
from .reverse_capabilities import BATCH_CAPABILITIES
from .reverse_quotes import (
    OPERATION_STATUSES,
    TERMINAL_STATUSES,
    ReverseOperationConflict,
    ReverseOperationInvalid,
    ReverseOperationNotFound,
    _aware,
    _log_operation_event,
    request_fingerprint,
    utcnow,
)

log = logging.getLogger("reverse_operations")


def batch_request_fingerprint(body: ReverseBatchCreate) -> str:
    payload = body.model_dump(mode="json", exclude_none=True)
    payload.pop("client_request_id", None)
    payload.pop("quote_id", None)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def batch_request_snapshot(body: ReverseBatchCreate) -> dict[str, Any]:
    payload = body.model_dump(mode="json", exclude_none=True)
    payload.pop("quote_id", None)
    return payload


def _assert_quote_replay_binding(
    quote: GenerationQuote | None,
    *,
    user_id: int,
    kind: str,
    ref_type: str,
    ref_id: int,
    message: str,
) -> None:
    if (
        quote is None
        or int(quote.user_id) != int(user_id)
        or quote.kind != kind
        or quote.status != "consumed"
        or quote.consumed_ref_type != ref_type
        or int(quote.consumed_ref_id or 0) != int(ref_id)
    ):
        raise ReverseOperationConflict(message)


def _batch_operation_client_request_id(body: ReverseBatchCreate, item_index: int) -> str:
    seed = f"{body.client_request_id}:{batch_request_fingerprint(body)}"
    return f"rb-{hashlib.sha256(seed.encode()).hexdigest()[:32]}-{item_index:02d}"


def batch_operation_bodies(body: ReverseBatchCreate) -> list[ReverseOperationCreate]:
    common = {
        "project_id": body.project_id,
        "target": body.target,
        "analysis_focus": body.analysis_focus,
        "analysis_precision": body.analysis_precision,
        "output_purpose": body.output_purpose,
        "custom_instruction": body.custom_instruction,
        "include_audio": body.include_audio,
        "model_config_id": body.model_config_id,
    }
    operations: list[ReverseOperationCreate] = []
    for item_index, item in enumerate(body.items):
        audio_policy = item.audio_policy
        operations.append(ReverseOperationCreate.model_validate({
            **common,
            "client_request_id": _batch_operation_client_request_id(body, item_index),
            "asset_url": item.asset_url,
            "source_type": item.source_type,
            "sources": [source.model_dump(mode="json") for source in item.sources],
            "fallback_image": item.fallback_image,
            "workspace_snapshot_v3": item.workspace_snapshot_v3,
            "target": item.target or body.target,
            "analysis_precision": item.analysis_precision or body.analysis_precision,
            "source_ranges": [row.model_dump(mode="json") for row in item.source_ranges],
            "custom_keyframes": item.custom_keyframes,
            "include_audio": (
                body.include_audio if audio_policy == "inherit"
                else audio_policy == "analyze"
            ),
        }))
    return operations


def find_idempotent_batch(
    db: Session,
    *,
    user_id: int,
    body: ReverseBatchCreate,
) -> ReverseOperationBatch | None:
    existing = db.execute(
        select(ReverseOperationBatch).where(
            ReverseOperationBatch.user_id == user_id,
            ReverseOperationBatch.client_request_id == body.client_request_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    if existing.request_fingerprint != batch_request_fingerprint(body):
        raise ReverseOperationConflict("client_request_id 已用于不同批量反推请求")
    if body.quote_id is not None and int(existing.quote_id or 0) != int(body.quote_id):
        raise ReverseOperationConflict("client_request_id 已绑定其他批量反推报价")
    return existing


def find_quoted_idempotent_batch(
    db: Session,
    *,
    user_id: int,
    body: ReverseBatchCreate,
) -> ReverseOperationBatch | None:
    if body.quote_id is None:
        raise ReverseOperationInvalid("批量反推执行前必须确认有效报价")
    existing = find_idempotent_batch(db, user_id=user_id, body=body)
    if existing is None:
        return None
    quote = db.get(GenerationQuote, int(body.quote_id))
    _assert_quote_replay_binding(
        quote,
        user_id=user_id,
        kind="reverse_batch",
        ref_type="reverse_batch",
        ref_id=int(existing.id),
        message="批量反推报价与幂等任务绑定不一致",
    )
    if (
        quote is None
        or quote.client_request_id != body.client_request_id
        or quote.request_fingerprint != batch_request_fingerprint(body)
    ):
        raise ReverseOperationConflict("批量反推报价与幂等请求不一致")
    return existing


def _batch_item_rows(
    db: Session,
    batch_id: int,
) -> list[tuple[ReverseOperationBatchItem, ReverseOperation]]:
    return list(db.execute(
        select(ReverseOperationBatchItem, ReverseOperation)
        .join(ReverseOperation, ReverseOperation.id == ReverseOperationBatchItem.operation_id)
        .where(ReverseOperationBatchItem.batch_id == batch_id)
        .order_by(ReverseOperationBatchItem.item_index.asc())
    ).all())


def _derived_batch_status(counts: dict[str, int], total_count: int) -> str:
    terminal_count = sum(counts[status] for status in TERMINAL_STATUSES)
    if terminal_count >= total_count:
        if counts["succeeded"] == total_count:
            return "succeeded"
        if counts["failed"] == total_count:
            return "failed"
        if counts["canceled"] == total_count:
            return "canceled"
        return "partial"
    if counts["running"]:
        return "running"
    if counts["needs_confirmation"] and not counts["queued"]:
        return "needs_confirmation"
    if terminal_count or counts["needs_confirmation"]:
        return "running"
    return "queued"


def sync_batch_state(
    db: Session,
    batch: ReverseOperationBatch,
    *,
    commit: bool = True,
) -> ReverseOperationBatch:
    rows = _batch_item_rows(db, int(batch.id))
    if not rows:
        return batch
    operations = [operation for _, operation in rows]
    counts = {status: 0 for status in OPERATION_STATUSES}
    for operation in operations:
        if operation.status in counts:
            counts[operation.status] += 1
    status = _derived_batch_status(counts, len(operations))
    started_values = [_aware(operation.started_at) for operation in operations if operation.started_at]
    started_at = min(started_values) if started_values else None
    terminal = sum(counts[item] for item in TERMINAL_STATUSES) == len(operations)
    finished_values = [
        _aware(operation.finished_at) for operation in operations if operation.finished_at
    ]
    finished_at = max(finished_values) if terminal and finished_values else None
    changed = (
        batch.status != status
        or dict(batch.status_counts or {}) != counts
        or int(batch.total_count or 0) != len(operations)
        or _aware(batch.started_at) != started_at
        or _aware(batch.finished_at) != finished_at
    )
    if changed:
        batch.status = status
        batch.status_counts = counts
        batch.total_count = len(operations)
        batch.started_at = started_at
        batch.finished_at = finished_at
        batch.updated_at = utcnow()
        if commit:
            db.commit()
            db.refresh(batch)
        else:
            db.flush()
    return batch


def sync_batch_for_operation(db: Session, operation_id: int) -> None:
    batch_id = db.execute(
        select(ReverseOperationBatchItem.batch_id).where(
            ReverseOperationBatchItem.operation_id == operation_id
        )
    ).scalar_one_or_none()
    if batch_id is None:
        return
    batch = db.get(ReverseOperationBatch, int(batch_id))
    if batch is not None:
        sync_batch_state(db, batch)


def serialize_batch(
    db: Session,
    batch: ReverseOperationBatch,
    *,
    include_items: bool,
) -> dict[str, Any]:
    from . import reverse_operations as _ro

    sync_batch_state(db, batch)
    rows = _batch_item_rows(db, int(batch.id))
    operations = [operation for _, operation in rows]
    items = []
    if include_items:
        items = [
            {
                "id": int(item.id),
                "index": int(item.item_index),
                "operation_id": int(operation.id),
                "operation": _ro.serialize_operation(operation),
            }
            for item, operation in rows
        ]
    return {
        "id": int(batch.id),
        "quote_id": int(batch.quote_id) if batch.quote_id is not None else None,
        "client_request_id": batch.client_request_id,
        "name": batch.name,
        "target": batch.target,
        "shared_config_snapshot": dict(batch.shared_config_snapshot or {}),
        "capabilities": deepcopy(BATCH_CAPABILITIES),
        "status": batch.status,
        "status_counts": dict(batch.status_counts or {}),
        "total_count": int(batch.total_count or 0),
        # 批级费用 = 各子项 ReverseOperation 费用求和。前端在批次列表
        # (include_items=false) 无子项可求和，依赖这两个字段展示总费用；
        # 批级字段一旦下发即优先于前端对 items 的求和值。
        "cost_frozen": sum(int(operation.cost_frozen or 0) for operation in operations),
        "cost_settled": sum(int(operation.cost_settled or 0) for operation in operations),
        "cancel_requested": bool(batch.cancel_requested),
        "items": items,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "started_at": batch.started_at,
        "finished_at": batch.finished_at,
    }


def create_batch(
    db: Session,
    *,
    user_id: int,
    body: ReverseBatchCreate,
) -> tuple[ReverseOperationBatch, bool, list[int]]:
    from . import reverse_operations as _ro

    existing = find_idempotent_batch(db, user_id=user_id, body=body)
    if existing is not None:
        return existing, False, []
    fingerprint = batch_request_fingerprint(body)
    operation_bodies = batch_operation_bodies(body)
    shared_config = {
        "project_id": body.project_id,
        "target": body.target,
        "analysis_focus": operation_bodies[0].analysis_focus,
        "analysis_precision": operation_bodies[0].analysis_precision,
        "output_purpose": operation_bodies[0].output_purpose,
        "custom_instruction": operation_bodies[0].custom_instruction,
        "include_audio": operation_bodies[0].include_audio,
        "model_config_id": body.model_config_id,
        "immutable": True,
        "schema_version": "reverse-batch-defaults.v2",
    }
    counts = {status: 0 for status in OPERATION_STATUSES}
    counts["queued"] = len(operation_bodies)
    batch = ReverseOperationBatch(
        user_id=user_id,
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
        name=body.name,
        target=body.target,
        shared_config_snapshot=shared_config,
        status="queued",
        status_counts=counts,
        total_count=len(operation_bodies),
    )
    operation_ids: list[int] = []
    try:
        db.add(batch)
        db.flush()
        for item_index, operation_body in enumerate(operation_bodies):
            operation, created = _ro.create_operation(
                db,
                user_id=user_id,
                body=operation_body,
                commit=False,
            )
            if not created:
                raise ReverseOperationConflict("批量反推单项 client_request_id 发生冲突")
            db.add(ReverseOperationBatchItem(
                batch_id=batch.id,
                item_index=item_index,
                operation_id=operation.id,
            ))
            operation_ids.append(int(operation.id))
        db.commit()
        db.refresh(batch)
    except IntegrityError as exc:
        db.rollback()
        existing = find_idempotent_batch(db, user_id=user_id, body=body)
        if existing is not None:
            return existing, False, []
        raise ReverseOperationConflict("批量反推请求发生幂等冲突") from exc
    except Exception:
        db.rollback()
        raise
    for operation_id in operation_ids:
        operation = db.get(ReverseOperation, operation_id)
        if operation is not None:
            _log_operation_event(
                "reverse_operation_created",
                operation_id=operation.id,
                status=operation.status,
                phase=operation.phase,
                target=operation.target,
                cost_frozen=int(operation.cost_frozen or 0),
                batch_id=int(batch.id),
            )
    return batch, True, operation_ids


def create_quoted_batch(
    db: Session,
    *,
    user_id: int,
    body: ReverseBatchCreate,
) -> tuple[ReverseOperationBatch, bool, list[int]]:
    from . import reverse_operations as _ro

    if body.quote_id is None:
        raise ReverseOperationInvalid("批量反推执行前必须确认有效报价")
    quote = generation_quotes.lock_execution_quote(
        db,
        quote_id=int(body.quote_id),
        user_id=user_id,
        kind="reverse_batch",
        allow_consumed=True,
    )
    allocations = generation_quotes.validate_reverse_batch_quote(db, quote, body=body)
    existing = find_idempotent_batch(db, user_id=user_id, body=body)
    if existing is not None:
        _assert_quote_replay_binding(
            quote,
            user_id=user_id,
            kind="reverse_batch",
            ref_type="reverse_batch",
            ref_id=int(existing.id),
            message="批量反推报价与幂等任务绑定不一致",
        )
        db.commit()
        db.refresh(existing)
        return existing, False, []
    if quote.status == "consumed":
        db.rollback()
        raise ReverseOperationConflict("批量反推报价已被其他任务使用")

    fingerprint = batch_request_fingerprint(body)
    operation_bodies = batch_operation_bodies(body)
    shared_config = {
        "project_id": body.project_id,
        "target": body.target,
        "analysis_focus": operation_bodies[0].analysis_focus,
        "analysis_precision": operation_bodies[0].analysis_precision,
        "output_purpose": operation_bodies[0].output_purpose,
        "custom_instruction": operation_bodies[0].custom_instruction,
        "include_audio": operation_bodies[0].include_audio,
        "model_config_id": int(quote.model_config_id),
        "immutable": True,
        "schema_version": "reverse-batch-defaults.v2",
    }
    counts = {status: 0 for status in OPERATION_STATUSES}
    counts["queued"] = len(operation_bodies)
    batch = ReverseOperationBatch(
        user_id=user_id,
        quote_id=int(quote.id),
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
        name=body.name,
        target=body.target,
        shared_config_snapshot=shared_config,
        status="queued",
        status_counts=counts,
        total_count=len(operation_bodies),
    )
    operation_ids: list[int] = []
    try:
        db.add(batch)
        db.flush()
        for item_index, (operation_body, allocation) in enumerate(
            zip(operation_bodies, allocations, strict=True)
        ):
            execution_snapshot = {
                "model_snapshot": deepcopy(allocation["model_snapshot"]),
                "template_snapshot": deepcopy(allocation["template_snapshot"]),
                "pricing_snapshot": deepcopy(allocation["pricing_snapshot"]),
            }
            operation, created = _ro._create_operation_record(
                db,
                user_id=user_id,
                body=operation_body,
                client_request_id=operation_body.client_request_id,
                fingerprint=request_fingerprint(operation_body),
                commit=False,
                execution_snapshot=execution_snapshot,
            )
            if not created:
                raise ReverseOperationConflict("批量反推单项 client_request_id 发生冲突")
            db.add(
                ReverseOperationBatchItem(
                    batch_id=int(batch.id),
                    item_index=item_index,
                    operation_id=int(operation.id),
                )
            )
            operation_ids.append(int(operation.id))
        generation_quotes.consume_execution_quote(
            quote,
            ref_type="reverse_batch",
            ref_id=int(batch.id),
        )
        db.commit()
        db.refresh(batch)
    except IntegrityError as exc:
        db.rollback()
        replay = find_quoted_idempotent_batch(db, user_id=user_id, body=body)
        if replay is not None:
            return replay, False, []
        raise ReverseOperationConflict("批量反推请求发生并发幂等冲突") from exc
    except Exception:
        db.rollback()
        raise

    for operation_id in operation_ids:
        operation = db.get(ReverseOperation, operation_id)
        if operation is not None:
            _log_operation_event(
                "reverse_operation_created",
                operation_id=operation.id,
                status=operation.status,
                phase=operation.phase,
                target=operation.target,
                cost_frozen=int(operation.cost_frozen or 0),
                batch_id=int(batch.id),
                quote_id=int(quote.id),
            )
    return batch, True, operation_ids


def get_owned_batch(
    db: Session,
    *,
    batch_id: int,
    user_id: int,
) -> ReverseOperationBatch:
    batch = db.execute(
        select(ReverseOperationBatch).where(
            ReverseOperationBatch.id == batch_id,
            ReverseOperationBatch.user_id == user_id,
        )
    ).scalar_one_or_none()
    if batch is None:
        raise ReverseOperationNotFound("批量反推任务不存在")
    return batch


def list_owned_batches(
    db: Session,
    *,
    user_id: int,
    limit: int = 30,
    offset: int = 0,
) -> list[ReverseOperationBatch]:
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    return list(db.execute(
        select(ReverseOperationBatch)
        .where(ReverseOperationBatch.user_id == user_id)
        .order_by(ReverseOperationBatch.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars())


def request_batch_cancel(
    db: Session,
    *,
    batch_id: int,
    user_id: int,
) -> ReverseOperationBatch:
    from . import reverse_operations as _ro

    batch = db.execute(
        select(ReverseOperationBatch)
        .where(
            ReverseOperationBatch.id == batch_id,
            ReverseOperationBatch.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if batch is None:
        raise ReverseOperationNotFound("批量反推任务不存在")
    active_ids = [
        int(operation.id)
        for _, operation in _batch_item_rows(db, int(batch.id))
        if operation.status not in TERMINAL_STATUSES
    ]
    if active_ids and not batch.cancel_requested:
        batch.cancel_requested = True
        batch.updated_at = utcnow()
        db.commit()
    else:
        db.rollback()
    for operation_id in active_ids:
        _ro.request_cancel(db, operation_id=operation_id, user_id=user_id)
    batch = get_owned_batch(db, batch_id=batch_id, user_id=user_id)
    return sync_batch_state(db, batch)
