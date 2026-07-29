"""Workflow ownership queries and API serialization read models."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ToolNodeAttempt, ToolNodeRun, ToolRun, WorkflowRun
from .workflow_contracts import TERMINAL_RUN_STATUSES, WorkflowNotFound


def _owned_run(
    db: Session,
    *,
    run_id: int,
    user_id: int,
    lock: bool = False,
) -> WorkflowRun:
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


def serialize_node(
    db: Session,
    row: ToolNodeRun,
    *,
    attempts: bool = True,
) -> dict[str, Any]:
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


def serialize_run(
    db: Session,
    run: WorkflowRun,
    *,
    attempts: bool = True,
) -> dict[str, Any]:
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
