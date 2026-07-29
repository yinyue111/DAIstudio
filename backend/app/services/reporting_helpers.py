"""Shared parsing and CSV safety helpers for reporting read models."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import HTTPException

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def parse_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            400,
            f"日期格式应为 ISO(YYYY-MM-DD),收到:{value}",
        ) from None
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    if text.lstrip(" \t\r\n").startswith(CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def date_key(value: datetime | None) -> str | None:
    return value.date().isoformat() if value else None
