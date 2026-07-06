"""Admin whitelist management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import PhoneWhitelist, User
from ..schemas import WhitelistDeleteIn, WhitelistIn
from ..services import audit
from .admin_helpers import require_admin_password as _require_admin_password

router = APIRouter()


@router.get("/whitelist")
def list_whitelist(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = list(db.execute(select(PhoneWhitelist).order_by(PhoneWhitelist.created_at.desc())).scalars())
    return [
        {
            "phone": r.phone,
            "note": r.note,
            "department": r.department,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.post("/whitelist")
def add_whitelist(
    body: WhitelistIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password, request)
    if db.get(PhoneWhitelist, body.phone):
        raise HTTPException(409, "该手机号已在白名单")
    db.add(PhoneWhitelist(phone=body.phone, note=body.note, department=body.department, added_by=admin.id))
    db.commit()
    audit.log(
        db,
        user_id=admin.id,
        action="add_whitelist",
        biz_type="admin",
        biz_id=None,
        ip=get_client_ip(request) if request else None,
        detail={"phone": body.phone, "department": body.department},
    )
    return {"ok": True}


@router.delete("/whitelist/{phone}")
def remove_whitelist(
    phone: str,
    db: Session = Depends(get_db),
    body: WhitelistDeleteIn | None = None,
    admin: User = Depends(require_admin),
    request: Request = None,
):
    _require_admin_password(admin, body.admin_password if body else None, request)
    row = db.get(PhoneWhitelist, phone)
    if not row:
        raise HTTPException(404, "不存在")
    db.delete(row)
    db.commit()
    audit.log(
        db,
        user_id=admin.id,
        action="remove_whitelist",
        biz_type="admin",
        biz_id=None,
        ip=get_client_ip(request) if request else None,
        detail={"phone": phone},
    )
    return {"ok": True}
