"""Provider callback verification and notification validation."""
from __future__ import annotations

import base64
import json
import time

from sqlalchemy.orm import Session

from . import payment_config
from .payment_transport import _rsa_sha256_verify
from .payments import PaymentError, log, mock_payments_allowed


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
