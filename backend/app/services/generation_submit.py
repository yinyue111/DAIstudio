"""Generation task submission side effects."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import GenTask
from ..schemas import TaskOut
from . import audit, credits
from .prompt_history import remember_prompt
from .task_output import build_task_out


def submit_generation_task(
    db: Session,
    *,
    user_id: int,
    source_asset_url: str | None,
    source_type: str | None,
    category: str,
    stage: str,
    prompt: dict,
    model_use: str,
    params: dict,
    client_request_id: str | None,
    cost: int,
    parent_task_id: int | None,
    ip: str | None,
    replay_conflicting_task: Callable[[], TaskOut | None],
) -> TaskOut:
    task = GenTask(
        user_id=user_id,
        source_asset_url=source_asset_url,
        source_type=source_type,
        category=category,
        stage=stage,
        prompt=prompt,
        model_use=model_use,
        params=params,
        client_request_id=client_request_id,
        status="queued",
        cost_frozen=cost,
        parent_task_id=parent_task_id,
    )
    db.add(task)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        replay = replay_conflicting_task()
        if replay is not None:
            return replay
        raise

    try:
        credits.freeze(db, user_id, cost, biz_ref=task.id, commit=False)
    except (credits.InsufficientCredits, ValueError) as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = replay_conflicting_task()
        if replay is not None:
            return replay
        raise
    db.refresh(task)

    _remember_generation_prompt(db, task, category=category, stage=stage)
    _enqueue_generation_task(db, task, category=category, user_id=user_id, cost=cost)
    audit.log(
        db,
        user_id=user_id,
        action="generate",
        biz_type="gen_task",
        biz_id=task.id,
        ip=ip,
        detail={"category": category, "stage": stage, "cost": cost},
    )
    return build_task_out(db, task)


def _remember_generation_prompt(
    db: Session,
    task: GenTask,
    *,
    category: str,
    stage: str,
) -> None:
    try:
        prompt = task.prompt or {}
        final_prompt_text = str(prompt.get("final_text") or prompt.get("instruction") or "").strip()
        if final_prompt_text:
            remember_prompt(
                db,
                user_id=task.user_id,
                prompt=final_prompt_text,
                title=f"{'视频' if category == 'video' else '图片'}生成提示词",
                category=category,
                source="generate",
                params={"task_id": task.id, "stage": stage},
                commit=True,
            )
    except Exception:
        db.rollback()


def _enqueue_generation_task(
    db: Session,
    task: GenTask,
    *,
    category: str,
    user_id: int,
    cost: int,
) -> None:
    try:
        from ..tasks import (
            enqueue_with_request_context,
            generate_image_task,
            generate_video_task,
        )

        if category == "image":
            enqueue_with_request_context(generate_image_task, task.id)
        else:
            enqueue_with_request_context(generate_video_task, task.id)
    except Exception as e:  # noqa: BLE001
        credits.refund(db, user_id, cost, biz_ref=task.id, commit=False)
        task.status = "failed"
        task.error = f"入队失败:{e}"
        db.commit()
        raise HTTPException(503, "任务入队失败,已退回额度。请确认 Worker/Redis 运行中") from e
