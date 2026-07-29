"""Read models backing the admin usage-reporting HTTP endpoints."""
from __future__ import annotations

import csv
import io

from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    CreditTransaction,
    GatewayCall,
    GenTask,
    ReverseOperation,
    User,
)
from . import reverse_usage_read_model as reverse_usage
from .error_codes import task_error_type
from .reporting_helpers import csv_cell, date_key, parse_date

_SYNC_TRANSACTION_TYPES = ("unlock", "consume")


def build_usage_dashboard(
    db: Session,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    start_dt = parse_date(start)
    end_dt = parse_date(end, end_of_day=True)
    query = reverse_usage._filter_range(
        select(GenTask),
        GenTask.created_at,
        start_dt,
        end_dt,
    )
    tasks = list(db.execute(query).scalars())
    task_count = len(tasks)
    success_count = sum(task.status == "succeeded" for task in tasks)
    failed_count = sum(task.status == "failed" for task in tasks)
    review_count = sum(task.status == "needs_review" for task in tasks)
    durations = [
        seconds
        for task in tasks
        if (seconds := reverse_usage._duration_seconds(task))
    ]
    by_category: dict[str, int] = {}
    failure_reasons = {
        "moderation": 0,
        "user_input": 0,
        "provider_timeout": 0,
        "provider_error": 0,
        "system_error": 0,
    }
    daily: dict[str, dict[str, int]] = {}
    for task in tasks:
        by_category[task.category] = by_category.get(task.category, 0) + 1
        if task.status in {"failed", "needs_review"}:
            key = task_error_type(task.params, task.error, status=task.status) or "system_error"
            failure_reasons[key] = failure_reasons.get(key, 0) + 1
        day = date_key(task.created_at)
        if day:
            row = daily.setdefault(
                day,
                {"date": day, "tasks": 0, "succeeded": 0, "failed": 0},
            )
            row["tasks"] += 1
            row["succeeded"] += int(task.status == "succeeded")
            row["failed"] += int(task.status == "failed")
    return {
        "summary": {
            "dau": len({task.user_id for task in tasks if task.user_id}),
            "task_count": task_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "needs_review_count": review_count,
            "success_rate": round(success_count / task_count, 4) if task_count else 0,
        },
        "by_category": by_category,
        "failure_reasons": failure_reasons,
        "avg_duration_seconds": (
            round(sum(durations) / len(durations), 1) if durations else 0
        ),
        "daily": [daily[key] for key in sorted(daily)],
    }


def build_model_costs(
    db: Session,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    query = reverse_usage._filter_range(
        select(GatewayCall),
        GatewayCall.created_at,
        parse_date(start),
        parse_date(end, end_of_day=True),
    )
    grouped: dict[tuple[str, str], dict] = {}
    for call in db.execute(query).scalars():
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
        row["failed_count" if call.status == "failed" else "ok_count"] += 1
        row["total_tokens"] += int(call.total_tokens or 0)
        row["estimated_cost_credits"] += reverse_usage._detail_cost_credits(call.detail)
        if call.latency_ms is not None:
            row["latency_ms_total"] += int(call.latency_ms or 0)
            row["latency_samples"] += 1
    rows = []
    for row in grouped.values():
        samples = row.pop("latency_samples")
        total_latency = row.pop("latency_ms_total")
        row["avg_latency_ms"] = round(total_latency / samples, 1) if samples else 0
        row["failure_rate"] = (
            round(row["failed_count"] / row["call_count"], 4)
            if row["call_count"]
            else 0
        )
        rows.append(row)
    rows.sort(
        key=lambda item: (
            -item["estimated_cost_credits"],
            -item["call_count"],
            item["model_id"],
        )
    )
    return {"models": rows}


def _unrepresented_transaction_filter():
    represented_reverse_transaction = select(ReverseOperation.id).where(
        ReverseOperation.id == CreditTransaction.biz_ref,
        ReverseOperation.user_id == CreditTransaction.user_id,
    ).exists()
    return or_(
        CreditTransaction.biz_type.is_(None),
        ~CreditTransaction.biz_type.in_(("reverse", "reverse_operation")),
        CreditTransaction.biz_ref.is_(None),
        ~represented_reverse_transaction,
    )


def _collect_settled_spend(db: Session, start_dt, end_dt):
    spend_by_user: dict[int, int] = {}
    daily_by_date: dict[str, int] = {}
    terminal_sources = (
        (
            select(GenTask.user_id, GenTask.cost_settled, GenTask.finished_at).where(
                GenTask.status == "succeeded",
                GenTask.cost_settled > 0,
            ),
            GenTask.finished_at,
        ),
        (
            select(
                ReverseOperation.user_id,
                ReverseOperation.cost_settled,
                ReverseOperation.finished_at,
            ).where(
                ReverseOperation.status == "succeeded",
                ReverseOperation.cost_settled > 0,
            ),
            ReverseOperation.finished_at,
        ),
    )
    for query, finished_column in terminal_sources:
        query = reverse_usage._filter_range(query, finished_column, start_dt, end_dt)
        for user_id, cost, finished_at in db.execute(query).all():
            value = int(cost or 0)
            spend_by_user[user_id] = spend_by_user.get(user_id, 0) + value
            if day := date_key(finished_at):
                daily_by_date[day] = daily_by_date.get(day, 0) + value

    sync_query = (
        select(
            CreditTransaction.user_id,
            func.coalesce(func.sum(-CreditTransaction.change), 0),
        )
        .where(
            CreditTransaction.type.in_(_SYNC_TRANSACTION_TYPES),
            CreditTransaction.biz_type != "gen_task",
            _unrepresented_transaction_filter(),
        )
        .group_by(CreditTransaction.user_id)
    )
    sync_query = reverse_usage._filter_range(
        sync_query,
        CreditTransaction.created_at,
        start_dt,
        end_dt,
    )
    for user_id, total in db.execute(sync_query).all():
        spend_by_user[user_id] = max(
            0,
            spend_by_user.get(user_id, 0) + int(total or 0),
        )
    return spend_by_user, daily_by_date


def _collect_task_counts(db: Session, start_dt, end_dt) -> dict[int, dict]:
    task_query = (
        select(GenTask.user_id, func.count(), GenTask.category)
        .where(GenTask.status == "succeeded")
        .group_by(GenTask.user_id, GenTask.category)
    )
    task_query = reverse_usage._filter_range(
        task_query,
        GenTask.finished_at,
        start_dt,
        end_dt,
    )
    tasks_by_user: dict[int, dict] = {}
    for user_id, count, category in db.execute(task_query).all():
        tasks_by_user.setdefault(user_id, {})[category] = int(count)

    reverse_query = (
        select(ReverseOperation.user_id, func.count())
        .where(
            ReverseOperation.status == "succeeded",
            ReverseOperation.cost_settled > 0,
        )
        .group_by(ReverseOperation.user_id)
    )
    reverse_query = reverse_usage._filter_range(
        reverse_query,
        ReverseOperation.finished_at,
        start_dt,
        end_dt,
    )
    for user_id, count in db.execute(reverse_query).all():
        tasks_by_user.setdefault(user_id, {})["reverse"] = int(count)
    return tasks_by_user


def _collect_tokens(db: Session, start_dt, end_dt) -> dict[int, int]:
    query = select(
        GatewayCall.user_id,
        func.coalesce(func.sum(GatewayCall.total_tokens), 0),
    ).group_by(GatewayCall.user_id)
    query = reverse_usage._filter_range(
        query,
        GatewayCall.created_at,
        start_dt,
        end_dt,
    )
    return {user_id: int(tokens) for user_id, tokens in db.execute(query).all()}


def _user_usage_rows(
    users: list[User],
    spend_by_user: dict[int, int],
    tasks_by_user: dict[int, dict],
    tokens_by_user: dict[int, int],
) -> tuple[list[dict], list[dict]]:
    per_user = []
    per_department: dict[str, int] = {}
    for user in users:
        spend = spend_by_user.get(user.id, 0)
        per_user.append({
            "user_id": user.id,
            "phone": user.phone,
            "department": user.department,
            "spend_credits": spend,
            "balance": user.balance_credits,
            "frozen": user.frozen_credits,
            "tasks": tasks_by_user.get(user.id, {}),
            "real_tokens": tokens_by_user.get(user.id, 0),
        })
        department = user.department or "未分配"
        per_department[department] = per_department.get(department, 0) + spend
    per_user.sort(key=lambda row: -row["spend_credits"])
    department_rows = [
        {"department": department, "spend_credits": spend}
        for department, spend in sorted(
            per_department.items(),
            key=lambda item: -item[1],
        )
    ]
    return per_user, department_rows


def _daily_spend_rows(db: Session, daily_by_date: dict[str, int], start_dt, end_dt):
    query = (
        select(
            func.date(CreditTransaction.created_at),
            func.coalesce(func.sum(-CreditTransaction.change), 0),
        )
        .where(
            CreditTransaction.type.in_(_SYNC_TRANSACTION_TYPES),
            CreditTransaction.biz_type != "gen_task",
            _unrepresented_transaction_filter(),
        )
        .group_by(func.date(CreditTransaction.created_at))
        .order_by(func.date(CreditTransaction.created_at))
    )
    query = reverse_usage._filter_range(
        query,
        CreditTransaction.created_at,
        start_dt,
        end_dt,
    )
    for day, spend in db.execute(query).all():
        if day:
            key = str(day)
            daily_by_date[key] = max(
                0,
                daily_by_date.get(key, 0) + int(spend or 0),
            )
    return [
        {"date": day, "spend_credits": daily_by_date[day]}
        for day in sorted(daily_by_date)
    ]


def _usage_report_csv(per_user: list[dict]) -> StreamingResponse:
    def iter_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def emit(row):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(row)
            return buffer.getvalue()

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
        for row in per_user:
            yield emit([
                csv_cell(row["user_id"]),
                csv_cell(row["phone"]),
                csv_cell(row["department"]),
                csv_cell(row["spend_credits"]),
                csv_cell(row["balance"]),
                csv_cell(row["frozen"]),
                csv_cell(row["tasks"].get("image", 0)),
                csv_cell(row["tasks"].get("video", 0)),
                csv_cell(row["tasks"].get("reverse", 0)),
                csv_cell(row["real_tokens"]),
            ])

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_report.csv"},
    )


def build_usage_report(
    db: Session,
    *,
    start: str | None = None,
    end: str | None = None,
    format: str = "json",
):
    start_dt = parse_date(start)
    end_dt = parse_date(end, end_of_day=True)
    spend_by_user, daily_by_date = _collect_settled_spend(db, start_dt, end_dt)
    tasks_by_user = _collect_task_counts(db, start_dt, end_dt)
    tokens_by_user = _collect_tokens(db, start_dt, end_dt)
    per_user, per_department = _user_usage_rows(
        list(db.execute(select(User)).scalars()),
        spend_by_user,
        tasks_by_user,
        tokens_by_user,
    )
    daily = _daily_spend_rows(db, daily_by_date, start_dt, end_dt)
    if format == "csv":
        return _usage_report_csv(per_user)
    return {
        "per_user": per_user,
        "per_department": per_department,
        "daily": daily,
    }
