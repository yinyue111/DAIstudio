"""Payment-provider signing, HTTP transport, order creation, and query helpers."""
from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from ..config import settings
from ..models import PaymentOrder
from . import payment_config
from .payments import (
    CLOSED,
    FAILED,
    PAID,
    PENDING,
    PaymentError,
    _alipay_configured,
    _aware,
    _mock_code_url,
    _now,
    _wechat_configured,
    mock_payments_allowed,
)
from .ssrf import pinned_client


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
