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

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..models import PaymentOrder, User
from . import credits, locks, payment_config
from .config_store import get_bool_setting

PAYMENT_PACKAGES = payment_config.DEFAULT_PAYMENT_PACKAGES
log = logging.getLogger("payments")

VALID_PROVIDERS = {"alipay", "wechat"}
PENDING = "pending"
PAID = "paid"
CLOSED = "closed"
FAILED = "failed"


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
    return "-----BEGIN PRIVATE KEY-----\n" + _wrap_pem_body(text) + "\n-----END PRIVATE KEY-----"


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
    with httpx.Client(timeout=settings.gateway_timeout_seconds) as client:
        resp = client.post(cfg.public.get("gateway_url") or settings.alipay_gateway_url, data=params)
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
    with httpx.Client(timeout=settings.gateway_timeout_seconds) as client:
        resp = client.post((cfg.public.get("gateway_url") or settings.wechat_pay_gateway_url).rstrip("/") + path,
                           headers=headers, content=body)
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


def list_user_orders(db: Session, user_id: int, limit: int = 20) -> list[PaymentOrder]:
    db.execute(
        update(PaymentOrder)
        .where(
            PaymentOrder.user_id == user_id,
            PaymentOrder.status == PENDING,
            PaymentOrder.expires_at < _now(),
        )
        .values(status=CLOSED)
    )
    db.commit()
    return list(
        db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.user_id == user_id)
            .order_by(PaymentOrder.id.desc())
            .limit(min(max(limit, 1), 100))
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
    ).scalar_one_or_none()
    if not order:
        raise PaymentError("订单不存在")
    if provider and order.provider != provider:
        raise PaymentError("订单支付渠道不匹配")
    _assert_positive_order(order)
    if order.status == PAID:
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
    order.status = PAID
    order.paid_at = _now()
    order.provider_trade_no = provider_trade_no or order.provider_trade_no
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
    db.commit()
    db.refresh(order)
    return order, True


def close_expired(order: PaymentOrder, db: Session) -> PaymentOrder:
    if order.status == PENDING and _aware(order.expires_at) and _aware(order.expires_at) < _now():
        order.status = CLOSED
        db.commit()
        db.refresh(order)
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
