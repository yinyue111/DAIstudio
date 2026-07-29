"""Workflow run creation: compiled snapshot, idempotency and billing freeze.

Split out of ``tool_workflows`` verbatim; the execution engine still owns
claiming, compensation and lease recovery.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    ToolDefinition,
    ToolNodeRun,
    ToolRun,
    ToolVersion,
    WorkflowRun,
)
from ..workflow_schemas import ToolRunCreateIn, WorkflowSpec
from . import credits, generation_quotes
from .catalog_metadata import resolved_tool_metadata_snapshot
from .workflow_contracts import (
    DEFAULT_NODE_LEASE_SECONDS,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowServiceError,
    utcnow,
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


def _compiled_nodes(spec: WorkflowSpec) -> tuple[list[dict[str, Any]], list[str], list[str]]:
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
    return nodes, declared_order, topological_order


def _tool_snapshot(tool: ToolDefinition, version) -> dict[str, Any]:
    metadata = resolved_tool_metadata_snapshot(tool, version.metadata_snapshot)
    return {
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


def _quote_snapshot(quote) -> dict[str, Any]:
    return {
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


def _compiled_workflow_snapshot(
    *,
    tool: ToolDefinition,
    version,
    spec: WorkflowSpec,
    quote,
    body: ToolRunCreateIn,
    request_fingerprint: str,
) -> tuple[dict[str, Any], str]:
    nodes, declared_order, topological_order = _compiled_nodes(spec)
    tool_snapshot = _tool_snapshot(tool, version)
    quote_snapshot = _quote_snapshot(quote)
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


def _require_owned_project(db: Session, user_id: int, project_id: int | None):
    if project_id is None:
        return None
    from . import project_collection

    try:
        return project_collection.require_owned_project(db, user_id, int(project_id))
    except project_collection.ProjectNotFound as exc:
        raise WorkflowNotFound(str(exc)) from exc


def _attach_project(db: Session, project, run: WorkflowRun) -> None:
    if project is None:
        return
    from . import project_collection

    project_collection.attach_task(
        db,
        project=project,
        task_kind="workflow",
        task_id=int(run.id),
    )


def _finish_existing_run(
    db: Session,
    *,
    quote,
    run: WorkflowRun,
    project,
    conflict_message: str,
) -> tuple[WorkflowRun, bool]:
    if (
        quote.status != "consumed"
        or quote.consumed_ref_type != "tool_run"
        or int(quote.consumed_ref_id or 0) != int(run.tool_run_id)
    ):
        db.rollback()
        raise WorkflowConflict(conflict_message)
    _attach_project(db, project, run)
    db.commit()
    db.refresh(run)
    return run, False


def _new_tool_run(*, user_id: int, tool, version, quote, body, fingerprint: str, frozen: int):
    return ToolRun(
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


def _new_workflow_run(
    db: Session,
    *,
    user_id: int,
    tool_run: ToolRun,
    spec: WorkflowSpec,
    compiled_snapshot: dict[str, Any],
    compiled_snapshot_hash: str,
) -> WorkflowRun:
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
    return run


def _add_node_runs(db: Session, *, run: WorkflowRun, spec: WorkflowSpec) -> None:
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


def _recover_concurrent_run(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> tuple[WorkflowRun, bool]:
    concurrent_quote = generation_quotes.lock_execution_quote(
        db,
        quote_id=int(body.quote_id),
        user_id=user_id,
        kind="workflow",
        allow_consumed=True,
    )
    generation_quotes.validate_workflow_quote(db, concurrent_quote, body=body)
    existing_run = _existing_idempotent_run(db, user_id=user_id, body=body)
    if existing_run is None:
        db.rollback()
        raise WorkflowConflict("工具运行发生并发创建冲突")
    try:
        project = _require_owned_project(db, user_id, body.project_id)
    except WorkflowNotFound:
        db.rollback()
        raise
    return _finish_existing_run(
        db,
        quote=concurrent_quote,
        run=existing_run,
        project=project,
        conflict_message="工作流报价与并发运行绑定不一致",
    )


def _persist_new_run(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
    project,
    quote,
    tool,
    version,
    spec: WorkflowSpec,
) -> tuple[WorkflowRun, bool]:
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
    tool_run = _new_tool_run(
        user_id=user_id,
        tool=tool,
        version=version,
        quote=quote,
        body=body,
        fingerprint=fingerprint,
        frozen=frozen,
    )
    db.add(tool_run)
    try:
        db.flush()
        run = _new_workflow_run(
            db,
            user_id=user_id,
            tool_run=tool_run,
            spec=spec,
            compiled_snapshot=compiled_snapshot,
            compiled_snapshot_hash=compiled_snapshot_hash,
        )
        _attach_project(db, project, run)
        _add_node_runs(db, run=run, spec=spec)
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
        return _recover_concurrent_run(db, user_id=user_id, body=body)


def create_run(
    db: Session,
    *,
    user_id: int,
    body: ToolRunCreateIn,
) -> tuple[WorkflowRun, bool]:
    if body.quote_id is None:
        raise WorkflowServiceError(422, "执行工作流前必须先确认有效报价")
    project = _require_owned_project(db, user_id, body.project_id)
    quote = generation_quotes.lock_execution_quote(
        db,
        quote_id=int(body.quote_id),
        user_id=user_id,
        kind="workflow",
        allow_consumed=True,
    )
    tool, version, spec = generation_quotes.validate_workflow_quote(db, quote, body=body)
    existing_run = _existing_idempotent_run(db, user_id=user_id, body=body)
    if existing_run is not None:
        return _finish_existing_run(
            db,
            quote=quote,
            run=existing_run,
            project=project,
            conflict_message="工作流报价与幂等运行绑定不一致",
        )
    if quote.status == "consumed":
        db.rollback()
        raise WorkflowConflict("工作流报价已被其他运行使用")
    return _persist_new_run(
        db,
        user_id=user_id,
        body=body,
        project=project,
        quote=quote,
        tool=tool,
        version=version,
        spec=spec,
    )
