"""Admin usage reporting endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..models import User
from ..services import admin_usage_read_model as _usage
from ..services import reverse_usage_read_model as _reverse_usage

router = APIRouter()

# Compatibility aliases retained for existing tests and internal imports.
_detail_cost_credits = _reverse_usage._detail_cost_credits
_duration_seconds = _reverse_usage._duration_seconds
_evidence_coverage = _reverse_usage._evidence_coverage
_filter_range = _reverse_usage._filter_range


@router.get("/usage/dashboard")
def usage_dashboard(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
):
    return _usage.build_usage_dashboard(db, start=start, end=end)


@router.get("/usage/model-costs")
def model_costs(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
):
    return _usage.build_model_costs(db, start=start, end=end)


@router.get("/usage/reverse-operations")
def reverse_operation_usage(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
    media_type: Literal["image", "video"] | None = None,
    model: str | None = None,
    focus: str | None = None,
    format: Literal["json", "csv"] = "json",
):
    return _reverse_usage.build_reverse_operation_usage(
        db,
        start=start,
        end=end,
        media_type=media_type,
        model=model,
        focus=focus,
        format=format,
    )


@router.get("/usage/report")
def usage_report(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
    format: str = "json",
):
    """Return per-user and per-department spend for an optional date range."""
    return _usage.build_usage_report(db, start=start, end=end, format=format)
