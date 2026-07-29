"""Workflow quote creation and validation."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GenerationQuote, ToolDefinition, ToolVersion
from ..workflow_schemas import ToolRunCreateIn, WorkflowSpec
from .catalog_metadata import resolved_tool_metadata_snapshot
from .generation_quote_core import (
    _balance_warning,
    _invalid_quote_snapshot,
    _utc,
    create_execution_quote,
    quote_item,
)


def workflow_request_fingerprint(
    *,
    tool_version_id: int,
    payload: dict[str, Any],
    project_id: int | None = None,
) -> str:
    raw = json.dumps(
        {
            "tool_version_id": int(tool_version_id),
            "project_id": int(project_id) if project_id is not None else None,
            "input": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _active_tool_version(db: Session, slug: str) -> tuple[ToolDefinition, ToolVersion]:
    row = db.execute(
        select(ToolDefinition, ToolVersion)
        .join(ToolVersion, ToolVersion.tool_definition_id == ToolDefinition.id)
        .where(
            ToolDefinition.slug == slug,
            ToolDefinition.enabled.is_(True),
            ToolVersion.is_active.is_(True),
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            404,
            detail={"code": "TOOL_NOT_FOUND", "message": "工具不存在或没有可用版本"},
        )
    return row[0], row[1]


def _workflow_request_snapshot(body: ToolRunCreateIn) -> dict[str, Any]:
    return {
        "tool_slug": body.tool_slug,
        "project_id": int(body.project_id) if body.project_id is not None else None,
        "client_request_id": body.client_request_id,
        "input": deepcopy(body.input),
    }


def _workflow_subject_snapshot(
    tool: ToolDefinition,
    version: ToolVersion,
    spec: WorkflowSpec,
) -> dict[str, Any]:
    metadata = resolved_tool_metadata_snapshot(tool, version.metadata_snapshot)
    return {
        "tool_definition_id": int(tool.id),
        "tool_slug": metadata["slug"],
        "tool_name": metadata["name"],
        "tool_metadata": metadata,
        "tool_version_id": int(version.id),
        "tool_version": int(version.version),
        "schema_version": version.schema_version,
        "input_schema": deepcopy(version.input_schema or {}),
        "workflow": spec.model_dump(mode="json"),
        "pricing_policy": deepcopy(version.pricing_policy or {}),
        "capabilities": deepcopy(version.capabilities or {}),
    }


def _find_workflow_quote_replay(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> GenerationQuote | None:
    quote = db.scalar(
        select(GenerationQuote)
        .where(
            GenerationQuote.user_id == int(user_id),
            GenerationQuote.kind == "workflow",
            GenerationQuote.client_request_id == body.client_request_id,
        )
        .order_by(GenerationQuote.id.desc())
        .limit(1)
    )
    if quote is None:
        return None
    if deepcopy(quote.request_snapshot or {}) != _workflow_request_snapshot(body):
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
    if quote.status == "active" and _utc(quote.expires_at) <= datetime.now(timezone.utc):
        quote.status = "expired"
        db.flush()
        return None
    return quote if quote.status == "active" else None


def create_workflow_quote(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> GenerationQuote:
    replay = _find_workflow_quote_replay(db, user_id=user_id, body=body)
    if replay is not None:
        return replay
    tool, version = _active_tool_version(db, body.tool_slug)
    try:
        spec = WorkflowSpec.model_validate(version.workflow or {})
    except Exception as exc:
        raise HTTPException(
            409,
            detail={"code": "WORKFLOW_INVALID", "message": "当前工具版本不是可执行工作流"},
        ) from exc
    fingerprint = workflow_request_fingerprint(
        tool_version_id=int(version.id),
        payload=body.input,
        project_id=body.project_id,
    )
    pricing = deepcopy(version.pricing_policy or {})
    try:
        total = max(0, int(pricing.get("credits") or 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            409,
            detail={"code": "WORKFLOW_PRICING_INVALID", "message": "工具版本计价策略非法"},
        ) from exc
    snapshot = _workflow_subject_snapshot(tool, version, spec)
    return create_execution_quote(
        db,
        user_id=user_id,
        kind="workflow",
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
        category="workflow",
        stage="final",
        request_snapshot=_workflow_request_snapshot(body),
        model_snapshot={},
        pricing_snapshot=pricing,
        price_breakdown={
            "items": [
                quote_item(
                    code="workflow_orchestration",
                    label="工作流编排",
                    credits=total,
                )
            ]
        },
        estimated_credits=total,
        tool_version_id=int(version.id),
        subject_snapshot=snapshot,
        warnings=_balance_warning(db, user_id=user_id, total=total),
    )


def validate_workflow_quote(
    db: Session,
    quote: GenerationQuote,
    *,
    body: ToolRunCreateIn,
) -> tuple[ToolDefinition, ToolVersion, WorkflowSpec]:
    if quote.kind != "workflow":
        raise HTTPException(
            409,
            detail={"code": "QUOTE_KIND_MISMATCH", "message": "报价不是工作流类型"},
        )
    if str(quote.client_request_id or "") != body.client_request_id:
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_MISMATCH",
                "message": "工作流报价与执行请求标识不一致，请重新报价",
            },
        )
    if quote.tool_version_id is None:
        _invalid_quote_snapshot("工作流报价缺少工具版本，请重新报价")
    version = db.get(ToolVersion, int(quote.tool_version_id))
    subject = quote.subject_snapshot if isinstance(quote.subject_snapshot, dict) else {}
    if version is None:
        _invalid_quote_snapshot("工作流报价引用的工具版本不存在")
    tool = db.get(ToolDefinition, int(version.tool_definition_id))
    if tool is None:
        _invalid_quote_snapshot("工作流报价引用的工具定义不存在")
    metadata = resolved_tool_metadata_snapshot(tool, version.metadata_snapshot)
    if metadata["slug"] != body.tool_slug:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_MISMATCH", "message": "工作流工具已变化，请重新报价"},
        )
    fingerprint = workflow_request_fingerprint(
        tool_version_id=int(version.id),
        payload=body.input,
        project_id=body.project_id,
    )
    if quote.request_fingerprint != fingerprint:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_MISMATCH", "message": "工作流输入已变化，请重新报价"},
        )
    expected_request = _workflow_request_snapshot(body)
    if deepcopy(quote.request_snapshot or {}) != expected_request:
        _invalid_quote_snapshot("工作流报价请求快照已被篡改，请重新报价")
    try:
        spec = WorkflowSpec.model_validate(version.workflow or {})
    except Exception as exc:
        _invalid_quote_snapshot(f"工作流报价 DAG 非法: {exc}")
    expected = _workflow_subject_snapshot(tool, version, spec)
    if subject != expected or deepcopy(quote.pricing_snapshot or {}) != expected["pricing_policy"]:
        _invalid_quote_snapshot("工作流报价版本快照已被篡改，请重新报价")
    return tool, version, spec
