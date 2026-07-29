"""Retry request reconstruction for terminal reverse operations."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReverseOperation, ReverseOperationBatchItem
from ..schemas import ReverseOperationCreate
from . import project_collection
from .reverse_operation_records import _create_quoted_operation
from .reverse_quotes import (
    TERMINAL_STATUSES,
    ReverseOperationConflict,
    ReverseOperationNotFound,
)


def build_retry_operation_body(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    client_request_id: str,
    model_config_id: int | None = None,
    quote_id: int | None = None,
    lock: bool = False,
) -> tuple[ReverseOperation, ReverseOperationCreate]:
    query = select(ReverseOperation).where(
        ReverseOperation.id == int(operation_id),
        ReverseOperation.user_id == int(user_id),
    )
    if lock:
        query = query.with_for_update()
    previous = db.scalar(query)
    if previous is None:
        raise ReverseOperationNotFound("反推任务不存在")
    if previous.status not in TERMINAL_STATUSES:
        raise ReverseOperationConflict("当前反推任务尚未结束，不能再次反推")
    context = dict(previous.request_context or {})
    context["client_request_id"] = client_request_id.strip()
    context.pop("quote_id", None)
    if model_config_id is not None:
        context["model_config_id"] = model_config_id
    else:
        context["model_config_id"] = previous.model_config_id
    inherited_project_id = project_collection.primary_project_id_for_task(
        db,
        user_id=user_id,
        task_kind="reverse",
        task_id=int(previous.id),
    )
    if inherited_project_id is not None:
        context["project_id"] = inherited_project_id
    else:
        context.pop("project_id", None)
    if quote_id is not None:
        context["quote_id"] = int(quote_id)
    body = ReverseOperationCreate.model_validate(context)
    return previous, body


def retry_operation(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    client_request_id: str,
    quote_id: int,
    model_config_id: int | None = None,
) -> tuple[ReverseOperation, bool]:
    previous, body = build_retry_operation_body(
        db,
        operation_id=operation_id,
        user_id=user_id,
        client_request_id=client_request_id,
        model_config_id=model_config_id,
        quote_id=quote_id,
        lock=True,
    )
    batch_item = db.scalar(
        select(ReverseOperationBatchItem)
        .where(ReverseOperationBatchItem.operation_id == int(previous.id))
        .with_for_update()
    )
    return _create_quoted_operation(
        db,
        user_id=user_id,
        body=body,
        retry_of_operation_id=int(previous.id),
        batch_item=batch_item,
    )
