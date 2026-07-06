"""Lightweight text safety gate for prompts and generation instructions."""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .config_store import get_bool_setting, get_setting

_SPLIT_RE = re.compile(r"[\n,，、;；]+")
_LATIN_WORD_RE = re.compile(r"[a-z0-9_]", re.IGNORECASE)


def _terms(raw: str | None) -> list[str]:
    return [term.strip().lower() for term in _SPLIT_RE.split(str(raw or "")) if term.strip()]


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _term_matches(text: str, term: str) -> bool:
    if not term:
        return False
    if not _LATIN_WORD_RE.search(term):
        return term in text
    pattern = rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def assert_text_allowed(db: Session, *values: Any) -> None:
    """Block configured banned terms when content safety is enabled.

    This is intentionally a local deterministic guard. It does not replace a
    model/provider moderation service for generated images/videos, but it gives
    admins an immediate kill-switch for terms they do not want sent upstream.
    """
    if not get_bool_setting(db, "content_safety_enabled", False):
        return
    terms = _terms(get_setting(db, "content_safety_banned_terms", ""))
    if not terms:
        return
    text = "\n".join(_flatten_text(v) for v in values).lower()
    for term in terms:
        if _term_matches(text, term):
            raise HTTPException(400, "内容安全拦截:提示词包含平台禁止内容")
