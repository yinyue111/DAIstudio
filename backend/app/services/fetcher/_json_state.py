"""Parsers for the JSON / JSONP / JS-string state blobs SSR pages embed."""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("fetcher")


_JS_STRING_RE = r"""(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)')"""


def _json_like_state(raw: str) -> dict | None:
    """Parse JSON-like JS state emitted by SSR pages.

    Xiaohongshu's ``window.__INITIAL_STATE__`` is mostly JSON but may contain
    bare ``undefined`` values, so strict json.loads fails without a small,
    value-position-only cleanup.
    """
    raw = raw.strip().rstrip(";")
    cleaned = re.sub(r"([:\[,])\s*undefined\s*(?=[,\]}])", r"\1null", raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("failed to parse xiaohongshu initial state", exc_info=True)
        return None


def _plain_json(raw: str, label: str) -> dict | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("failed to parse %s json state", label, exc_info=True)
        return None


def _json_from_jsonp(raw: str, label: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        return _plain_json(text, label)
    m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text, flags=re.S)
    if not m:
        return None
    return _plain_json(m.group(1), label)


def _js_unquote(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return bytes(raw, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return raw


def _extract_js_string_assignment(html: str, name: str) -> str | None:
    m = re.search(rf"\b{re.escape(name)}\s*=\s*{_JS_STRING_RE}", html)
    if not m:
        return None
    return _js_unquote(m.group(1) or m.group(2))


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_strings(value)
