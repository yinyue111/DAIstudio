"""Read model for reverse-operation quality and economics reporting."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    CreationRecipe,
    GatewayCall,
    GenTask,
    ReverseOperation,
    ReverseOperationFeedback,
    ReverseResultRevision,
)
from .reporting_helpers import parse_date as _parse_date
from .reverse_usage_csv import reverse_quality_csv as _reverse_quality_csv


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


def _load_operation_scope(
    db: Session,
    *,
    start: str | None,
    end: str | None,
    media_type: Literal["image", "video"] | None,
    model: str | None,
    focus: str | None,
) -> dict:
    start_dt = _parse_date(start)
    end_dt = _parse_date(end, end_of_day=True)
    reporting_at = func.coalesce(
        ReverseOperation.finished_at,
        ReverseOperation.created_at,
    )
    query = _filter_range(
        select(ReverseOperation),
        reporting_at,
        start_dt,
        end_dt,
    )
    unfiltered = list(db.execute(query).scalars())
    operations = [
        operation
        for operation in unfiltered
        if (media_type is None or _operation_source_type(operation) == media_type)
        and (model is None or _operation_model(operation) == model)
        and (focus is None or str(operation.analysis_focus or "unknown") == focus)
    ]
    return {
        "start_dt": start_dt,
        "end_dt": end_dt,
        "operations": operations,
        "dimension_filtered": any(
            value is not None for value in (media_type, model, focus)
        ),
        "filter_options": {
            "media_types": sorted({_operation_source_type(item) for item in unfiltered}),
            "models": sorted({_operation_model(item) for item in unfiltered}),
            "focuses": sorted(
                {str(item.analysis_focus or "unknown") for item in unfiltered}
            ),
        },
    }


def _operation_rollups(operations: list[ReverseOperation]) -> dict:
    by_status: dict[str, int] = {}
    by_preset: dict[str, dict[str, int | str]] = {}
    by_target: dict[str, dict[str, int | str]] = {}
    for operation in operations:
        by_status[operation.status] = by_status.get(operation.status, 0) + 1
        pricing = (
            operation.pricing_snapshot
            if isinstance(operation.pricing_snapshot, dict)
            else {}
        )
        context = _reverse_context(operation)
        preset = str(
            pricing.get("preset")
            or context.get("video_analysis_preset")
            or ("standard" if operation.target == "video" else "image")
        )
        preset_row = by_preset.setdefault(
            preset,
            {
                "preset": preset,
                "operation_count": 0,
                "succeeded": 0,
                "settled_credits": 0,
            },
        )
        target = str(operation.target or "unknown")
        target_row = by_target.setdefault(
            target,
            {
                "target": target,
                "operation_count": 0,
                "succeeded": 0,
                "settled_credits": 0,
            },
        )
        for row in (preset_row, target_row):
            row["operation_count"] += 1
            row["succeeded"] += int(operation.status == "succeeded")
            row["settled_credits"] += int(operation.cost_settled or 0)

    video_operations = [
        operation for operation in operations if operation.target == "video"
    ]
    cover_operations = [
        operation
        for operation in video_operations
        if _reverse_analysis(operation).get("analysis_mode") == "cover_fallback"
    ]
    cover_flags = [
        _cover_confirmation_flags(operation) for operation in video_operations
    ]
    evidence_coverage = {
        int(operation.id): value
        for operation in operations
        if (value := _operation_evidence_coverage(operation)) is not None
    }
    return {
        "by_status": by_status,
        "terminal_count": sum(
            by_status.get(status, 0)
            for status in ("succeeded", "failed", "canceled")
        ),
        "durations": [
            value
            for operation in operations
            if (value := _operation_duration_seconds(operation)) is not None
        ],
        "queue_delays": [
            value
            for operation in operations
            if (value := _queue_delay_seconds(operation)) is not None
        ],
        "video_operations": video_operations,
        "cover_operations": cover_operations,
        "cover_required": sum(required for required, _ in cover_flags),
        "cover_confirmed": sum(confirmed for _, confirmed in cover_flags),
        "evidence_coverage": evidence_coverage,
        "by_preset": by_preset,
        "by_target": by_target,
    }


def _load_quality_rows(db: Session, operation_ids: set[int]) -> dict:
    if not operation_ids:
        return {"revisions": [], "feedback": [], "recipe_operation_ids": set()}
    revisions = list(
        db.execute(
            select(ReverseResultRevision)
            .where(ReverseResultRevision.operation_id.in_(operation_ids))
            .order_by(
                ReverseResultRevision.operation_id,
                ReverseResultRevision.version,
            )
        ).scalars()
    )
    feedback = list(
        db.execute(
            select(ReverseOperationFeedback).where(
                ReverseOperationFeedback.operation_id.in_(operation_ids)
            )
        ).scalars()
    )
    recipe_operation_ids = {
        int(value)
        for value in db.execute(
            select(CreationRecipe.source_operation_id).where(
                CreationRecipe.source_operation_id.in_(operation_ids)
            )
        ).scalars()
        if value is not None
    }
    return {
        "revisions": revisions,
        "feedback": feedback,
        "recipe_operation_ids": recipe_operation_ids,
    }


def _quality_lineage(rows: dict) -> dict:
    revisions_by_operation: dict[int, list[ReverseResultRevision]] = {}
    for revision in rows["revisions"]:
        revisions_by_operation.setdefault(int(revision.operation_id), []).append(revision)
    source_operation_ids = {
        source: {
            operation_id
            for operation_id, revisions in revisions_by_operation.items()
            if any(row.source == source for row in revisions)
        }
        for source in ("applied", "user_edit", "generation")
    }
    edit_ratios: dict[int, float] = {}
    for operation_id, revisions in revisions_by_operation.items():
        normalized = next(
            (row for row in revisions if row.source == "normalized"),
            None,
        )
        edited = next(
            (
                row
                for row in reversed(revisions)
                if row.source in {"user_edit", "applied"}
            ),
            None,
        )
        before = _revision_prompt(normalized.payload if normalized else None)
        after = _revision_prompt(edited.payload if edited else None)
        if before and after:
            edit_ratios[operation_id] = round(
                1 - SequenceMatcher(None, before, after).ratio(),
                4,
            )
    feedback_by_operation = {
        int(row.operation_id): row for row in rows["feedback"]
    }
    useful_operation_ids = {
        operation_id
        for operation_id, row in feedback_by_operation.items()
        if row.rating == "useful"
    }
    issue_types: dict[str, int] = {}
    for feedback in rows["feedback"]:
        for issue_type in feedback.issue_types or []:
            key = str(issue_type)
            issue_types[key] = issue_types.get(key, 0) + 1
    return {
        "adopted_operation_ids": source_operation_ids["applied"],
        "edited_operation_ids": source_operation_ids["user_edit"],
        "generated_operation_ids": source_operation_ids["generation"],
        "edit_ratios": edit_ratios,
        "feedback_by_operation": feedback_by_operation,
        "useful_operation_ids": useful_operation_ids,
        "issue_types": issue_types,
        "recipe_operation_ids": rows["recipe_operation_ids"],
        "feedback": rows["feedback"],
    }


def _load_gateway_calls(
    db: Session,
    *,
    start_dt,
    end_dt,
    dimension_filtered: bool,
    operation_ids: set[int],
) -> list[GatewayCall]:
    query = select(GatewayCall).where(GatewayCall.kind == "reverse")
    if not (start_dt or end_dt or dimension_filtered):
        return list(db.execute(query).scalars())
    candidates: dict[int, GatewayCall] = {}
    if not dimension_filtered:
        legacy_query = _filter_range(
            query,
            GatewayCall.created_at,
            start_dt,
            end_dt,
        )
        for call in db.execute(legacy_query).scalars():
            candidates[int(call.id)] = call
    if operation_ids:
        linked_query = query.where(
            GatewayCall.detail["operation_id"].as_integer().in_(operation_ids)
        )
        for call in db.execute(linked_query).scalars():
            candidates[int(call.id)] = call
    return [
        call
        for call in candidates.values()
        if (operation_id := _detail_operation_id(call.detail)) is None
        or operation_id in operation_ids
    ]


def _repair_metrics(
    operations: list[ReverseOperation],
    gateway_calls: list[GatewayCall],
) -> dict:
    operation_ids = {int(operation.id) for operation in operations}
    repaired_ids = {
        int(operation.id)
        for operation in operations
        if bool(_reverse_result(operation).get("repair_attempted"))
        or operation.error_code == "REPAIR_FAILED"
    }
    for call in gateway_calls:
        detail = call.detail if isinstance(call.detail, dict) else {}
        operation_id = _detail_operation_id(detail)
        if detail.get("repair_attempted") and operation_id in operation_ids:
            repaired_ids.add(operation_id)
    operation_by_id = {int(operation.id): operation for operation in operations}
    return {
        "operation_ids": repaired_ids,
        "succeeded": sum(
            operation_by_id[operation_id].status == "succeeded"
            for operation_id in repaired_ids
        ),
        "failed": sum(
            operation_by_id[operation_id].status in {"failed", "canceled"}
            for operation_id in repaired_ids
        ),
    }


def _gateway_cost_metrics(
    operations: list[ReverseOperation],
    gateway_calls: list[GatewayCall],
) -> dict:
    operation_ids = {int(operation.id) for operation in operations}
    entries: list[tuple[str, int, bool]] = []
    entries_by_operation: dict[int, list[tuple[str, int, bool]]] = {}
    cost_recorded_operation_ids: set[int] = set()
    model_costs: dict[str, dict[str, int | str]] = {}
    for call in gateway_calls:
        entry = _provider_cost_entry(call.detail)
        entries.append(entry)
        operation_id = _detail_operation_id(call.detail)
        if operation_id is not None and operation_id in operation_ids:
            entries_by_operation.setdefault(operation_id, []).append(entry)
            if entry[2]:
                cost_recorded_operation_ids.add(operation_id)
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
        row["cost_credits"] += _detail_cost_entry(call.detail)[1]

    known_count = sum(known for _, _, known in entries)
    provider_cost = sum(cost for _, cost, known in entries if known)
    attributed_cost = sum(
        cost
        for operation_entries in entries_by_operation.values()
        for _, cost, known in operation_entries
        if known
    )
    revenue = sum(int(operation.cost_settled or 0) for operation in operations)
    cost_status = _provider_cost_status(entries)
    gross_profit = revenue - provider_cost
    economics = {
        "basis": "settled_credits_minus_provider_cost_snapshot",
        "cost_status": cost_status,
        "revenue_credits": revenue,
        "provider_cost_credits": provider_cost,
        "attributed_provider_cost_credits": attributed_cost,
        "unattributed_provider_cost_credits": provider_cost - attributed_cost,
        "gross_profit_credits": gross_profit if cost_status == "complete" else None,
        "gross_margin_rate": (
            _rate(gross_profit, revenue)
            if cost_status == "complete" and revenue
            else None
        ),
        "gateway_call_count": len(gateway_calls),
        "gateway_cost_record_count": known_count,
        "gateway_cost_coverage_rate": _rate(known_count, len(gateway_calls)),
        "operation_cost_coverage_rate": _rate(
            len(cost_recorded_operation_ids),
            len(operations),
        ),
    }
    return {
        "entries_by_operation": entries_by_operation,
        "economics": economics,
        "model_costs": model_costs,
    }


def _quality_dimension(
    label: str,
    value_for,
    operations: list[ReverseOperation],
    rollups: dict,
    lineage: dict,
    cost_metrics: dict,
) -> list[dict]:
    grouped: dict[str, dict] = {}
    for operation in operations:
        value = str(value_for(operation) or "unknown")
        row = grouped.setdefault(
            value,
            {
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
            },
        )
        operation_id = int(operation.id)
        row["operation_count"] += 1
        row["succeeded"] += int(operation.status == "succeeded")
        row["failed"] += int(operation.status == "failed")
        row["canceled"] += int(operation.status == "canceled")
        row["settled_credits"] += int(operation.cost_settled or 0)
        row["adopted"] += int(operation_id in lineage["adopted_operation_ids"])
        row["edited"] += int(operation_id in lineage["edited_operation_ids"])
        row["generated"] += int(operation_id in lineage["generated_operation_ids"])
        row["recipe"] += int(operation_id in lineage["recipe_operation_ids"])
        row["feedback_count"] += int(operation_id in lineage["feedback_by_operation"])
        row["useful"] += int(operation_id in lineage["useful_operation_ids"])
        if operation_id in rollups["evidence_coverage"]:
            row["evidence_coverage_total"] += rollups["evidence_coverage"][operation_id]
            row["evidence_coverage_samples"] += 1
        if operation_id in lineage["edit_ratios"]:
            row["edit_ratio_total"] += lineage["edit_ratios"][operation_id]
            row["edit_ratio_samples"] += 1
        operation_costs = cost_metrics["entries_by_operation"].get(operation_id, [])
        row["provider_cost_entries"].extend(operation_costs)
        row["provider_cost_credits"] += sum(
            cost for _, cost, known in operation_costs if known
        )
    for row in grouped.values():
        succeeded = int(row["succeeded"])
        feedback_count = int(row["feedback_count"])
        terminal = succeeded + int(row["failed"]) + int(row["canceled"])
        revenue = int(row["settled_credits"])
        gross_profit = revenue - int(row["provider_cost_credits"])
        cost_entries = row.pop("provider_cost_entries")
        cost_status = _provider_cost_status(cost_entries)
        row.update({
            "success_rate": _rate(succeeded, terminal),
            "adoption_rate": _rate(int(row["adopted"]), succeeded),
            "edit_rate": _rate(int(row["edited"]), succeeded),
            "generation_conversion_rate": _rate(int(row["generated"]), succeeded),
            "recipe_conversion_rate": _rate(int(row["recipe"]), succeeded),
            "useful_rate": _rate(int(row["useful"]), feedback_count),
            "avg_evidence_coverage": _rate(
                row.pop("evidence_coverage_total"),
                row.pop("evidence_coverage_samples"),
            ),
            "avg_edit_ratio": _rate(
                row.pop("edit_ratio_total"),
                row.pop("edit_ratio_samples"),
            ),
            "cost_status": cost_status,
            "gross_profit_credits": (
                gross_profit if cost_status == "complete" else None
            ),
            "gross_margin_rate": (
                _rate(gross_profit, revenue)
                if cost_status == "complete" and revenue
                else None
            ),
            "cost_coverage_rate": _rate(
                sum(known for _, _, known in cost_entries),
                len(cost_entries),
            ),
        })
    return [grouped[key] for key in sorted(grouped)]


def _assemble_reverse_usage_payload(
    *,
    operations: list[ReverseOperation],
    scope: dict,
    rollups: dict,
    lineage: dict,
    repair: dict,
    cost_metrics: dict,
    selected_filters: dict,
) -> dict:
    by_status = rollups["by_status"]
    succeeded = by_status.get("succeeded", 0)
    coverage = list(rollups["evidence_coverage"].values())
    edit_ratios = list(lineage["edit_ratios"].values())
    economics = cost_metrics["economics"]
    feedback = lineage["feedback"]
    payload = {
        "summary": {
            "operation_count": len(operations),
            "succeeded": succeeded,
            "failed": by_status.get("failed", 0),
            "canceled": by_status.get("canceled", 0),
            "needs_confirmation": by_status.get("needs_confirmation", 0),
            "success_rate": _rate(succeeded, rollups["terminal_count"]),
            "failure_rate": _rate(
                by_status.get("failed", 0),
                rollups["terminal_count"],
            ),
            "cancel_rate": _rate(
                by_status.get("canceled", 0),
                rollups["terminal_count"],
            ),
            "settled_credits": economics["revenue_credits"],
        },
        "by_status": by_status,
        "latency": {
            "queue_p50_seconds": _percentile(rollups["queue_delays"], 0.5),
            "queue_p95_seconds": _percentile(rollups["queue_delays"], 0.95),
            "total_p50_seconds": _percentile(rollups["durations"], 0.5),
            "total_p95_seconds": _percentile(rollups["durations"], 0.95),
        },
        "quality": {
            "video_operation_count": len(rollups["video_operations"]),
            "cover_fallback_count": len(rollups["cover_operations"]),
            "cover_fallback_rate": _rate(
                len(rollups["cover_operations"]),
                len(rollups["video_operations"]),
            ),
            "cover_confirmation_required_count": rollups["cover_required"],
            "cover_confirmed_count": rollups["cover_confirmed"],
            "cover_confirmation_rate": _rate(
                rollups["cover_confirmed"],
                rollups["cover_required"],
            ),
            "repair_count": len(repair["operation_ids"]),
            "repair_succeeded_count": repair["succeeded"],
            "repair_failed_count": repair["failed"],
            "repair_rate": _rate(len(repair["operation_ids"]), len(operations)),
            "avg_evidence_coverage": (
                round(sum(coverage) / len(coverage), 4) if coverage else 0
            ),
            "feedback_count": len(feedback),
            "useful_count": len(lineage["useful_operation_ids"]),
            "not_useful_count": len(feedback) - len(lineage["useful_operation_ids"]),
            "useful_rate": _rate(len(lineage["useful_operation_ids"]), len(feedback)),
            "adopted_operation_count": len(lineage["adopted_operation_ids"]),
            "adoption_rate": _rate(len(lineage["adopted_operation_ids"]), succeeded),
            "edited_operation_count": len(lineage["edited_operation_ids"]),
            "edit_rate": _rate(len(lineage["edited_operation_ids"]), succeeded),
            "avg_edit_ratio": (
                round(sum(edit_ratios) / len(edit_ratios), 4) if edit_ratios else 0
            ),
            "generated_operation_count": len(lineage["generated_operation_ids"]),
            "generation_conversion_rate": _rate(
                len(lineage["generated_operation_ids"]),
                succeeded,
            ),
            "recipe_operation_count": len(lineage["recipe_operation_ids"]),
            "recipe_conversion_rate": _rate(
                len(lineage["recipe_operation_ids"]),
                succeeded,
            ),
            "issue_types": dict(sorted(lineage["issue_types"].items())),
        },
        "economics": economics,
        "filters": {
            "selected": selected_filters,
            "options": scope["filter_options"],
        },
        "by_preset": [
            rollups["by_preset"][key] for key in sorted(rollups["by_preset"])
        ],
        "by_target": [
            rollups["by_target"][key] for key in sorted(rollups["by_target"])
        ],
        "model_costs": sorted(
            cost_metrics["model_costs"].values(),
            key=lambda row: (
                -int(row["cost_credits"]),
                -int(row["call_count"]),
                str(row["model_id"]),
            ),
        ),
    }
    for key, label, value_for in (
        ("by_focus", "focus", lambda operation: operation.analysis_focus),
        ("by_media_type", "media_type", _operation_source_type),
        ("by_model_quality", "model", _operation_model),
    ):
        payload[key] = _quality_dimension(
            label,
            value_for,
            operations,
            rollups,
            lineage,
            cost_metrics,
        )
    return payload


def build_reverse_operation_usage(
    db: Session,
    *,
    start: str | None = None,
    end: str | None = None,
    media_type: Literal["image", "video"] | None = None,
    model: str | None = None,
    focus: str | None = None,
    format: Literal["json", "csv"] = "json",
):
    scope = _load_operation_scope(
        db,
        start=start,
        end=end,
        media_type=media_type,
        model=model,
        focus=focus,
    )
    operations = scope["operations"]
    operation_ids = {int(operation.id) for operation in operations}
    rollups = _operation_rollups(operations)
    quality_rows = _load_quality_rows(db, operation_ids)

    lineage = _quality_lineage(quality_rows)

    gateway_calls = _load_gateway_calls(
        db,
        start_dt=scope["start_dt"],
        end_dt=scope["end_dt"],
        dimension_filtered=scope["dimension_filtered"],
        operation_ids=operation_ids,
    )
    repair = _repair_metrics(operations, gateway_calls)
    cost_metrics = _gateway_cost_metrics(operations, gateway_calls)
    payload = _assemble_reverse_usage_payload(
        operations=operations,
        scope=scope,
        rollups=rollups,
        lineage=lineage,
        repair=repair,
        cost_metrics=cost_metrics,
        selected_filters={
            "media_type": media_type,
            "model": model,
            "focus": focus,
        },
    )
    if format == "csv":
        return _reverse_quality_csv(payload)
    return payload
