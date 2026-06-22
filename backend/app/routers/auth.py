"""Phone + password auth with whitelist gate (register = auto login).

Registration is allowed only for phones an admin has added to
phone_whitelist, and the phone must prove possession with an SMS code.
Login has a per-phone brute-force guard.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip
from ..models import PhoneWhitelist, User
from ..password_policy import MIN_PASSWORD_LEN
from ..redis_client import redis_client
from ..schemas import LoginIn, RegisterIn, SmsCodeIn, TokenOut
from ..security import create_access_token, dummy_password_hash, hash_password, verify_password
from ..services import audit, sms
from ..services.config_store import get_bool_setting
from ..services.rate_limit import incr_window

router = APIRouter(prefix="/api/auth", tags=["auth"])

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")  # mainland China mobile
LOGIN_FAIL_LIMIT = 10
LOGIN_IP_FAIL_LIMIT = 50  # per-IP cap: catches phone-spraying without locking one account
LOGIN_FAIL_WINDOW = 15 * 60  # 15 minutes
SMS_IP_SEND_LIMIT = 30
SMS_IP_SEND_WINDOW = 60 * 60
_REGISTER_GATE_ERROR = "注册申请无法完成,请确认手机号已获得授权且未注册"


def _valid_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone))


def _whitelisted(db: Session, phone: str) -> PhoneWhitelist | None:
    return db.get(PhoneWhitelist, phone)


def _check_sms_ip_rate(ip: str) -> None:
    key = f"sms:sendip:{ip}"
    n = incr_window(key, SMS_IP_SEND_WINDOW)
    if n > SMS_IP_SEND_LIMIT:
        raise HTTPException(429, "验证码发送过于频繁,请稍后再试")


def _sms_retry_after(phone: str) -> int:
    _ok, ttl = sms.can_send(phone)
    return max(0, int(ttl or 0))


def _sms_required_for_registration(db: Session) -> bool:
    return get_bool_setting(db, "sms_auth_enabled", False)


def _registration_allowed_without_sms() -> bool:
    # A whitelist proves the phone is allowed, not that the registrant controls
    # it. Keep no-SMS self-registration only as a local development convenience.
    return settings.debug


def _issue_login_response(response: Response, user: User) -> TokenOut:
    token = create_access_token(user.id, user.token_version)
    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
        path="/",
    )
    if settings.debug or settings.auth_bearer_response_enabled:
        return TokenOut(access_token=token)
    return TokenOut()


@router.post("/register", response_model=TokenOut)
def register(body: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not _valid_phone(phone):
        raise HTTPException(400, "手机号格式不正确")
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PASSWORD_LEN} 位")

    wl = _whitelisted(db, phone)
    if wl is None:
        audit.log(db, user_id=None, action="register_denied", ip=get_client_ip(request),
                  detail={"phone": phone, "reason": "not_whitelisted"})
        raise HTTPException(400, _REGISTER_GATE_ERROR)
    if db.query(User).filter(User.phone == phone).first():
        audit.log(db, user_id=None, action="register_denied", ip=get_client_ip(request),
                  detail={"phone": phone, "reason": "already_registered"})
        raise HTTPException(400, _REGISTER_GATE_ERROR)
    sms_required = _sms_required_for_registration(db)
    if not sms_required and not _registration_allowed_without_sms():
        raise HTTPException(400, "注册暂未开放,请联系管理员开启短信验证码注册")
    if sms_required:
        code = (body.sms_code or "").strip()
        if not code:
            raise HTTPException(400, "请输入短信验证码")
        try:
            sms.verify_code(phone, code)
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
    return _issue_login_response(response, user)


@router.get("/features")
def auth_features(db: Session = Depends(get_db)):
    sms_enabled = get_bool_setting(db, "sms_auth_enabled", False)
    return {
        "sms_auth_enabled": sms_enabled,
        "sms_required_for_registration": sms_enabled,
        "registration_enabled": sms_enabled or _registration_allowed_without_sms(),
    }


@router.post("/sms/send")
def send_sms_code(body: SmsCodeIn, request: Request, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not _valid_phone(phone):
        raise HTTPException(400, "手机号格式不正确")
    if not _sms_required_for_registration(db):
        return {"ok": False, "disabled": True}
    sms_issues = sms.readiness_issues()
    if sms_issues:
        raise HTTPException(503, "; ".join(sms_issues))
    ip = get_client_ip(request)
    _check_sms_ip_rate(ip)
    if _whitelisted(db, phone) is None:
        audit.log(db, user_id=None, action="send_sms_code_skipped", ip=ip,
                  detail={"phone": phone, "reason": "not_whitelisted"})
        return {"ok": True}
    if db.query(User).filter(User.phone == phone).first():
        audit.log(db, user_id=None, action="send_sms_code_skipped", ip=ip,
                  detail={"phone": phone, "reason": "already_registered"})
        return {"ok": True}
    try:
        code = sms.send_code(phone)
    except sms.SmsError as e:
        retry_after = _sms_retry_after(phone)
        if retry_after:
            raise HTTPException(
                429,
                {"message": str(e), "retry_after": retry_after},
            )
        raise HTTPException(400, str(e))
    audit.log(db, user_id=None, action="send_sms_code", ip=ip, detail={"phone": phone})
    out = {"ok": True, "retry_after": settings.sms_send_cooldown_seconds}
    if sms.is_dev_mode():
        out["code"] = code
    return out


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
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
    password_ok = verify_password(
        body.password,
        user.password_hash if user else dummy_password_hash(),
    )
    if not user or not password_ok:
        for key in (fail_key, ip_key):
            incr_window(key, LOGIN_FAIL_WINDOW)
        raise HTTPException(401, "手机号或密码错误")

    if user.status != "active":
        raise HTTPException(403, "账号未启用或已被禁用")

    redis_client.delete(fail_key)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    audit.log(db, user_id=user.id, action="login", ip=get_client_ip(request))
    return _issue_login_response(response, user)
