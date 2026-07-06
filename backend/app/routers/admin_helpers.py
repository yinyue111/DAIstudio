"""Helper modules for admin router endpoints."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import AdminIdempotencyKey, GenTask, ModelConfig, User
from ..redis_client import redis_client
from ..schemas import ModelConfigIn, QuotaBulkGrantIn, QuotaGrantIn, QuotaGrantItemIn
from ..services import generation, payment_config
from ..services.config_store import get_setting
from ..services.model_gateway_config import (
    encrypted_key_present,
    normalise_base_url,
    normalise_gateway_format,
    normalise_provider,
)
from ..services.rate_limit import incr_window

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
IMAGE_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
QUOTA_GRANT_REPLAY_WINDOW_SECONDS = 10 * 60
ACTIVE_MODEL_TASK_STATUSES = ("queued", "running", generation.NEEDS_REVIEW)
ADMIN_RATE_WINDOW_SECONDS = 3600


def model_gateway_update_values(row: ModelConfig, body: ModelConfigIn) -> tuple[
    str | None,
    str | None,
    str | None,
]:
    fields = getattr(body, "model_fields_set", set())
    provider = body.provider if "provider" in fields else row.provider
    base_url = body.base_url if "base_url" in fields else row.base_url
    gateway_format = body.gateway_format if "gateway_format" in fields else row.gateway_format
    return provider, base_url, gateway_format


def model_audit_snapshot(row: ModelConfig | None) -> dict:
    if row is None:
        return {}
    extra = row.extra if isinstance(row.extra, dict) else {}
    return {
        "use": row.use,
        "model_id": row.model_id,
        "provider": row.provider,
        "base_url": row.base_url,
        "gateway_format": row.gateway_format,
        "api_key_configured": encrypted_key_present(row),
        "cost_credits": row.cost_credits,
        "unlock_cost": row.unlock_cost,
        "enabled": bool(row.enabled),
        "extra_keys": sorted(str(k) for k in extra),
    }


def payment_provider_audit_snapshot(row) -> dict:
    if row is None:
        return {}
    return {
        "provider": row.provider,
        "enabled": bool(row.enabled),
        "mode": row.mode or "mock",
        "public_config": payment_config.public_config_for_provider(
            row.provider,
            row.public_config,
        ),
        "secret_configured": {
            key: bool((row.secret_config or {}).get(key))
            for key in sorted(payment_config.SECRET_FIELDS.get(row.provider, set()))
        },
    }


def page(limit: int, offset: int, cap: int) -> tuple[int, int]:
    return min(max(int(limit), 1), cap), max(int(offset), 0)


def gateway_update_changes_runtime(row: ModelConfig, body: ModelConfigIn) -> bool:
    provider, base_url, gateway_format = model_gateway_update_values(row, body)
    current_provider = normalise_provider(row.provider)
    current_base_url = normalise_base_url(row.base_url)
    current_gateway_format = normalise_gateway_format(row.gateway_format, current_provider, row.use)
    next_provider = normalise_provider(provider)
    next_base_url = normalise_base_url(base_url)
    next_gateway_format = normalise_gateway_format(gateway_format, next_provider, row.use)
    current_key_present = encrypted_key_present(row)
    new_key_supplied = body.api_key not in (None, "", "__keep__", "已配置")
    next_key_present = False if body.api_key_clear else (True if new_key_supplied else current_key_present)
    return (
        current_provider != next_provider
        or current_base_url != next_base_url
        or current_gateway_format != next_gateway_format
        or current_key_present != next_key_present
        or new_key_supplied
    )


def assert_no_active_model_tasks(db: Session, use: str) -> None:
    active_id = db.execute(
        select(GenTask.id)
        .where(
            GenTask.model_use == use,
            GenTask.status.in_(ACTIVE_MODEL_TASK_STATUSES),
        )
        .limit(1)
    ).scalar_one_or_none()
    if active_id is not None:
        raise HTTPException(
            409,
            "当前模型仍有排队、运行中或待对账任务,请等待任务结束或处理后再切换网关配置",
        )


def parse_date(s: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"日期格式应为 ISO(YYYY-MM-DD),收到:{s}") from None
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    # gen_tasks.finished_at / created_at are tz-aware; a naive bound would raise
    # on PostgreSQL ("can't compare offset-naive and offset-aware"). Assume UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    if text.lstrip(" \t\r\n").startswith(CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def date_key(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.date().isoformat()


def quota_grant_idempotency_raw(body: QuotaGrantIn) -> str:
    raw = (body.idempotency_key or "").strip()
    if not raw:
        raise HTTPException(400, "idempotency_key 必填")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", raw):
        raise HTTPException(400, "idempotency_key 格式非法")
    return raw


def quota_bulk_grant_idempotency_raw(body: QuotaBulkGrantIn) -> str:
    raw = (body.idempotency_key or "").strip()
    if not raw:
        raise HTTPException(400, "idempotency_key 必填")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,96}", raw):
        raise HTTPException(400, "idempotency_key 格式非法")
    return raw


def quota_bulk_grant_fingerprint(body: QuotaBulkGrantIn) -> str:
    payload = [
        {
            "user_id": int(item.user_id),
            "amount": int(item.amount),
            "note": (item.note or "").strip(),
        }
        for item in body.items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reserve_quota_bulk_grant_idempotency(
    db: Session,
    *,
    admin_id: int,
    body: QuotaBulkGrantIn,
) -> tuple[str, bool]:
    raw = quota_bulk_grant_idempotency_raw(body)
    marker_key = f"bulk:{raw}"
    marker_note = f"bulk:{quota_bulk_grant_fingerprint(body)}"
    try:
        db.add(
            AdminIdempotencyKey(
                admin_id=admin_id,
                scope="quota_grant",
                key=marker_key,
                amount=0,
                note=marker_note,
            )
        )
        db.flush()
    except IntegrityError as e:
        db.rollback()
        existing = db.execute(
            select(AdminIdempotencyKey).where(
                AdminIdempotencyKey.admin_id == admin_id,
                AdminIdempotencyKey.scope == "quota_grant",
                AdminIdempotencyKey.key == marker_key,
            )
        ).scalar_one_or_none()
        if existing and (existing.note or "") == marker_note:
            return raw, False
        raise HTTPException(409, "幂等键已用于不同批量额度发放请求") from e
    return raw, True


def quota_bulk_grant_item_key(raw_key: str, index: int, item: QuotaGrantItemIn) -> str:
    note = (item.note or "").strip()
    raw_digest = hashlib.sha256(raw_key.encode()).hexdigest()[:16]
    digest = hashlib.sha256(
        f"{raw_key}\x1f{index}\x1f{item.user_id}\x1f{item.amount}\x1f{note}".encode()
    ).hexdigest()[:24]
    return f"bulk:{raw_digest}:{index}:{digest}"


def reserve_quota_grant_idempotency(
    db: Session,
    *,
    admin_id: int,
    body: QuotaGrantIn,
    note: str,
) -> tuple[str, bool]:
    raw = quota_grant_idempotency_raw(body)
    try:
        db.add(
            AdminIdempotencyKey(
                admin_id=admin_id,
                scope="quota_grant",
                key=raw,
                target_user_id=body.user_id,
                amount=body.amount,
                note=note,
            )
        )
        db.flush()
    except IntegrityError as e:
        db.rollback()
        existing = db.execute(
            select(AdminIdempotencyKey).where(
                AdminIdempotencyKey.admin_id == admin_id,
                AdminIdempotencyKey.scope == "quota_grant",
                AdminIdempotencyKey.key == raw,
            )
        ).scalar_one_or_none()
        if not existing:
            raise HTTPException(409, "重复的额度发放请求已拦截,请更换幂等键") from e
        if (
            int(existing.target_user_id or 0) == int(body.user_id)
            and int(existing.amount or 0) == int(body.amount)
            and (existing.note or "") == note
        ):
            return raw, False
        raise HTTPException(409, "幂等键已用于不同额度发放请求") from e
    return raw, True


def quota_grant_fingerprint(admin_id: int, body: QuotaGrantIn, note: str) -> str:
    return f"admin:quota:fingerprint:{admin_id}:{body.user_id}:{body.amount}:{note}"


def remember_quota_grant_fingerprint(admin_id: int, body: QuotaGrantIn, note: str, idem_key: str) -> None:
    fingerprint_key = quota_grant_fingerprint(admin_id, body, note)
    redis_client.setex(fingerprint_key, QUOTA_GRANT_REPLAY_WINDOW_SECONDS, idem_key)


def replay_quota_grant(
    db: Session,
    *,
    admin_id: int,
    body: QuotaGrantIn,
    note: str,
) -> User | None:
    raw_key = (body.idempotency_key or "").strip()
    if raw_key:
        existing = db.execute(
            select(AdminIdempotencyKey).where(
                AdminIdempotencyKey.admin_id == admin_id,
                AdminIdempotencyKey.scope == "quota_grant",
                AdminIdempotencyKey.key == raw_key,
            )
        ).scalar_one_or_none()
        if existing:
            if (
                int(existing.target_user_id or 0) == int(body.user_id)
                and int(existing.amount or 0) == int(body.amount)
                and (existing.note or "") == note
            ):
                user = db.get(User, body.user_id)
                if not user:
                    raise HTTPException(404, "用户不存在")
                return user
            raise HTTPException(409, "幂等键已用于不同额度发放请求")
    fingerprint_key = quota_grant_fingerprint(admin_id, body, note)
    previous_key = redis_client.get(fingerprint_key)
    if previous_key and previous_key != raw_key:
        existing = db.execute(
            select(AdminIdempotencyKey).where(
                AdminIdempotencyKey.admin_id == admin_id,
                AdminIdempotencyKey.scope == "quota_grant",
                AdminIdempotencyKey.key == str(previous_key),
                AdminIdempotencyKey.target_user_id == body.user_id,
                AdminIdempotencyKey.amount == body.amount,
                AdminIdempotencyKey.note == note,
            )
        ).scalar_one_or_none()
        if existing:
            user = db.get(User, body.user_id)
            if not user:
                raise HTTPException(404, "用户不存在")
            return user
    return None


def setting_int(db: Session, key: str, default: int) -> int:
    try:
        return int(get_setting(db, key, default))
    except (TypeError, ValueError):
        return default


def age_minutes(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    base = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - base).total_seconds() // 60))


def require_admin_rate_limit(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    limit = max(1, setting_int(db, "admin_api_rate_per_hour", 600))
    ip = get_client_ip(request)
    checks = (
        (f"admin:rate:user:{admin.id}", limit),
        (f"admin:rate:ip:{ip}", limit * 5),
    )
    for key, threshold in checks:
        n = incr_window(key, ADMIN_RATE_WINDOW_SECONDS)
        if n > threshold:
            raise HTTPException(429, "管理操作过于频繁,请稍后再试")
    return admin


def quota_granted_today(db: Session, admin_id: int) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.execute(
        select(func.coalesce(func.sum(AdminIdempotencyKey.amount), 0)).where(
            AdminIdempotencyKey.admin_id == admin_id,
            AdminIdempotencyKey.scope == "quota_grant",
            AdminIdempotencyKey.created_at >= start,
        )
    ).scalar_one()
    return int(total or 0)


def assert_quota_grant_limits(db: Session, admin: User, amount: int) -> None:
    single_limit = setting_int(db, "admin_quota_grant_single_limit", 100000)
    daily_limit = setting_int(db, "admin_quota_grant_daily_limit", 500000)
    if amount > single_limit:
        raise HTTPException(400, f"单次发放额度不能超过 {single_limit}")
    if quota_granted_today(db, admin.id) + amount > daily_limit:
        raise HTTPException(400, f"今日额度发放累计不能超过 {daily_limit}")
