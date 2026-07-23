"""Owner-gated tool workflow runs and administrator operations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import User
from ..services import tool_workflows
from ..workflow_schemas import (
    ToolRunCreateIn,
    WorkflowExternalCompletionIn,
    WorkflowRetryIn,
    WorkflowReviewIn,
)

router = APIRouter(tags=["tool-workflows"])


def _raise_http(exc: tool_workflows.WorkflowServiceError):
    raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/api/workflows/runs", status_code=status.HTTP_201_CREATED)
def create_workflow_run(
    body: ToolRunCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        run, created = tool_workflows.create_run(db, user_id=user.id, body=body)
        if created or run.status in {"queued", "running", "compensating"}:
            tool_workflows.enqueue_run(run.id)
        db.expire_all()
        return {
            **tool_workflows.get_owned_run(db, run_id=run.id, user_id=user.id),
            "idempotent_replay": not created,
        }
    except tool_workflows.WorkflowServiceError as exc:
        _raise_http(exc)

@router.get("/api/workflows/runs")
def list_workflow_runs(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return tool_workflows.list_owned_runs(db, user_id=user.id, limit=limit)


@router.get("/api/workflows/runs/{run_id}")
def get_workflow_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return tool_workflows.get_owned_run(db, run_id=run_id, user_id=user.id)
    except tool_workflows.WorkflowServiceError as exc:
        _raise_http(exc)


@router.post("/api/workflows/runs/{run_id}/resume")
def resume_workflow_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        current = tool_workflows.get_owned_run(db, run_id=run_id, user_id=user.id)
        if current["status"] in tool_workflows.TERMINAL_RUN_STATUSES:
            raise tool_workflows.WorkflowConflict("终态工作流不能恢复")
        if current["status"] == "waiting_review":
            raise tool_workflows.WorkflowConflict("请先完成人工审核节点")
        tool_workflows.enqueue_run(run_id)
        db.expire_all()
        return tool_workflows.get_owned_run(db, run_id=run_id, user_id=user.id)
    except tool_workflows.WorkflowServiceError as exc:
        _raise_http(exc)


@router.post("/api/workflows/runs/{run_id}/cancel")
def cancel_workflow_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        run = tool_workflows.request_cancel(db, run_id=run_id, user_id=user.id)
        if run.status not in tool_workflows.TERMINAL_RUN_STATUSES:
            tool_workflows.enqueue_run(run.id)
        db.expire_all()
        return tool_workflows.get_owned_run(db, run_id=run.id, user_id=user.id)
    except tool_workflows.WorkflowServiceError as exc:
        _raise_http(exc)


@router.post("/api/workflows/runs/{run_id}/nodes/{node_key}/review")
def review_workflow_node(
    run_id: int,
    node_key: str,
    body: WorkflowReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        run = tool_workflows.review_node(
            db,
            run_id=run_id,
            user_id=user.id,
            node_key=node_key,
            approve=body.decision == "approve",
            output=body.output,
            note=body.note,
        )
        tool_workflows.enqueue_run(run.id)
        db.expire_all()
        return tool_workflows.get_owned_run(db, run_id=run.id, user_id=user.id)
    except tool_workflows.WorkflowServiceError as exc:
        _raise_http(exc)


@router.post("/api/workflows/runs/{run_id}/nodes/{node_key}/retry")
def retry_workflow_node(
    run_id: int,
    node_key: str,
    _body: WorkflowRetryIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        run = tool_workflows.retry_node(
            db,
            run_id=run_id,
            user_id=user.id,
            node_key=node_key,
        )
        tool_workflows.enqueue_run(run.id)
        db.expire_all()
        return tool_workflows.get_owned_run(db, run_id=run.id, user_id=user.id)
    except tool_workflows.WorkflowServiceError as exc:
        _raise_http(exc)


@router.get("/api/admin/workflows/summary")
def workflow_admin_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    return tool_workflows.admin_summary(db)


@router.post("/api/admin/workflows/runs/{run_id}/nodes/{node_key}/complete")
def complete_external_workflow_node(
    run_id: int,
    node_key: str,
    body: WorkflowExternalCompletionIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        run = tool_workflows.complete_external_node(
            db,
            run_id=run_id,
            node_key=node_key,
            status=body.status,
            output=body.output,
            error_code=body.error_code,
            error=body.error,
            external_kind=body.external_kind,
            external_id=body.external_id,
        )
        tool_workflows.enqueue_run(run.id)
        db.expire_all()
        refreshed = db.get(type(run), run.id)
        return tool_workflows.serialize_run(db, refreshed)
    except tool_workflows.WorkflowServiceError as exc:
        _raise_http(exc)
