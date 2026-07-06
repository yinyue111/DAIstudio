"""Admin user management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import User
from ..password_policy import MIN_PASSWORD_LEN
from ..schemas import ResetPasswordIn, UserOut, UserStatusIn
from ..security import hash_password
from ..services import audit
from .admin_helpers import page as _page
from .admin_helpers import require_admin_password as _require_admin_password

router = APIRouter()


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    q: str | None = None,
    status: str | None = None,
    is_admin: bool | None = None,
    limit: int = 50,
    offset: int = 0,
):
    limit, offset = _page(limit, offset, 200)
    query = select(User)
    text = (q or "").strip()
    if text:
        like = f"%{text}%"
        query = query.where(
            (User.phone.ilike(like))
            | (User.nickname.ilike(like))
            | (User.department.ilike(like))
        )
    if status in {"active", "pending", "disabled"}:
        query = query.where(User.status == status)
    if is_admin is not None:
        query = query.where(User.is_admin.is_(bool(is_admin)))
    return list(db.execute(query.order_by(User.id.desc()).limit(limit).offset(offset)).scalars())


@router.patch("/users/{user_id}/status")
def set_user_status(
    user_id: int,
    body: UserStatusIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
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
    audit.log(
        db,
        user_id=admin.id,
        action="set_user_status",
        biz_type="admin",
        biz_id=user_id,
        ip=get_client_ip(request) if request else None,
        detail={"status": body.status},
    )
    return {"ok": True}


@router.post("/users/{user_id}/reset_password")
def reset_password(
    user_id: int,
    body: ResetPasswordIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PASSWORD_LEN} 位")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    user.password_hash = hash_password(body.password)
    user.token_version += 1  # force re-login on all devices
    db.commit()
    audit.log(
        db,
        user_id=admin.id,
        action="reset_password",
        biz_type="admin",
        biz_id=user_id,
        ip=get_client_ip(request) if request else None,
        detail={"target_user_id": user_id},
    )
    return {"ok": True}
