"""Workflow orchestration, lifecycle transitions, and operator actions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ToolNodeAttempt, ToolNodeRun, ToolRun, WorkflowRun
from ..workflow_schemas import WorkflowSpec
from .workflow_compensation import _complete_compensation, _run_one_compensation
from .workflow_contracts import (
    TERMINAL_RUN_STATUSES,
    NodeExecutionResult,
    WorkflowConflict,
    WorkflowNotFound,
    utcnow,
)
from .workflow_node_execution import (
    _apply_execution_result,
    _attempt_by_token,
    _claim_node,
    _recover_expired_claims,
    _refund_tool_run,
    _set_run_status,
    _settle_tool_run,
)
from .workflow_serialization import _owned_run, _run_nodes, serialize_run


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


def _finish_success(
    db: Session,
    run: WorkflowRun,
    nodes: list[ToolNodeRun],
) -> None:
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
            project = project_collection.require_owned_project(
                db,
                int(run.user_id),
                int(project_id),
            )
        except project_collection.ProjectNotFound:
            project = None
        if project is not None:
            project_collection.attach_workflow_outputs(
                db,
                project=project,
                workflow_run_id=int(run.id),
            )
    db.commit()


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
                        for node in sorted(
                            nodes,
                            key=lambda item: item.topological_index,
                            reverse=True,
                        )
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
                (node for node in nodes if node.status == "waiting_review"),
                None,
            )
            if waiting_review is not None:
                run.current_node_key = waiting_review.node_key
                _set_run_status(db, run, "waiting_review")
                db.commit()
                return serialize_run(db, run)
            running_node = next(
                (node for node in nodes if node.status == "running"),
                None,
            )
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
                    and all(
                        status_by_key.get(dependency) == "succeeded"
                        for dependency in node.depends_on or []
                    )
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
        _set_run_status(
            db,
            run,
            "running",
            error_code=node.error_code,
            error=node.error,
        )
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
    run = db.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
    )
    if run is None:
        raise WorkflowNotFound()
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
        .where(
            ToolNodeRun.workflow_run_id == run.id,
            ToolNodeRun.node_key == node_key,
        )
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
            if item.node_key not in descendants and descendants.intersection(
                item.depends_on or []
            ):
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


def ensure_orphaned_run_dispatches(
    db: Session,
    *,
    limit: int = 200,
) -> dict[str, int]:
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
