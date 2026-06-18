from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import User
from ..redis_client import redis_client
from ..schemas import ChangePasswordIn, UserOut
from ..security import hash_password, verify_password
from ..services import audit

router = APIRouter(prefix="/api", tags=["me"])

MIN_PASSWORD_LEN = 10
PASSWORD_FAIL_LIMIT = 8
PASSWORD_FAIL_WINDOW = 15 * 60


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/me/password")
def change_password(body: ChangePasswordIn, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    fail_key = f"password:fail:{user.id}"
    ip_key = f"password:failip:{get_client_ip(request)}"
    if int(redis_client.get(fail_key) or 0) >= PASSWORD_FAIL_LIMIT:
        raise HTTPException(429, "密码错误次数过多,请稍后再试")
    if int(redis_client.get(ip_key) or 0) >= PASSWORD_FAIL_LIMIT * 3:
        raise HTTPException(429, "密码错误次数过多,请稍后再试")
    if not verify_password(body.old_password, user.password_hash):
        for key in (fail_key, ip_key):
            n = redis_client.incr(key)
            if n == 1:
                redis_client.expire(key, PASSWORD_FAIL_WINDOW)
        audit.log(db, user_id=user.id, action="change_password_failed",
                  ip=get_client_ip(request))
        raise HTTPException(400, "原密码不正确")
    if len(body.new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"新密码至少 {MIN_PASSWORD_LEN} 位")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # invalidate all existing tokens -> must re-login
    db.commit()
    redis_client.delete(fail_key)
    audit.log(db, user_id=user.id, action="change_password", ip=get_client_ip(request))
    return {"ok": True, "relogin": True}


@router.post("/me/logout")
def logout(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Server-side logout: invalidates every issued token for this user."""
    user.token_version += 1
    db.commit()
    return {"ok": True}
