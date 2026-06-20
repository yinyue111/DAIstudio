"""Admin backoffice: whitelist, users, quota grants, usage report, model config,
platform settings, audit log, gateway status."""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    AdminIdempotencyKey,
    AuditLog,
    CreditTransaction,
    GatewayCall,
    GenTask,
    ModelConfig,
    PhoneWhitelist,
    User,
)
from ..password_policy import MIN_PASSWORD_LEN
from ..redis_client import redis_client
from ..schemas import (
    AdminTaskRefundIn,
    AdminTaskSettleIn,
    AuditOut,
    ModelConfigIn,
    ModelProbeIn,
    PaymentPackageDisableIn,
    PaymentPackageIn,
    PaymentPackageOut,
    PaymentProviderConfigIn,
    PaymentProviderConfigOut,
    QuotaGrantIn,
    ResetPasswordIn,
    SettingsIn,
    UserOut,
    UserStatusIn,
    WhitelistDeleteIn,
    WhitelistIn,
)
from ..security import hash_password, verify_password
from ..services import (
    audit,
    credits,
    gateway,
    generation,
    payment_config,
    payments,
    safe_logging,
    sms,
)
from ..services.config_store import DEFAULT_SETTINGS, get_setting, set_settings
from ..services.model_gateway_config import (
    PROVIDER_PRESETS,
    ModelGatewayConfigError,
    apply_model_gateway_update,
    encrypted_key_present,
    model_to_admin_dict,
    normalise_base_url,
    normalise_gateway_format,
    normalise_provider,
    runtime_config_from_probe,
)
from ..services.task_output import build_task_out

router = APIRouter(prefix="/api/admin", tags=["admin"])
_IMAGE_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_ADMIN_CONFIRM_TTL_SECONDS = 300
_ADMIN_CONFIRM_FAIL_LIMIT = 8
_ADMIN_CONFIRM_FAIL_IP_LIMIT = 30
_ADMIN_CONFIRM_FAIL_WINDOW_SECONDS = 15 * 60
_QUOTA_GRANT_REPLAY_WINDOW_SECONDS = 10 * 60
_ACTIVE_MODEL_TASK_STATUSES = ("queued", "running", generation.NEEDS_REVIEW)


def _model_gateway_update_values(row: ModelConfig, body: ModelConfigIn) -> tuple[
    str | None,
    str | None,
    str | None,
]:
    fields = getattr(body, "model_fields_set", set())
    provider = body.provider if "provider" in fields else row.provider
    base_url = body.base_url if "base_url" in fields else row.base_url
    gateway_format = body.gateway_format if "gateway_format" in fields else row.gateway_format
    return provider, base_url, gateway_format


def _page(limit: int, offset: int, cap: int) -> tuple[int, int]:
    return min(max(int(limit), 1), cap), max(int(offset), 0)


def _gateway_update_changes_runtime(row: ModelConfig, body: ModelConfigIn) -> bool:
    provider, base_url, gateway_format = _model_gateway_update_values(row, body)
    current_provider = normalise_provider(row.provider)
    current_base_url = normalise_base_url(row.base_url)
    current_gateway_format = normalise_gateway_format(row.gateway_format, current_provider, row.use)
    next_provider = normalise_provider(provider)
    next_base_url = normalise_base_url(base_url)
    next_gateway_format = normalise_gateway_format(gateway_format, next_provider, row.use)
    current_key_present = encrypted_key_present(row)
    new_key_supplied = body.api_key not in (None, "", "__keep__", "已配置")
    next_key_present = False if body.api_key_clear else (True if new_key_supplied else current_key_present)
    return (
        current_provider != next_provider
        or current_base_url != next_base_url
        or current_gateway_format != next_gateway_format
        or current_key_present != next_key_present
        or new_key_supplied
    )


def _assert_no_active_model_tasks(db: Session, use: str) -> None:
    active_id = db.execute(
        select(GenTask.id)
        .where(
            GenTask.model_use == use,
            GenTask.status.in_(_ACTIVE_MODEL_TASK_STATUSES),
        )
        .limit(1)
    ).scalar_one_or_none()
    if active_id is not None:
        raise HTTPException(
            409,
            "当前模型仍有排队、运行中或待对账任务,请等待任务结束或处理后再切换网关配置",
        )


def _parse_date(s: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"日期格式应为 ISO(YYYY-MM-DD),收到:{s}")
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    # gen_tasks.finished_at / created_at are tz-aware; a naive bound would raise
    # on PostgreSQL ("can't compare offset-naive and offset-aware"). Assume UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    if text.lstrip(" \t\r\n").startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def _date_key(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.date().isoformat()


def _require_admin_password(admin: User, password: str | None, request: Request | None = None) -> None:
    ip = get_client_ip(request) if request else "unknown"
    fail_key = f"admin:confirm:fail:{admin.id}"
    ip_key = f"admin:confirm:failip:{ip}"
    if int(redis_client.get(fail_key) or 0) >= _ADMIN_CONFIRM_FAIL_LIMIT:
        raise HTTPException(429, "管理员密码确认失败次数过多,请稍后再试")
    if int(redis_client.get(ip_key) or 0) >= _ADMIN_CONFIRM_FAIL_IP_LIMIT:
        raise HTTPException(429, "管理员密码确认失败次数过多,请稍后再试")
    if not password or not verify_password(password, admin.password_hash):
        for key in (fail_key, ip_key):
            n = redis_client.incr(key)
            if n == 1:
                redis_client.expire(key, _ADMIN_CONFIRM_FAIL_WINDOW_SECONDS)
        raise HTTPException(403, "请重新输入管理员密码确认该高危操作")
    redis_client.delete(fail_key)


def _quota_grant_idempotency_raw(body: QuotaGrantIn) -> str:
    raw = (body.idempotency_key or "").strip()
    if not raw:
        raise HTTPException(400, "idempotency_key 必填")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", raw):
        raise HTTPException(400, "idempotency_key 格式非法")
    return raw


def _reserve_quota_grant_idempotency(
    db: Session,
    *,
    admin_id: int,
    body: QuotaGrantIn,
    note: str,
) -> tuple[str, bool]:
    raw = _quota_grant_idempotency_raw(body)
    try:
        db.add(
            AdminIdempotencyKey(
                admin_id=admin_id,
                scope="quota_grant",
                key=raw,
                target_user_id=body.user_id,
                amount=body.amount,
                note=note,
            )
        )
        db.flush()
    except IntegrityError as e:
        db.rollback()
        existing = db.execute(
            select(AdminIdempotencyKey).where(
                AdminIdempotencyKey.admin_id == admin_id,
                AdminIdempotencyKey.scope == "quota_grant",
                AdminIdempotencyKey.key == raw,
            )
        ).scalar_one_or_none()
        if not existing:
            raise HTTPException(409, "重复的额度发放请求已拦截,请更换幂等键") from e
        if (
            int(existing.target_user_id or 0) == int(body.user_id)
            and int(existing.amount or 0) == int(body.amount)
            and (existing.note or "") == note
        ):
            return raw, False
        raise HTTPException(409, "幂等键已用于不同额度发放请求") from e
    return raw, True


def _quota_grant_fingerprint(admin_id: int, body: QuotaGrantIn, note: str) -> str:
    return f"admin:quota:fingerprint:{admin_id}:{body.user_id}:{body.amount}:{note}"


# ----------------------------------------------------------------- whitelist
@router.get("/whitelist")
def list_whitelist(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = list(db.execute(select(PhoneWhitelist).order_by(PhoneWhitelist.created_at.desc())).scalars())
    return [
        {"phone": r.phone, "note": r.note, "department": r.department,
         "created_at": r.created_at}
        for r in rows
    ]


@router.post("/whitelist")
def add_whitelist(body: WhitelistIn, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password, request)
    if db.get(PhoneWhitelist, body.phone):
        raise HTTPException(409, "该手机号已在白名单")
    db.add(PhoneWhitelist(phone=body.phone, note=body.note,
                          department=body.department, added_by=admin.id))
    db.commit()
    audit.log(db, user_id=admin.id, action="add_whitelist", biz_type="admin",
              biz_id=None, ip=get_client_ip(request) if request else None,
              detail={"phone": body.phone, "department": body.department})
    return {"ok": True}


@router.post("/tasks/{task_id}/refund_review", response_model=object)
def refund_needs_review_task(
    task_id: int,
    body: AdminTaskRefundIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    task = db.get(GenTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status == generation.NEEDS_REVIEW)
        .values(status="running", phase="reconciling", error=None)
    ).rowcount
    if (claimed or 0) != 1:
        db.rollback()
        db.refresh(task)
        return build_task_out(db, task)
    db.refresh(task)
    if task.cost_frozen and task.cost_settled == 0:
        try:
            credits.refund(db, task.user_id, task.cost_frozen, biz_ref=task.id, commit=False)
        except credits.InsufficientCredits as e:
            db.rollback()
            raise HTTPException(400, str(e))
    note = (body.note or "").strip()
    task.status = "failed"
    task.error = note or "管理员对账后已退款"
    task.phase = None
    task.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    audit.log(db, user_id=admin.id, action="refund_review_task", biz_type="gen_task",
              biz_id=task.id, ip=get_client_ip(request) if request else None,
              detail={"target_user_id": task.user_id, "note": note})
    return build_task_out(db, task)


@router.post("/tasks/{task_id}/settle_review", response_model=object)
def settle_needs_review_task(
    task_id: int,
    body: AdminTaskSettleIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    task = db.get(GenTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status == generation.NEEDS_REVIEW)
        .values(status="running", phase="reconciling", error=None, finished_at=None)
    ).rowcount
    if (claimed or 0) != 1:
        db.rollback()
        db.refresh(task)
        return build_task_out(db, task)
    db.refresh(task)
    try:
        generation.admin_settle_needs_review_video(
            db,
            task,
            result_url=body.result_url.strip(),
            external_task_id=(body.external_task_id or "").strip() or None,
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise HTTPException(400, f"补结果结算失败:{e}")
    db.refresh(task)
    note = (body.note or "").strip()
    audit.log(db, user_id=admin.id, action="settle_review_task", biz_type="gen_task",
              biz_id=task.id, ip=get_client_ip(request) if request else None,
              detail={
                  "target_user_id": task.user_id,
                  "result_url": safe_logging.redact_url_for_log(body.result_url),
                  "external_task_id": body.external_task_id,
                  "note": note,
              })
    return build_task_out(db, task)


@router.delete("/whitelist/{phone}")
def remove_whitelist(phone: str, db: Session = Depends(get_db),
                     body: WhitelistDeleteIn | None = None,
                     admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password if body else None, request)
    row = db.get(PhoneWhitelist, phone)
    if not row:
        raise HTTPException(404, "不存在")
    db.delete(row)
    db.commit()
    audit.log(db, user_id=admin.id, action="remove_whitelist", biz_type="admin",
              biz_id=None, ip=get_client_ip(request) if request else None,
              detail={"phone": phone})
    return {"ok": True}


# --------------------------------------------------------------------- users
@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return list(db.execute(select(User).order_by(User.id.desc())).scalars())


@router.patch("/users/{user_id}/status")
def set_user_status(user_id: int, body: UserStatusIn, db: Session = Depends(get_db),
                    admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password, request)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.status != body.status:
        user.status = body.status
        user.token_version += 1
    db.commit()
    audit.log(db, user_id=admin.id, action="set_user_status", biz_type="admin",
              biz_id=user_id, ip=get_client_ip(request) if request else None,
              detail={"status": body.status})
    return {"ok": True}


@router.post("/users/{user_id}/reset_password")
def reset_password(user_id: int, body: ResetPasswordIn, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password, request)
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PASSWORD_LEN} 位")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    user.password_hash = hash_password(body.password)
    user.token_version += 1  # force re-login on all devices
    db.commit()
    audit.log(db, user_id=admin.id, action="reset_password", biz_type="admin",
              biz_id=user_id, ip=get_client_ip(request) if request else None,
              detail={"target_user_id": user_id})
    return {"ok": True}


# --------------------------------------------------------------------- quota
@router.post("/quota/grant", response_model=UserOut)
def grant_quota(body: QuotaGrantIn, db: Session = Depends(get_db),
                admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password, request)
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(400, "请填写额度发放原因")
    if body.amount > 100000:
        raise HTTPException(400, "单次发放额度不能超过 100000")
    if not db.get(User, body.user_id):
        raise HTTPException(404, "用户不存在")
    fingerprint_key = _quota_grant_fingerprint(admin.id, body, note)
    previous_key = redis_client.get(fingerprint_key)
    if previous_key and previous_key != (body.idempotency_key or "").strip():
        existing = db.execute(
            select(AdminIdempotencyKey).where(
                AdminIdempotencyKey.admin_id == admin.id,
                AdminIdempotencyKey.scope == "quota_grant",
                AdminIdempotencyKey.key == str(previous_key),
                AdminIdempotencyKey.target_user_id == body.user_id,
                AdminIdempotencyKey.amount == body.amount,
                AdminIdempotencyKey.note == note,
            )
        ).scalar_one_or_none()
        if existing:
            user = db.get(User, body.user_id)
            if not user:
                raise HTTPException(404, "用户不存在")
            return user
    idem_key, created_idem = _reserve_quota_grant_idempotency(
        db,
        admin_id=admin.id,
        body=body,
        note=note,
    )
    if not created_idem:
        user = db.get(User, body.user_id)
        if not user:
            raise HTTPException(404, "用户不存在")
        return user
    try:
        user = credits.grant(db, body.user_id, body.amount, note=note, commit=False)
        db.commit()
        db.refresh(user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    redis_client.setex(fingerprint_key, _QUOTA_GRANT_REPLAY_WINDOW_SECONDS, idem_key)
    audit.log(db, user_id=admin.id, action="grant_quota", biz_type="admin",
              biz_id=body.user_id, ip=get_client_ip(request) if request else None,
              detail={"amount": body.amount, "note": note, "idempotency_key": idem_key})
    return user


@router.get("/tasks/review")
def list_review_tasks(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    limit: int = 50,
    offset: int = 0,
):
    limit, offset = _page(limit, offset, 200)
    rows = list(
        db.execute(
            select(GenTask)
            .where(GenTask.status == generation.NEEDS_REVIEW)
            .order_by(GenTask.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    out = []
    for row in rows:
        item = build_task_out(db, row).model_dump()
        item.update({
            "user_id": row.user_id,
            "phase": row.phase,
            "external_task_id": row.external_task_id,
            "external_submitted_at": row.external_submitted_at,
            "video_request_id": (row.params or {}).get("_video_request_id"),
        })
        out.append(item)
    return out


# -------------------------------------------------------------- usage report
@router.get("/usage/report")
def usage_report(db: Session = Depends(get_db), _: User = Depends(require_admin),
                 start: str | None = None, end: str | None = None,
                 format: str = "json"):
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
        select(GatewayCall.user_id,
               func.coalesce(func.sum(GatewayCall.total_tokens), 0))
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
            "user_id": u.id, "phone": u.phone, "department": u.department,
            "spend_credits": spend, "balance": u.balance_credits,
            "frozen": u.frozen_credits, "tasks": tasks_by_user.get(u.id, {}),
            "real_tokens": tokens_by_user.get(u.id, 0),
        })
        dept = u.department or "未分配"
        per_dept[dept] = per_dept.get(dept, 0) + spend

    per_user.sort(key=lambda x: -x["spend_credits"])
    per_dept_list = [{"department": k, "spend_credits": v}
                     for k, v in sorted(per_dept.items(), key=lambda x: -x[1])]

    # Daily trend: terminal task spend + synchronous transaction spend.
    daily_q = (
        select(func.date(CreditTransaction.created_at),
               func.coalesce(func.sum(-CreditTransaction.change), 0))
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
    daily = [{"date": d, "spend_credits": daily_by_date[d]}
             for d in sorted(daily_by_date)]

    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "phone", "department", "spend_credits", "balance",
                    "frozen", "image_tasks", "video_tasks", "real_tokens"])
        for r in per_user:
            w.writerow([_csv_cell(r["user_id"]), _csv_cell(r["phone"]),
                        _csv_cell(r["department"]), _csv_cell(r["spend_credits"]),
                        _csv_cell(r["balance"]), _csv_cell(r["frozen"]),
                        _csv_cell(r["tasks"].get("image", 0)),
                        _csv_cell(r["tasks"].get("video", 0)),
                        _csv_cell(r["real_tokens"])])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=usage_report.csv"},
        )

    return {"per_user": per_user, "per_department": per_dept_list, "daily": daily}


# ------------------------------------------------------------- model config
@router.get("/models")
def get_models(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = list(db.execute(select(ModelConfig).order_by(ModelConfig.use)).scalars())
    return {
        "providers": PROVIDER_PRESETS,
        "models": [model_to_admin_dict(r) for r in rows],
    }


@router.put("/models")
def upsert_model(body: ModelConfigIn, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password, request)
    row = db.execute(select(ModelConfig).where(ModelConfig.use == body.use)).scalar_one_or_none()
    if row is None:
        row = ModelConfig(use=body.use)
        db.add(row)
    elif _gateway_update_changes_runtime(row, body):
        _assert_no_active_model_tasks(db, body.use)
    provider, base_url, gateway_format = _model_gateway_update_values(row, body)
    try:
        apply_model_gateway_update(
            row,
            provider=provider,
            base_url=base_url,
            api_key=body.api_key,
            api_key_clear=body.api_key_clear,
            gateway_format=gateway_format,
        )
    except ModelGatewayConfigError as e:
        raise HTTPException(400, str(e)) from e
    row.model_id = body.model_id
    row.cost_credits = body.cost_credits
    row.unlock_cost = body.unlock_cost
    row.enabled = body.enabled
    row.extra = body.extra
    db.commit()
    audit.log(db, user_id=admin.id, action="update_model", biz_type="admin",
              ip=get_client_ip(request) if request else None,
              detail={"use": body.use, "model_id": body.model_id,
                      "provider": body.provider, "base_url": body.base_url,
                      "gateway_format": body.gateway_format,
                      "api_key_changed": bool(body.api_key or body.api_key_clear),
                      "cost_credits": body.cost_credits,
                      "unlock_cost": body.unlock_cost,
                      "enabled": body.enabled})
    return {"ok": True}


@router.post("/models/probe")
def probe_models(body: ModelProbeIn, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin), request: Request = None):
    fallback = None
    if body.use:
        fallback = db.execute(select(ModelConfig).where(ModelConfig.use == body.use)).scalar_one_or_none()
    if fallback is not None and fallback.api_key_encrypted and not body.api_key:
        _require_admin_password(admin, body.admin_password, request)
    try:
        cfg = runtime_config_from_probe(
            use=body.use,
            provider=body.provider,
            base_url=body.base_url,
            api_key=body.api_key,
            gateway_format=body.gateway_format,
            fallback_row=fallback,
        )
        models = gateway.list_models(cfg)
    except (ModelGatewayConfigError, gateway.GatewayError) as e:
        raise HTTPException(400, str(e)) from e
    return {
        "provider": cfg.provider,
        "base_url": cfg.base_url,
        "gateway_format": cfg.gateway_format,
        "models": models,
    }


# ------------------------------------------------------------ platform settings
@router.get("/settings")
def get_settings(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password, request)
    changed = {}
    for key in DEFAULT_SETTINGS:
        val = getattr(body, key, None)
        if val is not None:
            if key == "image_size":
                m = _IMAGE_SIZE_RE.match(str(val))
                if not m or not (
                    0 < int(m.group(1)) <= app_config.max_image_dim
                    and 0 < int(m.group(2)) <= app_config.max_image_dim
                ):
                    raise HTTPException(400, f"默认图片尺寸非法(最大 {app_config.max_image_dim}px)")
            changed[key] = val
    if changed.get("sms_auth_enabled") is True:
        sms_issues = sms.readiness_issues()
        if sms_issues:
            raise HTTPException(400, "短信验证码注册未就绪: " + "; ".join(sms_issues))
    if changed.get("payment_enabled") is True:
        payment_status = payment_config.export_public_status(db)
        ready = any(
            provider.get("enabled")
            and provider.get("ready")
            and (
                provider.get("mode") != "mock"
                or payments.mock_payments_allowed()
            )
            for provider in payment_status.get("providers", [])
        )
        if not ready:
            raise HTTPException(400, "支付充值功能未就绪: 请先配置至少一个可用支付渠道")
    if changed:
        set_settings(db, changed)
    audit.log(db, user_id=admin.id, action="update_settings", biz_type="admin",
              ip=get_client_ip(request) if request else None, detail=changed)
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}


# ------------------------------------------------------------------ audit log
@router.get("/audit", response_model=list[AuditOut])
def list_audit(db: Session = Depends(get_db), _: User = Depends(require_admin),
               action: str | None = None, user_id: int | None = None,
               limit: int = 50, offset: int = 0):
    q = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        q = q.where(AuditLog.action == action)
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    limit, offset = _page(limit, offset, 200)
    q = q.limit(limit).offset(offset)
    return list(db.execute(q).scalars())


# ------------------------------------------------------------- gateway status
@router.get("/gateway")
def gateway_status(_: User = Depends(require_admin)):
    """Read-only view of gateway config. The api_key is masked; the full key
    never leaves the server."""
    def _mask(k: str) -> str:
        return (k[:6] + "…" + k[-4:]) if len(k) > 12 else ("已配置" if k else "")

    return {
        "base_url": app_config.gateway_base_url,
        "api_key_masked": _mask(app_config.gateway_api_key or ""),
        "mock_mode": app_config.effective_mock_mode,
        "timeout_seconds": app_config.gateway_timeout_seconds,
        "max_retries": app_config.gateway_max_retries,
        "image": {
            "timeout_seconds": app_config.image_gateway_timeout_seconds,
            "download_timeout_seconds": app_config.image_download_timeout_seconds,
            "parallelism": app_config.image_gateway_parallelism,
        },
        "video": {
            "base_url": app_config.video_base,
            "api_key_masked": _mask(app_config.video_key or ""),
            "format": app_config.video_gateway_format,
            "mock_mode": app_config.effective_video_mock,
            "submit_timeout_seconds": app_config.video_submit_timeout_seconds,
            "poll_max_seconds": app_config.video_poll_max_seconds,
            "poll_interval_seconds": app_config.video_poll_interval_seconds,
        },
    }


# --------------------------------------------------------------- payment config
@router.get("/payments/packages", response_model=list[PaymentPackageOut])
def admin_payment_packages(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    payment_config.seed_defaults(db)
    return [
        payment_config.package_to_dict(p)
        for p in payment_config.list_packages(db, enabled_only=False)
    ]


@router.post("/payments/packages", response_model=PaymentPackageOut)
def admin_upsert_payment_package(
    body: PaymentPackageIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    row = payment_config.upsert_package(db, body.model_dump())
    audit.log(
        db,
        user_id=admin.id,
        action="upsert_payment_package",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail=payment_config.package_to_dict(row),
    )
    return payment_config.package_to_dict(row)


@router.delete("/payments/packages/{package_id}")
def admin_disable_payment_package(
    package_id: str,
    body: PaymentPackageDisableIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    try:
        payment_config.delete_package(db, package_id)
    except payment_config.PaymentConfigError as e:
        raise HTTPException(404, str(e))
    audit.log(
        db,
        user_id=admin.id,
        action="disable_payment_package",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail={"package_id": package_id},
    )
    return {"ok": True}


@router.get("/payments/providers", response_model=list[PaymentProviderConfigOut])
def admin_payment_providers(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.list_providers_with_runtime(db)


@router.put("/payments/providers/{provider}", response_model=PaymentProviderConfigOut)
def admin_save_payment_provider(
    provider: str,
    body: PaymentProviderConfigIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    if body.provider != provider:
        raise HTTPException(400, "路径支付渠道与请求体不一致")
    if body.mode == "mock" and not app_config.debug and body.enabled:
        raise HTTPException(400, "生产环境不允许启用 mock 支付")
    try:
        row = payment_config.save_provider(
            db,
            provider=provider,
            enabled=body.enabled,
            mode=body.mode,
            public_config=body.public_config,
            secret_config=body.secret_config,
        )
    except payment_config.PaymentConfigError as e:
        raise HTTPException(400, str(e))
    safe_public_config = payment_config.public_config_for_provider(provider, body.public_config)
    audit.log(
        db,
        user_id=admin.id,
        action="update_payment_provider",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail={
            "provider": provider,
            "enabled": body.enabled,
            "mode": body.mode,
            "public_config": safe_public_config,
            "secret_keys": sorted(
                key
                for key in (body.secret_config or {})
                if key in payment_config.SECRET_FIELDS.get(provider, set())
            ),
        },
    )
    return payment_config.provider_out(row)


@router.get("/payments/config")
def admin_payment_config(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.export_public_status(db)
