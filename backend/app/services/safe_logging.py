"""Helpers for logging user-supplied URLs without leaking query tokens."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def redact_url_for_log(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
    except Exception:  # noqa: BLE001
        return str(url)[:120]
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    query = "<redacted>" if parts.query else ""
    fragment = "<redacted>" if parts.fragment else ""
    return urlunsplit((parts.scheme, host, parts.path, query, fragment))
