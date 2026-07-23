"""Admin usage reporting endpoints."""
from __future__ import annotations

import csv
import io
from difflib import SequenceMatcher
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..models import (
    CreationRecipe,
    CreditTransaction,
    GatewayCall,
    GenTask,
    ReverseOperation,
    ReverseOperationFeedback,
    ReverseResultRevision,
    User,
)
from ..services.error_codes import task_error_type
from .admin_helpers import csv_cell as _csv_cell
from .admin_helpers import date_key as _date_key
from .admin_helpers import parse_date as _parse_date

router = APIRouter()


def _filter_range(query, column, start_dt, end_dt):
    if start_dt:
        query = query.where(column >= start_dt)
    if end_dt:
        query = query.where(column <= end_dt)
    return query


def _duration_seconds(task: GenTask) -> int | None:
    if not task.created_at or not task.finished_at:
        return None
    seconds = int((task.finished_at - task.created_at).total_seconds())
    return seconds if seconds > 0 else None


def _operation_duration_seconds(operation: ReverseOperation) -> float | None:
    if not operation.created_at or not operation.finished_at:
        return None
    seconds = (operation.finished_at - operation.created_at).total_seconds()
    return round(seconds, 3) if seconds >= 0 else None


def _queue_delay_seconds(operation: ReverseOperation) -> float | None:
    if not operation.created_at or not operation.started_at:
        return None
    seconds = (operation.started_at - operation.created_at).total_seconds()
    return round(seconds, 3) if seconds >= 0 else None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _reverse_analysis(operation: ReverseOperation) -> dict:
    result = _reverse_result(operation)
    analysis = result.get("video_analysis")
    return analysis if isinstance(analysis, dict) else {}


def _reverse_result(operation: ReverseOperation) -> dict:
    return operation.result if isinstance(operation.result, dict) else {}


def _reverse_context(operation: ReverseOperation) -> dict:
    return operation.request_context if isinstance(operation.request_context, dict) else {}


def _cover_confirmation_flags(operation: ReverseOperation) -> tuple[bool, bool]:
    """Return whether cover fallback was requested and confirmed.

    v2 operations persist explicit flags in request_context. The status/result
    fallbacks keep pre-flag records measurable without treating every video
    operation as a confirmation opportunity.
    """
    context = _reverse_context(operation)
    analysis_mode = _reverse_analysis(operation).get("analysis_mode")
    confirmed = bool(context.get("cover_confirmed"))
    if not confirmed and operation.status == "succeeded" and analysis_mode == "cover_fallback":
        confirmed = True
    required = bool(
        context.get("cover_confirmation_required")
        or context.get("cover_confirmation_required_at")
        or confirmed
        or operation.status == "needs_confirmation"
        or operation.error_code in {"VIDEO_FRAMES_UNAVAILABLE", "CONFIRMATION_EXPIRED"}
        or analysis_mode in {"cover_fallback", "unavailable"}
    )
    return required, confirmed


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _bounded_intervals(
    rows: object,
    *,
    duration: float,
) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    if not isinstance(rows, list):
        return intervals
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            start = max(0.0, min(duration, float(row.get("start_seconds"))))
            end = max(0.0, min(duration, float(row.get("end_seconds"))))
        except (TypeError, ValueError):
            continue
        if end > start:
            intervals.append((start, end))
    return _merge_intervals(intervals)


def _evidence_coverage(operation: ReverseOperation) -> float | None:
    analysis = _reverse_analysis(operation)
    source = analysis.get("source") if isinstance(analysis.get("source"), dict) else {}
    try:
        duration = float(source.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None

    context = _reverse_context(operation)
    raw_ranges = operation.source_ranges
    if not isinstance(raw_ranges, list):
        raw_ranges = context.get("source_ranges")
    if not isinstance(raw_ranges, list):
        legacy_range = operation.source_range or context.get("source_range")
        raw_ranges = [legacy_range] if isinstance(legacy_range, dict) else []

    selected = _bounded_intervals(raw_ranges, duration=duration)
    if raw_ranges and not selected:
        return None
    if not selected:
        selected = [(0.0, duration)]

    gap_intersections: list[tuple[float, float]] = []
    for gap_start, gap_end in _bounded_intervals(
        analysis.get("analysis_gaps") or [],
        duration=duration,
    ):
        for selected_start, selected_end in selected:
            start = max(gap_start, selected_start)
            end = min(gap_end, selected_end)
            if end > start:
                gap_intersections.append((start, end))

    selected_duration = sum(end - start for start, end in selected)
    uncovered = sum(
        end - start
        for start, end in _merge_intervals(gap_intersections)
    )
    return round(max(0.0, min(1.0, 1 - uncovered / selected_duration)), 4)


def _detail_cost_credits(detail: dict | None) -> int:
    return _detail_cost_entry(detail)[1]


def _detail_cost_entry(detail: dict | None) -> tuple[bool, int]:
    if not isinstance(detail, dict):
        return False, 0
    # Keep the legacy usage/model-costs contract readable. These fields may be
    # user settlement prices, so margin reporting must use _provider_cost_entry.
    for key in (
        "cost_credits",
        "cost",
        "estimated_cost_credits",
        "credits",
        "real_cost_credits",
    ):
        value = detail.get(key)
        if value is None:
            continue
        try:
            return True, max(0, int(value))
        except (TypeError, ValueError):
            continue
    return False, 0


def _provider_cost_entry(detail: dict | None) -> tuple[str, int, bool]:
    if not isinstance(detail, dict):
        return "unavailable", 0, False
    status = str(detail.get("provider_cost_status") or "unavailable").lower()
    if status == "complete":
        keys = ("provider_cost_credits",)
    elif status == "partial":
        keys = ("provider_cost_known_credits", "provider_cost_credits")
    else:
        return "unavailable", 0, False
    for key in keys:
        value = detail.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return status, max(0, int(value)), True
        except (TypeError, ValueError):
            continue
    return "unavailable", 0, False


def _provider_cost_status(entries: list[tuple[str, int, bool]]) -> str:
    if entries and all(status == "complete" and known for status, _, known in entries):
        return "complete"
    if any(known for _, _, known in entries):
        return "partial"
    return "unavailable"


def _detail_operation_id(detail: dict | None) -> int | None:
    if not isinstance(detail, dict):
        return None
    try:
        return int(detail.get("operation_id"))
    except (TypeError, ValueError):
        return None


def _revision_prompt(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("final_text", "prompt", "optimized_text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("final_text") or value.get("prompt")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    for key in ("result", "reverse_result", "normalized_result"):
        value = payload.get(key)
        if isinstance(value, dict):
            prompt = _revision_prompt(value)
            if prompt:
                return prompt
    return ""


def _operation_source_type(operation: ReverseOperation) -> str:
    context = _reverse_context(operation)
    value = context.get("source_type")
    if value in {"image", "video"}:
        return str(value)
    sources = context.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict) or source.get("role") != "primary":
                continue
            value = source.get("source_type")
            if value in {"image", "video"}:
                return str(value)
    return "video" if operation.target == "video" else "image"


def _operation_model(operation: ReverseOperation) -> str:
    snapshot = operation.model_snapshot if isinstance(operation.model_snapshot, dict) else {}
    return str(
        snapshot.get("model_id")
        or snapshot.get("model_name")
        or snapshot.get("display_name")
        or (f"config:{operation.model_config_id}" if operation.model_config_id else "unknown")
    )


def _image_evidence_coverage(operation: ReverseOperation) -> float | None:
    result = _reverse_result(operation)
    analyzers = result.get("image_evidence_analyzers")
    statuses: list[str] = []
    if isinstance(analyzers, dict):
        for value in analyzers.values():
            rows = value if isinstance(value, list) else [value]
            for row in rows:
                if isinstance(row, dict) and row.get("status"):
                    statuses.append(str(row["status"]).lower())
    if statuses:
        ready = sum(status in {"analyzed", "ready", "available"} for status in statuses)
        return round(ready / len(statuses), 4)

    evidence = result.get("image_evidence")
    if not isinstance(evidence, list) or not evidence:
        return None
    usable = 0
    considered = 0
    for row in evidence:
        if not isinstance(row, dict):
            continue
        considered += 1
        analyzer_status = str(
            row.get("analyzer_status") or row.get("analysis_status") or "analyzed"
        ).lower()
        if (
            row.get("fact_status") == "visible"
            and analyzer_status in {"analyzed", "ready", "available"}
        ):
            usable += 1
    return round(usable / considered, 4) if considered else None


def _operation_evidence_coverage(operation: ReverseOperation) -> float | None:
    if _operation_source_type(operation) == "video":
        return _evidence_coverage(operation)
    return _image_evidence_coverage(operation)


def _rate(numerator: int | float, denominator: int | float) -> float:
    return round(numerator / denominator, 4) if denominator else 0


def _reverse_quality_csv(payload: dict) -> StreamingResponse:
    summary = payload["summary"]
    quality = payload["quality"]
    economics = payload["economics"]
    all_row = {
        "operation_count": summary["operation_count"],
        "succeeded": summary["succeeded"],
        "success_rate": summary["success_rate"],
        "adoption_rate": quality["adoption_rate"],
        "edit_rate": quality["edit_rate"],
        "avg_edit_ratio": quality["avg_edit_ratio"],
        "avg_evidence_coverage": quality["avg_evidence_coverage"],
        "generation_conversion_rate": quality["generation_conversion_rate"],
        "recipe_conversion_rate": quality["recipe_conversion_rate"],
        "useful_rate": quality["useful_rate"],
        "settled_credits": economics["revenue_credits"],
        "cost_status": economics["cost_status"],
        "provider_cost_credits": economics["provider_cost_credits"],
        "gross_profit_credits": economics["gross_profit_credits"],
        "gross_margin_rate": economics["gross_margin_rate"],
        "cost_coverage_rate": economics["gateway_cost_coverage_rate"],
    }
    dimensions = [
        ("all", "all", all_row),
        *(("media_type", row.get("media_type"), row) for row in payload["by_media_type"]),
        *(("model", row.get("model"), row) for row in payload["by_model_quality"]),
        *(("focus", row.get("focus"), row) for row in payload["by_focus"]),
    ]
    columns = [
        "dimension",
        "value",
        "operation_count",
        "succeeded",
        "success_rate",
        "avg_evidence_coverage",
        "adoption_rate",
        "edit_rate",
        "avg_edit_ratio",
        "generation_conversion_rate",
        "recipe_conversion_rate",
        "useful_rate",
        "settled_credits",
        "cost_status",
        "provider_cost_credits",
        "gross_profit_credits",
        "gross_margin_rate",
        "cost_coverage_rate",
    ]

    def iter_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def emit(row):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([_csv_cell(value) for value in row])
            return buffer.getvalue()

        yield "\ufeff" + emit(columns)
        for dimension, value, row in dimensions:
            yield emit([
                dimension,
                value,
                *(row.get(column, "") for column in columns[2:]),
            ])

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=reverse_quality_report.csv",
        },
    )


@router.get("/usage/dashboard")
def usage_dashboard(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
):
    start_dt = _parse_date(start)
    end_dt = _parse_date(end, end_of_day=True)
    q = select(GenTask)
    q = _filter_range(q, GenTask.created_at, start_dt, end_dt)
    tasks = list(db.execute(q).scalars())
    task_count = len(tasks)
    success_count = sum(1 for task in tasks if task.status == "succeeded")
    failed_count = sum(1 for task in tasks if task.status == "failed")
    review_count = sum(1 for task in tasks if task.status == "needs_review")
    durations = [seconds for task in tasks if (seconds := _duration_seconds(task))]
    dau = len({task.user_id for task in tasks if task.user_id})
    by_category: dict[str, int] = {}
    failure_reasons = {
        "moderation": 0,
        "user_input": 0,
        "provider_timeout": 0,
        "provider_error": 0,
        "system_error": 0,
    }
    for task in tasks:
        by_category[task.category] = by_category.get(task.category, 0) + 1
        if task.status in {"failed", "needs_review"}:
            key = task_error_type(task.params, task.error, status=task.status) or "system_error"
            failure_reasons[key] = failure_reasons.get(key, 0) + 1
    daily: dict[str, dict[str, int]] = {}
    for task in tasks:
        day = _date_key(task.created_at)
        if not day:
            continue
        row = daily.setdefault(day, {"date": day, "tasks": 0, "succeeded": 0, "failed": 0})
        row["tasks"] += 1
        if task.status == "succeeded":
            row["succeeded"] += 1
        if task.status == "failed":
            row["failed"] += 1
    return {
        "summary": {
            "dau": dau,
            "task_count": task_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "needs_review_count": review_count,
            "success_rate": round(success_count / task_count, 4) if task_count else 0,
        },
        "by_category": by_category,
        "failure_reasons": failure_reasons,
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
        "daily": [daily[key] for key in sorted(daily)],
    }


@router.get("/usage/model-costs")
def model_costs(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
):
    start_dt = _parse_date(start)
    end_dt = _parse_date(end, end_of_day=True)
    q = select(GatewayCall)
    q = _filter_range(q, GatewayCall.created_at, start_dt, end_dt)
    grouped: dict[tuple[str, str], dict] = {}
    for call in db.execute(q).scalars():
        key = (call.model_id or "unknown", call.kind or "unknown")
        row = grouped.setdefault(
            key,
            {
                "model_id": key[0],
                "kind": key[1],
                "call_count": 0,
                "ok_count": 0,
                "failed_count": 0,
                "total_tokens": 0,
                "estimated_cost_credits": 0,
                "latency_ms_total": 0,
                "latency_samples": 0,
            },
        )
        row["call_count"] += 1
        if call.status == "failed":
            row["failed_count"] += 1
        else:
            row["ok_count"] += 1
        row["total_tokens"] += int(call.total_tokens or 0)
        row["estimated_cost_credits"] += _detail_cost_credits(call.detail)
        if call.latency_ms is not None:
            row["latency_ms_total"] += int(call.latency_ms or 0)
            row["latency_samples"] += 1
    rows = []
    for row in grouped.values():
        samples = row.pop("latency_samples")
        total_latency = row.pop("latency_ms_total")
        row["avg_latency_ms"] = round(total_latency / samples, 1) if samples else 0
        row["failure_rate"] = round(row["failed_count"] / row["call_count"], 4) if row["call_count"] else 0
        rows.append(row)
    rows.sort(key=lambda item: (-item["estimated_cost_credits"], -item["call_count"], item["model_id"]))
    return {"models": rows}


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
    start_dt = _parse_date(start)
    end_dt = _parse_date(end, end_of_day=True)
    # Attribute terminal work to settlement/finish time. Active operations do
    # not have a finish timestamp yet, so they remain visible by creation time.
    reporting_at = func.coalesce(
        ReverseOperation.finished_at,
        ReverseOperation.created_at,
    )
    query = select(ReverseOperation)
    query = _filter_range(query, reporting_at, start_dt, end_dt)
    unfiltered_operations = list(db.execute(query).scalars())
    filter_options = {
        "media_types": sorted({_operation_source_type(item) for item in unfiltered_operations}),
        "models": sorted({_operation_model(item) for item in unfiltered_operations}),
        "focuses": sorted({str(item.analysis_focus or "unknown") for item in unfiltered_operations}),
    }
    operations = [
        operation
        for operation in unfiltered_operations
        if (media_type is None or _operation_source_type(operation) == media_type)
        and (model is None or _operation_model(operation) == model)
        and (focus is None or str(operation.analysis_focus or "unknown") == focus)
    ]
    dimension_filtered = any(value is not None for value in (media_type, model, focus))

    by_status: dict[str, int] = {}
    for operation in operations:
        by_status[operation.status] = by_status.get(operation.status, 0) + 1
    terminal_count = sum(
        by_status.get(status, 0) for status in ("succeeded", "failed", "canceled")
    )
    durations = [
        value for operation in operations
        if (value := _operation_duration_seconds(operation)) is not None
    ]
    queue_delays = [
        value for operation in operations
        if (value := _queue_delay_seconds(operation)) is not None
    ]
    video_operations = [operation for operation in operations if operation.target == "video"]
    cover_operations = [
        operation for operation in video_operations
        if _reverse_analysis(operation).get("analysis_mode") == "cover_fallback"
    ]
    cover_flags = {
        int(operation.id): _cover_confirmation_flags(operation)
        for operation in video_operations
    }
    cover_required = sum(1 for required, _ in cover_flags.values() if required)
    cover_confirmed = sum(1 for _, confirmed in cover_flags.values() if confirmed)
    evidence_coverage_by_operation = {
        int(operation.id): value
        for operation in operations
        if (value := _operation_evidence_coverage(operation)) is not None
    }
    coverage = list(evidence_coverage_by_operation.values())
    succeeded = by_status.get("succeeded", 0)
    failed = by_status.get("failed", 0)
    canceled = by_status.get("canceled", 0)

    by_preset: dict[str, dict[str, int | str]] = {}
    by_target: dict[str, dict[str, int | str]] = {}
    for operation in operations:
        pricing = operation.pricing_snapshot if isinstance(operation.pricing_snapshot, dict) else {}
        context = operation.request_context if isinstance(operation.request_context, dict) else {}
        preset = str(
            pricing.get("preset")
            or context.get("video_analysis_preset")
            or ("standard" if operation.target == "video" else "image")
        )
        preset_row = by_preset.setdefault(
            preset,
            {"preset": preset, "operation_count": 0, "succeeded": 0, "settled_credits": 0},
        )
        preset_row["operation_count"] += 1
        preset_row["succeeded"] += int(operation.status == "succeeded")
        preset_row["settled_credits"] += int(operation.cost_settled or 0)

        target = str(operation.target or "unknown")
        target_row = by_target.setdefault(
            target,
            {"target": target, "operation_count": 0, "succeeded": 0, "settled_credits": 0},
        )
        target_row["operation_count"] += 1
        target_row["succeeded"] += int(operation.status == "succeeded")
        target_row["settled_credits"] += int(operation.cost_settled or 0)

    operation_ids = {int(operation.id) for operation in operations}
    revisions: list[ReverseResultRevision] = []
    feedback_rows: list[ReverseOperationFeedback] = []
    recipe_operation_ids: set[int] = set()
    if operation_ids:
        revisions = list(db.execute(
            select(ReverseResultRevision)
            .where(ReverseResultRevision.operation_id.in_(operation_ids))
            .order_by(ReverseResultRevision.operation_id, ReverseResultRevision.version)
        ).scalars())
        feedback_rows = list(db.execute(
            select(ReverseOperationFeedback).where(
                ReverseOperationFeedback.operation_id.in_(operation_ids)
            )
        ).scalars())
        recipe_operation_ids = {
            int(value)
            for value in db.execute(
                select(CreationRecipe.source_operation_id).where(
                    CreationRecipe.source_operation_id.in_(operation_ids)
                )
            ).scalars()
            if value is not None
        }

    revisions_by_operation: dict[int, list[ReverseResultRevision]] = {}
    for revision in revisions:
        revisions_by_operation.setdefault(int(revision.operation_id), []).append(revision)
    adopted_operation_ids = {
        operation_id
        for operation_id, rows in revisions_by_operation.items()
        if any(row.source == "applied" for row in rows)
    }
    edited_operation_ids = {
        operation_id
        for operation_id, rows in revisions_by_operation.items()
        if any(row.source == "user_edit" for row in rows)
    }
    generated_operation_ids = {
        operation_id
        for operation_id, rows in revisions_by_operation.items()
        if any(row.source == "generation" for row in rows)
    }
    edit_ratio_by_operation: dict[int, float] = {}
    for operation_id, rows in revisions_by_operation.items():
        normalized = next((row for row in rows if row.source == "normalized"), None)
        edited = next(
            (row for row in reversed(rows) if row.source in {"user_edit", "applied"}),
            None,
        )
        before = _revision_prompt(normalized.payload if normalized else None)
        after = _revision_prompt(edited.payload if edited else None)
        if before and after:
            edit_ratio_by_operation[operation_id] = round(
                1 - SequenceMatcher(None, before, after).ratio(),
                4,
            )
    edit_ratios = list(edit_ratio_by_operation.values())

    feedback_by_operation = {
        int(row.operation_id): row
        for row in feedback_rows
    }
    useful_operation_ids = {
        operation_id
        for operation_id, row in feedback_by_operation.items()
        if row.rating == "useful"
    }
    issue_types: dict[str, int] = {}
    for feedback in feedback_rows:
        for issue_type in feedback.issue_types or []:
            key = str(issue_type)
            issue_types[key] = issue_types.get(key, 0) + 1

    def quality_dimension(label: str, value_for) -> list[dict]:
        grouped: dict[str, dict] = {}
        for operation in operations:
            value = str(value_for(operation) or "unknown")
            row = grouped.setdefault(value, {
                label: value,
                "operation_count": 0,
                "succeeded": 0,
                "settled_credits": 0,
                "failed": 0,
                "canceled": 0,
                "adopted": 0,
                "edited": 0,
                "generated": 0,
                "recipe": 0,
                "feedback_count": 0,
                "useful": 0,
                "evidence_coverage_total": 0.0,
                "evidence_coverage_samples": 0,
                "edit_ratio_total": 0.0,
                "edit_ratio_samples": 0,
                "provider_cost_credits": 0,
                "provider_cost_entries": [],
            })
            operation_id = int(operation.id)
            row["operation_count"] += 1
            row["succeeded"] += int(operation.status == "succeeded")
            row["failed"] += int(operation.status == "failed")
            row["canceled"] += int(operation.status == "canceled")
            row["settled_credits"] += int(operation.cost_settled or 0)
            row["adopted"] += int(operation_id in adopted_operation_ids)
            row["edited"] += int(operation_id in edited_operation_ids)
            row["generated"] += int(operation_id in generated_operation_ids)
            row["recipe"] += int(operation_id in recipe_operation_ids)
            row["feedback_count"] += int(operation_id in feedback_by_operation)
            row["useful"] += int(operation_id in useful_operation_ids)
            if operation_id in evidence_coverage_by_operation:
                row["evidence_coverage_total"] += evidence_coverage_by_operation[operation_id]
                row["evidence_coverage_samples"] += 1
            if operation_id in edit_ratio_by_operation:
                row["edit_ratio_total"] += edit_ratio_by_operation[operation_id]
                row["edit_ratio_samples"] += 1
            operation_cost_entries = provider_cost_entries_by_operation.get(
                operation_id,
                [],
            )
            row["provider_cost_entries"].extend(operation_cost_entries)
            row["provider_cost_credits"] += sum(
                cost for _, cost, known in operation_cost_entries if known
            )
        for row in grouped.values():
            succeeded_count = int(row["succeeded"])
            feedback_count = int(row["feedback_count"])
            terminal = succeeded_count + int(row["failed"]) + int(row["canceled"])
            revenue = int(row["settled_credits"])
            provider_cost = int(row["provider_cost_credits"])
            gross_profit = revenue - provider_cost
            cost_entries = row.pop("provider_cost_entries")
            cost_status = _provider_cost_status(cost_entries)
            row["success_rate"] = _rate(succeeded_count, terminal)
            row["adoption_rate"] = _rate(int(row["adopted"]), succeeded_count)
            row["edit_rate"] = _rate(int(row["edited"]), succeeded_count)
            row["generation_conversion_rate"] = _rate(
                int(row["generated"]), succeeded_count
            )
            row["recipe_conversion_rate"] = _rate(int(row["recipe"]), succeeded_count)
            row["useful_rate"] = _rate(int(row["useful"]), feedback_count)
            row["avg_evidence_coverage"] = _rate(
                row.pop("evidence_coverage_total"),
                row.pop("evidence_coverage_samples"),
            )
            row["avg_edit_ratio"] = _rate(
                row.pop("edit_ratio_total"),
                row.pop("edit_ratio_samples"),
            )
            row["cost_status"] = cost_status
            row["gross_profit_credits"] = (
                gross_profit if cost_status == "complete" else None
            )
            row["gross_margin_rate"] = (
                _rate(gross_profit, revenue)
                if cost_status == "complete" and revenue
                else None
            )
            row["cost_coverage_rate"] = _rate(
                sum(known for _, _, known in cost_entries),
                len(cost_entries),
            )
        return [grouped[key] for key in sorted(grouped)]

    gateway_query = select(GatewayCall).where(GatewayCall.kind == "reverse")
    if start_dt or end_dt or dimension_filtered:
        # Operation-linked model calls follow the operation's reporting date,
        # even when a call and settlement cross midnight. Calls from the legacy
        # synchronous path have no operation_id and retain their call date.
        candidates: dict[int, GatewayCall] = {}
        if not dimension_filtered:
            legacy_query = _filter_range(
                gateway_query,
                GatewayCall.created_at,
                start_dt,
                end_dt,
            )
            for call in db.execute(legacy_query).scalars():
                candidates[int(call.id)] = call
        if operation_ids:
            linked_query = gateway_query.where(
                GatewayCall.detail["operation_id"].as_integer().in_(operation_ids)
            )
            for call in db.execute(linked_query).scalars():
                candidates[int(call.id)] = call
        gateway_calls = [
            call
            for call in candidates.values()
            if (
                (operation_id := _detail_operation_id(call.detail)) is None
                or operation_id in operation_ids
            )
        ]
    else:
        gateway_calls = list(db.execute(gateway_query).scalars())
    repaired_operation_ids = {
        int(operation.id)
        for operation in operations
        if bool(_reverse_result(operation).get("repair_attempted"))
        or operation.error_code == "REPAIR_FAILED"
    }
    for call in gateway_calls:
        detail = call.detail if isinstance(call.detail, dict) else {}
        if not detail.get("repair_attempted"):
            continue
        operation_id = _detail_operation_id(detail)
        if operation_id is None:
            continue
        if operation_id in operation_ids:
            repaired_operation_ids.add(operation_id)
    operation_by_id = {int(operation.id): operation for operation in operations}
    repair_succeeded = sum(
        1
        for operation_id in repaired_operation_ids
        if operation_by_id[operation_id].status == "succeeded"
    )
    repair_failed = sum(
        1
        for operation_id in repaired_operation_ids
        if operation_by_id[operation_id].status in {"failed", "canceled"}
    )

    provider_cost_entries: list[tuple[str, int, bool]] = []
    provider_cost_entries_by_operation: dict[int, list[tuple[str, int, bool]]] = {}
    cost_recorded_operation_ids: set[int] = set()
    for call in gateway_calls:
        entry = _provider_cost_entry(call.detail)
        provider_cost_entries.append(entry)
        operation_id = _detail_operation_id(call.detail)
        if operation_id is None or operation_id not in operation_ids:
            continue
        provider_cost_entries_by_operation.setdefault(operation_id, []).append(entry)
        if entry[2]:
            cost_recorded_operation_ids.add(operation_id)

    gateway_cost_record_count = sum(known for _, _, known in provider_cost_entries)
    provider_cost_credits = sum(
        cost for _, cost, known in provider_cost_entries if known
    )
    attributed_provider_cost_credits = sum(
        cost
        for entries in provider_cost_entries_by_operation.values()
        for _, cost, known in entries
        if known
    )
    cost_status = _provider_cost_status(provider_cost_entries)

    model_costs: dict[str, dict[str, int | str]] = {}
    for call in gateway_calls:
        model_id = str(call.model_id or "unknown")
        row = model_costs.setdefault(
            model_id,
            {
                "model_id": model_id,
                "call_count": 0,
                "failed_count": 0,
                "total_tokens": 0,
                "cost_credits": 0,
            },
        )
        row["call_count"] += 1
        row["failed_count"] += int(call.status != "ok")
        row["total_tokens"] += int(call.total_tokens or 0)
        _, call_cost = _detail_cost_entry(call.detail)
        row["cost_credits"] += call_cost

    revenue_credits = sum(int(operation.cost_settled or 0) for operation in operations)
    gross_profit_credits = revenue_credits - provider_cost_credits
    economics = {
        "basis": "settled_credits_minus_provider_cost_snapshot",
        "cost_status": cost_status,
        "revenue_credits": revenue_credits,
        "provider_cost_credits": provider_cost_credits,
        "attributed_provider_cost_credits": attributed_provider_cost_credits,
        "unattributed_provider_cost_credits": (
            provider_cost_credits - attributed_provider_cost_credits
        ),
        "gross_profit_credits": (
            gross_profit_credits if cost_status == "complete" else None
        ),
        "gross_margin_rate": (
            _rate(gross_profit_credits, revenue_credits)
            if cost_status == "complete" and revenue_credits
            else None
        ),
        "gateway_call_count": len(gateway_calls),
        "gateway_cost_record_count": gateway_cost_record_count,
        "gateway_cost_coverage_rate": _rate(
            gateway_cost_record_count,
            len(gateway_calls),
        ),
        "operation_cost_coverage_rate": _rate(
            len(cost_recorded_operation_ids),
            len(operations),
        ),
    }

    payload = {
        "summary": {
            "operation_count": len(operations),
            "succeeded": succeeded,
            "failed": by_status.get("failed", 0),
            "canceled": by_status.get("canceled", 0),
            "needs_confirmation": by_status.get("needs_confirmation", 0),
            "success_rate": _rate(succeeded, terminal_count),
            "failure_rate": _rate(failed, terminal_count),
            "cancel_rate": _rate(canceled, terminal_count),
            "settled_credits": revenue_credits,
        },
        "by_status": by_status,
        "latency": {
            "queue_p50_seconds": _percentile(queue_delays, 0.5),
            "queue_p95_seconds": _percentile(queue_delays, 0.95),
            "total_p50_seconds": _percentile(durations, 0.5),
            "total_p95_seconds": _percentile(durations, 0.95),
        },
        "quality": {
            "video_operation_count": len(video_operations),
            "cover_fallback_count": len(cover_operations),
            "cover_fallback_rate": _rate(len(cover_operations), len(video_operations)),
            "cover_confirmation_required_count": cover_required,
            "cover_confirmed_count": cover_confirmed,
            "cover_confirmation_rate": _rate(cover_confirmed, cover_required),
            "repair_count": len(repaired_operation_ids),
            "repair_succeeded_count": repair_succeeded,
            "repair_failed_count": repair_failed,
            "repair_rate": _rate(len(repaired_operation_ids), len(operations)),
            "avg_evidence_coverage": round(sum(coverage) / len(coverage), 4) if coverage else 0,
            "feedback_count": len(feedback_rows),
            "useful_count": len(useful_operation_ids),
            "not_useful_count": len(feedback_rows) - len(useful_operation_ids),
            "useful_rate": _rate(len(useful_operation_ids), len(feedback_rows)),
            "adopted_operation_count": len(adopted_operation_ids),
            "adoption_rate": _rate(len(adopted_operation_ids), succeeded),
            "edited_operation_count": len(edited_operation_ids),
            "edit_rate": _rate(len(edited_operation_ids), succeeded),
            "avg_edit_ratio": round(sum(edit_ratios) / len(edit_ratios), 4) if edit_ratios else 0,
            "generated_operation_count": len(generated_operation_ids),
            "generation_conversion_rate": _rate(len(generated_operation_ids), succeeded),
            "recipe_operation_count": len(recipe_operation_ids),
            "recipe_conversion_rate": _rate(len(recipe_operation_ids), succeeded),
            "issue_types": dict(sorted(issue_types.items())),
        },
        "economics": economics,
        "filters": {
            "selected": {
                "media_type": media_type,
                "model": model,
                "focus": focus,
            },
            "options": filter_options,
        },
        "by_preset": [by_preset[key] for key in sorted(by_preset)],
        "by_target": [by_target[key] for key in sorted(by_target)],
        "by_focus": quality_dimension("focus", lambda operation: operation.analysis_focus),
        "by_media_type": quality_dimension("media_type", _operation_source_type),
        "by_model_quality": quality_dimension("model", _operation_model),
        "model_costs": sorted(
            model_costs.values(),
            key=lambda row: (
                -int(row["cost_credits"]),
                -int(row["call_count"]),
                str(row["model_id"]),
            ),
        ),
    }
    if format == "csv":
        return _reverse_quality_csv(payload)
    return payload


@router.get("/usage/report")
def usage_report(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
    format: str = "json",
):
    """Per-user / per-department spend. Optional ISO date range (filters on
    settled task finish time + unlock time). ``format=csv`` streams a CSV."""
    start_dt = _parse_date(start)
    end_dt = _parse_date(end, end_of_day=True)

    spend_by_user: dict[int, int] = {}
    daily_by_date: dict[str, int] = {}

    # Generation spend belongs to the terminal task date, not the separate
    # freeze/refund transaction dates. Cross-day renders otherwise distort
    # daily and bounded reports.
    task_spend_q = (
        select(GenTask.user_id, GenTask.cost_settled, GenTask.finished_at)
        .where(GenTask.status == "succeeded", GenTask.cost_settled > 0)
    )
    if start_dt:
        task_spend_q = task_spend_q.where(GenTask.finished_at >= start_dt)
    if end_dt:
        task_spend_q = task_spend_q.where(GenTask.finished_at <= end_dt)
    for uid, cost, finished_at in db.execute(task_spend_q).all():
        value = int(cost or 0)
        spend_by_user[uid] = spend_by_user.get(uid, 0) + value
        day = _date_key(finished_at)
        if day:
            daily_by_date[day] = daily_by_date.get(day, 0) + value

    # Reverse spend belongs to the successful operation's terminal time. The
    # migration backfills legacy successes, while new async operations settle
    # this value in the same transaction that marks them successful.
    reverse_spend_q = select(
        ReverseOperation.user_id,
        ReverseOperation.cost_settled,
        ReverseOperation.finished_at,
    ).where(
        ReverseOperation.status == "succeeded",
        ReverseOperation.cost_settled > 0,
    )
    if start_dt:
        reverse_spend_q = reverse_spend_q.where(ReverseOperation.finished_at >= start_dt)
    if end_dt:
        reverse_spend_q = reverse_spend_q.where(ReverseOperation.finished_at <= end_dt)
    for uid, cost, finished_at in db.execute(reverse_spend_q).all():
        value = int(cost or 0)
        spend_by_user[uid] = spend_by_user.get(uid, 0) + value
        day = _date_key(finished_at)
        if day:
            daily_by_date[day] = daily_by_date.get(day, 0) + value

    # Synchronous model/asset charges stay anchored to their transaction date.
    # Exclude gen_task freeze/settle/refund rows; they are represented above by
    # GenTask.cost_settled on the finished_at date. Legacy reverse consume rows
    # with a matching operation are represented by ReverseOperation above.
    sync_tx_types = ("unlock", "consume")
    represented_reverse_transaction = select(ReverseOperation.id).where(
        ReverseOperation.id == CreditTransaction.biz_ref,
        ReverseOperation.user_id == CreditTransaction.user_id,
    ).exists()
    unrepresented_transaction = or_(
        CreditTransaction.biz_type.is_(None),
        ~CreditTransaction.biz_type.in_(("reverse", "reverse_operation")),
        CreditTransaction.biz_ref.is_(None),
        ~represented_reverse_transaction,
    )
    spend_q = (
        select(
            CreditTransaction.user_id,
            func.coalesce(func.sum(-CreditTransaction.change), 0),
        )
        .where(
            CreditTransaction.type.in_(sync_tx_types),
            CreditTransaction.biz_type != "gen_task",
            unrepresented_transaction,
        )
        .group_by(CreditTransaction.user_id)
    )
    if start_dt:
        spend_q = spend_q.where(CreditTransaction.created_at >= start_dt)
    if end_dt:
        spend_q = spend_q.where(CreditTransaction.created_at <= end_dt)
    for uid, total in db.execute(spend_q).all():
        spend_by_user[uid] = max(0, spend_by_user.get(uid, 0) + int(total or 0))

    task_q = (
        select(GenTask.user_id, func.count(), GenTask.category)
        .where(GenTask.status == "succeeded")
        .group_by(GenTask.user_id, GenTask.category)
    )
    if start_dt:
        task_q = task_q.where(GenTask.finished_at >= start_dt)
    if end_dt:
        task_q = task_q.where(GenTask.finished_at <= end_dt)
    tasks_by_user: dict[int, dict] = {}
    for uid, cnt, cat in db.execute(task_q).all():
        tasks_by_user.setdefault(uid, {}).update({cat: int(cnt)})

    reverse_count_q = (
        select(ReverseOperation.user_id, func.count())
        .where(
            ReverseOperation.status == "succeeded",
            ReverseOperation.cost_settled > 0,
        )
        .group_by(ReverseOperation.user_id)
    )
    if start_dt:
        reverse_count_q = reverse_count_q.where(ReverseOperation.finished_at >= start_dt)
    if end_dt:
        reverse_count_q = reverse_count_q.where(ReverseOperation.finished_at <= end_dt)
    for uid, count in db.execute(reverse_count_q).all():
        tasks_by_user.setdefault(uid, {})["reverse"] = int(count)

    # real provider usage (tokens) from the gateway call log
    token_q = (
        select(GatewayCall.user_id, func.coalesce(func.sum(GatewayCall.total_tokens), 0))
        .group_by(GatewayCall.user_id)
    )
    if start_dt:
        token_q = token_q.where(GatewayCall.created_at >= start_dt)
    if end_dt:
        token_q = token_q.where(GatewayCall.created_at <= end_dt)
    tokens_by_user = {uid: int(t) for uid, t in db.execute(token_q).all()}

    users = list(db.execute(select(User)).scalars())
    per_user = []
    per_dept: dict[str, int] = {}
    for u in users:
        spend = spend_by_user.get(u.id, 0)
        per_user.append({
            "user_id": u.id,
            "phone": u.phone,
            "department": u.department,
            "spend_credits": spend,
            "balance": u.balance_credits,
            "frozen": u.frozen_credits,
            "tasks": tasks_by_user.get(u.id, {}),
            "real_tokens": tokens_by_user.get(u.id, 0),
        })
        dept = u.department or "未分配"
        per_dept[dept] = per_dept.get(dept, 0) + spend

    per_user.sort(key=lambda x: -x["spend_credits"])
    per_dept_list = [
        {"department": k, "spend_credits": v}
        for k, v in sorted(per_dept.items(), key=lambda x: -x[1])
    ]

    # Daily trend: terminal task spend + synchronous transaction spend.
    daily_q = (
        select(
            func.date(CreditTransaction.created_at),
            func.coalesce(func.sum(-CreditTransaction.change), 0),
        )
        .where(
            CreditTransaction.type.in_(sync_tx_types),
            CreditTransaction.biz_type != "gen_task",
            unrepresented_transaction,
        )
        .group_by(func.date(CreditTransaction.created_at))
        .order_by(func.date(CreditTransaction.created_at))
    )
    if start_dt:
        daily_q = daily_q.where(CreditTransaction.created_at >= start_dt)
    if end_dt:
        daily_q = daily_q.where(CreditTransaction.created_at <= end_dt)
    for d, s in db.execute(daily_q).all():
        if d:
            key = str(d)
            daily_by_date[key] = max(0, daily_by_date.get(key, 0) + int(s or 0))
    daily = [{"date": d, "spend_credits": daily_by_date[d]} for d in sorted(daily_by_date)]

    if format == "csv":
        def iter_csv():
            buf = io.StringIO()
            w = csv.writer(buf)

            def emit(row):
                buf.seek(0)
                buf.truncate(0)
                w.writerow(row)
                return buf.getvalue()

            yield emit([
                "user_id",
                "phone",
                "department",
                "spend_credits",
                "balance",
                "frozen",
                "image_tasks",
                "video_tasks",
                "reverse_operations",
                "real_tokens",
            ])
            for r in per_user:
                yield emit([
                    _csv_cell(r["user_id"]),
                    _csv_cell(r["phone"]),
                    _csv_cell(r["department"]),
                    _csv_cell(r["spend_credits"]),
                    _csv_cell(r["balance"]),
                    _csv_cell(r["frozen"]),
                    _csv_cell(r["tasks"].get("image", 0)),
                    _csv_cell(r["tasks"].get("video", 0)),
                    _csv_cell(r["tasks"].get("reverse", 0)),
                    _csv_cell(r["real_tokens"]),
                ])

        return StreamingResponse(
            iter_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=usage_report.csv"},
        )

    return {"per_user": per_user, "per_department": per_dept_list, "daily": daily}
