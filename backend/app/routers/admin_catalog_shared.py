"""Shared catalog administration helpers."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    ModelConfig,
    ModelRoute,
    ModelRouteHealthEvent,
    ToolDefinition,
    ToolVersion,
)
from ..services.catalog_metadata import (
    TOOL_METADATA_FIELDS,
    TOOL_METADATA_SCHEMA_VERSION,
    recorded_snapshot,
    snapshot_fields,
    tool_metadata_snapshot,
)


def _commit(db: Session, message: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, message) from exc


def _route_for_model(db: Session, model_config_id: int, route_id: int) -> tuple[ModelConfig, ModelRoute]:
    model = db.get(ModelConfig, model_config_id)
    if model is None:
        raise HTTPException(404, "模型配置不存在")
    route = db.scalar(
        select(ModelRoute).where(
            ModelRoute.id == route_id,
            ModelRoute.model_config_id == model.id,
            ModelRoute.deleted_at.is_(None),
        )
    )
    if route is None:
        raise HTTPException(404, "模型路由不存在")
    return model, route


def _route_probe_error_code(value: str | None) -> str | None:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())[:64]
    return normalized or None


def _route_probe_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_route_probe_outcome(
    db: Session,
    route: ModelRoute,
    *,
    success: bool,
    latency_ms: int,
    error_code: str | None = None,
    counts_toward_circuit: bool = True,
) -> None:
    now = datetime.now(timezone.utc)
    latency = max(0, int(latency_ms))
    safe_error_code = _route_probe_error_code(error_code)
    db.add(
        ModelRouteHealthEvent(
            route_id=int(route.id),
            operation="probe",
            outcome="success"
            if success
            else ("failure" if counts_toward_circuit else "ignored"),
            counts_toward_circuit=bool(not success and counts_toward_circuit),
            latency_ms=latency,
            error_code=safe_error_code,
        )
    )
    route.last_probe_at = now
    previous = route.latency_ema_ms
    route.latency_ema_ms = latency if previous is None else round(previous * 0.8 + latency * 0.2)
    if success:
        route.last_success_at = now
        route.health_status = "closed"
        route.window_started_at = now
        route.window_requests = 1
        route.window_failures = 0
        route.consecutive_failures = 0
        route.opened_at = None
        route.cooldown_until = None
        route.half_open_claimed_until = None
        route.last_error_code = None
    elif counts_toward_circuit:
        window_started = _route_probe_utc(route.window_started_at)
        if (
            window_started is None
            or window_started + timedelta(seconds=int(route.window_seconds)) <= now
        ):
            route.window_started_at = now
            route.window_requests = 0
            route.window_failures = 0
        route.window_requests = int(route.window_requests or 0) + 1
        route.window_failures = int(route.window_failures or 0) + 1
        route.consecutive_failures = int(route.consecutive_failures or 0) + 1
        route.last_failure_at = now
        route.last_error_code = safe_error_code
        should_open = (
            route.health_status == "half_open"
            or int(route.window_failures) >= int(route.failure_threshold)
            or int(route.consecutive_failures) >= int(route.failure_threshold)
        )
        if should_open:
            route.health_status = "open"
            route.opened_at = now
            route.cooldown_until = now + timedelta(seconds=int(route.cooldown_seconds))
            route.half_open_claimed_until = None
    db.flush()


_TOOL_VERSION_PAYLOAD_FIELDS = (
    "schema_version",
    "input_schema",
    "workflow",
    "pricing_policy",
    "capabilities",
)


def _validated_tool_metadata(row: ToolVersion) -> dict:
    values = snapshot_fields(row.metadata_snapshot, TOOL_METADATA_FIELDS)
    if set(values) != set(TOOL_METADATA_FIELDS):
        raise HTTPException(409, "工具版本缺少完整目录元数据快照")
    required_text = ("slug", "name", "category", "renderer", "entry_path")
    if any(not isinstance(values[field], str) or not values[field].strip() for field in required_text):
        raise HTTPException(409, "工具版本的目录文本元数据无效")
    if not values["entry_path"].startswith("/") or values["entry_path"].startswith("//"):
        raise HTTPException(409, "工具版本的站内入口无效")
    if not isinstance(values["enabled"], bool) or not isinstance(values["featured"], bool):
        raise HTTPException(409, "工具版本的目录状态元数据无效")
    if isinstance(values["sort_order"], bool) or not isinstance(values["sort_order"], int):
        raise HTTPException(409, "工具版本的目录排序元数据无效")
    return values


def _apply_tool_metadata(tool: ToolDefinition, row: ToolVersion) -> None:
    for field, value in _validated_tool_metadata(row).items():
        setattr(tool, field, deepcopy(value))


def _serialize_tool_version(row: ToolVersion) -> dict:
    return {
        "id": int(row.id),
        "version": int(row.version),
        "schema_version": row.schema_version,
        "input_schema": deepcopy(row.input_schema or {}),
        "workflow": deepcopy(row.workflow or {}),
        "pricing_policy": deepcopy(row.pricing_policy or {}),
        "capabilities": deepcopy(row.capabilities or {}),
        "metadata_snapshot": deepcopy(row.metadata_snapshot or {}),
        "status": row.status,
        "is_active": bool(row.is_active),
        "source_version_id": row.source_version_id,
        "activated_at": row.activated_at,
        "disabled_at": row.disabled_at,
        "retired_at": row.retired_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _admin_tool_detail(
    db: Session,
    tool: ToolDefinition,
    *,
    history: bool = False,
) -> dict:
    query = select(ToolVersion).where(ToolVersion.tool_definition_id == int(tool.id))
    if not history:
        query = query.where(ToolVersion.is_active.is_(True))
    versions = list(db.scalars(query.order_by(ToolVersion.version.desc())))
    active = next((row for row in versions if row.is_active), None)
    return {
        "id": int(tool.id),
        "slug": tool.slug,
        "name": tool.name,
        "description": tool.description,
        "category": tool.category,
        "renderer": tool.renderer,
        "entry_path": tool.entry_path,
        "icon": tool.icon,
        "sort_order": int(tool.sort_order or 0),
        "enabled": bool(tool.enabled),
        "featured": bool(tool.featured),
        "active_version": _serialize_tool_version(active) if active is not None else None,
        "created_at": tool.created_at,
        "updated_at": tool.updated_at,
        **(
            {"versions": [_serialize_tool_version(row) for row in versions]}
            if history
            else {}
        ),
    }


def _lock_tool(db: Session, tool_id: int) -> ToolDefinition:
    tool = db.scalar(
        select(ToolDefinition)
        .where(
            ToolDefinition.id == int(tool_id),
            ToolDefinition.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if tool is None:
        raise HTTPException(404, "工具不存在")
    return tool


def _locked_tool_versions(db: Session, tool: ToolDefinition) -> list[ToolVersion]:
    return list(
        db.scalars(
            select(ToolVersion)
            .where(ToolVersion.tool_definition_id == int(tool.id))
            .order_by(ToolVersion.version)
            .with_for_update()
        )
    )


def _tool_version_row(
    versions: list[ToolVersion],
    version_number: int,
) -> ToolVersion:
    target = next((row for row in versions if int(row.version) == int(version_number)), None)
    if target is None:
        raise HTTPException(404, "工具版本不存在")
    return target


def _disable_active_tool_version(
    versions: list[ToolVersion],
    *,
    now: datetime,
    except_id: int | None = None,
) -> None:
    for row in versions:
        if row.is_active and (except_id is None or int(row.id) != int(except_id)):
            row.is_active = False
            row.status = "disabled"
            row.disabled_at = now


def _publish_tool_version_draft(
    db: Session,
    tool: ToolDefinition,
    versions: list[ToolVersion],
    target: ToolVersion,
) -> ToolVersion:
    if target.status != "draft" or target.is_active:
        raise HTTPException(409, "只有草稿工具版本可以发布")
    now = datetime.now(timezone.utc)
    _disable_active_tool_version(versions, now=now, except_id=int(target.id))
    db.flush()
    target.status = "published"
    target.is_active = True
    target.activated_at = now
    target.disabled_at = None
    target.retired_at = None
    _apply_tool_metadata(tool, target)
    db.flush()
    return target


def _rollback_tool_version_copy(
    db: Session,
    tool: ToolDefinition,
    versions: list[ToolVersion],
    source: ToolVersion,
) -> ToolVersion:
    if source.status != "disabled" or source.is_active:
        raise HTTPException(409, "只能回滚到已停用且未退役的历史工具版本")
    now = datetime.now(timezone.utc)
    _disable_active_tool_version(versions, now=now)
    db.flush()
    target = ToolVersion(
        tool_definition_id=int(tool.id),
        version=max((int(row.version) for row in versions), default=0) + 1,
        **{
            field: deepcopy(getattr(source, field))
            for field in _TOOL_VERSION_PAYLOAD_FIELDS
        },
        metadata_snapshot=recorded_snapshot(
            source.metadata_snapshot,
            schema_version=TOOL_METADATA_SCHEMA_VERSION,
            fields=TOOL_METADATA_FIELDS,
        ),
        status="published",
        is_active=True,
        source_version_id=int(source.id),
        activated_at=now,
    )
    db.add(target)
    db.flush()
    _apply_tool_metadata(tool, target)
    db.flush()
    return target


def _publish_tool_metadata_change(
    db: Session,
    tool: ToolDefinition,
    versions: list[ToolVersion],
) -> ToolVersion:
    source = next((row for row in versions if row.is_active), None)
    if source is None:
        source = next(
            (
                row
                for row in reversed(versions)
                if row.status in {"published", "disabled"} and not row.is_active
            ),
            None,
        )
    now = datetime.now(timezone.utc)
    _disable_active_tool_version(versions, now=now)
    db.flush()
    target = ToolVersion(
        tool_definition_id=int(tool.id),
        version=max((int(row.version) for row in versions), default=0) + 1,
        schema_version=source.schema_version if source is not None else "tool.v1",
        input_schema=deepcopy(source.input_schema or {}) if source is not None else {},
        workflow=deepcopy(source.workflow or {}) if source is not None else {},
        pricing_policy=deepcopy(source.pricing_policy or {}) if source is not None else {},
        capabilities=deepcopy(source.capabilities or {}) if source is not None else {},
        metadata_snapshot=tool_metadata_snapshot(tool),
        status="published",
        is_active=True,
        source_version_id=int(source.id) if source is not None else None,
        activated_at=now,
    )
    db.add(target)
    db.flush()
    return target
