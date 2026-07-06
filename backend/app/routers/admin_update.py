"""Admin online update endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import User
from ..schemas import OnlineUpdateRunIn, OnlineUpdateRunOut, OnlineUpdateStatusOut
from ..services import audit, online_update

router = APIRouter()


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
    if body.apply and body.confirm != "UPDATE":
        raise HTTPException(400, "执行在线升级需要确认码 UPDATE")
    try:
        result = online_update.run_update(
            apply=body.apply,
            force_apply=body.force_apply,
            expected_remote_head=body.expected_remote_head,
        )
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
