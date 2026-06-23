"""Admin backoffice: whitelist, users, quota grants, usage report, model config,
platform settings, audit log, gateway status."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import (
    AssetReport,
    AuditLog,
    CreditTransaction,
    GatewayCall,
    GenAsset,
    GenTask,
    ModelConfig,
    PhoneWhitelist,
    User,
)
from ..password_policy import MIN_PASSWORD_LEN
from ..schemas import (
    AdminAssetReportHandleIn,
    AdminTaskRefundIn,
    AdminTaskSettleIn,
    AssetReportOut,
    AuditOut,
    ModelConfigIn,
    ModelProbeIn,
    OnlineUpdateRunIn,
    OnlineUpdateRunOut,
    OnlineUpdateStatusOut,
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
from ..security import hash_password
from ..services import (
    audit,
    credits,
    gateway,
    generation,
    locks,
    online_update,
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
    model_to_admin_dict,
    normalise_base_url,
    runtime_config_from_probe,
)
from ..services.task_output import build_task_out
from . import admin_helpers as _admin_helpers
from .admin_helpers import IMAGE_SIZE_RE as _IMAGE_SIZE_RE
from .admin_helpers import age_minutes as _age_minutes
from .admin_helpers import assert_no_active_model_tasks as _assert_no_active_model_tasks
from .admin_helpers import assert_quota_grant_limits as _assert_quota_grant_limits
from .admin_helpers import csv_cell as _csv_cell
from .admin_helpers import date_key as _date_key
from .admin_helpers import gateway_update_changes_runtime as _gateway_update_changes_runtime
from .admin_helpers import model_audit_snapshot as _model_audit_snapshot
from .admin_helpers import model_gateway_update_values as _model_gateway_update_values
from .admin_helpers import page as _page
from .admin_helpers import parse_date as _parse_date
from .admin_helpers import payment_provider_audit_snapshot as _payment_provider_audit_snapshot
from .admin_helpers import remember_quota_grant_fingerprint as _remember_quota_grant_fingerprint
from .admin_helpers import replay_quota_grant as _replay_quota_grant
from .admin_helpers import require_admin_rate_limit as _require_admin_rate_limit
from .admin_helpers import reserve_quota_grant_idempotency as _reserve_quota_grant_idempotency
from .admin_helpers import setting_int as _setting_int

router = APIRouter(prefix="/api/admin", tags=["admin"])
router.dependencies.append(Depends(_require_admin_rate_limit))

_ADMIN_CONFIRM_FAIL_LIMIT = _admin_helpers.ADMIN_CONFIRM_FAIL_LIMIT
_ADMIN_CONFIRM_FAIL_IP_LIMIT = _admin_helpers.ADMIN_CONFIRM_FAIL_IP_LIMIT
_ADMIN_CONFIRM_FAIL_WINDOW_SECONDS = _admin_helpers.ADMIN_CONFIRM_FAIL_WINDOW_SECONDS


def _require_admin_password(admin: User, password: str | None, request: Request | None = None) -> None:
    """Compatibility bridge for tests/operators monkeypatching admin module constants."""
    _admin_helpers.ADMIN_CONFIRM_FAIL_LIMIT = _ADMIN_CONFIRM_FAIL_LIMIT
    _admin_helpers.ADMIN_CONFIRM_FAIL_IP_LIMIT = _ADMIN_CONFIRM_FAIL_IP_LIMIT
    _admin_helpers.ADMIN_CONFIRM_FAIL_WINDOW_SECONDS = _ADMIN_CONFIRM_FAIL_WINDOW_SECONDS
    _admin_helpers.require_admin_password(admin, password, request)


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
        generation.admin_settle_needs_review_task(
            db,
            task,
            result_url=(body.result_url or "").strip() or None,
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
                  "result_url": safe_logging.redact_url_for_log(body.result_url or ""),
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
    if body.status == "disabled" and user.is_admin:
        if user.id == admin.id:
            raise HTTPException(400, "不能禁用当前管理员账号")
        active_admin_count = db.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.is_admin.is_(True),
                User.status == "active",
                User.id != user.id,
            )
        ).scalar_one()
        if int(active_admin_count or 0) < 1:
            raise HTTPException(400, "至少需要保留一个可用管理员账号")
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
    replay = _replay_quota_grant(db, admin_id=admin.id, body=body, note=note)
    if replay is not None:
        return replay
    if not db.get(User, body.user_id):
        raise HTTPException(404, "用户不存在")
    lock_key = f"admin:quota:grant:{admin.id}"
    lock_token = locks.acquire(lock_key, ttl=30)
    if not lock_token:
        raise HTTPException(409, "额度发放正在处理中,请稍后重试")
    try:
        replay = _replay_quota_grant(db, admin_id=admin.id, body=body, note=note)
        if replay is not None:
            return replay
        _assert_quota_grant_limits(db, admin, body.amount)
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
    finally:
        locks.release(lock_key, lock_token)
    _remember_quota_grant_fingerprint(admin.id, body, note, idem_key)
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
    sla_minutes = max(1, _setting_int(db, "review_task_sla_minutes", 30))
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
        age_minutes = _age_minutes(row.created_at)
        item.update({
            "user_id": row.user_id,
            "phase": row.phase,
            "external_task_id": row.external_task_id,
            "external_submitted_at": row.external_submitted_at,
            "video_request_id": (row.params or {}).get("_video_request_id"),
            "age_minutes": age_minutes,
            "review_sla_minutes": sla_minutes,
            "review_overdue": age_minutes is not None and age_minutes >= sla_minutes,
        })
        out.append(item)
    return out


# --------------------------------------------------------------- asset reports
@router.get("/asset-reports", response_model=list[AssetReportOut])
def list_asset_reports(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    status: str = "open",
    limit: int = 50,
    offset: int = 0,
):
    if status not in {"open", "dismissed", "takedown", "all"}:
        raise HTTPException(400, "举报状态非法")
    limit, offset = _page(limit, offset, 200)
    q = select(AssetReport).order_by(AssetReport.id.desc())
    if status != "all":
        q = q.where(AssetReport.status == status)
    return list(db.execute(q.limit(limit).offset(offset)).scalars())


@router.post("/asset-reports/{report_id}/handle", response_model=AssetReportOut)
def handle_asset_report(
    report_id: int,
    body: AdminAssetReportHandleIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    report = db.get(AssetReport, report_id)
    if not report:
        raise HTTPException(404, "举报不存在")
    if report.status != "open":
        return report
    note = (body.note or "").strip()
    if body.action == "takedown":
        asset = db.get(GenAsset, report.asset_id) if report.asset_id else None
        if asset:
            asset.moderation_status = "takedown"
        report.status = "takedown"
    else:
        report.status = "dismissed"
    report.handled_by = admin.id
    report.handle_note = note or None
    report.handled_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)
    audit.log(
        db,
        user_id=admin.id,
        action="handle_asset_report",
        biz_type="asset_report",
        biz_id=report.id,
        ip=get_client_ip(request) if request else None,
        detail={
            "asset_id": report.asset_id,
            "action": body.action,
            "status": report.status,
            "note": note,
        },
    )
    return report


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
    row = db.execute(select(ModelConfig).where(ModelConfig.use == body.use)).scalar_one_or_none()
    before = _model_audit_snapshot(row)
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
    db.refresh(row)
    after = _model_audit_snapshot(row)
    audit.log(db, user_id=admin.id, action="update_model", biz_type="admin",
              ip=get_client_ip(request) if request else None,
              detail={
                  "use": body.use,
                  "before": before,
                  "after": after,
                  "api_key_changed": bool(body.api_key or body.api_key_clear),
              })
    return {"ok": True}


@router.post("/models/probe")
def probe_models(body: ModelProbeIn, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin), request: Request = None):
    fallback = None
    if body.use:
        fallback = db.execute(select(ModelConfig).where(ModelConfig.use == body.use)).scalar_one_or_none()
    requested_base = normalise_base_url(body.base_url)
    requested_host = (urlparse(requested_base).hostname or "").lower() if requested_base else ""
    if requested_host and requested_host in app_config.trusted_egress_host_list:
        raise HTTPException(400, "不能临时探测受信任内网网关,请保存配置后再探测")
    if requested_base and requested_base.lower().startswith("http://"):
        raise HTTPException(400, "不能临时探测非 HTTPS 网关,请使用 HTTPS Base URL")
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


# --------------------------------------------------------------- online update
@router.get("/update/status", response_model=OnlineUpdateStatusOut)
def online_update_status(check_remote: bool = False, _: User = Depends(require_admin)):
    return online_update.status(check_remote=check_remote)


@router.post("/update/run", response_model=OnlineUpdateRunOut)
def online_update_run(
    body: OnlineUpdateRunIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    try:
        result = online_update.run_update(apply=body.apply)
    except online_update.OnlineUpdateError as e:
        audit.log(
            db,
            user_id=admin.id,
            action="online_update_failed",
            biz_type="admin",
            ip=get_client_ip(request) if request else None,
            detail={"error": str(e)},
        )
        raise HTTPException(400, str(e)) from e
    audit.log(
        db,
        user_id=admin.id,
        action="online_update_run",
        biz_type="admin",
        ip=get_client_ip(request) if request else None,
        detail={
            "changed": result.get("changed"),
            "applied": result.get("applied"),
            "partial_failure": result.get("partial_failure"),
            "error": result.get("error"),
            "before": result.get("before"),
            "after": result.get("after"),
            "remote_head": result.get("remote_head"),
        },
    )
    return result


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
    if body.provider != provider:
        raise HTTPException(400, "路径支付渠道与请求体不一致")
    if body.mode == "mock" and not app_config.debug and body.enabled:
        raise HTTPException(400, "生产环境不允许启用 mock 支付")
    before = _payment_provider_audit_snapshot(payment_config.get_provider(db, provider))
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
            "before": before,
            "after": _payment_provider_audit_snapshot(row),
            "secret_keys_changed": sorted(
                key
                for key, value in (body.secret_config or {}).items()
                if key in payment_config.SECRET_FIELDS.get(provider, set())
                and str(value or "").strip()
                and str(value).strip() not in {"__keep__", "已配置"}
            ),
        },
    )
    return payment_config.provider_out(row)


@router.get("/payments/config")
def admin_payment_config(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.export_public_status(db)
