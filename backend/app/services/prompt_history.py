"""User prompt history helpers."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import UserPrompt

MAX_TITLE_CHARS = 128


def title_from_prompt(prompt: str, fallback: str = "未命名提示词") -> str:
    text = " ".join(str(prompt or "").strip().split())
    if not text:
        return fallback
    return text[:MAX_TITLE_CHARS]


def remember_prompt(
    db: Session,
    *,
    user_id: int,
    prompt: str | None,
    title: str | None = None,
    category: str = "general",
    source: str = "manual",
    favorite: bool = False,
    params: dict | None = None,
    commit: bool = False,
) -> UserPrompt | None:
    text = str(prompt or "").strip()
    if not text:
        return None
    row = UserPrompt(
        user_id=user_id,
        title=(title or title_from_prompt(text)).strip()[:MAX_TITLE_CHARS] or "未命名提示词",
        prompt=text,
        category=category if category in {"image", "video", "general"} else "general",
        source=source if source in {"manual", "reverse", "generate", "library"} else "manual",
        favorite=bool(favorite),
        usage_count=0,
        params=params if isinstance(params, dict) else None,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row
