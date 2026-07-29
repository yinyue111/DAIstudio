"""Node claiming, execution result persistence, and lease recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ToolNodeAttempt, ToolNodeRun, ToolRun, WorkflowRun
from . import credits
from .workflow_contracts import (
    _NODE_HANDLERS,
    DEFAULT_NODE_LEASE_SECONDS,
    TERMINAL_RUN_STATUSES,
    NodeExecutionContext,
    NodeExecutionResult,
    WorkflowConflict,
    utcnow,
)


def _lease_deadline(config: dict[str, Any], now: datetime) -> datetime:
    try:
        configured_timeout = int(config.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        configured_timeout = 0
    lease_seconds = min(
        24 * 60 * 60,
        max(30, configured_timeout or DEFAULT_NODE_LEASE_SECONDS),
    )
    return now + timedelta(seconds=lease_seconds)


def _deadline_reached(deadline: datetime | None, now: datetime) -> bool:
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now


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


def _execution_input(
    db: Session,
    run: WorkflowRun,
    node: ToolNodeRun,
) -> dict[str, Any]:
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
        select(ToolNodeAttempt)
        .where(ToolNodeAttempt.dispatch_token == token)
        .with_for_update()
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

        if node.compensation_status == "running" and _deadline_reached(
            node.available_at,
            now,
        ):
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
