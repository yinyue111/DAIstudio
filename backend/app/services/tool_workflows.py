"""Durable, idempotent execution for versioned ToolVersion DAGs."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import (
    ToolDefinition,
    ToolNodeAttempt,
    ToolNodeRun,
    ToolRun,
    ToolVersion,
    WorkflowRun,
)
from ..workflow_schemas import ToolRunCreateIn, WorkflowSpec
from . import credits, generation_quotes
from .catalog_metadata import resolved_tool_metadata_snapshot

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "canceled"}
TERMINAL_NODE_STATUSES = {"succeeded", "failed", "canceled"}
DEFAULT_NODE_LEASE_SECONDS = 15 * 60


class WorkflowServiceError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class WorkflowNotFound(WorkflowServiceError):
    def __init__(self, message: str = "工作流运行不存在"):
        super().__init__(404, message)


class WorkflowConflict(WorkflowServiceError):
    def __init__(self, message: str):
        super().__init__(409, message)


@dataclass(frozen=True)
class NodeExecutionContext:
    workflow_run_id: int
    tool_run_id: int
    node_run_id: int
    node_key: str
    node_type: str
    attempt_number: int
    workflow_input: dict[str, Any]
    dependency_outputs: dict[str, Any]
    node_input: dict[str, Any]
    config: dict[str, Any]
    previous_output: dict[str, Any] | None = None
    external_kind: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class NodeExecutionResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None
    external_kind: str | None = None
    external_id: str | None = None

    @classmethod
    def succeeded(cls, output: dict[str, Any] | None = None):
        return cls(status="succeeded", output=dict(output or {}))

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        code: str = "NODE_FAILED",
        output: dict[str, Any] | None = None,
    ):
        return cls(
            status="failed",
            output=dict(output or {}),
            error_code=code,
            error=str(error),
        )

    @classmethod
    def unsupported(cls, capability: str, *, reason: str):
        return cls.failed(
            reason,
            code="NODE_CAPABILITY_UNSUPPORTED",
            output={
                "capability": str(capability),
                "capability_status": "unsupported",
                "reason": str(reason),
            },
        )

    @classmethod
    def waiting_external(
        cls,
        *,
        external_kind: str,
        external_id: str,
        output: dict[str, Any] | None = None,
    ):
        return cls(
            status="waiting_external",
            output=dict(output or {}),
            external_kind=external_kind,
            external_id=external_id,
        )


NodeHandler = Callable[[NodeExecutionContext], NodeExecutionResult]
_NODE_HANDLERS: dict[str, NodeHandler] = {}
_COMPENSATION_HANDLERS: dict[str, NodeHandler] = {}


def register_node_handler(node_type: str, handler: NodeHandler) -> None:
    if node_type not in {"parse", "reverse", "compile", "generate", "compose", "export"}:
        raise ValueError("不支持的工作流节点处理器")
    _NODE_HANDLERS[node_type] = handler


def unregister_node_handler(node_type: str) -> None:
    _NODE_HANDLERS.pop(node_type, None)


def register_compensation_handler(compensation_type: str, handler: NodeHandler) -> None:
    _COMPENSATION_HANDLERS[compensation_type] = handler


def unregister_compensation_handler(compensation_type: str) -> None:
    _COMPENSATION_HANDLERS.pop(compensation_type, None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_deadline(config: dict[str, Any], now: datetime) -> datetime:
    try:
        configured_timeout = int(config.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        configured_timeout = 0
    lease_seconds = min(24 * 60 * 60, max(30, configured_timeout or DEFAULT_NODE_LEASE_SECONDS))
    return now + timedelta(seconds=lease_seconds)


def _deadline_reached(deadline: datetime | None, now: datetime) -> bool:
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now


def _schedule_lease_wakeup(run_id: int, deadline: datetime | None) -> None:
    if deadline is None:
        return
    from ..celery_app import celery_app

    if celery_app.conf.task_always_eager:
        return
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    countdown = max(1, int((deadline - utcnow()).total_seconds()) + 1)
    enqueue_run(
        run_id,
        countdown=countdown,
        dedupe_suffix=f"lease:{deadline.isoformat()}",
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(
    *,
    version_id: int,
    payload: dict[str, Any],
    project_id: int | None = None,
) -> str:
    raw = _canonical_json(
        {
            "tool_version_id": version_id,
            "project_id": int(project_id) if project_id is not None else None,
            "input": payload,
        }
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


_REFERENCE_FIELDS = {
    "model_config_id": "model_config_ids",
    "capability_version_id": "capability_version_ids",
    "price_version_id": "price_version_ids",
    "route_id": "route_ids",
    "route_version_id": "route_version_ids",
}


def _runtime_references(*values: Any) -> dict[str, list[int]]:
    references = {target: set() for target in _REFERENCE_FIELDS.values()}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                target = _REFERENCE_FIELDS.get(str(key))
                if target is not None and item is not None and not isinstance(item, bool):
                    try:
                        references[target].add(int(item))
                    except (TypeError, ValueError):
                        pass
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return {key: sorted(items) for key, items in references.items()}


def _compiled_workflow_snapshot(
    *,
    tool: ToolDefinition,
    version,
    spec: WorkflowSpec,
    quote,
    body: ToolRunCreateIn,
    request_fingerprint: str,
) -> tuple[dict[str, Any], str]:
    declared_order = [node.key for node in spec.nodes]
    topological_order = spec.topological_keys()
    topological_indexes = {key: index for index, key in enumerate(topological_order)}
    nodes = [
        {
            "key": node.key,
            "type": node.type,
            "declared_index": index,
            "topological_index": topological_indexes[node.key],
            "depends_on": list(node.depends_on),
            "config": deepcopy(node.config),
            "max_attempts": int(node.max_attempts),
            "side_effect": bool(node.side_effect),
            "compensation": (
                node.compensation.model_dump(mode="json")
                if node.compensation is not None
                else None
            ),
        }
        for index, node in enumerate(spec.nodes)
    ]
    metadata = resolved_tool_metadata_snapshot(tool, version.metadata_snapshot)
    tool_snapshot = {
        "definition": {
            "id": int(tool.id),
            **{
                field: deepcopy(metadata[field])
                for field in (
                    "slug",
                    "name",
                    "description",
                    "category",
                    "renderer",
                    "entry_path",
                    "icon",
                    "sort_order",
                    "enabled",
                    "featured",
                )
            },
            "created_at": _iso(tool.created_at),
            "updated_at": _iso(tool.updated_at),
        },
        "version": {
            "id": int(version.id),
            "tool_definition_id": int(version.tool_definition_id),
            "version": int(version.version),
            "schema_version": version.schema_version,
            "input_schema": deepcopy(version.input_schema or {}),
            "workflow": deepcopy(version.workflow or {}),
            "pricing_policy": deepcopy(version.pricing_policy or {}),
            "capabilities": deepcopy(version.capabilities or {}),
            "metadata_snapshot": deepcopy(metadata),
            "status": version.status,
            "is_active": bool(version.is_active),
            "source_version_id": version.source_version_id,
            "activated_at": _iso(version.activated_at),
            "disabled_at": _iso(version.disabled_at),
            "retired_at": _iso(version.retired_at),
            "created_at": _iso(version.created_at),
            "updated_at": _iso(version.updated_at),
        },
    }
    quote_snapshot = {
        "id": int(quote.id),
        "user_id": int(quote.user_id),
        "kind": quote.kind,
        "status": quote.status,
        "category": quote.category,
        "stage": quote.stage,
        "client_request_id": quote.client_request_id,
        "request_fingerprint": quote.request_fingerprint,
        "model_config_id": quote.model_config_id,
        "capability_version_id": quote.capability_version_id,
        "price_version_id": quote.price_version_id,
        "tool_version_id": quote.tool_version_id,
        "request_snapshot": deepcopy(quote.request_snapshot or {}),
        "model_snapshot": deepcopy(quote.model_snapshot or {}),
        "pricing_snapshot": deepcopy(quote.pricing_snapshot or {}),
        "price_breakdown": deepcopy(quote.price_breakdown or {}),
        "subject_snapshot": deepcopy(quote.subject_snapshot or {}),
        "warnings": deepcopy(quote.warnings or []),
        "estimated_credits": int(quote.estimated_credits or 0),
        "expires_at": _iso(quote.expires_at),
        "created_at": _iso(quote.created_at),
    }
    capabilities = deepcopy(version.capabilities or {})
    node_safety = {
        node.key: deepcopy(node.config.get("safety_policy"))
        for node in spec.nodes
        if isinstance(node.config.get("safety_policy"), dict)
    }
    snapshot = {
        "schema_version": "workflow-compiled.v1",
        "tool": tool_snapshot,
        "input": {
            "project_id": int(body.project_id) if body.project_id is not None else None,
            "client_request_id": body.client_request_id,
            "request_fingerprint": request_fingerprint,
            "payload": deepcopy(body.input),
        },
        "dag": {
            "schema_version": spec.type,
            "declared_order": declared_order,
            "topological_order": topological_order,
            "output_node": spec.output_node,
            "studio_preset": (
                spec.studio_preset.model_dump(mode="json")
                if spec.studio_preset is not None
                else None
            ),
            "nodes": nodes,
        },
        "runtime_references": _runtime_references(
            quote_snapshot,
            tool_snapshot["version"],
        ),
        "model_binding": {
            "model_config_id": quote.model_config_id,
            "capability_version_id": quote.capability_version_id,
            "price_version_id": quote.price_version_id,
            "model_snapshot": deepcopy(quote.model_snapshot or {}),
            "route_snapshot": deepcopy((quote.model_snapshot or {}).get("route_snapshot")),
        },
        "quote_binding": quote_snapshot,
        "pricing_binding": {
            "tool_policy": deepcopy(version.pricing_policy or {}),
            "quote_snapshot": deepcopy(quote.pricing_snapshot or {}),
            "price_breakdown": deepcopy(quote.price_breakdown or {}),
            "estimated_credits": int(quote.estimated_credits or 0),
        },
        "safety_policy": {
            "declared": deepcopy(capabilities.get("safety_policy") or {}),
            "tool_capabilities": capabilities,
            "node_overrides": node_safety,
        },
        "warnings": deepcopy(quote.warnings or []),
        "retry_policy": {
            "default_lease_seconds": DEFAULT_NODE_LEASE_SECONDS,
            "nodes": [
                {"key": node["key"], "max_attempts": node["max_attempts"]}
                for node in nodes
            ],
        },
        "failure_policy": {
            "terminal_status": "failed",
            "cancel_unstarted_nodes": True,
            "refund_unsettled_reservation": True,
        },
        "compensation_policy": {
            "execution_order": "reverse_topological",
            "trigger_on": ["failure", "cancellation"],
            "nodes": [
                {
                    "key": node["key"],
                    "side_effect": node["side_effect"],
                    "compensation": deepcopy(node["compensation"]),
                }
                for node in nodes
                if node["side_effect"] or node["compensation"] is not None
            ],
        },
    }
    digest = hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()
    return snapshot, digest


def _existing_idempotent_run(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> WorkflowRun | None:
    existing = db.scalar(
        select(ToolRun).where(
            ToolRun.user_id == user_id,
            ToolRun.client_request_id == body.client_request_id,
        )
    )
    if existing is None:
        return None
    if body.quote_id is None or int(existing.quote_id or 0) != int(body.quote_id):
        raise WorkflowConflict("client_request_id 已绑定其他工作流报价")
    tool = db.get(ToolDefinition, existing.tool_definition_id)
    version = db.get(ToolVersion, existing.tool_version_id)
    expected = _fingerprint(
        version_id=int(existing.tool_version_id),
        payload=body.input,
        project_id=body.project_id,
    )
    metadata = (
        resolved_tool_metadata_snapshot(tool, version.metadata_snapshot)
        if tool is not None and version is not None
        else None
    )
    if (
        metadata is None
        or metadata["slug"] != body.tool_slug
        or existing.request_fingerprint != expected
    ):
        raise WorkflowConflict("client_request_id 已用于不同的工具输入")
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.tool_run_id == existing.id))
    if run is None:
        raise WorkflowConflict("幂等工具运行缺少工作流实例")
    return run


def create_run(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> tuple[WorkflowRun, bool]:
    if body.quote_id is None:
        raise WorkflowServiceError(422, "执行工作流前必须先确认有效报价")
    project = None
    if body.project_id is not None:
        from . import project_collection

        try:
            project = project_collection.require_owned_project(db, user_id, int(body.project_id))
        except project_collection.ProjectNotFound as exc:
            raise WorkflowNotFound(str(exc)) from exc
    quote = generation_quotes.lock_execution_quote(
        db,
        quote_id=int(body.quote_id),
        user_id=user_id,
        kind="workflow",
        allow_consumed=True,
    )
    tool, version, spec = generation_quotes.validate_workflow_quote(
        db,
        quote,
        body=body,
    )
    existing_run = _existing_idempotent_run(db, user_id=user_id, body=body)
    if existing_run is not None:
        if (
            quote.status != "consumed"
            or quote.consumed_ref_type != "tool_run"
            or int(quote.consumed_ref_id or 0) != int(existing_run.tool_run_id)
        ):
            db.rollback()
            raise WorkflowConflict("工作流报价与幂等运行绑定不一致")
        if project is not None:
            project_collection.attach_task(
                db,
                project=project,
                task_kind="workflow",
                task_id=int(existing_run.id),
            )
        db.commit()
        db.refresh(existing_run)
        return existing_run, False
    if quote.status == "consumed":
        db.rollback()
        raise WorkflowConflict("工作流报价已被其他运行使用")
    fingerprint = _fingerprint(
        version_id=int(version.id),
        payload=body.input,
        project_id=body.project_id,
    )
    frozen = max(0, int(quote.estimated_credits or 0))
    compiled_snapshot, compiled_snapshot_hash = _compiled_workflow_snapshot(
        tool=tool,
        version=version,
        spec=spec,
        quote=quote,
        body=body,
        request_fingerprint=fingerprint,
    )

    tool_run = ToolRun(
        user_id=user_id,
        tool_definition_id=tool.id,
        tool_version_id=version.id,
        quote_id=int(quote.id),
        client_request_id=body.client_request_id,
        request_fingerprint=fingerprint,
        input_snapshot=deepcopy(body.input),
        pricing_snapshot=deepcopy(quote.pricing_snapshot or {}),
        cost_frozen=frozen,
        cost_settled=0,
        status="queued",
    )
    db.add(tool_run)
    try:
        db.flush()
        run = WorkflowRun(
            tool_run_id=tool_run.id,
            user_id=user_id,
            workflow_schema_version=spec.type,
            workflow_snapshot=spec.model_dump(mode="json"),
            compiled_snapshot=compiled_snapshot,
            compiled_snapshot_hash=compiled_snapshot_hash,
            compiled_at=utcnow(),
            status="queued",
        )
        db.add(run)
        db.flush()
        if project is not None:
            project_collection.attach_task(
                db,
                project=project,
                task_kind="workflow",
                task_id=int(run.id),
            )
        by_key = {node.key: node for node in spec.nodes}
        for index, key in enumerate(spec.topological_keys()):
            node = by_key[key]
            db.add(
                ToolNodeRun(
                    workflow_run_id=run.id,
                    node_key=node.key,
                    node_type=node.type,
                    topological_index=index,
                    depends_on=list(node.depends_on),
                    config_snapshot=dict(node.config),
                    compensation_snapshot=(
                        node.compensation.model_dump(mode="json")
                        if node.compensation is not None
                        else None
                    ),
                    max_attempts=node.max_attempts,
                    status="queued",
                )
            )
        # The first broker publication intent is committed with the run. If the
        # API process exits before publish, the workflow reconciler can still
        # deliver this deterministic outbox row.
        from .workflow_dispatch import ensure_orchestrator_dispatch

        ensure_orchestrator_dispatch(db, run=run)
        credits.freeze(
            db,
            user_id,
            frozen,
            int(tool_run.id),
            biz_type="tool_run",
            commit=False,
        )
        generation_quotes.consume_execution_quote(
            quote,
            ref_type="tool_run",
            ref_id=int(tool_run.id),
        )
        db.commit()
        db.refresh(run)
        return run, True
    except credits.InsufficientCredits as exc:
        db.rollback()
        raise WorkflowServiceError(402, str(exc)) from exc
    except IntegrityError:
        db.rollback()
        concurrent_quote = generation_quotes.lock_execution_quote(
            db,
            quote_id=int(body.quote_id),
            user_id=user_id,
            kind="workflow",
            allow_consumed=True,
        )
        generation_quotes.validate_workflow_quote(
            db,
            concurrent_quote,
            body=body,
        )
        existing_run = _existing_idempotent_run(db, user_id=user_id, body=body)
        if existing_run is None:
            db.rollback()
            raise WorkflowConflict("工具运行发生并发创建冲突")
        if (
            concurrent_quote.status != "consumed"
            or concurrent_quote.consumed_ref_type != "tool_run"
            or int(concurrent_quote.consumed_ref_id or 0) != int(existing_run.tool_run_id)
        ):
            db.rollback()
            raise WorkflowConflict("工作流报价与并发运行绑定不一致")
        if body.project_id is not None:
            from . import project_collection

            try:
                concurrent_project = project_collection.require_owned_project(
                    db,
                    user_id,
                    int(body.project_id),
                )
            except project_collection.ProjectNotFound as exc:
                db.rollback()
                raise WorkflowNotFound(str(exc)) from exc
            project_collection.attach_task(
                db,
                project=concurrent_project,
                task_kind="workflow",
                task_id=int(existing_run.id),
            )
        db.commit()
        db.refresh(existing_run)
        return existing_run, False


def _owned_run(db: Session, *, run_id: int, user_id: int, lock: bool = False) -> WorkflowRun:
    query = select(WorkflowRun).where(
        WorkflowRun.id == run_id,
        WorkflowRun.user_id == user_id,
    )
    if lock:
        query = query.with_for_update()
    run = db.scalar(query)
    if run is None:
        raise WorkflowNotFound()
    return run


def _run_nodes(db: Session, run_id: int) -> list[ToolNodeRun]:
    return list(
        db.scalars(
            select(ToolNodeRun)
            .where(ToolNodeRun.workflow_run_id == run_id)
            .order_by(ToolNodeRun.topological_index, ToolNodeRun.id)
        )
    )


def _node_attempts(db: Session, node_id: int) -> list[ToolNodeAttempt]:
    return list(
        db.scalars(
            select(ToolNodeAttempt)
            .where(ToolNodeAttempt.node_run_id == node_id)
            .order_by(ToolNodeAttempt.created_at, ToolNodeAttempt.id)
        )
    )


def serialize_attempt(row: ToolNodeAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "attempt_number": row.attempt_number,
        "status": row.status,
        "input": row.input_snapshot,
        "output": row.output,
        "error_code": row.error_code,
        "error": row.error,
        "external_kind": row.external_kind,
        "external_id": row.external_id,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def serialize_node(db: Session, row: ToolNodeRun, *, attempts: bool = True) -> dict[str, Any]:
    return {
        "id": row.id,
        "key": row.node_key,
        "type": row.node_type,
        "depends_on": list(row.depends_on or []),
        "topological_index": row.topological_index,
        "status": row.status,
        "input": row.input_snapshot,
        "output": row.output,
        "error_code": row.error_code,
        "error": row.error,
        "external_kind": row.external_kind,
        "external_id": row.external_id,
        "attempt_count": row.attempt_count,
        "max_attempts": row.max_attempts,
        "compensation_status": row.compensation_status,
        "compensation_error": row.compensation_error,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        **(
            {"attempts": [serialize_attempt(item) for item in _node_attempts(db, row.id)]}
            if attempts
            else {}
        ),
    }


def serialize_run(db: Session, run: WorkflowRun, *, attempts: bool = True) -> dict[str, Any]:
    from . import project_collection

    tool_run = db.get(ToolRun, run.tool_run_id)
    nodes = _run_nodes(db, run.id)
    frozen = max(0, int(tool_run.cost_frozen or 0)) if tool_run else 0
    settled = max(0, int(tool_run.cost_settled or 0)) if tool_run else 0
    released = max(0, frozen - settled)
    terminal = run.status in TERMINAL_RUN_STATUSES
    return {
        "id": run.id,
        "tool_run_id": run.tool_run_id,
        "tool_definition_id": tool_run.tool_definition_id if tool_run else None,
        "tool_version_id": tool_run.tool_version_id if tool_run else None,
        "quote_id": tool_run.quote_id if tool_run else None,
        "client_request_id": tool_run.client_request_id if tool_run else None,
        "project_id": project_collection.primary_project_id_for_task(
            db,
            user_id=int(run.user_id),
            task_kind="workflow",
            task_id=int(run.id),
        ),
        "pricing_snapshot": deepcopy(tool_run.pricing_snapshot or {}) if tool_run else {},
        "cost_frozen": frozen,
        "cost_settled": settled,
        "cost_refunded": released if terminal else 0,
        "cost_outstanding": 0 if terminal else released,
        "status": run.status,
        "current_node_key": run.current_node_key,
        "input": tool_run.input_snapshot if tool_run else None,
        "output": tool_run.output if tool_run else None,
        "error_code": run.error_code,
        "error": run.error,
        "cancel_requested": run.cancel_requested,
        "workflow_schema_version": run.workflow_schema_version,
        "compiled_snapshot_hash": run.compiled_snapshot_hash,
        "compiled_at": run.compiled_at,
        "nodes": [serialize_node(db, node, attempts=attempts) for node in nodes],
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def get_owned_run(db: Session, *, run_id: int, user_id: int) -> dict[str, Any]:
    return serialize_run(db, _owned_run(db, run_id=run_id, user_id=user_id))


def list_owned_runs(db: Session, *, user_id: int, limit: int = 50) -> dict[str, Any]:
    total = int(
        db.scalar(select(func.count(WorkflowRun.id)).where(WorkflowRun.user_id == user_id)) or 0
    )
    rows = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.user_id == user_id)
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .limit(limit)
        )
    )
    return {"items": [serialize_run(db, row, attempts=False) for row in rows], "total": total}


def _set_run_status(
    db: Session,
    run: WorkflowRun,
    status: str,
    *,
    error_code: str | None = None,
    error: str | None = None,
) -> None:
    now = utcnow()
    tool_run = db.get(ToolRun, run.tool_run_id)
    run.status = status
    run.error_code = error_code
    run.error = error
    run.revision = int(run.revision or 0) + 1
    run.updated_at = now
    if status == "running" and run.started_at is None:
        run.started_at = now
    if status in TERMINAL_RUN_STATUSES:
        run.current_node_key = None
        run.finished_at = now
    if tool_run is not None:
        tool_run.status = status
        tool_run.error_code = error_code
        tool_run.error = error
        tool_run.cancel_requested = bool(run.cancel_requested)
        tool_run.updated_at = now
        if status == "running" and tool_run.started_at is None:
            tool_run.started_at = now
        if status in TERMINAL_RUN_STATUSES:
            tool_run.finished_at = now


def _remaining_tool_reservation(tool_run: ToolRun | None) -> int:
    if tool_run is None:
        return 0
    return max(
        0,
        int(tool_run.cost_frozen or 0) - int(tool_run.cost_settled or 0),
    )


def _settle_tool_run(db: Session, run: WorkflowRun) -> None:
    tool_run = db.get(ToolRun, run.tool_run_id)
    reserved = _remaining_tool_reservation(tool_run)
    if tool_run is None or reserved == 0:
        return
    credits.settle(
        db,
        int(tool_run.user_id),
        reserved=reserved,
        real_cost=reserved,
        biz_ref=int(tool_run.id),
        biz_type="tool_run",
        commit=False,
    )
    tool_run.cost_settled = int(tool_run.cost_settled or 0) + reserved


def _refund_tool_run(db: Session, run: WorkflowRun) -> None:
    tool_run = db.get(ToolRun, run.tool_run_id)
    reserved = _remaining_tool_reservation(tool_run)
    if tool_run is None or reserved == 0:
        return
    credits.refund(
        db,
        int(tool_run.user_id),
        reserved,
        int(tool_run.id),
        biz_type="tool_run",
        commit=False,
    )


def _execution_input(db: Session, run: WorkflowRun, node: ToolNodeRun) -> dict[str, Any]:
    tool_run = db.get(ToolRun, run.tool_run_id)
    dependency_outputs: dict[str, Any] = {}
    if node.depends_on:
        dependencies = list(
            db.scalars(
                select(ToolNodeRun).where(
                    ToolNodeRun.workflow_run_id == run.id,
                    ToolNodeRun.node_key.in_(list(node.depends_on)),
                )
            )
        )
        dependency_outputs = {item.node_key: item.output for item in dependencies}
    return {
        "workflow_input": dict(tool_run.input_snapshot or {}) if tool_run else {},
        "dependencies": dependency_outputs,
    }


def _claim_node(
    db: Session,
    run: WorkflowRun,
    node: ToolNodeRun,
) -> tuple[str, dict[str, Any], int | None] | None:
    if int(node.attempt_count or 0) >= int(node.max_attempts or 1):
        return None
    now = utcnow()
    token = str(uuid4())
    node_input = _execution_input(db, run, node)
    claimed = db.execute(
        update(ToolNodeRun)
        .where(
            ToolNodeRun.id == node.id,
            ToolNodeRun.status == "queued",
            ToolNodeRun.revision == int(node.revision or 0),
        )
        .values(
            status="running",
            input_snapshot=node_input,
            dispatch_token=token,
            attempt_count=int(node.attempt_count or 0) + 1,
            revision=int(node.revision or 0) + 1,
            started_at=now,
            finished_at=None,
            error_code=None,
            error=None,
            available_at=_lease_deadline(dict(node.config_snapshot or {}), now),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        return None
    attempt_number = int(node.attempt_count or 0) + 1
    db.add(
        ToolNodeAttempt(
            node_run_id=node.id,
            kind="execution",
            attempt_number=attempt_number,
            dispatch_token=token,
            status="running",
            input_snapshot=node_input,
            started_at=now,
        )
    )
    run.current_node_key = node.node_key
    _set_run_status(db, run, "running")
    dispatch_id = None
    if node.node_type != "manual_review":
        from .workflow_dispatch import create_node_dispatch

        dispatch = create_node_dispatch(
            db,
            run_id=int(run.id),
            node_id=int(node.id),
            attempt_number=attempt_number,
            dispatch_token=token,
        )
        dispatch_id = int(dispatch.id)
    db.commit()
    return token, node_input, dispatch_id


def _attempt_by_token(db: Session, token: str) -> ToolNodeAttempt:
    attempt = db.scalar(
        select(ToolNodeAttempt).where(ToolNodeAttempt.dispatch_token == token).with_for_update()
    )
    if attempt is None:
        raise WorkflowConflict("节点尝试记录不存在")
    return attempt


def _apply_execution_result(
    db: Session,
    *,
    node_id: int,
    token: str,
    result: NodeExecutionResult,
) -> None:
    workflow_run_id = db.scalar(
        select(ToolNodeRun.workflow_run_id).where(ToolNodeRun.id == node_id)
    )
    if workflow_run_id is None:
        db.rollback()
        return
    run = db.scalar(
        select(WorkflowRun).where(WorkflowRun.id == workflow_run_id).with_for_update()
    )
    if run is None:
        db.rollback()
        return
    node = db.scalar(
        select(ToolNodeRun).where(ToolNodeRun.id == node_id).with_for_update()
    )
    if node is None or node.status != "running" or node.dispatch_token != token:
        db.rollback()
        return
    attempt = _attempt_by_token(db, token)
    now = utcnow()
    node.revision = int(node.revision or 0) + 1
    node.updated_at = now

    if result.status == "succeeded":
        node.status = "succeeded"
        node.output = dict(result.output or {})
        node.error_code = None
        node.error = None
        node.finished_at = now
        node.dispatch_token = None
        node.available_at = None
        attempt.status = "succeeded"
        attempt.output = node.output
        attempt.finished_at = now
    elif result.status == "waiting_external":
        if not result.external_kind or not result.external_id:
            result = NodeExecutionResult.failed(
                "外部等待结果必须包含 external_kind 和 external_id",
                code="INVALID_EXTERNAL_BINDING",
            )
            _apply_failed_result(node, attempt, result, now)
        else:
            node.status = "waiting_external"
            node.output = dict(result.output or {})
            node.external_kind = result.external_kind
            node.external_id = result.external_id
            node.available_at = None
            attempt.status = "waiting_external"
            attempt.output = node.output
            attempt.external_kind = result.external_kind
            attempt.external_id = result.external_id
    else:
        _apply_failed_result(node, attempt, result, now)
    if run.cancel_requested and node.status == "waiting_external":
        node.status = "canceled"
        node.finished_at = now
        node.dispatch_token = None
        node.available_at = None
        attempt.status = "canceled"
        attempt.finished_at = now
    db.commit()


def _apply_failed_result(
    node: ToolNodeRun,
    attempt: ToolNodeAttempt,
    result: NodeExecutionResult,
    now: datetime,
) -> None:
    node.status = "failed"
    node.output = dict(result.output or {})
    node.error_code = str(result.error_code or "NODE_FAILED")[:64]
    node.error = str(result.error or "节点执行失败")[:2000]
    node.finished_at = now
    node.dispatch_token = None
    node.available_at = None
    attempt.status = "failed"
    attempt.output = node.output
    attempt.error_code = node.error_code
    attempt.error = node.error
    attempt.finished_at = now


def _validated_handler_result(value: Any) -> NodeExecutionResult:
    if not isinstance(value, NodeExecutionResult):
        return NodeExecutionResult.failed(
            "节点处理器必须返回 NodeExecutionResult",
            code="INVALID_NODE_RESULT",
        )
    if value.status not in {"succeeded", "failed", "waiting_external"}:
        return NodeExecutionResult.failed(
            f"节点处理器返回了不支持的状态: {value.status}",
            code="INVALID_NODE_RESULT",
        )
    if not isinstance(value.output, dict):
        return NodeExecutionResult.failed(
            "节点处理器输出必须是 JSON 对象",
            code="INVALID_NODE_RESULT",
        )
    return value


def _validated_compensation_result(value: Any) -> NodeExecutionResult:
    if not isinstance(value, NodeExecutionResult):
        return NodeExecutionResult.failed(
            "补偿处理器必须返回 NodeExecutionResult",
            code="INVALID_COMPENSATION_RESULT",
        )
    if value.status not in {"succeeded", "failed"} or not isinstance(value.output, dict):
        return NodeExecutionResult.failed(
            "补偿处理器必须返回 succeeded/failed 和 JSON 对象输出",
            code="INVALID_COMPENSATION_RESULT",
        )
    return value


def _recover_expired_claims(
    db: Session,
    run: WorkflowRun,
    nodes: list[ToolNodeRun],
) -> bool:
    now = utcnow()
    changed = False
    for node in nodes:
        if node.status == "running" and _deadline_reached(node.available_at, now):
            attempt = (
                db.scalar(
                    select(ToolNodeAttempt)
                    .where(ToolNodeAttempt.dispatch_token == node.dispatch_token)
                    .with_for_update()
                )
                if node.dispatch_token
                else None
            )
            if attempt is not None and attempt.status == "running":
                attempt.finished_at = now
                if run.cancel_requested:
                    attempt.status = "canceled"
                else:
                    attempt.status = "failed"
                    attempt.error_code = "NODE_LEASE_EXPIRED"
                    attempt.error = "节点执行租约已过期，原 worker 结果将被忽略"
            node.dispatch_token = None
            node.available_at = None
            node.revision = int(node.revision or 0) + 1
            node.updated_at = now
            if run.cancel_requested:
                node.status = "canceled"
                node.error_code = None
                node.error = None
                node.finished_at = now
            elif int(node.attempt_count or 0) < int(node.max_attempts or 1):
                node.status = "queued"
                node.error_code = None
                node.error = None
                node.finished_at = None
            else:
                node.status = "failed"
                node.error_code = "NODE_LEASE_EXPIRED"
                node.error = "节点执行租约已过期，且已达到最大尝试次数"
                node.finished_at = now
            changed = True

        if node.compensation_status == "running" and _deadline_reached(node.available_at, now):
            attempt = (
                db.scalar(
                    select(ToolNodeAttempt)
                    .where(ToolNodeAttempt.dispatch_token == node.dispatch_token)
                    .with_for_update()
                )
                if node.dispatch_token
                else None
            )
            if attempt is not None and attempt.status == "running":
                attempt.status = "failed"
                attempt.error_code = "COMPENSATION_LEASE_EXPIRED"
                attempt.error = "补偿执行租约已过期，原 worker 结果将被忽略"
                attempt.finished_at = now
            node.compensation_status = "failed"
            node.compensation_error = "补偿执行租约已过期"
            node.compensation_finished_at = now
            node.dispatch_token = None
            node.available_at = None
            node.revision = int(node.revision or 0) + 1
            node.updated_at = now
            changed = True

    if not changed:
        return False
    run.current_node_key = None
    run.revision = int(run.revision or 0) + 1
    run.updated_at = now
    db.commit()
    return True


def _terminalize_or_compensate(
    db: Session,
    run: WorkflowRun,
    *,
    final_status: str,
    error_code: str | None,
    error: str | None,
) -> None:
    now = utcnow()
    nodes = _run_nodes(db, run.id)
    has_running = any(node.status == "running" for node in nodes)
    for node in nodes:
        if node.status in {"queued", "waiting_review", "waiting_external"}:
            was_external = node.status == "waiting_external"
            token = node.dispatch_token
            node.status = "canceled"
            node.finished_at = now
            node.dispatch_token = None
            node.available_at = None
            node.revision = int(node.revision or 0) + 1
            node.updated_at = now
            if token:
                attempt = db.scalar(
                    select(ToolNodeAttempt)
                    .where(ToolNodeAttempt.dispatch_token == token)
                    .with_for_update()
                )
                if attempt is not None and attempt.status in {
                    "running",
                    "waiting_review",
                    "waiting_external",
                }:
                    attempt.status = "canceled"
                    attempt.finished_at = now
            if was_external and not node.compensation_snapshot and node.external_kind in {
                "parse_record",
                "reverse_operation",
                "generation_task",
            }:
                node.compensation_snapshot = {
                    "type": "cancel_external_task",
                    "config": {},
                }
            if was_external and node.compensation_snapshot:
                node.compensation_status = "pending"
        if node.status == "succeeded" and node.compensation_snapshot:
            node.compensation_status = "pending"
    if has_running:
        _set_run_status(db, run, "running", error_code=error_code, error=error)
        db.commit()
        return
    pending = any(node.compensation_status == "pending" for node in nodes)
    if pending:
        _set_run_status(db, run, "compensating", error_code=error_code, error=error)
    else:
        _refund_tool_run(db, run)
        _set_run_status(db, run, final_status, error_code=error_code, error=error)
    db.commit()


def _finish_success(db: Session, run: WorkflowRun, nodes: list[ToolNodeRun]) -> None:
    spec = WorkflowSpec.model_validate(run.workflow_snapshot)
    outputs = {node.node_key: node.output for node in nodes}
    output = outputs.get(spec.output_node) if spec.output_node else outputs
    if output is None:
        output = {}
    tool_run = db.get(ToolRun, run.tool_run_id)
    if tool_run is not None:
        tool_run.output = output if isinstance(output, dict) else {"value": output}
    _settle_tool_run(db, run)
    run.current_node_key = None
    _set_run_status(db, run, "succeeded")
    from . import project_collection

    project_id = project_collection.primary_project_id_for_task(
        db,
        user_id=int(run.user_id),
        task_kind="workflow",
        task_id=int(run.id),
    )
    if project_id is not None:
        try:
            project = project_collection.require_owned_project(db, int(run.user_id), int(project_id))
        except project_collection.ProjectNotFound:
            project = None
        if project is not None:
            project_collection.attach_workflow_outputs(
                db,
                project=project,
                workflow_run_id=int(run.id),
            )
    db.commit()


def _compensation_context(
    db: Session,
    run: WorkflowRun,
    node: ToolNodeRun,
    attempt_number: int,
) -> NodeExecutionContext:
    node_input = dict(node.input_snapshot or {})
    return NodeExecutionContext(
        workflow_run_id=run.id,
        tool_run_id=run.tool_run_id,
        node_run_id=node.id,
        node_key=node.node_key,
        node_type=node.node_type,
        attempt_number=attempt_number,
        workflow_input=dict(node_input.get("workflow_input") or {}),
        dependency_outputs=dict(node_input.get("dependencies") or {}),
        node_input=node_input,
        config=dict((node.compensation_snapshot or {}).get("config") or {}),
        previous_output=dict(node.output or {}),
        external_kind=node.external_kind,
        external_id=node.external_id,
    )


def _run_one_compensation(db: Session, run: WorkflowRun, node: ToolNodeRun) -> None:
    now = utcnow()
    token = str(uuid4())
    attempt_number = 1 + int(
        db.scalar(
            select(func.count(ToolNodeAttempt.id)).where(
                ToolNodeAttempt.node_run_id == node.id,
                ToolNodeAttempt.kind == "compensation",
            )
        )
        or 0
    )
    claimed = db.execute(
        update(ToolNodeRun)
        .where(
            ToolNodeRun.id == node.id,
            ToolNodeRun.compensation_status == "pending",
            ToolNodeRun.revision == int(node.revision or 0),
        )
        .values(
            compensation_status="running",
            compensation_started_at=now,
            dispatch_token=token,
            available_at=_lease_deadline(
                dict((node.compensation_snapshot or {}).get("config") or {}),
                now,
            ),
            revision=int(node.revision or 0) + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        return
    context = _compensation_context(db, run, node, attempt_number)
    db.add(
        ToolNodeAttempt(
            node_run_id=node.id,
            kind="compensation",
            attempt_number=attempt_number,
            dispatch_token=token,
            status="running",
            input_snapshot={"config": context.config, "previous_output": context.previous_output},
            started_at=now,
        )
    )
    db.commit()
    compensation_type = str((node.compensation_snapshot or {}).get("type") or "")
    handler = _COMPENSATION_HANDLERS.get(compensation_type)
    if handler is None:
        result = NodeExecutionResult.failed(
            f"未注册补偿处理器: {compensation_type or 'unknown'}",
            code="COMPENSATION_HANDLER_NOT_REGISTERED",
        )
    else:
        try:
            result = handler(context)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - handler failures are persisted
            result = NodeExecutionResult.failed(str(exc), code="COMPENSATION_HANDLER_ERROR")
    result = _validated_compensation_result(result)
    locked_run = db.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run.id).with_for_update()
    )
    if locked_run is None:
        db.rollback()
        return
    current = db.scalar(select(ToolNodeRun).where(ToolNodeRun.id == node.id).with_for_update())
    if (
        current is None
        or current.compensation_status != "running"
        or current.dispatch_token != token
    ):
        db.rollback()
        return
    attempt = _attempt_by_token(db, token)
    if attempt.status != "running":
        db.rollback()
        return
    finished = utcnow()
    current.revision = int(current.revision or 0) + 1
    current.compensation_finished_at = finished
    current.dispatch_token = None
    current.available_at = None
    current.updated_at = finished
    if result.status == "succeeded":
        current.compensation_status = "succeeded"
        current.compensation_error = None
        attempt.status = "succeeded"
        attempt.output = dict(result.output or {})
    else:
        current.compensation_status = "failed"
        current.compensation_error = str(result.error or "补偿失败")[:2000]
        attempt.status = "failed"
        attempt.error_code = str(result.error_code or "COMPENSATION_FAILED")[:64]
        attempt.error = current.compensation_error
    attempt.finished_at = finished
    db.commit()


def _complete_compensation(db: Session, run: WorkflowRun) -> None:
    nodes = _run_nodes(db, run.id)
    if any(node.compensation_status in {"pending", "running"} for node in nodes):
        return
    failed = [node for node in nodes if node.compensation_status == "failed"]
    final_status = "canceled" if run.cancel_requested else "failed"
    error_code = "COMPENSATION_FAILED" if failed else run.error_code
    error = (
        "；".join(f"{node.node_key}: {node.compensation_error}" for node in failed)[:2000]
        if failed
        else run.error
    )
    _refund_tool_run(db, run)
    _set_run_status(db, run, final_status, error_code=error_code, error=error)
    db.commit()


def execute_claimed_node(run_id: int, node_id: int, dispatch_token: str) -> bool:
    """Execute one already-claimed node without holding an orchestration lock."""
    read_db = SessionLocal()
    try:
        run = read_db.get(WorkflowRun, int(run_id))
        node = read_db.get(ToolNodeRun, int(node_id))
        if (
            run is None
            or node is None
            or int(node.workflow_run_id) != int(run_id)
            or node.status != "running"
            or node.dispatch_token != dispatch_token
        ):
            read_db.rollback()
            return False
        node_input = dict(node.input_snapshot or {})
        context = NodeExecutionContext(
            workflow_run_id=int(run.id),
            tool_run_id=int(run.tool_run_id),
            node_run_id=int(node.id),
            node_key=node.node_key,
            node_type=node.node_type,
            attempt_number=int(node.attempt_count or 0),
            workflow_input=dict(node_input.get("workflow_input") or {}),
            dependency_outputs=dict(node_input.get("dependencies") or {}),
            node_input=node_input,
            config=dict(node.config_snapshot or {}),
            external_kind=node.external_kind,
            external_id=node.external_id,
        )
        handler = _NODE_HANDLERS.get(node.node_type)
    finally:
        read_db.close()

    if handler is None:
        result = NodeExecutionResult.failed(
            f"节点 {context.node_type} 未注册真实处理器",
            code="NODE_HANDLER_NOT_REGISTERED",
        )
    else:
        try:
            result = handler(context)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - handler failures are persisted
            result = NodeExecutionResult.failed(str(exc), code="NODE_HANDLER_ERROR")
    result = _validated_handler_result(result)
    result_db = SessionLocal()
    try:
        _apply_execution_result(
            result_db,
            node_id=int(node_id),
            token=dispatch_token,
            result=result,
        )
    finally:
        result_db.close()
    return True


def run_workflow(run_id: int) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        for _ in range(256):
            db.expire_all()
            run = db.scalar(
                select(WorkflowRun).where(WorkflowRun.id == int(run_id)).with_for_update()
            )
            if run is None:
                return None
            if run.status in TERMINAL_RUN_STATUSES:
                db.rollback()
                return serialize_run(db, run)
            nodes = _run_nodes(db, run.id)
            if _recover_expired_claims(db, run, nodes):
                continue
            if run.status == "compensating":
                pending = next(
                    (
                        node
                        for node in sorted(nodes, key=lambda item: item.topological_index, reverse=True)
                        if node.compensation_status == "pending"
                    ),
                    None,
                )
                if pending is None:
                    running_compensation = next(
                        (node for node in nodes if node.compensation_status == "running"),
                        None,
                    )
                    if running_compensation is not None:
                        db.commit()
                        _schedule_lease_wakeup(run.id, running_compensation.available_at)
                        return serialize_run(db, run)
                    _complete_compensation(db, run)
                    continue
                _run_one_compensation(db, run, pending)
                continue
            if run.cancel_requested:
                _terminalize_or_compensate(
                    db,
                    run,
                    final_status="canceled",
                    error_code="CANCELED",
                    error=None,
                )
                continue
            failed = next((node for node in nodes if node.status == "failed"), None)
            if failed is not None:
                _terminalize_or_compensate(
                    db,
                    run,
                    final_status="failed",
                    error_code=failed.error_code or "NODE_FAILED",
                    error=failed.error or f"节点 {failed.node_key} 执行失败",
                )
                continue
            if nodes and all(node.status == "succeeded" for node in nodes):
                _finish_success(db, run, nodes)
                continue
            waiting_review = next(
                (node for node in nodes if node.status == "waiting_review"), None
            )
            if waiting_review is not None:
                run.current_node_key = waiting_review.node_key
                _set_run_status(db, run, "waiting_review")
                db.commit()
                return serialize_run(db, run)
            running_node = next((node for node in nodes if node.status == "running"), None)
            if running_node is not None:
                _set_run_status(db, run, "running")
                db.commit()
                _schedule_lease_wakeup(run.id, running_node.available_at)
                return serialize_run(db, run)
            if any(node.status == "waiting_external" for node in nodes):
                _set_run_status(db, run, "running")
                db.commit()
                return serialize_run(db, run)
            status_by_key = {node.node_key: node.status for node in nodes}
            ready = next(
                (
                    node
                    for node in nodes
                    if node.status == "queued"
                    and all(status_by_key.get(dep) == "succeeded" for dep in node.depends_on or [])
                ),
                None,
            )
            if ready is None:
                _terminalize_or_compensate(
                    db,
                    run,
                    final_status="failed",
                    error_code="WORKFLOW_DEADLOCK",
                    error="工作流没有可执行节点，请检查依赖状态",
                )
                continue
            claimed = _claim_node(db, run, ready)
            if claimed is None:
                continue
            token, _node_input, dispatch_id = claimed
            db.expire_all()
            current = db.get(ToolNodeRun, ready.id)
            if current.node_type == "manual_review":
                attempt = _attempt_by_token(db, token)
                now = utcnow()
                current.status = "waiting_review"
                current.revision = int(current.revision or 0) + 1
                current.available_at = None
                attempt.status = "waiting_review"
                current.updated_at = now
                db.commit()
                continue
            if dispatch_id is None:
                _apply_execution_result(
                    db,
                    node_id=current.id,
                    token=token,
                    result=NodeExecutionResult.failed(
                        "节点调度意图缺失",
                        code="NODE_DISPATCH_MISSING",
                    ),
                )
                continue
            from .workflow_dispatch import publish_dispatch

            publish_dispatch(db, dispatch_id)
            db.expire_all()
            refreshed = db.get(WorkflowRun, int(run_id))
            refreshed_node = db.get(ToolNodeRun, int(current.id))
            if refreshed is None:
                return None
            if refreshed_node is not None and refreshed_node.status == "running":
                _schedule_lease_wakeup(refreshed.id, refreshed_node.available_at)
            return serialize_run(db, refreshed)
        raise RuntimeError("工作流单次推进超过安全步数")
    finally:
        db.close()


def fail_active_node(run_id: int, *, code: str, error: str) -> bool:
    """Persist a worker timeout instead of leaving a claimed node running forever."""
    db = SessionLocal()
    handled = False
    try:
        run = db.scalar(
            select(WorkflowRun).where(WorkflowRun.id == int(run_id)).with_for_update()
        )
        if run is None:
            return False
        node = db.scalar(
            select(ToolNodeRun)
            .where(
                ToolNodeRun.workflow_run_id == int(run_id),
                ToolNodeRun.status == "running",
            )
            .order_by(ToolNodeRun.topological_index)
        )
        if node is not None and node.dispatch_token:
            _apply_execution_result(
                db,
                node_id=node.id,
                token=node.dispatch_token,
                result=NodeExecutionResult.failed(error, code=code),
            )
            handled = True
        else:
            node = db.scalar(
                select(ToolNodeRun)
                .where(
                    ToolNodeRun.workflow_run_id == int(run_id),
                    ToolNodeRun.compensation_status == "running",
                )
                .order_by(ToolNodeRun.topological_index.desc())
                .with_for_update()
            )
            if node is not None and node.dispatch_token:
                attempt = db.scalar(
                    select(ToolNodeAttempt)
                    .where(
                        ToolNodeAttempt.dispatch_token == node.dispatch_token,
                        ToolNodeAttempt.status == "running",
                    )
                    .with_for_update()
                )
                if attempt is not None:
                    now = utcnow()
                    node.compensation_status = "failed"
                    node.compensation_error = str(error)[:2000]
                    node.compensation_finished_at = now
                    node.dispatch_token = None
                    node.available_at = None
                    node.revision = int(node.revision or 0) + 1
                    node.updated_at = now
                    attempt.status = "failed"
                    attempt.error_code = str(code)[:64]
                    attempt.error = str(error)[:2000]
                    attempt.finished_at = now
                    db.commit()
                    handled = True
    finally:
        db.close()
    if handled:
        run_workflow(run_id)
    return handled


def request_cancel(db: Session, *, run_id: int, user_id: int) -> WorkflowRun:
    run = _owned_run(db, run_id=run_id, user_id=user_id, lock=True)
    if run.status in TERMINAL_RUN_STATUSES:
        db.rollback()
        return run
    run.cancel_requested = True
    tool_run = db.get(ToolRun, run.tool_run_id)
    if tool_run is not None:
        tool_run.cancel_requested = True
    db.commit()
    db.refresh(run)
    return run


def review_node(
    db: Session,
    *,
    run_id: int,
    user_id: int,
    node_key: str,
    approve: bool,
    output: dict[str, Any],
    note: str | None,
) -> WorkflowRun:
    run = _owned_run(db, run_id=run_id, user_id=user_id, lock=True)
    node = db.scalar(
        select(ToolNodeRun)
        .where(
            ToolNodeRun.workflow_run_id == run.id,
            ToolNodeRun.node_key == node_key,
        )
        .with_for_update()
    )
    if node is None:
        raise WorkflowNotFound("工作流节点不存在")
    if node.node_type != "manual_review" or node.status != "waiting_review":
        raise WorkflowConflict("当前节点不在人工审核状态")
    if not node.dispatch_token:
        raise WorkflowConflict("人工审核节点缺少执行令牌")
    attempt = _attempt_by_token(db, node.dispatch_token)
    now = utcnow()
    node.revision = int(node.revision or 0) + 1
    node.finished_at = now
    node.dispatch_token = None
    node.available_at = None
    node.updated_at = now
    attempt.finished_at = now
    if approve:
        node.status = "succeeded"
        node.output = {**dict(output or {}), **({"review_note": note} if note else {})}
        attempt.status = "succeeded"
        attempt.output = node.output
        _set_run_status(db, run, "running")
    else:
        node.status = "failed"
        node.error_code = "REVIEW_REJECTED"
        node.error = str(note or "人工审核未通过")[:2000]
        attempt.status = "failed"
        attempt.error_code = node.error_code
        attempt.error = node.error
        _set_run_status(db, run, "running", error_code=node.error_code, error=node.error)
    db.commit()
    db.refresh(run)
    return run


def complete_external_node(
    db: Session,
    *,
    run_id: int,
    node_key: str,
    status: str,
    output: dict[str, Any],
    error_code: str | None,
    error: str | None,
    external_kind: str,
    external_id: str,
) -> WorkflowRun:
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
    if run is None:
        raise WorkflowNotFound()
    node = db.scalar(
        select(ToolNodeRun)
        .where(ToolNodeRun.workflow_run_id == run.id, ToolNodeRun.node_key == node_key)
        .with_for_update()
    )
    if node is None:
        raise WorkflowNotFound("工作流节点不存在")
    if node.status != "waiting_external" or not node.dispatch_token:
        raise WorkflowConflict("节点不在外部结果等待状态")
    if node.external_kind != external_kind or node.external_id != external_id:
        raise WorkflowConflict("外部任务身份与节点绑定不一致")
    attempt = _attempt_by_token(db, node.dispatch_token)
    now = utcnow()
    node.revision = int(node.revision or 0) + 1
    node.finished_at = now
    node.dispatch_token = None
    node.available_at = None
    node.updated_at = now
    attempt.finished_at = now
    if status == "succeeded":
        node.status = "succeeded"
        node.output = dict(output or {})
        node.error_code = None
        node.error = None
        attempt.status = "succeeded"
        attempt.output = node.output
    else:
        node.status = "failed"
        node.error_code = str(error_code or "EXTERNAL_TASK_FAILED")[:64]
        node.error = str(error or "外部任务失败")[:2000]
        attempt.status = "failed"
        attempt.error_code = node.error_code
        attempt.error = node.error
    _set_run_status(db, run, "running")
    db.commit()
    db.refresh(run)
    return run


def retry_node(
    db: Session,
    *,
    run_id: int,
    user_id: int,
    node_key: str,
) -> WorkflowRun:
    run = _owned_run(db, run_id=run_id, user_id=user_id, lock=True)
    node = db.scalar(
        select(ToolNodeRun)
        .where(ToolNodeRun.workflow_run_id == run.id, ToolNodeRun.node_key == node_key)
        .with_for_update()
    )
    if node is None:
        raise WorkflowNotFound("工作流节点不存在")
    if run.status != "failed" or node.status != "failed":
        raise WorkflowConflict("只能重试失败工作流中的失败节点")
    tool_run = db.get(ToolRun, run.tool_run_id)
    if tool_run is not None and int(tool_run.cost_frozen or 0) > 0:
        raise WorkflowConflict("付费工作流已完成退款，请重新报价并创建新运行")
    nodes = _run_nodes(db, run.id)
    if any(item.compensation_status != "none" for item in nodes):
        raise WorkflowConflict("工作流已进入补偿，必须创建新的工具运行")
    if int(node.attempt_count or 0) >= int(node.max_attempts or 1):
        raise WorkflowConflict("节点已达到最大重试次数")
    descendants = {node.node_key}
    changed = True
    while changed:
        changed = False
        for item in nodes:
            if item.node_key not in descendants and descendants.intersection(item.depends_on or []):
                descendants.add(item.node_key)
                changed = True
    for item in nodes:
        if item.node_key not in descendants:
            continue
        item.status = "queued"
        item.output = None
        item.error_code = None
        item.error = None
        item.external_kind = None
        item.external_id = None
        item.dispatch_token = None
        item.finished_at = None
        item.available_at = None
        item.revision = int(item.revision or 0) + 1
    run.cancel_requested = False
    run.finished_at = None
    run.current_node_key = node.node_key
    if tool_run is not None:
        tool_run.cancel_requested = False
        tool_run.finished_at = None
    _set_run_status(db, run, "queued")
    db.commit()
    db.refresh(run)
    return run


def admin_summary(db: Session) -> dict[str, Any]:
    run_counts = {
        status: int(count)
        for status, count in db.execute(
            select(WorkflowRun.status, func.count(WorkflowRun.id)).group_by(WorkflowRun.status)
        )
    }
    node_counts = {
        status: int(count)
        for status, count in db.execute(
            select(ToolNodeRun.status, func.count(ToolNodeRun.id)).group_by(ToolNodeRun.status)
        )
    }
    compensation_counts = {
        status: int(count)
        for status, count in db.execute(
            select(ToolNodeRun.compensation_status, func.count(ToolNodeRun.id)).group_by(
                ToolNodeRun.compensation_status
            )
        )
        if status != "none"
    }
    recent = list(
        db.scalars(select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(20))
    )
    return {
        "runs_by_status": run_counts,
        "nodes_by_status": node_counts,
        "compensations_by_status": compensation_counts,
        "recent": [serialize_run(db, row, attempts=False) for row in recent],
    }


def enqueue_run(
    run_id: int,
    *,
    countdown: int | None = None,
    dedupe_suffix: str | None = None,
) -> str:
    """Persist, then publish, one deterministic orchestrator dispatch."""
    from .workflow_dispatch import ensure_orchestrator_dispatch, publish_dispatch

    db = SessionLocal()
    try:
        run = db.get(WorkflowRun, int(run_id))
        if run is None:
            raise WorkflowNotFound()
        scheduled_for = (
            utcnow() + timedelta(seconds=max(0, int(countdown)))
            if countdown is not None
            else None
        )
        dispatch = ensure_orchestrator_dispatch(
            db,
            run=run,
            scheduled_for=scheduled_for,
            dedupe_suffix=dedupe_suffix,
        )
        dispatch_id = int(dispatch.id)
        task_id = str(dispatch.celery_task_id)
        db.commit()
        publish_dispatch(db, dispatch_id)
        return task_id
    finally:
        db.close()


def ensure_orphaned_run_dispatches(db: Session, *, limit: int = 200) -> dict[str, int]:
    """Backfill outbox intents for legacy or interrupted non-terminal runs."""
    from .workflow_dispatch import ensure_orchestrator_dispatch

    runs = list(
        db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.status.in_(("queued", "running", "compensating")),
            )
            .order_by(WorkflowRun.updated_at, WorkflowRun.id)
            .limit(max(1, min(int(limit), 500)))
        )
    )
    created = 0
    for run in runs:
        dispatch = ensure_orchestrator_dispatch(db, run=run)
        if dispatch.status == "pending" and int(dispatch.publish_attempts or 0) == 0:
            created += 1
    db.commit()
    return {"scanned": len(runs), "created": created}
