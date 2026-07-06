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
from .admin_helpers import csv_cell as _csv_cell
from .admin_helpers import date_key as _date_key
from .admin_helpers import parse_date as _parse_date

router = APIRouter()


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
