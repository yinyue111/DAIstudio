"""Normalize and dispatch unified quote requests.

The HTTP router owns authentication, feature gates, rate limits, and exception
mapping. This module owns the executable-kind request envelope and delegates to
the domain-specific quote builders.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ..models import GenerationQuote
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from ..schemas import (
    ExecutionQuoteIn,
    GenerationQuoteIn,
    ReverseBatchCreate,
    ReverseOperationCreate,
)
from ..workflow_schemas import ToolRunCreateIn
from . import reverse_operations
from .generation_quote_asset_unlock import create_asset_unlock_quote
from .generation_quote_prompt_opt import create_prompt_optimization_quote
from .generation_quote_reverse import create_reverse_batch_quote, create_reverse_quote
from .generation_quote_workflow import create_workflow_quote

GenerationQuoteFactory = Callable[[GenerationQuoteIn], GenerationQuote]


def validate_execution_quote_request(
    schema: type[BaseModel],
    request_payload: dict,
) -> BaseModel:
    try:
        return schema.model_validate(request_payload)
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "code": "QUOTE_REQUEST_INVALID",
                "message": "报价请求参数无效",
                "errors": exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                ),
            },
        ) from exc


def normalized_execution_quote_payload(body: ExecutionQuoteIn) -> dict:
    request_payload = dict(body.request or {})
    nested_request_id = request_payload.get("client_request_id")
    if (
        nested_request_id is not None
        and str(nested_request_id).strip() != body.client_request_id
    ):
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_IDEMPOTENCY_CONFLICT",
                "message": "报价 envelope 与 request 的 client_request_id 不一致",
            },
        )
    request_payload.pop("quote_id", None)
    if body.kind == "prompt_optimization":
        nested_idempotency_key = request_payload.get("idempotency_key")
        if (
            nested_idempotency_key is not None
            and str(nested_idempotency_key).strip() != body.client_request_id
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "QUOTE_IDEMPOTENCY_CONFLICT",
                    "message": "报价 envelope 与 request 的 idempotency_key 不一致",
                },
            )
        request_payload.pop("client_request_id", None)
        request_payload["idempotency_key"] = body.client_request_id
    else:
        request_payload["client_request_id"] = body.client_request_id
    return request_payload


def _create_reverse_request_quote(
    db: Session,
    *,
    user_id: int,
    client_request_id: str,
    request_payload: dict,
) -> GenerationQuote:
    retry_operation_id = request_payload.get("reverse_operation_id")
    if retry_operation_id is None:
        return create_reverse_quote(
            db,
            user_id=user_id,
            body=validate_execution_quote_request(
                ReverseOperationCreate,
                request_payload,
            ),
        )

    unexpected = set(request_payload).difference(
        {"reverse_operation_id", "client_request_id", "model_config_id"}
    )
    if (
        unexpected
        or isinstance(retry_operation_id, bool)
        or not isinstance(retry_operation_id, int)
        or retry_operation_id <= 0
    ):
        raise HTTPException(
            422,
            detail={
                "code": "QUOTE_REQUEST_INVALID",
                "message": "反推重试报价只接受任务、请求标识和可选模型",
            },
        )
    previous, retry_body = reverse_operations.build_retry_operation_body(
        db,
        operation_id=retry_operation_id,
        user_id=user_id,
        client_request_id=client_request_id,
        model_config_id=request_payload.get("model_config_id"),
    )
    return create_reverse_quote(
        db,
        user_id=user_id,
        body=retry_body,
        retry_of_operation_id=int(previous.id),
    )


def _asset_unlock_id(request_payload: dict) -> int:
    unexpected = set(request_payload).difference({"asset_id", "client_request_id"})
    asset_id = request_payload.get("asset_id")
    if (
        unexpected
        or isinstance(asset_id, bool)
        or not isinstance(asset_id, int)
        or asset_id <= 0
    ):
        raise HTTPException(
            422,
            detail={
                "code": "QUOTE_REQUEST_INVALID",
                "message": "素材解锁报价只接受有效的 asset_id",
            },
        )
    return asset_id


def create_requested_execution_quote(
    db: Session,
    *,
    user_id: int,
    body: ExecutionQuoteIn,
    generation_quote_factory: GenerationQuoteFactory,
) -> GenerationQuote:
    request_payload = normalized_execution_quote_payload(body)
    if body.kind == "generation":
        generation_body = validate_execution_quote_request(
            GenerationQuoteIn,
            request_payload,
        )
        return generation_quote_factory(generation_body)
    if body.kind == "reverse":
        return _create_reverse_request_quote(
            db,
            user_id=user_id,
            client_request_id=body.client_request_id,
            request_payload=request_payload,
        )
    if body.kind == "reverse_batch":
        return create_reverse_batch_quote(
            db,
            user_id=user_id,
            body=validate_execution_quote_request(
                ReverseBatchCreate,
                request_payload,
            ),
        )
    if body.kind == "workflow":
        return create_workflow_quote(
            db,
            user_id=user_id,
            body=validate_execution_quote_request(ToolRunCreateIn, request_payload),
        )
    if body.kind == "prompt_optimization":
        return create_prompt_optimization_quote(
            db,
            user_id=user_id,
            body=validate_execution_quote_request(
                StudioPromptOptimizationIn,
                request_payload,
            ),
        )
    return create_asset_unlock_quote(
        db,
        user_id=user_id,
        asset_id=_asset_unlock_id(request_payload),
        client_request_id=body.client_request_id,
    )
