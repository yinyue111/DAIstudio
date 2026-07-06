"""Admin quota grant endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import AdminIdempotencyKey, User
from ..schemas import (
    QuotaBulkGrantIn,
    QuotaBulkGrantItemOut,
    QuotaBulkGrantOut,
    QuotaGrantIn,
    UserOut,
)
from ..services import audit, credits, locks
from .admin_helpers import assert_quota_grant_limits as _assert_quota_grant_limits
from .admin_helpers import quota_bulk_grant_item_key as _quota_bulk_grant_item_key
from .admin_helpers import remember_quota_grant_fingerprint as _remember_quota_grant_fingerprint
from .admin_helpers import replay_quota_grant as _replay_quota_grant
from .admin_helpers import require_admin_password as _require_admin_password
from .admin_helpers import (
    reserve_quota_bulk_grant_idempotency as _reserve_quota_bulk_grant_idempotency,
)
from .admin_helpers import reserve_quota_grant_idempotency as _reserve_quota_grant_idempotency

router = APIRouter()


@router.post("/quota/grant", response_model=UserOut)
def grant_quota(
    body: QuotaGrantIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
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
            raise HTTPException(400, str(e)) from e
    finally:
        locks.release(lock_key, lock_token)
    _remember_quota_grant_fingerprint(admin.id, body, note, idem_key)
    audit.log(
        db,
        user_id=admin.id,
        action="grant_quota",
        biz_type="admin",
        biz_id=body.user_id,
        ip=get_client_ip(request) if request else None,
        detail={"amount": body.amount, "note": note, "idempotency_key": idem_key},
    )
    return user


@router.post("/quota/bulk-grant", response_model=QuotaBulkGrantOut)
def bulk_grant_quota(
    body: QuotaBulkGrantIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    granted: list[QuotaBulkGrantItemOut] = []
    failed: list[QuotaBulkGrantItemOut] = []
    lock_key = f"admin:quota:grant:{admin.id}"
    lock_token = locks.acquire(lock_key, ttl=30)
    if not lock_token:
        raise HTTPException(409, "额度发放正在处理中,请稍后重试")
    try:
        idem_key, created_request = _reserve_quota_bulk_grant_idempotency(
            db,
            admin_id=admin.id,
            body=body,
        )
        for index, item in enumerate(body.items):
            note = (item.note or "").strip()
            if not note:
                failed.append(QuotaBulkGrantItemOut(user_id=item.user_id, ok=False, error="请填写额度发放原因"))
                continue
            item_key = _quota_bulk_grant_item_key(idem_key, index, item)
            existing = db.execute(
                select(AdminIdempotencyKey).where(
                    AdminIdempotencyKey.admin_id == admin.id,
                    AdminIdempotencyKey.scope == "quota_grant",
                    AdminIdempotencyKey.key == item_key,
                )
            ).scalar_one_or_none()
            if existing:
                user = db.get(User, item.user_id)
                if user:
                    granted.append(
                        QuotaBulkGrantItemOut(
                            user_id=item.user_id,
                            ok=True,
                            balance_credits=int(user.balance_credits or 0),
                        )
                    )
                else:
                    failed.append(QuotaBulkGrantItemOut(user_id=item.user_id, ok=False, error="用户不存在"))
                continue
            if not created_request:
                failed.append(
                    QuotaBulkGrantItemOut(
                        user_id=item.user_id,
                        ok=False,
                        error="该批量请求已处理且此项未发放,请使用新的幂等键重试",
                    )
                )
                continue
            if not db.get(User, item.user_id):
                failed.append(QuotaBulkGrantItemOut(user_id=item.user_id, ok=False, error="用户不存在"))
                continue
            try:
                _assert_quota_grant_limits(db, admin, item.amount)
                db.add(
                    AdminIdempotencyKey(
                        admin_id=admin.id,
                        scope="quota_grant",
                        key=item_key,
                        target_user_id=item.user_id,
                        amount=item.amount,
                        note=note,
                    )
                )
                user = credits.grant(db, item.user_id, item.amount, note=note, commit=False)
                granted.append(
                    QuotaBulkGrantItemOut(
                        user_id=item.user_id,
                        ok=True,
                        balance_credits=int(user.balance_credits or 0),
                    )
                )
            except (ValueError, credits.InsufficientCredits, HTTPException) as e:
                message = str(getattr(e, "detail", None) or e)
                failed.append(QuotaBulkGrantItemOut(user_id=item.user_id, ok=False, error=message[:160]))
        db.commit()
    finally:
        locks.release(lock_key, lock_token)
    audit.log(
        db,
        user_id=admin.id,
        action="bulk_grant_quota",
        biz_type="admin",
        biz_id=None,
        ip=get_client_ip(request) if request else None,
        detail={
            "granted": [item.model_dump() for item in granted],
            "failed": [item.model_dump() for item in failed],
            "idempotency_key": body.idempotency_key,
        },
    )
    return QuotaBulkGrantOut(granted=granted, failed=failed)
