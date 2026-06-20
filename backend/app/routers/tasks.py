"""Task history + progress."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import GenTask, User
from ..redis_client import redis_client
from ..schemas import TaskOut
from ..services import credits, generation
from ..services.config_store import get_model_config
from ..services.generation import assert_model_snapshot_compatible, model_snapshot
from ..services.ssrf import SsrfError, assert_safe_user_asset_url
from ..services.task_output import build_task_out, build_task_outs
from .generate import (
    _assert_reference_access,
    _default_image_n,
    _estimate_cost,
    _estimate_cost_from_snapshot,
    _validate_params,
)

router = APIRouter(prefix="/api", tags=["tasks"])

_WS_TICKET_TTL_SECONDS = 60


def _page(limit: int, offset: int, cap: int) -> tuple[int, int]:
    return min(max(int(limit), 1), cap), max(int(offset), 0)


def _retry_params(task: GenTask) -> dict:
    """Drop worker-internal lifecycle metadata before a user retry."""
    return {
        k: v
        for k, v in dict(task.params or {}).items()
        if not str(k).startswith("_")
    }


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(db: Session = Depends(get_db), user: User = Depends(get_current_user),
               limit: int = 30, offset: int = 0):
    limit, offset = _page(limit, offset, 100)
    rows = list(
        db.execute(
            select(GenTask)
            .where(GenTask.user_id == user.id)
            .order_by(GenTask.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return build_task_outs(db, rows)


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    task = db.get(GenTask, task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    return build_task_out(db, task)


@router.post("/tasks/{task_id}/ws-ticket")
def create_task_ws_ticket(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.get(GenTask, task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    ticket = secrets.token_urlsafe(32)
    redis_client.setex(
        f"ws:task-ticket:{ticket}",
        _WS_TICKET_TTL_SECONDS,
        f"{user.id}:{task_id}:{user.token_version}",
    )
    return {"ticket": ticket, "expires_in": _WS_TICKET_TTL_SECONDS}


@router.post("/tasks/{task_id}/retry", response_model=TaskOut)
def retry_task(task_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    task = db.get(GenTask, task_id)
    if not task or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    if task.status != "failed":
        raise HTTPException(400, "只有失败的任务可以重试")

    model = get_model_config(db, task.category)
    if not model or not model.enabled:
        raise HTTPException(400, f"未配置可用的{task.category}模型")
    try:
        task_params = _validate_params(task.category, _retry_params(task))
        if task.category == "image" and task_params.get("n") is None:
            task_params["n"] = _default_image_n(db)
        n_images = int(task_params.get("n") or 1) if task.category == "image" else 1
    except (TypeError, ValueError):
        raise HTTPException(400, "任务参数非法,无法重试")
    try:
        assert_safe_user_asset_url(task.source_asset_url)
        for _url_key in ("reference_image_url", "first_frame_image"):
            assert_safe_user_asset_url(task_params.get(_url_key))
    except SsrfError as e:
        raise HTTPException(400, f"素材链接被安全策略拦截:{e}")
    _assert_reference_access(
        db,
        user.id,
        task.source_asset_url,
        task_params.get("reference_image_url"),
        task_params.get("first_frame_image"),
    )
    snapshot = (task.params or {}).get("_model_snapshot") or model_snapshot(model)
    try:
        assert_model_snapshot_compatible(model, snapshot)
    except generation.ModelSnapshotMismatchError as e:
        raise HTTPException(409, str(e)) from e
    task_params["_model_snapshot"] = snapshot
    if snapshot:
        cost = _estimate_cost_from_snapshot(snapshot, task.category, task.stage, n_images)
    else:
        cost = _estimate_cost(model, task.category, task.stage, n_images)
    if cost < 0:
        raise HTTPException(400, "任务成本配置非法,无法重试")

    # Atomic claim: only one concurrent retry may move failed -> queued. This is
    # what makes the re-freeze idempotent — two racing retries can't both freeze.
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status == "failed")
        # reset the staleness clock + any old video handles so the reaper won't
        # immediately fail the re-queued task and the video starts a fresh submit.
        .values(status="queued", error=None, finished_at=None, cost_frozen=cost,
                cost_settled=0, params=task_params,
                created_at=datetime.now(timezone.utc), phase=None,
                external_task_id=None, external_submitted_at=None)
    ).rowcount
    if (claimed or 0) != 1:
        db.rollback()
        raise HTTPException(409, "任务正在重试或状态已变更,请刷新后再试")

    if cost > 0:
        try:
            credits.freeze(db, user.id, cost, biz_ref=task_id, commit=False)
        except (credits.InsufficientCredits, ValueError) as e:
            db.rollback()  # reverts the claim too -> task stays failed, no freeze
            raise HTTPException(400, str(e))
    db.commit()
    db.refresh(task)

    try:
        from ..tasks import generate_image_task, generate_video_task

        if task.category == "image":
            generate_image_task.delay(task_id)
        else:
            generate_video_task.delay(task_id)
    except Exception as e:  # noqa: BLE001
        if cost > 0:
            credits.refund(db, user.id, cost, biz_ref=task_id, commit=False)
        task.status = "failed"
        task.error = f"入队失败:{e}"
        db.commit()
        raise HTTPException(503, "任务入队失败,已退回额度")

    return build_task_out(db, task)
