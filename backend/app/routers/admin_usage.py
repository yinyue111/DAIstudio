"""Admin usage reporting endpoints."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..models import CreditTransaction, GatewayCall, GenTask, ReverseOperation, User
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


def _evidence_coverage(operation: ReverseOperation) -> float | None:
    analysis = _reverse_analysis(operation)
    source = analysis.get("source") if isinstance(analysis.get("source"), dict) else {}
    try:
        duration = float(source.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    intervals: list[tuple[float, float]] = []
    for gap in analysis.get("analysis_gaps") or []:
        if not isinstance(gap, dict):
            continue
        try:
            start = max(0.0, min(duration, float(gap.get("start_seconds"))))
            end = max(0.0, min(duration, float(gap.get("end_seconds"))))
        except (TypeError, ValueError):
            continue
        if end > start:
            intervals.append((start, end))
    uncovered = 0.0
    merged_end = 0.0
    for start, end in sorted(intervals):
        if start >= merged_end:
            uncovered += end - start
        elif end > merged_end:
            uncovered += end - merged_end
        merged_end = max(merged_end, end)
    return round(max(0.0, min(1.0, 1 - uncovered / duration)), 4)


def _detail_cost_credits(detail: dict | None) -> int:
    if not isinstance(detail, dict):
        return 0
    # `cost` was written by the original reverse-prompt path. Keep reading it
    # so historical provider spend remains visible after all new callers move
    # to the canonical `cost_credits` key.
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
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _detail_operation_id(detail: dict | None) -> int | None:
    if not isinstance(detail, dict):
        return None
    try:
        return int(detail.get("operation_id"))
    except (TypeError, ValueError):
        return None


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
    operations = list(db.execute(query).scalars())

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
    coverage = [
        value for operation in video_operations
        if (value := _evidence_coverage(operation)) is not None
    ]
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
    gateway_query = select(GatewayCall).where(GatewayCall.kind == "reverse")
    if start_dt or end_dt:
        # Operation-linked model calls follow the operation's reporting date,
        # even when a call and settlement cross midnight. Calls from the legacy
        # synchronous path have no operation_id and retain their call date.
        candidates: dict[int, GatewayCall] = {}
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
        row["cost_credits"] += _detail_cost_credits(call.detail)

    return {
        "summary": {
            "operation_count": len(operations),
            "succeeded": succeeded,
            "failed": by_status.get("failed", 0),
            "canceled": by_status.get("canceled", 0),
            "needs_confirmation": by_status.get("needs_confirmation", 0),
            "success_rate": round(succeeded / terminal_count, 4) if terminal_count else 0,
            "failure_rate": round(failed / terminal_count, 4) if terminal_count else 0,
            "cancel_rate": round(canceled / terminal_count, 4) if terminal_count else 0,
            "settled_credits": sum(int(operation.cost_settled or 0) for operation in operations),
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
            "cover_fallback_rate": round(len(cover_operations) / len(video_operations), 4)
            if video_operations else 0,
            "cover_confirmation_required_count": cover_required,
            "cover_confirmed_count": cover_confirmed,
            "cover_confirmation_rate": round(cover_confirmed / cover_required, 4)
            if cover_required else 0,
            "repair_count": len(repaired_operation_ids),
            "repair_succeeded_count": repair_succeeded,
            "repair_failed_count": repair_failed,
            "repair_rate": round(len(repaired_operation_ids) / len(operations), 4)
            if operations else 0,
            "avg_evidence_coverage": round(sum(coverage) / len(coverage), 4) if coverage else 0,
        },
        "by_preset": [by_preset[key] for key in sorted(by_preset)],
        "by_target": [by_target[key] for key in sorted(by_target)],
        "model_costs": sorted(
            model_costs.values(),
            key=lambda row: (
                -int(row["cost_credits"]),
                -int(row["call_count"]),
                str(row["model_id"]),
            ),
        ),
    }


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
