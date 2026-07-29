"""Reverse-order workflow compensation execution."""
from __future__ import annotations

from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import ToolNodeAttempt, ToolNodeRun, WorkflowRun
from .workflow_contracts import (
    _COMPENSATION_HANDLERS,
    NodeExecutionContext,
    NodeExecutionResult,
    utcnow,
)
from .workflow_node_execution import (
    _attempt_by_token,
    _lease_deadline,
    _refund_tool_run,
    _set_run_status,
    _validated_compensation_result,
)
from .workflow_serialization import _run_nodes


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


def _run_one_compensation(
    db: Session,
    run: WorkflowRun,
    node: ToolNodeRun,
) -> None:
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
            input_snapshot={
                "config": context.config,
                "previous_output": context.previous_output,
            },
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
            result = NodeExecutionResult.failed(
                str(exc),
                code="COMPENSATION_HANDLER_ERROR",
            )
    result = _validated_compensation_result(result)
    locked_run = db.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run.id).with_for_update()
    )
    if locked_run is None:
        db.rollback()
        return
    current = db.scalar(
        select(ToolNodeRun).where(ToolNodeRun.id == node.id).with_for_update()
    )
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
