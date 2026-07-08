"""Historical ETA helpers for generation tasks."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GenTask


def _seconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if not start or not end:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    seconds = int((end - start).total_seconds())
    return seconds if seconds > 0 else None


def _param_value(params: dict | None, *keys: str) -> str:
    params = params or {}
    for key in keys:
        value = params.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _safe_duration_seconds(value: str) -> int:
    try:
        duration = int(float(value or 5))
    except (TypeError, ValueError):
        duration = 5
    return max(1, min(duration, 15))


def video_eta_for_task(db: Session, task: GenTask, *, sample_limit: int = 200) -> dict | None:
    if task.category != "video" or task.status in {"succeeded", "failed", "needs_review", "canceled"}:
        return None
    params = task.params or {}
    duration = _param_value(params, "duration", "target_duration")
    resolution = _param_value(params, "resolution", "target_resolution")
    stage = task.stage or "preview"

    rows = list(
        db.execute(
            select(GenTask)
            .where(
                GenTask.category == "video",
                GenTask.stage == stage,
                GenTask.status == "succeeded",
                GenTask.created_at.is_not(None),
                GenTask.finished_at.is_not(None),
            )
            .order_by(GenTask.finished_at.desc())
            .limit(sample_limit)
        ).scalars()
    )
    samples: list[int] = []
    for row in rows:
        row_params = row.params or {}
        if duration and _param_value(row_params, "duration", "target_duration") != duration:
            continue
        if resolution and _param_value(row_params, "resolution", "target_resolution") != resolution:
            continue
        seconds = _seconds_between(row.created_at, row.finished_at)
        if seconds:
            samples.append(seconds)
    if samples:
        total = int(sum(samples) / len(samples))
        source = "history"
    else:
        numeric_duration = _safe_duration_seconds(duration)
        is_1080p = resolution == "1080p"
        total = max(120, numeric_duration * (45 if is_1080p else 30))
        source = "fallback"
    elapsed = _seconds_between(task.created_at, datetime.now(timezone.utc)) or 0
    progress = max(1, min(95, int((elapsed / max(total, 1)) * 100)))
    remaining = max(30, total - elapsed)
    return {
        "eta_source": source,
        "eta_total_seconds": int(total),
        "eta_remaining_seconds": int(remaining),
        "eta_sample_count": len(samples),
        "progress_estimate": progress,
    }
