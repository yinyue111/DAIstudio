"""Phone + password auth with whitelist gate (register = auto login).

Registration is allowed only for phones an admin has added to
phone_whitelist, and the phone must prove possession with an SMS code.
Login has a per-phone brute-force guard.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip
from ..models import PhoneWhitelist, User
from ..redis_client import redis_client
from ..schemas import LoginIn, RegisterIn, SmsCodeIn, TokenOut
from ..security import create_access_token, hash_password, verify_password
from ..services import audit, sms

router = APIRouter(prefix="/api/auth", tags=["auth"])

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")  # mainland China mobile
LOGIN_FAIL_LIMIT = 10
LOGIN_IP_FAIL_LIMIT = 50  # per-IP cap: catches phone-spraying without locking one account
LOGIN_FAIL_WINDOW = 15 * 60  # 15 minutes
SMS_IP_SEND_LIMIT = 30
SMS_IP_SEND_WINDOW = 60 * 60
MIN_PASSWORD_LEN = 10


def _valid_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone))


def _whitelisted(db: Session, phone: str) -> PhoneWhitelist | None:
    return db.get(PhoneWhitelist, phone)


def _check_sms_ip_rate(ip: str) -> None:
    key = f"sms:sendip:{ip}"
    n = redis_client.incr(key)
    if n == 1:
        redis_client.expire(key, SMS_IP_SEND_WINDOW)
    if n > SMS_IP_SEND_LIMIT:
        raise HTTPException(429, "验证码发送过于频繁,请稍后再试")


@router.post("/register", response_model=TokenOut)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not _valid_phone(phone):
        raise HTTPException(400, "手机号格式不正确")
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PASSWORD_LEN} 位")

    wl = _whitelisted(db, phone)
    if wl is None:
        raise HTTPException(403, "该手机号不在白名单内,请联系管理员开通")
    if db.query(User).filter(User.phone == phone).first():
        raise HTTPException(409, "该手机号已注册,请直接登录")
    try:
        sms.verify_code(phone, body.sms_code.strip())
    except sms.SmsError as e:
        raise HTTPException(400, str(e))

    user = User(
        phone=phone,
        password_hash=hash_password(body.password),
        nickname=body.nickname or wl.note,
        department=wl.department,
        status="active",
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    audit.log(db, user_id=user.id, action="register", ip=get_client_ip(request))
    return TokenOut(access_token=create_access_token(user.id, user.token_version))


@router.post("/sms/send")
def send_sms_code(body: SmsCodeIn, request: Request, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not _valid_phone(phone):
        raise HTTPException(400, "手机号格式不正确")
    ip = get_client_ip(request)
    _check_sms_ip_rate(ip)
    if _whitelisted(db, phone) is None:
        raise HTTPException(403, "该手机号不在白名单内,请联系管理员开通")
    if db.query(User).filter(User.phone == phone).first():
        raise HTTPException(409, "该手机号已注册,请直接登录")
    try:
        code = sms.send_code(phone)
    except sms.SmsError as e:
        raise HTTPException(400, str(e))
    audit.log(db, user_id=None, action="send_sms_code", ip=ip, detail={"phone": phone})
    out = {"ok": True}
    if sms.is_dev_mode():
        out["code"] = code
    return out


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not _valid_phone(phone):
        raise HTTPException(400, "手机号格式不正确")

    ip = get_client_ip(request)
    fail_key = f"login:fail:{phone}"
    ip_key = f"login:failip:{ip}"
    if int(redis_client.get(fail_key) or 0) >= LOGIN_FAIL_LIMIT:
        raise HTTPException(429, "登录失败次数过多,请稍后再试")
    if int(redis_client.get(ip_key) or 0) >= LOGIN_IP_FAIL_LIMIT:
        raise HTTPException(429, "登录失败次数过多,请稍后再试")

    user = db.query(User).filter(User.phone == phone).first()
    if not user or not verify_password(body.password, user.password_hash):
        for key in (fail_key, ip_key):
            n = redis_client.incr(key)
            if n == 1:
                redis_client.expire(key, LOGIN_FAIL_WINDOW)
        raise HTTPException(401, "手机号或密码错误")

    if user.status != "active":
        raise HTTPException(403, "账号未启用或已被禁用")

    redis_client.delete(fail_key)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    audit.log(db, user_id=user.id, action="login", ip=get_client_ip(request))
    return TokenOut(access_token=create_access_token(user.id, user.token_version))
