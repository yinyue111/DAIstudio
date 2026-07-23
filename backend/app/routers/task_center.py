"""Cross-workflow task center API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import UnifiedTaskPageOut
from ..services.task_center import InvalidTaskCursor, list_unified_tasks

router = APIRouter(prefix="/api", tags=["task-center"])


@router.get("/task-center", response_model=UnifiedTaskPageOut)
def get_task_center(
    kind: Literal["all", "generation", "reverse", "parse", "workflow"] = "all",
    status: Literal[
        "all",
        "active",
        "succeeded",
        "failed",
        "canceled",
        "needs_attention",
    ] = "all",
    category: Literal["all", "image", "video"] = "all",
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=8, max_length=512),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return list_unified_tasks(
            db,
            user_id=user.id,
            kind=kind,
            status_group=status,
            category=category,
            limit=limit,
            cursor=cursor,
        )
    except InvalidTaskCursor as exc:
        raise HTTPException(
            400,
            detail={"code": "INVALID_TASK_CURSOR", "message": str(exc)},
        ) from exc
