"""User prompt history and favorites."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User, UserPrompt
from ..schemas import UserPromptIn, UserPromptOut, UserPromptUpdateIn
from ..services.prompt_history import remember_prompt, title_from_prompt

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _page(limit: int, offset: int, cap: int = 100) -> tuple[int, int]:
    return min(max(int(limit), 1), cap), max(int(offset), 0)


def _owned_prompt(db: Session, prompt_id: int, user_id: int) -> UserPrompt:
    row = db.get(UserPrompt, prompt_id)
    if not row or row.user_id != user_id:
        raise HTTPException(404, "提示词不存在")
    return row


@router.get("/history", response_model=list[UserPromptOut])
def list_prompt_history(
    favorite: bool | None = None,
    category: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    limit, offset = _page(limit, offset)
    query = select(UserPrompt).where(UserPrompt.user_id == user.id)
    if favorite is not None:
        query = query.where(UserPrompt.favorite.is_(bool(favorite)))
    if category in {"image", "video", "general"}:
        query = query.where(UserPrompt.category == category)
    if source in {"manual", "reverse", "generate", "library"}:
        query = query.where(UserPrompt.source == source)
    text = (q or "").strip()
    if text:
        like = f"%{text}%"
        query = query.where(or_(UserPrompt.title.ilike(like), UserPrompt.prompt.ilike(like)))
    return list(
        db.execute(
            query.order_by(UserPrompt.favorite.desc(), UserPrompt.updated_at.desc(), UserPrompt.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


@router.post("/history", response_model=UserPromptOut)
def create_prompt_history(
    body: UserPromptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = remember_prompt(
        db,
        user_id=user.id,
        title=body.title,
        prompt=body.prompt,
        category=body.category,
        source=body.source,
        favorite=body.favorite,
        params=body.params,
        commit=True,
    )
    assert row is not None
    return row


@router.patch("/history/{prompt_id}", response_model=UserPromptOut)
def update_prompt_history(
    prompt_id: int,
    body: UserPromptUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_prompt(db, prompt_id, user.id)
    fields = getattr(body, "model_fields_set", set())
    if "title" in fields:
        row.title = body.title or title_from_prompt(row.prompt)
    if "prompt" in fields and body.prompt is not None:
        row.prompt = body.prompt
        if "title" not in fields:
            row.title = title_from_prompt(body.prompt)
    if "category" in fields and body.category is not None:
        row.category = body.category
    if "favorite" in fields and body.favorite is not None:
        row.favorite = bool(body.favorite)
    if "params" in fields:
        row.params = body.params
    if body.increment_usage:
        row.usage_count = int(row.usage_count or 0) + 1
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@router.post("/history/{prompt_id}/favorite", response_model=UserPromptOut)
def toggle_prompt_favorite(
    prompt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_prompt(db, prompt_id, user.id)
    row.favorite = not bool(row.favorite)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/history/{prompt_id}")
def delete_prompt_history(
    prompt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _owned_prompt(db, prompt_id, user.id)
    db.delete(row)
    db.commit()
    return {"ok": True}
