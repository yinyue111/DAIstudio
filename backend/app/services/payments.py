"""Payment order orchestration.

The production invariant is simple: only a server-confirmed paid order grants
credits, and the order transition is idempotent. Provider-specific QR creation
is kept behind this module so the rest of the app does not care whether the
payment came from Alipay or WeChat Pay.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus, urlparse

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..config import settings
from ..models import AppSetting, PaymentOrder, User
from ..models.payment import PaymentRefund
from . import credits, locks, payment_config
from .config_store import get_bool_setting
from .ssrf import pinned_client
from .user_events import publish_user_event

PAYMENT_PACKAGES = payment_config.DEFAULT_PAYMENT_PACKAGES
log = logging.getLogger("payments")

VALID_PROVIDERS = {"alipay", "wechat"}
PENDING = "pending"
PAID = "paid"
CLOSED = "closed"
FAILED = "failed"
REFUNDED = "refunded"
REFUND_PENDING = "pending"
REFUND_SUCCEEDED = "succeeded"
REFUND_FAILED = "failed"
_RECONCILE_LOCK_KEY = "payment:reconcile"
_RECONCILE_CURSOR_SETTING = "payment_reconcile_cursor"


class PaymentError(Exception):
    pass


def _is_local_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host in {"localhost", "127.0.0.1", "::1"}


def public_base_is_local() -> bool:
    return _is_local_url(settings.public_base_url)


def mock_payments_allowed() -> bool:
    return (
        settings.debug
        and settings.payment_mock_enabled
        and _is_local_url(settings.public_base_url)
        and _is_local_url(settings.payment_frontend_base_url)
    )


def list_packages(db: Session, *, enabled_only: bool = True) -> list[dict]:
    payment_config.seed_defaults(db)
    return [
        payment_config.package_to_dict(p)
        for p in payment_config.list_packages(db, enabled_only=enabled_only)
    ]


def public_config(db: Session) -> dict:
    payment_enabled = get_bool_setting(db, "payment_enabled", False)
    status = payment_config.export_public_status(db)
    mock_allowed = mock_payments_allowed()
    providers = []
    if payment_enabled:
        for p in status.get("providers", []):
            provider_mode = p.get("mode") or "mock"
            provider_enabled = bool(p.get("enabled"))
            provider_ready = bool(p.get("ready"))
            if provider_mode == "mock":
                provider_enabled = mock_allowed
                provider_ready = mock_allowed
            providers.append({
                "provider": p["provider"],
                "enabled": provider_enabled,
                "ready": provider_ready,
                "mode": provider_mode,
            })
    return {
        "enabled": payment_enabled,
        "packages": status.get("packages", []) if payment_enabled else [],
        "providers": providers,
    }


def _assert_positive_package(pkg: dict) -> None:
    if int(pkg.get("amount_cents") or 0) <= 0 or int(pkg.get("credits") or 0) <= 0:
        raise PaymentError("充值套餐金额或积分配置非法")


def _assert_positive_order(order: PaymentOrder) -> None:
    if int(order.amount_cents or 0) <= 0 or int(order.credits or 0) <= 0:
        raise PaymentError("支付订单金额或积分非法")


def package_by_id(db: Session, package_id: str) -> dict:
    payment_config.seed_defaults(db)
    row = payment_config.get_enabled_package(db, package_id)
    if row:
        pkg = payment_config.package_to_dict(row)
        _assert_positive_package(pkg)
        return pkg
    raise PaymentError("充值套餐不存在")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _provider_trade_no_conflict_message(exc: IntegrityError) -> str | None:
    detail = str(getattr(exc, "orig", exc))
    lowered = detail.lower()
    if "provider_trade_no" not in lowered and "uq_payment_orders_provider_trade_no" not in lowered:
        return None
    return "支付流水号已入账到其他订单"


def make_order_no(provider: str) -> str:
    return f"R{provider[:1].upper()}{int(time.time())}{secrets.token_hex(5)}"


def _mock_code_url(order_no: str, provider: str, credits_: int) -> str:
    # The frontend turns this into a QR image. It is intentionally local and
    # harmless; DEBUG mock success still requires an authenticated API call.
    base = settings.payment_frontend_base_url.rstrip("/")
    return f"{base}/recharge?mock_order={quote_plus(order_no)}&provider={provider}&credits={credits_}"


def _alipay_configured(cfg: payment_config.ProviderRuntimeConfig) -> bool:
    return bool(
        cfg.public.get("app_id")
        and cfg.secret.get("private_key")
        and cfg.secret.get("public_key")
    )


def _wechat_configured(cfg: payment_config.ProviderRuntimeConfig) -> bool:
    return bool(
        cfg.public.get("appid")
        and cfg.public.get("mchid")
        and cfg.public.get("serial_no")
        and cfg.secret.get("private_key")
        and cfg.secret.get("api_v3_key")
        and cfg.secret.get("platform_cert_pem")
    )


def _wrap_pem_body(text: str) -> str:
    body = "".join(text.split())
    return "\n".join(body[i:i + 64] for i in range(0, len(body), 64))


def _normalise_pem_text(pem: str) -> str:
    text = pem.replace("\\n", "\n").strip()
    return text


def _normalise_private_pem(pem: str) -> str:
    text = _normalise_pem_text(pem)
    if "-----BEGIN" in text:
        return text
    return "-----BEGIN PRIVATE KEY-----\n" + _wrap_pem_body(text) + "\n-----END PRIVATE KEY-----"  # gitleaks:allow


def _normalise_public_pem(pem: str) -> str:
    text = _normalise_pem_text(pem)
    if "-----BEGIN" in text:
        return text
    return "-----BEGIN PUBLIC KEY-----\n" + _wrap_pem_body(text) + "\n-----END PUBLIC KEY-----"


def _rsa_sha256_sign(data: str, private_key_pem: str) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception as e:  # noqa: BLE001
        raise PaymentError("缺少 cryptography 依赖,无法进行真实支付签名") from e
    key = serialization.load_pem_private_key(
        _normalise_private_pem(private_key_pem).encode(),
        password=None,
    )
    sig = key.sign(data.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def _rsa_sha256_verify(data: str, signature_b64: str, public_pem: str) -> bool:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception as e:  # noqa: BLE001
        raise PaymentError("缺少 cryptography 依赖,无法验签") from e
    pem = _normalise_public_pem(public_pem).encode()
    try:
        public_key = x509.load_pem_x509_certificate(pem).public_key()
    except Exception:
        public_key = serialization.load_pem_public_key(pem)
    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            data.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def _notify_url(provider: str, cfg: payment_config.ProviderRuntimeConfig) -> str:
    configured = str(cfg.public.get("notify_url") or "").strip()
    if configured:
        return configured
    suffix = "/api/payments/alipay/notify" if provider == "alipay" else "/api/payments/wechat/notify"
    return settings.public_base_url.rstrip("/") + suffix


def _alipay_precreate(order: PaymentOrder, cfg: payment_config.ProviderRuntimeConfig) -> tuple[str, dict]:
    notify_url = _notify_url("alipay", cfg)
    expires_at = _aware(order.expires_at)
    timeout_express = None
    if expires_at:
        timeout_minutes = max(1, int((expires_at - _now()).total_seconds() // 60))
        timeout_express = f"{timeout_minutes}m"
    biz_content = {
        "out_trade_no": order.order_no,
        "total_amount": f"{order.amount_cents / 100:.2f}",
        "subject": f"{settings.payment_subject_prefix} {order.credits}积分",
    }
    if timeout_express:
        biz_content["timeout_express"] = timeout_express
    params = {
        "app_id": cfg.public.get("app_id"),
        "method": "alipay.trade.precreate",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": notify_url,
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    sign_src = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["sign"] = _rsa_sha256_sign(sign_src, cfg.secret.get("private_key") or "")
    url = cfg.public.get("gateway_url") or settings.alipay_gateway_url
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, data=params)
    if resp.status_code >= 400:
        raise PaymentError(f"支付宝下单失败:{resp.status_code}")
    data = resp.json()
    body = data.get("alipay_trade_precreate_response") or {}
    if body.get("code") != "10000" or not body.get("qr_code"):
        raise PaymentError(body.get("sub_msg") or body.get("msg") or "支付宝未返回二维码")
    return body["qr_code"], data


def _wechat_authorization(
    method: str,
    path: str,
    body: str,
    cfg: payment_config.ProviderRuntimeConfig,
) -> str:
    nonce = secrets.token_hex(16)
    timestamp = str(int(time.time()))
    message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
    signature = _rsa_sha256_sign(message, cfg.secret.get("private_key") or "")
    return (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{cfg.public.get("mchid")}",'
        f'nonce_str="{nonce}",'
        f'signature="{signature}",'
        f'timestamp="{timestamp}",'
        f'serial_no="{cfg.public.get("serial_no")}"'
    )


def _wechat_native(order: PaymentOrder, cfg: payment_config.ProviderRuntimeConfig) -> tuple[str, dict]:
    path = "/v3/pay/transactions/native"
    notify_url = _notify_url("wechat", cfg)
    expires_at = _aware(order.expires_at)
    payload = {
        "appid": cfg.public.get("appid"),
        "mchid": cfg.public.get("mchid"),
        "description": f"{settings.payment_subject_prefix} {order.credits}积分",
        "out_trade_no": order.order_no,
        "notify_url": notify_url,
        "amount": {"total": order.amount_cents, "currency": "CNY"},
    }
    if expires_at:
        payload["time_expire"] = expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Authorization": _wechat_authorization("POST", path, body, cfg),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = (cfg.public.get("gateway_url") or settings.wechat_pay_gateway_url).rstrip("/") + path
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, headers=headers, content=body)
    if resp.status_code >= 400:
        raise PaymentError(f"微信支付下单失败:{resp.status_code} {resp.text[:160]}")
    data = resp.json()
    if not data.get("code_url"):
        raise PaymentError("微信支付未返回二维码链接")
    return data["code_url"], data


def _provider_code_url(order: PaymentOrder, db: Session) -> tuple[str, dict]:
    cfg = payment_config.runtime_or_env(db, order.provider)
    if not cfg.enabled:
        if mock_payments_allowed():
            cfg = payment_config.ProviderRuntimeConfig(
                provider=order.provider, enabled=True, mode="mock", public={}, secret={}
            )
        else:
            raise PaymentError(f"{order.provider} 支付渠道未启用")
    if cfg.mode == "mock":
        if mock_payments_allowed():
            return _mock_code_url(order.order_no, order.provider, order.credits), {
                "mock": True,
                "provider": order.provider,
            }
        raise PaymentError("当前环境不允许模拟支付")
    if order.provider == "alipay":
        if _alipay_configured(cfg):
            return _alipay_precreate(order, cfg)
    elif order.provider == "wechat":
        if _wechat_configured(cfg):
            return _wechat_native(order, cfg)
    else:
        raise PaymentError("支付渠道非法")

    raise PaymentError(f"{order.provider} 商户参数未配置")


def _provider_raw_subset(data: dict | None, keys: tuple[str, ...]) -> dict:
    if not isinstance(data, dict):
        return {}
    return {key: data.get(key) for key in keys if data.get(key) not in (None, "")}


def _alipay_query(order: PaymentOrder, cfg: payment_config.ProviderRuntimeConfig) -> dict:
    biz_content = {"out_trade_no": order.order_no}
    params = {
        "app_id": cfg.public.get("app_id"),
        "method": "alipay.trade.query",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    sign_src = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["sign"] = _rsa_sha256_sign(sign_src, cfg.secret.get("private_key") or "")
    url = cfg.public.get("gateway_url") or settings.alipay_gateway_url
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, data=params)
    if resp.status_code >= 400:
        raise PaymentError(f"支付宝查单失败:{resp.status_code}")
    data = resp.json()
    body = data.get("alipay_trade_query_response") or {}
    code = str(body.get("code") or "")
    if code != "10000":
        sub_code = str(body.get("sub_code") or "")
        if sub_code == "ACQ.TRADE_NOT_EXIST":
            return {"status": "unknown", "raw": {"code": code, "sub_code": sub_code}}
        raise PaymentError(body.get("sub_msg") or body.get("msg") or "支付宝查单失败")
    if body.get("out_trade_no") and str(body.get("out_trade_no")) != order.order_no:
        raise PaymentError("支付宝查单订单号不匹配")
    if body.get("total_amount"):
        ensure_amount_matches(str(body.get("total_amount")), order.amount_cents, "支付宝查单")
    trade_status = str(body.get("trade_status") or "")
    raw = _provider_raw_subset(
        body,
        ("out_trade_no", "trade_no", "trade_status", "total_amount", "buyer_pay_amount", "send_pay_date"),
    )
    if trade_status in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
        return {"status": PAID, "provider_trade_no": body.get("trade_no"), "raw": raw}
    if trade_status == "TRADE_CLOSED":
        return {"status": CLOSED, "provider_trade_no": body.get("trade_no"), "raw": raw}
    return {"status": PENDING if trade_status else "unknown", "raw": raw}


def _wechat_query(order: PaymentOrder, cfg: payment_config.ProviderRuntimeConfig) -> dict:
    mchid = str(cfg.public.get("mchid") or "").strip()
    path = f"/v3/pay/transactions/out-trade-no/{quote_plus(order.order_no)}?mchid={quote_plus(mchid)}"
    headers = {
        "Authorization": _wechat_authorization("GET", path, "", cfg),
        "Accept": "application/json",
    }
    url = (cfg.public.get("gateway_url") or settings.wechat_pay_gateway_url).rstrip("/") + path
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 404:
        return {"status": "unknown", "raw": {"status_code": 404}}
    if resp.status_code >= 400:
        raise PaymentError(f"微信支付查单失败:{resp.status_code} {resp.text[:160]}")
    data = resp.json()
    if data.get("out_trade_no") and str(data.get("out_trade_no")) != order.order_no:
        raise PaymentError("微信查单订单号不匹配")
    expected_appid = str(cfg.public.get("appid") or "")
    expected_mchid = str(cfg.public.get("mchid") or "")
    if expected_appid and data.get("appid") and str(data.get("appid")) != expected_appid:
        raise PaymentError("微信查单 appid 不匹配")
    if expected_mchid and data.get("mchid") and str(data.get("mchid")) != expected_mchid:
        raise PaymentError("微信查单 mchid 不匹配")
    amount = data.get("amount") or {}
    if amount.get("total") is not None and int(amount.get("total")) != int(order.amount_cents):
        raise PaymentError("微信查单金额不匹配")
    trade_state = str(data.get("trade_state") or "")
    raw = _provider_raw_subset(
        data,
        ("out_trade_no", "transaction_id", "trade_state", "trade_state_desc", "success_time"),
    )
    if amount.get("total") is not None:
        raw["amount"] = {"total": int(amount.get("total")), "currency": amount.get("currency") or "CNY"}
    if trade_state == "SUCCESS":
        return {"status": PAID, "provider_trade_no": data.get("transaction_id"), "raw": raw}
    if trade_state in {"CLOSED", "REVOKED"}:
        return {"status": CLOSED, "provider_trade_no": data.get("transaction_id"), "raw": raw}
    if trade_state == "PAYERROR":
        return {"status": FAILED, "provider_trade_no": data.get("transaction_id"), "raw": raw}
    return {"status": PENDING if trade_state else "unknown", "raw": raw}


def _query_provider_order(db: Session, order: PaymentOrder) -> dict:
    cfg = payment_config.runtime_or_env(db, order.provider)
    if not cfg.enabled or cfg.mode != "live":
        raise PaymentError(f"{order.provider} 支付渠道未启用真实查单")
    if order.provider == "alipay":
        if not _alipay_configured(cfg):
            raise PaymentError("支付宝商户参数未配置")
        return _alipay_query(order, cfg)
    if order.provider == "wechat":
        if not _wechat_configured(cfg):
            raise PaymentError("微信支付商户参数未配置")
        return _wechat_query(order, cfg)
    raise PaymentError("支付渠道非法")


def _apply_provider_query_result(db: Session, order: PaymentOrder, result: dict) -> str:
    """Apply one verified provider-query result to the local order.

    对账兜底与管理端手工补单共用这一段核心逻辑,避免两份状态机。
    返回值:'paid' 已补单入账 / 'closed' / 'failed' 已同步终态 /
    'skipped' 渠道侧未支付或未知,不改本地 / 'noop' 重复入账或并发竞争,无变化。
    """
    status = str(result.get("status") or "unknown")
    raw = {
        "verified_provider_query": True,
        "provider": order.provider,
        "status": status,
        "query": result.get("raw") or {},
    }
    if status == PAID:
        _paid, credited = mark_paid(
            db,
            order.order_no,
            provider=order.provider,
            provider_trade_no=result.get("provider_trade_no"),
            raw=raw,
            allow_expired=True,
        )
        return PAID if credited else "noop"
    if status in {CLOSED, FAILED} and order.status == PENDING:
        locked = db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.id == order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if locked.status == PENDING and locked.paid_at is None:
            locked.status = status
            locked.provider_trade_no = result.get("provider_trade_no") or locked.provider_trade_no
            locked.raw = raw
            db.commit()
            return status
        db.rollback()
        return "noop"
    return "skipped"


def _pending_reconcile_orders(
    db: Session,
    *,
    cutoff: datetime,
    limit: int,
    cursor: int,
    high_water: int,
    exclude_ids: set[int] | None = None,
) -> list[PaymentOrder]:
    filters = (
        PaymentOrder.status.in_((PENDING, CLOSED)),
        PaymentOrder.paid_at.is_(None),
        PaymentOrder.created_at >= cutoff,
    )
    id_filters = [
        PaymentOrder.id > cursor,
        PaymentOrder.id <= high_water,
    ]
    if exclude_ids:
        id_filters.append(PaymentOrder.id.not_in(exclude_ids))
    orders = list(
        db.execute(
            select(PaymentOrder)
            .where(*filters, *id_filters)
            .order_by(PaymentOrder.id.asc())
            .limit(limit)
        ).scalars()
    )
    return orders


def _reconcile_high_water(db: Session, *, cutoff: datetime) -> int:
    return int(
        db.execute(
            select(func.max(PaymentOrder.id)).where(
                PaymentOrder.status.in_((PENDING, CLOSED)),
                PaymentOrder.paid_at.is_(None),
                PaymentOrder.created_at >= cutoff,
            )
        ).scalar_one_or_none()
        or 0
    )


def _coerce_reconcile_position(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _claim_reconcile_state(
    db: Session,
    *,
    owner: str,
    lock_ttl: int,
) -> tuple[int, int] | None:
    row = db.execute(
        select(AppSetting)
        .where(AppSetting.key == _RECONCILE_CURSOR_SETTING)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    observed_value = dict(row.value or {}) if row is not None else None
    value = observed_value or {}
    cursor = _coerce_reconcile_position(value.get("v"))
    high_water = _coerce_reconcile_position(value.get("high_water"))
    claimed_value = {
        "v": cursor,
        "high_water": high_water,
        "owner": owner,
    }
    if not locks.refresh(_RECONCILE_LOCK_KEY, owner, lock_ttl):
        db.rollback()
        return None
    if row is None:
        db.add(AppSetting(key=_RECONCILE_CURSOR_SETTING, value=claimed_value))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
    else:
        result = db.execute(
            update(AppSetting)
            .where(
                AppSetting.key == _RECONCILE_CURSOR_SETTING,
                AppSetting.value == observed_value,
            )
            .values(value=claimed_value)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        db.commit()
    if not locks.refresh(_RECONCILE_LOCK_KEY, owner, lock_ttl):
        return None
    return cursor, high_water


def _persist_reconcile_state(
    db: Session,
    *,
    owner: str,
    cursor: int,
    high_water: int,
) -> bool:
    result = db.execute(
        update(AppSetting)
        .where(
            AppSetting.key == _RECONCILE_CURSOR_SETTING,
            AppSetting.value["owner"].as_string() == owner,
        )
        .values(value={
            "v": _coerce_reconcile_position(cursor),
            "high_water": _coerce_reconcile_position(high_water),
            "owner": owner,
        })
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def reconcile_pending_orders(db: Session) -> dict:
    """Query live providers for unpaid local orders and repair callback gaps.

    Provider notifications remain the primary path. This job is a backstop for
    missed callbacks, late provider success after local expiry, and provider-
    confirmed terminal failures. Unknown/provider-pending results never mutate
    local state.
    """
    stats = {
        "checked": 0,
        "paid": 0,
        "closed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "enabled": bool(settings.payment_reconcile_enabled),
    }
    if not settings.payment_reconcile_enabled or not get_bool_setting(db, "payment_enabled", False):
        return stats
    lock_ttl = max(180, int(settings.gateway_timeout_seconds) + 60)
    lock_token = locks.acquire(_RECONCILE_LOCK_KEY, ttl=lock_ttl)
    if not lock_token:
        return stats
    lookback = max(1, int(settings.payment_reconcile_lookback_hours))
    limit = max(1, min(int(settings.payment_reconcile_max_orders), 500))
    cutoff = _now() - timedelta(hours=lookback)
    try:
        try:
            claimed_state = _claim_reconcile_state(
                db,
                owner=lock_token,
                lock_ttl=lock_ttl,
            )
        except Exception:  # noqa: BLE001
            db.rollback()
            stats["errors"] += 1
            log.warning("failed to claim payment reconcile cursor", exc_info=True)
            return stats
        if claimed_state is None:
            stats["errors"] += 1
            log.warning("payment reconcile lock lost or state changed during cursor claim")
            return stats
        cursor, high_water = claimed_state
        if cursor >= high_water:
            cursor = 0
            high_water = _reconcile_high_water(db, cutoff=cutoff)
        orders = _pending_reconcile_orders(
            db,
            cutoff=cutoff,
            limit=limit,
            cursor=cursor,
            high_water=high_water,
        )
        wrapped = False
        if cursor > 0 and len(orders) < limit:
            high_water = _reconcile_high_water(db, cutoff=cutoff)
            wrapped = True
            orders.extend(
                _pending_reconcile_orders(
                    db,
                    cutoff=cutoff,
                    limit=limit - len(orders),
                    cursor=0,
                    high_water=high_water,
                    exclude_ids={order.id for order in orders},
                )
            )
        processed_ids = []
        for order in orders:
            if not locks.refresh(_RECONCILE_LOCK_KEY, lock_token, lock_ttl):
                log.warning("payment reconcile lock lost before order_no=%s", order.order_no)
                break
            try:
                result = _query_provider_order(db, order)
                stats["checked"] += 1
                outcome = _apply_provider_query_result(db, order, result)
                if outcome == "skipped":
                    stats["skipped"] += 1
                elif outcome in {PAID, CLOSED, FAILED}:
                    stats[outcome] += 1
            except Exception as e:  # noqa: BLE001
                db.rollback()
                stats["errors"] += 1
                log.warning(
                    "payment reconcile failed order_no=%s provider=%s error=%s",
                    getattr(order, "order_no", None),
                    getattr(order, "provider", None),
                    str(e)[:200],
                )
            finally:
                processed_ids.append(order.id)
        next_cursor = 0
        if high_water:
            if wrapped and processed_ids:
                next_cursor = processed_ids[-1]
            elif len(orders) < limit:
                next_cursor = high_water
            elif processed_ids:
                next_cursor = processed_ids[-1]
        if high_water:
            if not locks.refresh(_RECONCILE_LOCK_KEY, lock_token, lock_ttl):
                db.rollback()
                stats["errors"] += 1
                log.warning(
                    "payment reconcile lock lost before cursor persist cursor=%s",
                    next_cursor,
                )
            else:
                try:
                    persisted = _persist_reconcile_state(
                        db,
                        owner=lock_token,
                        cursor=next_cursor,
                        high_water=high_water,
                    )
                    if not persisted:
                        stats["errors"] += 1
                        log.warning(
                            "payment reconcile cursor fence rejected owner cursor=%s",
                            next_cursor,
                        )
                except Exception:  # noqa: BLE001
                    db.rollback()
                    stats["errors"] += 1
                    log.warning("failed to persist payment reconcile cursor", exc_info=True)
        return stats
    finally:
        locks.release(_RECONCILE_LOCK_KEY, lock_token)


def create_order(db: Session, user: User, provider: str, package_id: str) -> PaymentOrder:
    if not get_bool_setting(db, "payment_enabled", False):
        raise PaymentError("支付充值功能未开启")
    if provider not in VALID_PROVIDERS:
        raise PaymentError("支付渠道非法")
    pkg = package_by_id(db, package_id)
    _assert_positive_package(pkg)
    lock_key = f"payment:create:{user.id}"
    lock_ttl = max(180, int(settings.gateway_timeout_seconds) + 60)
    lock_token = locks.acquire(lock_key, ttl=lock_ttl)
    if not lock_token:
        raise PaymentError("支付订单正在创建,请稍后重试")
    try:
        now = _now()
        db.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.user_id == user.id,
                PaymentOrder.status == PENDING,
                PaymentOrder.expires_at < now,
            )
            .values(status=CLOSED)
        )
        db.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.user_id == user.id,
                PaymentOrder.status == PENDING,
                PaymentOrder.code_url.is_(None),
                PaymentOrder.paid_at.is_(None),
            )
            .values(
                status=FAILED,
                raw={"error": "支付二维码未创建完成,订单已自动关闭,请重新下单"},
            )
        )
        pending_count = db.execute(
            select(func.count())
            .select_from(PaymentOrder)
            .where(
                PaymentOrder.user_id == user.id,
                PaymentOrder.status == PENDING,
                PaymentOrder.expires_at >= now,
            )
        ).scalar_one()
        if int(pending_count or 0) >= int(settings.payment_order_pending_limit):
            db.commit()
            raise PaymentError("待支付订单过多,请先完成或等待旧订单过期")
        order = PaymentOrder(
            order_no=make_order_no(provider),
            user_id=user.id,
            provider=provider,
            package_id=pkg["id"],
            amount_cents=int(pkg["amount_cents"]),
            credits=int(pkg["credits"]),
            status=PENDING,
            expires_at=now + timedelta(minutes=settings.payment_order_expire_minutes),
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        try:
            code_url, raw = _provider_code_url(order, db)
        except Exception as e:
            order.status = FAILED
            order.raw = {"error": str(e)[:500]}
            db.commit()
            raise
        order.code_url = code_url
        order.raw = raw
        db.commit()
        db.refresh(order)
        return order
    finally:
        locks.release(lock_key, lock_token)


def user_order(db: Session, order_no: str, user_id: int) -> PaymentOrder | None:
    return db.execute(
        select(PaymentOrder).where(
            PaymentOrder.order_no == order_no,
            PaymentOrder.user_id == user_id,
        )
    ).scalar_one_or_none()


def list_user_orders(
    db: Session,
    user_id: int,
    limit: int = 20,
    *,
    offset: int = 0,
    before_id: int | None = None,
) -> list[PaymentOrder]:
    """List one user's orders, newest first.

    分页与积分账单游标同思路:订单 id 追加只增,`before_id` 传上一页最后一条的
    id 即为稳定游标;`offset` 供无游标场景兜底。
    """
    stmt = select(PaymentOrder).where(PaymentOrder.user_id == user_id)
    if before_id is not None and int(before_id) > 0:
        stmt = stmt.where(PaymentOrder.id < int(before_id))
    orders = list(
        db.execute(
            stmt.order_by(PaymentOrder.id.desc())
            .offset(max(int(offset), 0))
            .limit(min(max(limit, 1), 100))
        ).scalars()
    )
    return [with_display_status(order) for order in orders]


def list_user_invoice_orders(db: Session, user_id: int, limit: int = 100) -> list[PaymentOrder]:
    return list(
        db.execute(
            select(PaymentOrder)
            .where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.invoice_status != "none",
            )
            .order_by(PaymentOrder.id.desc())
            .limit(min(max(limit, 1), 200))
        ).scalars()
    )


def get_order(db: Session, order_no: str) -> PaymentOrder | None:
    return db.execute(
        select(PaymentOrder).where(PaymentOrder.order_no == order_no)
    ).scalar_one_or_none()


def mark_paid(
    db: Session,
    order_no: str,
    *,
    provider: str | None = None,
    provider_trade_no: str | None = None,
    raw: dict | None = None,
    allow_expired: bool = False,
) -> tuple[PaymentOrder, bool]:
    order = db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.order_no == order_no)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not order:
        raise PaymentError("订单不存在")
    if provider and order.provider != provider:
        raise PaymentError("订单支付渠道不匹配")
    _assert_positive_order(order)
    provider_trade_no = (provider_trade_no or "").strip()
    if order.status == PAID:
        if not provider_trade_no:
            raise PaymentError("支付通知缺少第三方流水号")
        if order.provider_trade_no and order.provider_trade_no != provider_trade_no:
            raise PaymentError("支付流水号与已入账订单不匹配")
        return order, False
    if provider_trade_no:
        existing = db.execute(
            select(PaymentOrder).where(
                PaymentOrder.provider == order.provider,
                PaymentOrder.provider_trade_no == provider_trade_no,
                PaymentOrder.id != order.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise PaymentError("支付流水号已入账到其他订单")
    expires_at = _aware(order.expires_at)
    now = _now()
    if expires_at and expires_at < now and not allow_expired:
        order.status = CLOSED
        db.commit()
        raise PaymentError("订单已过期")
    payable_statuses = (PENDING,)
    if allow_expired and order.status == CLOSED and not order.paid_at:
        payable_statuses = (PENDING, CLOSED)
    if order.status not in payable_statuses:
        raise PaymentError("订单状态不可入账")
    if not provider_trade_no:
        raise PaymentError("支付通知缺少第三方流水号")
    order.status = PAID
    order.paid_at = _now()
    order.provider_trade_no = provider_trade_no
    if raw is not None:
        order.raw = raw
    credits.grant(
        db,
        order.user_id,
        order.credits,
        note=f"payment {order.provider} {order.order_no}",
        biz_type="payment",
        biz_ref=order.id,
        commit=False,
    )
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        message = _provider_trade_no_conflict_message(e)
        if message:
            raise PaymentError(message) from e
        raise
    db.refresh(order)
    publish_user_event(
        order.user_id,
        "payment_paid",
        {
            "order_no": order.order_no,
            "provider": order.provider,
            "credits": order.credits,
            "amount_cents": order.amount_cents,
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        },
    )
    return order, True


def with_display_status(order: PaymentOrder) -> PaymentOrder:
    if order.status == PENDING and _aware(order.expires_at) and _aware(order.expires_at) < _now():
        set_committed_value(order, "status", CLOSED)
    return order


def amount_to_cents(amount: str) -> int:
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as e:
        raise PaymentError("支付金额非法") from e
    cents = value * Decimal(100)
    if value < 0 or cents != cents.to_integral_value():
        raise PaymentError("支付金额非法")
    return int(cents)


def ensure_amount_matches(amount: str, expected_cents: int, provider_label: str) -> None:
    if amount in (None, ""):
        raise PaymentError(f"{provider_label}通知缺少金额")
    if amount_to_cents(str(amount)) != int(expected_cents):
        raise PaymentError(f"{provider_label}通知金额不匹配")


def verify_alipay_notify(db: Session, form: dict) -> tuple[str, str]:
    order_no = str(form.get("out_trade_no") or "")
    total_amount = str(form.get("total_amount") or "")
    trade_status = str(form.get("trade_status") or "")
    if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        raise PaymentError("支付宝订单未支付")
    if not order_no:
        raise PaymentError("支付宝通知缺少订单号")
    if not total_amount:
        raise PaymentError("支付宝通知缺少金额")
    cfg = payment_config.runtime_or_env(db, "alipay")
    app_id = str(form.get("app_id") or "")
    expected_app_id = str(cfg.public.get("app_id") or "")
    if expected_app_id:
        if not app_id:
            raise PaymentError("支付宝通知缺少 app_id")
        if app_id != expected_app_id:
            raise PaymentError("支付宝通知 app_id 不匹配")
    expected_seller_id = str(cfg.public.get("seller_id") or "").strip()
    if expected_seller_id:
        supplied_seller_id = str(
            form.get("seller_id")
            or form.get("seller_email")
            or form.get("seller_user_id")
            or ""
        ).strip()
        if not supplied_seller_id:
            raise PaymentError("支付宝通知缺少 seller_id")
        if supplied_seller_id != expected_seller_id:
            raise PaymentError("支付宝通知 seller_id 不匹配")
    signature = str(form.get("sign") or "")
    sign_src = "&".join(
        f"{k}={form[k]}" for k in sorted(form)
        if k not in ("sign", "sign_type") and form.get(k) is not None
    )
    public_key = str(cfg.secret.get("public_key") or "")
    if not public_key:
        raise PaymentError("支付宝公钥未配置,无法验签")
    if not signature or not _rsa_sha256_verify(sign_src, signature, public_key):
        raise PaymentError("支付宝通知验签失败")
    return order_no, total_amount


def wechat_signature_valid(db: Session, headers: dict, body: bytes) -> bool:
    cfg = payment_config.runtime_or_env(db, "wechat")
    platform_cert = cfg.secret.get("platform_cert_pem")
    if not platform_cert:
        return False
    expected_serial = str(cfg.public.get("platform_serial_no") or "").strip()
    supplied_serial = (
        headers.get("Wechatpay-Serial")
        or headers.get("wechatpay-serial")
        or headers.get("Wechatpay-Serial-No")
        or headers.get("wechatpay-serial-no")
    )
    if expected_serial and supplied_serial != expected_serial:
        log.warning(
            "wechat notify platform serial mismatch expected=%s supplied=%s",
            expected_serial,
            supplied_serial,
        )
        return False
    supplied = headers.get("Wechatpay-Signature") or headers.get("wechatpay-signature")
    timestamp = headers.get("Wechatpay-Timestamp") or headers.get("wechatpay-timestamp")
    nonce = headers.get("Wechatpay-Nonce") or headers.get("wechatpay-nonce")
    if not supplied or not timestamp or not nonce:
        return False
    try:
        ts = int(str(timestamp))
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > 300:
        return False
    try:
        decoded_body = body.decode()
    except UnicodeDecodeError:
        return False
    message = f"{timestamp}\n{nonce}\n{decoded_body}\n"
    return _rsa_sha256_verify(message, supplied, platform_cert)


def decrypt_wechat_resource(db: Session, resource: dict) -> dict:
    if not resource:
        return {}
    if resource.get("out_trade_no"):
        return resource
    cfg = payment_config.runtime_or_env(db, "wechat")
    api_v3_key = str(cfg.secret.get("api_v3_key") or "")
    if not api_v3_key:
        if mock_payments_allowed():
            return resource
        raise PaymentError("微信支付 APIv3 密钥未配置,无法解密通知")
    if len(api_v3_key.encode()) != 32:
        raise PaymentError("微信支付 APIv3 密钥长度必须为 32 字节")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as e:  # noqa: BLE001
        raise PaymentError("缺少 cryptography 依赖,无法解密微信通知") from e
    try:
        aesgcm = AESGCM(api_v3_key.encode())
        plaintext = aesgcm.decrypt(
            str(resource["nonce"]).encode(),
            base64.b64decode(resource["ciphertext"]),
            str(resource.get("associated_data") or "").encode(),
        )
        return json.loads(plaintext.decode())
    except Exception as e:  # noqa: BLE001
        raise PaymentError("微信支付通知解密失败") from e


def ensure_wechat_merchant_matches(db: Session, resource: dict) -> None:
    if not isinstance(resource, dict):
        return
    cfg = payment_config.runtime_or_env(db, "wechat")
    appid = str(resource.get("appid") or "")
    mchid = str(resource.get("mchid") or "")
    expected_appid = str(cfg.public.get("appid") or "")
    expected_mchid = str(cfg.public.get("mchid") or "")
    if expected_appid:
        if not appid:
            raise PaymentError("微信通知缺少 appid")
        if appid != expected_appid:
            raise PaymentError("微信通知 appid 不匹配")
    if expected_mchid:
        if not mchid:
            raise PaymentError("微信通知缺少 mchid")
        if mchid != expected_mchid:
            raise PaymentError("微信通知 mchid 不匹配")


def ensure_wechat_notify_success(payload: dict, resource: dict) -> None:
    event_type = str(payload.get("event_type") or "")
    if event_type and event_type != "TRANSACTION.SUCCESS":
        raise PaymentError("微信通知不是支付成功事件")
    trade_state = str(resource.get("trade_state") or "")
    if trade_state != "SUCCESS":
        raise PaymentError("微信订单未支付成功")
    if not resource.get("success_time"):
        raise PaymentError("微信通知缺少支付成功时间")


# --- 管理端订单查询 / 手工补单 ---------------------------------------------

def admin_order_dict(order: PaymentOrder, phone: str | None = None) -> dict:
    order = with_display_status(order)
    return {
        "id": int(order.id),
        "order_no": order.order_no,
        "user_id": int(order.user_id),
        "phone": phone,
        "provider": order.provider,
        "package_id": order.package_id,
        "amount_cents": int(order.amount_cents),
        "credits": int(order.credits),
        "status": order.status,
        "provider_trade_no": order.provider_trade_no,
        "refunded_amount_cents": int(order.refunded_amount_cents or 0),
        "refunded_at": order.refunded_at,
        "invoice_status": order.invoice_status or "none",
        "invoice_type": order.invoice_type,
        "invoice_title": order.invoice_title,
        "invoice_tax_no": order.invoice_tax_no,
        "invoice_email": order.invoice_email,
        "invoice_note": order.invoice_note,
        "invoice_requested_at": order.invoice_requested_at,
        "invoice_issued_at": order.invoice_issued_at,
        "expires_at": order.expires_at,
        "paid_at": order.paid_at,
        "created_at": order.created_at,
    }


def admin_search_orders(
    db: Session,
    *,
    phone: str | None = None,
    order_no: str | None = None,
    provider_trade_no: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    invoice_status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """客服查单:按手机号/订单号/渠道单号/时间范围过滤,不限 24h 对账窗口。"""
    filters = []
    if phone and phone.strip():
        filters.append(User.phone == phone.strip())
    if order_no and order_no.strip():
        filters.append(PaymentOrder.order_no == order_no.strip())
    if provider_trade_no and provider_trade_no.strip():
        filters.append(PaymentOrder.provider_trade_no == provider_trade_no.strip())
    if provider:
        filters.append(PaymentOrder.provider == provider)
    if status:
        filters.append(PaymentOrder.status == status)
    if invoice_status == "any":
        filters.append(PaymentOrder.invoice_status != "none")
    elif invoice_status:
        filters.append(PaymentOrder.invoice_status == invoice_status)
    if created_from is not None:
        filters.append(PaymentOrder.created_at >= created_from)
    if created_to is not None:
        filters.append(PaymentOrder.created_at <= created_to)
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    joined = select(PaymentOrder, User.phone).join(User, User.id == PaymentOrder.user_id)
    total = int(
        db.scalar(
            select(func.count())
            .select_from(PaymentOrder)
            .join(User, User.id == PaymentOrder.user_id)
            .where(*filters)
        )
        or 0
    )
    rows = db.execute(
        joined.where(*filters)
        .order_by(PaymentOrder.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [admin_order_dict(order, user_phone) for order, user_phone in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def admin_sync_order(db: Session, order_no: str) -> tuple[dict, str]:
    """单笔主动向渠道查单并补单。

    复用对账兜底的 `_query_provider_order` + `_apply_provider_query_result`,
    与自动对账保持同一套状态机;渠道确认支付会立即入账积分。
    """
    order = get_order(db, order_no)
    if not order:
        raise PaymentError("订单不存在")
    result = _query_provider_order(db, order)
    outcome = _apply_provider_query_result(db, order, result)
    refreshed = get_order(db, order_no)
    return admin_order_dict(refreshed), outcome


# --- 渠道退款 ---------------------------------------------------------------

def refunds_enabled() -> bool:
    # 渠道退款未在真实商户环境联调过,默认关闭;需显式配置
    # PAYMENT_REFUND_ENABLED=true 才允许发起。getattr 兜底以兼容尚未
    # 添加该配置项的部署。
    return bool(getattr(settings, "payment_refund_enabled", False))


def make_refund_no(provider: str) -> str:
    return f"RF{provider[:1].upper()}{int(time.time())}{secrets.token_hex(5)}"


def _alipay_refund(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> dict:
    biz_content = {
        "out_trade_no": order.order_no,
        "refund_amount": f"{refund.amount_cents / 100:.2f}",
        "out_request_no": refund.refund_no,
        "refund_reason": (refund.reason or "")[:80] or "订单退款",
    }
    params = {
        "app_id": cfg.public.get("app_id"),
        "method": "alipay.trade.refund",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    sign_src = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["sign"] = _rsa_sha256_sign(sign_src, cfg.secret.get("private_key") or "")
    url = cfg.public.get("gateway_url") or settings.alipay_gateway_url
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, data=params)
    if resp.status_code >= 400:
        raise PaymentError(f"支付宝退款失败:{resp.status_code}")
    data = resp.json()
    body = data.get("alipay_trade_refund_response") or {}
    if str(body.get("code") or "") != "10000":
        raise PaymentError(body.get("sub_msg") or body.get("msg") or "支付宝退款失败")
    if body.get("out_trade_no") and str(body.get("out_trade_no")) != order.order_no:
        raise PaymentError("支付宝退款订单号不匹配")
    raw = _provider_raw_subset(
        body,
        ("out_trade_no", "trade_no", "buyer_logon_id", "fund_change", "refund_fee", "gmt_refund_pay"),
    )
    return {"provider_refund_no": body.get("trade_no"), "raw": raw}


def _wechat_refund(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> dict:
    path = "/v3/refund/domestic/refunds"
    payload = {
        "out_trade_no": order.order_no,
        "out_refund_no": refund.refund_no,
        "reason": (refund.reason or "")[:80] or "订单退款",
        "amount": {
            "refund": int(refund.amount_cents),
            "total": int(order.amount_cents),
            "currency": "CNY",
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Authorization": _wechat_authorization("POST", path, body, cfg),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = (cfg.public.get("gateway_url") or settings.wechat_pay_gateway_url).rstrip("/") + path
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, headers=headers, content=body)
    if resp.status_code >= 400:
        raise PaymentError(f"微信退款失败:{resp.status_code} {resp.text[:160]}")
    data = resp.json()
    refund_status = str(data.get("status") or "")
    # 微信退款为异步受理:SUCCESS 立即成功;PROCESSING 表示已受理但尚未到账,
    # 不能当成功入账——退款单保持 pending,由 _resolve_pending_refunds 在
    # 下次退款/管理员操作时按 out_refund_no 查询收敛终态。
    if refund_status not in {"SUCCESS", "PROCESSING"}:
        raise PaymentError(f"微信退款状态异常:{refund_status or '未知'}")
    raw = _provider_raw_subset(
        data,
        ("refund_id", "out_refund_no", "out_trade_no", "status", "channel", "success_time"),
    )
    return {
        "provider_refund_no": data.get("refund_id"),
        "raw": raw,
        "processing": refund_status == "PROCESSING",
    }


def _provider_refund(db: Session, order: PaymentOrder, refund: PaymentRefund) -> dict:
    cfg = payment_config.runtime_or_env(db, order.provider)
    if not cfg.enabled or cfg.mode == "mock":
        if mock_payments_allowed():
            return {
                "provider_refund_no": f"mock_refund_{refund.refund_no}",
                "raw": {"mock": True, "provider": order.provider},
            }
        raise PaymentError(f"{order.provider} 支付渠道未启用真实退款")
    if order.provider == "alipay":
        if not _alipay_configured(cfg):
            raise PaymentError("支付宝商户参数未配置")
        return _alipay_refund(order, refund, cfg)
    if order.provider == "wechat":
        if not _wechat_configured(cfg):
            raise PaymentError("微信支付商户参数未配置")
        return _wechat_refund(order, refund, cfg)
    raise PaymentError("支付渠道非法")


def _reclaimed_credits_before(db: Session, order_id: int) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(PaymentRefund.credits_reclaimed), 0)).where(
                PaymentRefund.order_id == order_id,
                PaymentRefund.status == REFUND_SUCCEEDED,
            )
        )
        or 0
    )


def _alipay_refund_query(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> str:
    """向支付宝按 out_request_no 查退款结果:succeeded/failed,不确定时抛错。"""
    biz_content = {
        "out_trade_no": order.order_no,
        "out_request_no": refund.refund_no,
    }
    params = {
        "app_id": cfg.public.get("app_id"),
        "method": "alipay.trade.fastpay.refund.query",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    sign_src = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["sign"] = _rsa_sha256_sign(sign_src, cfg.secret.get("private_key") or "")
    url = cfg.public.get("gateway_url") or settings.alipay_gateway_url
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, data=params)
    if resp.status_code >= 400:
        raise PaymentError(f"支付宝退款查询失败:{resp.status_code}")
    body = (resp.json() or {}).get("alipay_trade_fastpay_refund_query_response") or {}
    code = str(body.get("code") or "")
    sub_code = str(body.get("sub_code") or "")
    if code != "10000":
        if sub_code in {"ACQ.TRADE_NOT_EXIST", "TRADE_NOT_EXIST"}:
            # 渠道无此交易/退款请求:原退款调用从未到达渠道
            return "failed"
        raise PaymentError(body.get("sub_msg") or body.get("msg") or "支付宝退款查询失败")
    if str(body.get("refund_status") or "") == "REFUND_SUCCESS":
        return "succeeded"
    if body.get("refund_amount"):
        # 退款请求存在但尚未终态:不可当失败回滚,留待下次收敛
        return "processing"
    return "failed"


def _wechat_refund_query(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> str:
    """按 out_refund_no 查微信退款结果:succeeded/failed/processing,不确定时抛错。"""
    path = f"/v3/refund/domestic/refunds/{refund.refund_no}"
    headers = {
        "Authorization": _wechat_authorization("GET", path, "", cfg),
        "Accept": "application/json",
    }
    url = (cfg.public.get("gateway_url") or settings.wechat_pay_gateway_url).rstrip("/") + path
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 404:
        # 渠道无此退款单:原退款调用从未到达渠道
        return "failed"
    if resp.status_code >= 400:
        raise PaymentError(f"微信退款查询失败:{resp.status_code} {resp.text[:160]}")
    status = str((resp.json() or {}).get("status") or "")
    if status == "SUCCESS":
        return "succeeded"
    if status == "PROCESSING":
        return "processing"
    if status in {"CLOSED", "ABNORMAL"}:
        # ABNORMAL 表示打款到账失败,需商户后台人工处理;资金未到用户手中,
        # 平台侧按失败回滚积分,商户侧余款原路留存。
        return "failed"
    raise PaymentError(f"微信退款状态无法识别:{status or '空'}")


def _provider_refund_query(db: Session, order: PaymentOrder, refund: PaymentRefund) -> str:
    """查询渠道退款终态。返回 succeeded/failed/processing;网络或响应不可判定时抛
    PaymentError,调用方绝不能在不确定时二次扣积分或二次打款。"""
    cfg = payment_config.runtime_or_env(db, order.provider)
    if not cfg.enabled or cfg.mode == "mock":
        # mock 渠道没有真实打款,pending 残留只可能是成功落账前崩溃 -> 按失败回滚
        return "failed"
    if order.provider == "alipay":
        return _alipay_refund_query(order, refund, cfg)
    if order.provider == "wechat":
        return _wechat_refund_query(order, refund, cfg)
    raise PaymentError("支付渠道非法")


def _resolve_pending_refunds(db: Session, order: PaymentOrder) -> None:
    """把该订单的 pending 退款单收敛为终态,再允许发起新退款。

    崩溃残留的 pending 意味着积分已扣、渠道结果未知:必须先向渠道按
    refund_no 查询——成功则补记 refunded_amount,失败则回滚积分,仍在
    处理中或查询失败则拒绝本次新退款,绝不在不确定时二次扣款/打款。
    """
    pending = list(
        db.execute(
            select(PaymentRefund)
            .where(
                PaymentRefund.order_id == order.id,
                PaymentRefund.status == REFUND_PENDING,
            )
            .with_for_update()
        ).scalars()
    )
    for refund in pending:
        # 查询失败(网络/不可判定)直接抛出:该单保持 pending,之前已收敛的
        # 单据均已各自提交,不受影响。
        outcome = _provider_refund_query(db, order, refund)
        if outcome == "processing":
            raise PaymentError(f"退款单 {refund.refund_no} 渠道仍在处理中,请稍后再试")
        try:
            if outcome == "succeeded":
                refund.status = REFUND_SUCCEEDED
                refund.error = None
                order.refunded_amount_cents = (
                    int(order.refunded_amount_cents or 0) + int(refund.amount_cents)
                )
                if int(order.refunded_amount_cents) >= int(order.amount_cents):
                    order.status = REFUNDED
                    order.refunded_at = _now()
            else:
                # 先补回积分再置终态:任一步失败则整单回滚保持 pending,
                # 绝不提交"已失败但积分未回滚"的中间态。
                if int(refund.credits_reclaimed or 0) > 0:
                    credits.refund_consumed(
                        db,
                        order.user_id,
                        int(refund.credits_reclaimed),
                        biz_type="payment",
                        biz_ref=order.id,
                        note=f"payment_refund_rollback {refund.refund_no}",
                        commit=False,
                    )
                refund.status = REFUND_FAILED
                refund.error = "待确认退款经渠道查询未成功,已回滚积分扣减"
            db.commit()
        except Exception:
            db.rollback()
            raise


def refund_order(
    db: Session,
    order_no: str,
    *,
    operator_id: int | None,
    reason: str,
    amount_cents: int | None = None,
) -> PaymentRefund:
    """Refund a paid order (full or partial) through the original channel.

    顺序是"先扣回积分,再调渠道":渠道打款不可逆,而积分扣减随时可以正向补回。
    渠道调用失败时补回已扣积分并把退款单标记 failed,保证账实一致。
    """
    if not refunds_enabled():
        raise PaymentError("渠道退款功能未开启(需配置 PAYMENT_REFUND_ENABLED=true 并完成真实商户联调)")
    reason = (reason or "").strip()
    if not reason:
        raise PaymentError("请填写退款原因")
    lock_key = f"payment:refund:{order_no}"
    lock_ttl = max(180, int(settings.gateway_timeout_seconds) + 60)
    lock_token = locks.acquire(lock_key, ttl=lock_ttl)
    if not lock_token:
        raise PaymentError("该订单退款正在处理中,请稍后重试")
    try:
        order = db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.order_no == order_no)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if not order:
            raise PaymentError("订单不存在")
        if order.status not in (PAID, REFUNDED) or not order.paid_at:
            raise PaymentError("仅已支付订单可以退款")
        # 先把崩溃/异步残留的 pending 退款单收敛为终态(成功补记已退金额、
        # 失败回滚积分、处理中或查询失败则拒绝本次请求),再计算可退余额,
        # 否则重试会对同一笔钱二次扣积分、二次向渠道打款。
        _resolve_pending_refunds(db, order)
        remaining = int(order.amount_cents) - int(order.refunded_amount_cents or 0)
        if remaining <= 0:
            raise PaymentError("订单已全额退款")
        amount = int(amount_cents) if amount_cents is not None else remaining
        if amount <= 0:
            raise PaymentError("退款金额必须大于 0")
        if amount > remaining:
            raise PaymentError(f"退款金额超出可退余额(剩余可退 {remaining} 分)")
        reclaimed_before = _reclaimed_credits_before(db, order.id)
        credits_remaining = max(0, int(order.credits) - reclaimed_before)
        if amount == remaining:
            # 最后一笔退清:扣回剩余全部积分,避免比例取整留尾差
            credits_to_reclaim = credits_remaining
        else:
            credits_to_reclaim = min(
                credits_remaining,
                int(order.credits) * amount // int(order.amount_cents),
            )
        refund = PaymentRefund(
            refund_no=make_refund_no(order.provider),
            order_id=order.id,
            user_id=order.user_id,
            provider=order.provider,
            amount_cents=amount,
            credits_reclaimed=credits_to_reclaim,
            status=REFUND_PENDING,
            reason=reason[:255],
            operator_id=operator_id,
        )
        db.add(refund)
        if credits_to_reclaim > 0:
            try:
                credits.deduct(
                    db,
                    order.user_id,
                    credits_to_reclaim,
                    note=f"payment_refund {order.provider} {order.order_no} {refund.refund_no}",
                    biz_type="payment",
                    biz_ref=order.id,
                    commit=False,
                )
            except credits.InsufficientCredits as e:
                db.rollback()
                raise PaymentError(
                    f"用户可用积分不足,无法扣回本次退款对应的 {credits_to_reclaim} 积分;"
                    "请先与用户确认或改用更小金额的部分退款"
                ) from e
        db.commit()
        db.refresh(refund)
        try:
            result = _provider_refund(db, order, refund)
        except Exception as e:  # noqa: BLE001
            # 渠道失败:补回已扣积分并把退款单标记 failed
            if credits_to_reclaim > 0:
                credits.refund_consumed(
                    db,
                    order.user_id,
                    credits_to_reclaim,
                    biz_type="payment",
                    biz_ref=order.id,
                    note=f"payment_refund_rollback {refund.refund_no}",
                    commit=False,
                )
            refund.status = REFUND_FAILED
            refund.error = str(e)[:500]
            db.commit()
            raise PaymentError(f"渠道退款失败,已回滚积分扣减:{str(e)[:200]}") from e
        refund.provider_refund_no = str(result.get("provider_refund_no") or "") or None
        refund.raw = result.get("raw") or {}
        if result.get("processing"):
            # 渠道已受理但未到账(微信 PROCESSING):保持 pending,不计入已退
            # 金额;积分已扣不回滚(资金大概率在途),终态由下次退款操作或
            # 管理员触发的 _resolve_pending_refunds 查询收敛。
            db.commit()
            db.refresh(refund)
            return refund
        refund.status = REFUND_SUCCEEDED
        order.refunded_amount_cents = int(order.refunded_amount_cents or 0) + amount
        if int(order.refunded_amount_cents) >= int(order.amount_cents):
            order.status = REFUNDED
            order.refunded_at = _now()
        db.commit()
        db.refresh(refund)
        publish_user_event(
            order.user_id,
            "payment_refunded",
            {
                "order_no": order.order_no,
                "provider": order.provider,
                "refund_no": refund.refund_no,
                "amount_cents": int(refund.amount_cents),
                "credits_reclaimed": int(refund.credits_reclaimed or 0),
            },
        )
        return refund
    finally:
        locks.release(lock_key, lock_token)


def list_order_refunds(db: Session, order_id: int) -> list[PaymentRefund]:
    return list(
        db.execute(
            select(PaymentRefund)
            .where(PaymentRefund.order_id == order_id)
            .order_by(PaymentRefund.id.desc())
        ).scalars()
    )
