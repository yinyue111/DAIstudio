"""Persistence and state transitions for executable quotes."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenerationQuote
from .generation_quote_contracts import (
    QUOTE_KINDS,
    _utc,
    normalized_price_breakdown,
)


def create_execution_quote(
    db: Session,
    *,
    user_id: int,
    kind: str,
    client_request_id: str | None,
    request_fingerprint: str,
    category: str,
    stage: str,
    request_snapshot: dict[str, Any],
    model_snapshot: dict[str, Any] | None,
    pricing_snapshot: dict[str, Any] | None,
    price_breakdown: dict[str, Any] | None,
    estimated_credits: int,
    model_config_id: int | None = None,
    capability_version_id: int | None = None,
    price_version_id: int | None = None,
    tool_version_id: int | None = None,
    subject_snapshot: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> GenerationQuote:
    if kind not in QUOTE_KINDS:
        raise ValueError(f"unsupported quote kind: {kind}")
    total = max(0, int(estimated_credits))
    now = datetime.now(timezone.utc)
    quote = GenerationQuote(
        user_id=int(user_id),
        kind=kind,
        client_request_id=(str(client_request_id).strip() if client_request_id else None),
        model_config_id=model_config_id,
        capability_version_id=capability_version_id,
        price_version_id=price_version_id,
        tool_version_id=tool_version_id,
        request_fingerprint=request_fingerprint,
        category=category,
        stage=stage,
        request_snapshot=deepcopy(request_snapshot),
        model_snapshot=deepcopy(model_snapshot or {}),
        pricing_snapshot=deepcopy(pricing_snapshot or {}),
        price_breakdown=normalized_price_breakdown(
            price_breakdown,
            total_credits=total,
        ),
        subject_snapshot=deepcopy(subject_snapshot or {}),
        warnings=deepcopy(warnings or []),
        estimated_credits=total,
        status="active",
        expires_at=now + timedelta(seconds=int(settings.generation_quote_ttl_seconds)),
    )
    db.add(quote)
    db.flush()
    return quote


def find_idempotent_quote(
    db: Session,
    *,
    user_id: int,
    kind: str,
    client_request_id: str | None,
    request_fingerprint: str,
) -> GenerationQuote | None:
    if not client_request_id:
        return None
    quote = db.scalar(
        select(GenerationQuote)
        .where(
            GenerationQuote.user_id == int(user_id),
            GenerationQuote.kind == kind,
            GenerationQuote.client_request_id == client_request_id,
        )
        .order_by(GenerationQuote.id.desc())
        .limit(1)
    )
    if quote is None:
        return None
    if quote.request_fingerprint != request_fingerprint:
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_IDEMPOTENCY_CONFLICT",
                "message": "client_request_id 已用于不同的报价请求",
            },
        )
    consumed = (
        quote.status == "consumed"
        or quote.task_id is not None
        or quote.consumed_ref_id is not None
    )
    if consumed:
        return quote
    now = datetime.now(timezone.utc)
    if quote.status == "active" and _utc(quote.expires_at) <= now:
        quote.status = "expired"
        db.flush()
        return None
    if quote.status == "active":
        return quote
    return None


def lock_execution_quote(
    db: Session,
    *,
    quote_id: int,
    user_id: int,
    kind: str,
    allow_consumed: bool = False,
) -> GenerationQuote:
    quote = db.scalar(
        select(GenerationQuote)
        .where(
            GenerationQuote.id == int(quote_id),
            GenerationQuote.user_id == int(user_id),
        )
        .with_for_update()
    )
    if quote is None:
        raise HTTPException(404, detail={"code": "QUOTE_NOT_FOUND", "message": "报价不存在"})
    if quote.kind != kind:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价类型与执行请求不匹配"},
        )
    now = datetime.now(timezone.utc)
    if quote.status == "active" and _utc(quote.expires_at) <= now:
        quote.status = "expired"
        db.flush()
    if quote.status == "expired":
        raise HTTPException(409, detail={"code": "QUOTE_EXPIRED", "message": "报价已过期，请重新报价"})
    if quote.status == "consumed" and allow_consumed:
        return quote
    if quote.status != "active" or quote.consumed_ref_id is not None or quote.task_id is not None:
        raise HTTPException(409, detail={"code": "QUOTE_CONSUMED", "message": "报价已被使用"})
    return quote


def consume_execution_quote(
    quote: GenerationQuote,
    *,
    ref_type: str,
    ref_id: int,
) -> None:
    quote.status = "consumed"
    quote.consumed_ref_type = str(ref_type)
    quote.consumed_ref_id = int(ref_id)
    if quote.kind == "generation" and ref_type == "gen_task":
        quote.task_id = int(ref_id)
    quote.consumed_at = datetime.now(timezone.utc)
