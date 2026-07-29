"""Phone + password auth (register = auto login).

Registration is open to valid mobile numbers. When the SMS switch is enabled,
registration also requires a valid SMS code.
Login has a per-phone brute-force guard.
Self-service password reset: SMS code (purpose="reset") + new password.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
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
# 密码重置独立限流(与注册发码/登录失败计数互不影响)
RESET_SMS_IP_LIMIT = 20  # 每 IP 每小时可发起的重置验证码请求
RESET_SMS_IP_WINDOW = 60 * 60
RESET_ATTEMPT_LIMIT = 10  # 每手机号窗口内的重置提交上限
RESET_ATTEMPT_IP_LIMIT = 50  # 每 IP 窗口内的重置提交上限
RESET_ATTEMPT_WINDOW = 15 * 60
_REGISTER_GATE_ERROR = "注册申请无法完成,请确认手机号未注册"


class SmsSendIn(SmsCodeIn):
    """发码用途:register 注册(默认,保持旧客户端兼容) / reset 找回密码。"""

    purpose: Literal["register", "reset"] = "register"


class ResetPasswordSmsIn(BaseModel):
    phone: str
    sms_code: str
    new_password: str = Field(max_length=128)


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


def _registration_enabled() -> bool:
    return settings.registration_enabled


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
    if not _registration_enabled():
        raise HTTPException(400, "注册暂未开放,请联系管理员")

    wl = _whitelisted(db, phone)
    if db.query(User).filter(User.phone == phone).first():
        audit.log(db, user_id=None, action="register_denied", ip=get_client_ip(request),
                  detail={"phone": phone, "reason": "already_registered"})
        raise HTTPException(400, _REGISTER_GATE_ERROR)
    sms_required = _sms_required_for_registration(db)
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
        nickname=body.nickname or (wl.note if wl else None),
        department=wl.department if wl else None,
        status="active",
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        audit.log(db, user_id=None, action="register_denied", ip=get_client_ip(request),
                  detail={"phone": phone, "reason": "already_registered_race"})
        raise HTTPException(400, _REGISTER_GATE_ERROR)
    db.refresh(user)

    audit.log(db, user_id=user.id, action="register", ip=get_client_ip(request))
    return _issue_login_response(response, user)


@router.get("/features")
def auth_features(db: Session = Depends(get_db)):
    sms_enabled = get_bool_setting(db, "sms_auth_enabled", False)
    return {
        "sms_auth_enabled": sms_enabled,
        "sms_required_for_registration": sms_enabled,
        "registration_enabled": _registration_enabled(),
        # 自助找回密码依赖短信渠道可用(与注册短信开关无关)
        "password_reset_enabled": not sms.readiness_issues(),
    }


def _send_reset_sms(db: Session, phone: str, ip: str) -> dict:
    """给已注册手机号发送重置验证码。

    冷却/小时上限/响应结构对已注册与未注册手机号完全一致,未注册只是不真正
    发码,避免通过响应差异探测手机号是否存在。
    """
    sms_issues = sms.readiness_issues()
    if sms_issues:
        raise HTTPException(503, "; ".join(sms_issues))
    if incr_window(f"pwreset:sendip:{ip}", RESET_SMS_IP_WINDOW) > RESET_SMS_IP_LIMIT:
        raise HTTPException(429, "验证码发送过于频繁,请稍后再试")
    cooldown_key = f"pwreset:sendcd:{phone}"
    if not redis_client.set(cooldown_key, "1", ex=settings.sms_send_cooldown_seconds, nx=True):
        ttl = max(0, int(redis_client.ttl(cooldown_key) or 0))
        raise HTTPException(429, {"message": f"请稍后再试({ttl}s 后可重新发送)", "retry_after": ttl})
    if incr_window(f"pwreset:sendhr:{phone}", 3600) > settings.sms_send_hourly_limit:
        raise HTTPException(429, "发送过于频繁,请一小时后再试")

    out = {"ok": True, "retry_after": settings.sms_send_cooldown_seconds}
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        audit.log(db, user_id=None, action="send_reset_sms_skipped", ip=ip,
                  detail={"phone": phone, "reason": "not_registered"})
        return out
    try:
        code = sms.send_code(phone, purpose="reset")
    except sms.SmsError as e:
        raise HTTPException(400, str(e))
    audit.log(db, user_id=user.id, action="send_reset_sms", ip=ip, detail={"phone": phone})
    if sms.is_dev_mode():
        out["code"] = code
    return out


@router.post("/sms/send")
def send_sms_code(body: SmsSendIn, request: Request, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if not _valid_phone(phone):
        raise HTTPException(400, "手机号格式不正确")
    if body.purpose == "reset":
        return _send_reset_sms(db, phone, get_client_ip(request))
    # register 用途:保持原有语义(已注册手机号返回 ok 但不发码,防注册探测)
    if not _registration_enabled() or not _sms_required_for_registration(db):
        return {"ok": False, "disabled": True}
    sms_issues = sms.readiness_issues()
    if sms_issues:
        raise HTTPException(503, "; ".join(sms_issues))
    ip = get_client_ip(request)
    _check_sms_ip_rate(ip)
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


@router.post("/password/reset")
def reset_password_by_sms(body: ResetPasswordSmsIn, request: Request, db: Session = Depends(get_db)):
    """免登录自助重置密码:手机号 + 重置验证码 + 新密码。

    验证码由 /sms/send(purpose="reset")下发,一次性消费、过期即失效;
    成功后 token_version +1,所有已登录设备的旧 token 立即失效。
    """
    phone = body.phone.strip()
    if not _valid_phone(phone):
        raise HTTPException(400, "手机号格式不正确")
    code = (body.sms_code or "").strip()
    if not code:
        raise HTTPException(400, "请输入短信验证码")
    if len(body.new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PASSWORD_LEN} 位")

    ip = get_client_ip(request)
    if incr_window(f"pwreset:tryip:{ip}", RESET_ATTEMPT_WINDOW) > RESET_ATTEMPT_IP_LIMIT:
        raise HTTPException(429, "操作过于频繁,请稍后再试")
    if incr_window(f"pwreset:try:{phone}", RESET_ATTEMPT_WINDOW) > RESET_ATTEMPT_LIMIT:
        raise HTTPException(429, "操作过于频繁,请稍后再试")

    try:
        sms.verify_code(phone, code, purpose="reset")
    except sms.SmsError as e:
        raise HTTPException(400, str(e))

    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        # 理论上不会发生(重置码只发给已注册手机号);统一按验证码失效处理,
        # 不暴露手机号是否注册
        raise HTTPException(400, "验证码不存在或已过期")

    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # 让该用户所有已签发 token 失效,强制重新登录
    db.commit()
    redis_client.delete(f"login:fail:{phone}")

    audit.log(db, user_id=user.id, action="password_reset_self", ip=ip,
              detail={"phone": phone})
    return {"ok": True}
