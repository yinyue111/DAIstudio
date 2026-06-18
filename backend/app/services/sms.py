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

log = logging.getLogger("sms")


class SmsError(Exception):
    pass


def implemented_providers() -> set[str]:
    return {"mock", "http"}


def _k(prefix: str, phone: str) -> str:
    return f"sms:{prefix}:{phone}"


def can_send(phone: str) -> tuple[bool, int]:
    ttl = redis_client.ttl(_k("cooldown", phone))
    if ttl and ttl > 0:
        return False, ttl
    return True, 0


def send_code(phone: str) -> str:
    ok, ttl = can_send(phone)
    if not ok:
        raise SmsError(f"请稍后再试({ttl}s 后可重新发送)")

    hourly_key = _k("hourly", phone)
    sent = int(redis_client.get(hourly_key) or 0)
    if sent >= settings.sms_send_hourly_limit:
        raise SmsError("发送过于频繁,请一小时后再试")

    code = f"{secrets.randbelow(1_000_000):06d}"
    redis_client.setex(_k("code", phone), settings.sms_code_ttl_seconds, code)
    redis_client.setex(_k("cooldown", phone), settings.sms_send_cooldown_seconds, "1")
    redis_client.delete(_k("fail", phone))

    pipe = redis_client.pipeline()
    pipe.incr(hourly_key)
    if sent == 0:
        pipe.expire(hourly_key, 3600)
    pipe.execute()

    try:
        _dispatch(phone, code)
    except Exception:
        # Provider failed before delivery. Roll back Redis-side quota/cooldown so
        # the user can retry after the operator fixes the SMS channel.
        redis_client.delete(_k("code", phone))
        redis_client.delete(_k("cooldown", phone))
        redis_client.delete(_k("fail", phone))
        if sent == 0:
            redis_client.delete(hourly_key)
        else:
            redis_client.decr(hourly_key)
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
        with httpx.Client(timeout=settings.sms_http_timeout_seconds) as client:
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
        redis_client.incr(fail_key)
        redis_client.expire(fail_key, settings.sms_code_ttl_seconds)
        raise SmsError("验证码错误")

    # success -> burn the code
    redis_client.delete(_k("code", phone))
    redis_client.delete(fail_key)
    return True


def is_dev_mode() -> bool:
    return settings.debug and settings.sms_provider == "mock"
