"""Lightweight text safety gate for prompts and generation instructions."""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .config_store import get_bool_setting, get_setting

_SPLIT_RE = re.compile(r"[\n,，、;；]+")


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
        if term and term in text:
            raise HTTPException(400, "内容安全拦截:提示词包含平台禁止内容")
