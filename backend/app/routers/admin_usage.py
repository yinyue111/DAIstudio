"""Admin usage reporting endpoints."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..models import CreditTransaction, GatewayCall, GenTask, User
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


def _detail_cost_credits(detail: dict | None) -> int:
    if not isinstance(detail, dict):
        return 0
    for key in ("estimated_cost_credits", "cost_credits", "credits", "real_cost_credits"):
        value = detail.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


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

    # Synchronous model/asset charges stay anchored to their transaction date.
    # Exclude gen_task freeze/settle/refund rows; they are represented above by
    # GenTask.cost_settled on the finished_at date.
    sync_tx_types = ("unlock", "consume")
    spend_q = (
        select(
            CreditTransaction.user_id,
            func.coalesce(func.sum(-CreditTransaction.change), 0),
        )
        .where(
            CreditTransaction.type.in_(sync_tx_types),
            CreditTransaction.biz_type != "gen_task",
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
                    _csv_cell(r["real_tokens"]),
                ])

        return StreamingResponse(
            iter_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=usage_report.csv"},
        )

    return {"per_user": per_user, "per_department": per_dept_list, "daily": daily}
