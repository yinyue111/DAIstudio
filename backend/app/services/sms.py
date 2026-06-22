"""Verification-code flow.

Codes live in Redis only (never the DB):
  sms:code:{phone}   -> the 6 digit code        (TTL = sms_code_ttl_seconds)
  sms:cooldown:{phone} -> send cooldown lock     (TTL = cooldown)
  sms:hourly:{phone} -> rolling hourly send count (TTL = 3600)
  sms:fail:{phone}   -> wrong-attempt counter
Anti brute-force: cooldown between sends, hourly cap, max wrong attempts.
"""
from __future__ import annotations

import logging
import secrets

import httpx

from ..config import settings
from ..redis_client import redis_client
from .rate_limit import incr_window
from .ssrf import pinned_client

log = logging.getLogger("sms")


class SmsError(Exception):
    pass


def implemented_providers() -> set[str]:
    return {"mock", "http"}


def readiness_issues(*, allow_mock: bool | None = None) -> list[str]:
    provider = (settings.sms_provider or "").strip().lower()
    issues: list[str] = []
    if provider not in implemented_providers():
        return [f"SMS_PROVIDER={settings.sms_provider} 暂未实现"]
    if allow_mock is None:
        allow_mock = settings.debug
    if provider == "mock":
        if not allow_mock:
            issues.append("生产环境不能使用 mock 短信渠道")
        return issues
    if provider == "http":
        if not settings.sms_http_url.strip():
            issues.append("SMS_HTTP_URL 未配置")
        return issues
    return issues


def _k(prefix: str, phone: str) -> str:
    return f"sms:{prefix}:{phone}"


def can_send(phone: str) -> tuple[bool, int]:
    ttl = redis_client.ttl(_k("cooldown", phone))
    if ttl and ttl > 0:
        return False, ttl
    return True, 0


def _reserve_send_slot(phone: str) -> int:
    """Atomically reserve cooldown + hourly quota before contacting provider."""
    cooldown_key = _k("cooldown", phone)
    reserved = redis_client.set(
        cooldown_key,
        "1",
        ex=settings.sms_send_cooldown_seconds,
        nx=True,
    )
    if not reserved:
        ttl = redis_client.ttl(cooldown_key)
        raise SmsError(f"请稍后再试({max(0, int(ttl or 0))}s 后可重新发送)")

    hourly_key = _k("hourly", phone)
    sent = incr_window(hourly_key, 3600)
    if sent > settings.sms_send_hourly_limit:
        redis_client.delete(cooldown_key)
        redis_client.decr(hourly_key)
        raise SmsError("发送过于频繁,请一小时后再试")
    return sent


def _rollback_send_slot(phone: str, sent: int) -> None:
    redis_client.delete(_k("code", phone))
    redis_client.delete(_k("cooldown", phone))
    redis_client.delete(_k("fail", phone))
    hourly_key = _k("hourly", phone)
    if sent <= 1:
        redis_client.delete(hourly_key)
    else:
        redis_client.decr(hourly_key)


def send_code(phone: str) -> str:
    sent = _reserve_send_slot(phone)
    code = f"{secrets.randbelow(1_000_000):06d}"
    redis_client.setex(_k("code", phone), settings.sms_code_ttl_seconds, code)
    redis_client.delete(_k("fail", phone))

    try:
        _dispatch(phone, code)
    except Exception:
        # Provider failed before delivery. Roll back Redis-side quota/cooldown so
        # the user can retry after the operator fixes the SMS channel.
        _rollback_send_slot(phone, sent)
        raise
    return code


def _dispatch(phone: str, code: str) -> None:
    """Hand the code to the real SMS provider. Mock just logs it."""
    if settings.sms_provider == "mock":
        log.warning("[MOCK SMS] phone=%s code=%s", phone, code)
        return
    if settings.sms_provider == "http":
        _send_http(phone, code)
        return
    raise SmsError(f"SMS_PROVIDER={settings.sms_provider} 暂未实现,验证码未发送")


def _send_http(phone: str, code: str) -> None:
    if not settings.sms_http_url:
        raise SmsError("SMS_HTTP_URL 未配置,验证码未发送")
    headers = {"Content-Type": "application/json"}
    if settings.sms_http_api_key:
        headers["Authorization"] = f"Bearer {settings.sms_http_api_key}"
    payload = {
        "phone": phone,
        "code": code,
        "sign_name": settings.sms_sign_name,
        "template_code": settings.sms_template_code,
    }
    try:
        with pinned_client(settings.sms_http_url, timeout=settings.sms_http_timeout_seconds) as client:
            res = client.post(settings.sms_http_url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise SmsError(f"短信网关调用失败:{e}") from e
    if res.status_code >= 400:
        raise SmsError(f"短信网关返回 {res.status_code}")


def verify_code(phone: str, code: str) -> bool:
    real = redis_client.get(_k("code", phone))
    if not real:
        raise SmsError("验证码不存在或已过期")

    fail_key = _k("fail", phone)
    fails = int(redis_client.get(fail_key) or 0)
    if fails >= settings.sms_verify_max_attempts:
        redis_client.delete(_k("code", phone))
        raise SmsError("尝试次数过多,请重新获取验证码")

    if code != real:
        incr_window(fail_key, settings.sms_code_ttl_seconds)
        raise SmsError("验证码错误")

    # success -> burn the code
    redis_client.delete(_k("code", phone))
    redis_client.delete(fail_key)
    return True


def is_dev_mode() -> bool:
    return settings.debug and settings.sms_provider == "mock"
