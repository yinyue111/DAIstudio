"""Admin backoffice: whitelist, users, quota grants, usage report, model config,
platform settings, audit log, gateway status."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    AuditLog,
    CreditTransaction,
    GatewayCall,
    GenTask,
    ModelConfig,
    PhoneWhitelist,
    User,
)
from ..redis_client import redis_client
from ..schemas import (
    AuditOut,
    ModelConfigIn,
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
from ..services import audit, credits, payment_config
from ..services.config_store import DEFAULT_SETTINGS, get_setting, set_settings

router = APIRouter(prefix="/api/admin", tags=["admin"])
_IMAGE_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_ADMIN_CONFIRM_TTL_SECONDS = 300
_ADMIN_CONFIRM_FAIL_LIMIT = 8
_ADMIN_CONFIRM_FAIL_IP_LIMIT = 30
_ADMIN_CONFIRM_FAIL_WINDOW_SECONDS = 15 * 60


def _page(limit: int, offset: int, cap: int) -> tuple[int, int]:
    return min(max(int(limit), 1), cap), max(int(offset), 0)


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"日期格式应为 ISO(YYYY-MM-DD),收到:{s}")
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


def _grant_idempotency_key(admin_id: int, body: QuotaGrantIn) -> str:
    raw = (body.idempotency_key or "").strip()
    if raw:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", raw):
            raise HTTPException(400, "idempotency_key 格式非法")
        return f"admin:quota:{admin_id}:{raw}"
    basis = f"{body.user_id}:{body.amount}:{(body.note or '').strip()}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return f"admin:quota:{admin_id}:auto:{digest}"


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
    if len(body.password) < 10:
        raise HTTPException(400, "密码至少 10 位")
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
    idem_key = _grant_idempotency_key(admin.id, body)
    if not redis_client.set(idem_key, "1", nx=True, ex=_ADMIN_CONFIRM_TTL_SECONDS):
        raise HTTPException(409, "重复的额度发放请求已拦截,请稍后再试或更换幂等键")
    try:
        user = credits.grant(db, body.user_id, body.amount, note=note)
    except ValueError as e:
        redis_client.delete(idem_key)
        raise HTTPException(400, str(e))
    audit.log(db, user_id=admin.id, action="grant_quota", biz_type="admin",
              biz_id=body.user_id, ip=get_client_ip(request) if request else None,
              detail={"amount": body.amount, "note": note, "idempotency_key": body.idempotency_key})
    return user


# -------------------------------------------------------------- usage report
@router.get("/usage/report")
def usage_report(db: Session = Depends(get_db), _: User = Depends(require_admin),
                 start: str | None = None, end: str | None = None,
                 format: str = "json"):
    """Per-user / per-department spend. Optional ISO date range (filters on
    settled task finish time + unlock time). ``format=csv`` streams a CSV."""
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    # Credit spend is reconstructed from immutable credit_transactions, not
    # gen_tasks. A generation's real cost is the net of freeze(-reserved) plus
    # settle/refund(+unused/+all). This survives task retention cleanup.
    spend_tx_types = ("freeze", "settle", "refund", "unlock", "consume")
    spend_q = (
        select(
            CreditTransaction.user_id,
            func.coalesce(func.sum(-CreditTransaction.change), 0),
        )
        .where(CreditTransaction.type.in_(spend_tx_types))
        .group_by(CreditTransaction.user_id)
    )
    if start_dt:
        spend_q = spend_q.where(CreditTransaction.created_at >= start_dt)
    if end_dt:
        spend_q = spend_q.where(CreditTransaction.created_at <= end_dt)
    spend_by_user = {
        uid: max(0, int(total))
        for uid, total in db.execute(spend_q).all()
    }

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

    # daily spend trend from the same durable transaction facts
    daily_q = (
        select(func.date(CreditTransaction.created_at),
               func.coalesce(func.sum(-CreditTransaction.change), 0))
        .where(CreditTransaction.type.in_(spend_tx_types))
        .group_by(func.date(CreditTransaction.created_at))
        .order_by(func.date(CreditTransaction.created_at))
    )
    if start_dt:
        daily_q = daily_q.where(CreditTransaction.created_at >= start_dt)
    if end_dt:
        daily_q = daily_q.where(CreditTransaction.created_at <= end_dt)
    daily_by_date = {str(d): max(0, int(s)) for d, s in db.execute(daily_q).all() if d}
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
    return [
        {"use": r.use, "model_id": r.model_id, "cost_credits": r.cost_credits,
         "unlock_cost": r.unlock_cost, "enabled": r.enabled, "extra": r.extra}
        for r in rows
    ]


@router.put("/models")
def upsert_model(body: ModelConfigIn, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin), request: Request = None):
    _require_admin_password(admin, body.admin_password, request)
    row = db.execute(select(ModelConfig).where(ModelConfig.use == body.use)).scalar_one_or_none()
    if row is None:
        row = ModelConfig(use=body.use)
        db.add(row)
    row.model_id = body.model_id
    row.cost_credits = body.cost_credits
    row.unlock_cost = body.unlock_cost
    row.enabled = body.enabled
    row.extra = body.extra
    db.commit()
    audit.log(db, user_id=admin.id, action="update_model", biz_type="admin",
              ip=get_client_ip(request) if request else None,
              detail={"use": body.use, "model_id": body.model_id,
                      "cost_credits": body.cost_credits,
                      "unlock_cost": body.unlock_cost,
                      "enabled": body.enabled})
    return {"ok": True}


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
    return payment_config.list_providers(db)


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
            "public_config": body.public_config,
            "secret_keys": sorted((body.secret_config or {}).keys()),
        },
    )
    return payment_config.provider_out(row)


@router.get("/payments/config")
def admin_payment_config(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.export_public_status(db)
