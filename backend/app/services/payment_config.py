"""Runtime-editable payment packages and merchant configuration."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import PaymentPackage, PaymentProviderConfig

DEFAULT_PAYMENT_PACKAGES = [
    {"id": "starter", "title": "轻量包", "amount_cents": 990, "credits": 120,
     "badge": None, "enabled": True, "sort_order": 10},
    {"id": "creator", "title": "创作包", "amount_cents": 2990, "credits": 420,
     "badge": "常用", "enabled": True, "sort_order": 20},
    {"id": "pro", "title": "专业包", "amount_cents": 9990, "credits": 1600,
     "badge": "更划算", "enabled": True, "sort_order": 30},
]

VALID_PROVIDERS = {"alipay", "wechat"}
PUBLIC_FIELDS = {
    "alipay": {"app_id", "seller_id", "gateway_url", "notify_url"},
    "wechat": {"appid", "mchid", "serial_no", "platform_serial_no", "gateway_url", "notify_url"},
}
SECRET_FIELDS = {
    "alipay": {"private_key", "public_key"},
    "wechat": {"private_key", "api_v3_key", "platform_cert_pem"},
}
PAYMENT_GATEWAY_HOSTS = {
    "alipay": {"openapi.alipay.com", "openapi-sandbox.dl.alipaydev.com"},
    "wechat": {"api.mch.weixin.qq.com"},
}
NOTIFY_PATHS = {
    "alipay": "/api/payments/alipay/notify",
    "wechat": "/api/payments/wechat/notify",
}
_ADMIN_OVERRIDE_KEY = "_admin_override"


class PaymentConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProviderRuntimeConfig:
    provider: str
    enabled: bool
    mode: str
    public: dict[str, Any]
    secret: dict[str, str]
    source: str = "db"


def _fernet() -> Fernet:
    secret = settings.payment_config_secret or (
        settings.jwt_secret if settings.debug else ""
    )
    if not secret:
        raise PaymentConfigError("PAYMENT_CONFIG_SECRET 未配置,无法保存支付密钥")
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise PaymentConfigError("支付密钥解密失败,请检查 PAYMENT_CONFIG_SECRET") from e


def _is_encrypted(value: str) -> bool:
    return value.startswith("gAAAA")


def _normalise_secret(provider: str, current: dict | None, incoming: dict | None) -> dict:
    current = current or {}
    incoming = incoming or {}
    allowed = SECRET_FIELDS.get(provider, set())
    next_secret = dict(current)
    for key in allowed:
        if key not in incoming:
            continue
        raw = incoming.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or text in {"__keep__", "已配置"}:
            continue
        if text == "__clear__":
            next_secret.pop(key, None)
            continue
        next_secret[key] = encrypt_secret(text)
    return next_secret


def _admin_overrides_provider(row: PaymentProviderConfig) -> bool:
    """True once the DB row contains an operator-authored provider decision.

    Seeded default rows are disabled/mock/empty so local dev and env-only
    production configs can still fall back. As soon as an admin saves public or
    secret config, switches live mode, or enables the provider, the row becomes
    authoritative, including when later disabled.
    """
    return bool(
        (row.public_config or {}).get(_ADMIN_OVERRIDE_KEY)
        or row.enabled
        or (row.mode or "mock") != "mock"
        or public_config_for_provider(row.provider, row.public_config)
        or row.secret_config
    )


def _public_config_without_internal(public: dict | None) -> dict:
    return {k: v for k, v in dict(public or {}).items() if not str(k).startswith("_")}


def public_config_for_provider(
    provider: str,
    public: dict | None,
    *,
    reject_unknown: bool = False,
) -> dict:
    allowed = PUBLIC_FIELDS.get(provider, set())
    raw = dict(public or {})
    unknown = sorted(
        str(key)
        for key in raw
        if not str(key).startswith("_") and str(key) not in allowed
    )
    if reject_unknown and unknown:
        raise PaymentConfigError("支付 public_config 包含非法字段: " + ", ".join(unknown))
    return {str(k): v for k, v in raw.items() if str(k) in allowed}


def decrypt_secret_config(provider: str, secret_config: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in SECRET_FIELDS.get(provider, set()):
        value = (secret_config or {}).get(key)
        if not value:
            continue
        text = str(value)
        out[key] = decrypt_secret(text) if _is_encrypted(text) else text
    return out


def mask_secret_config(provider: str, secret_config: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in SECRET_FIELDS.get(provider, set()):
        value = (secret_config or {}).get(key)
        out[key] = "已配置" if value else ""
    return out


def mask_runtime_secret_config(provider: str, secret: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    secret = secret or {}
    for key in SECRET_FIELDS.get(provider, set()):
        out[key] = "已配置" if str(secret.get(key) or "").strip() else ""
    return out


def seed_defaults(db: Session) -> None:
    for item in DEFAULT_PAYMENT_PACKAGES:
        if db.get(PaymentPackage, item["id"]):
            continue
        db.add(PaymentPackage(**item))
    for provider in VALID_PROVIDERS:
        if db.get(PaymentProviderConfig, provider):
            continue
        db.add(
            PaymentProviderConfig(
                provider=provider,
                enabled=False,
                mode="mock",
                public_config={},
                secret_config={},
            )
        )
    db.commit()


def list_packages(db: Session, *, enabled_only: bool = False) -> list[PaymentPackage]:
    q = select(PaymentPackage).order_by(PaymentPackage.sort_order, PaymentPackage.id)
    if enabled_only:
        q = q.where(PaymentPackage.enabled.is_(True))
    return list(db.execute(q).scalars())


def package_to_dict(row: PaymentPackage) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "amount_cents": int(row.amount_cents),
        "credits": int(row.credits),
        "badge": row.badge,
        "enabled": bool(row.enabled),
        "sort_order": int(row.sort_order or 0),
    }


def get_enabled_package(db: Session, package_id: str) -> PaymentPackage | None:
    return db.execute(
        select(PaymentPackage).where(
            PaymentPackage.id == package_id,
            PaymentPackage.enabled.is_(True),
        )
    ).scalar_one_or_none()


def upsert_package(db: Session, data: dict) -> PaymentPackage:
    row = db.get(PaymentPackage, data["id"])
    if row is None:
        row = PaymentPackage(id=data["id"])
        db.add(row)
    row.title = data["title"]
    row.amount_cents = int(data["amount_cents"])
    row.credits = int(data["credits"])
    row.badge = data.get("badge") or None
    row.enabled = bool(data.get("enabled", True))
    row.sort_order = int(data.get("sort_order", 0))
    db.commit()
    db.refresh(row)
    return row


def delete_package(db: Session, package_id: str) -> None:
    row = db.get(PaymentPackage, package_id)
    if not row:
        raise PaymentConfigError("套餐不存在")
    # Soft-delete: historical payment_orders keep the package_id snapshot.
    row.enabled = False
    db.commit()


def get_provider(db: Session, provider: str) -> PaymentProviderConfig | None:
    if provider not in VALID_PROVIDERS:
        raise PaymentConfigError("支付渠道非法")
    return db.get(PaymentProviderConfig, provider)


def get_runtime_provider(db: Session, provider: str) -> ProviderRuntimeConfig | None:
    row = get_provider(db, provider)
    if not row:
        return None
    secret = decrypt_secret_config(provider, row.secret_config)
    return ProviderRuntimeConfig(
        provider=provider,
        enabled=bool(row.enabled),
        mode=row.mode or "mock",
        public=public_config_for_provider(provider, row.public_config),
        secret=secret,
        source="db",
    )


def _disabled_runtime(provider: str, *, source: str = "none") -> ProviderRuntimeConfig:
    return ProviderRuntimeConfig(
        provider=provider,
        enabled=False,
        mode="mock",
        public={},
        secret={},
        source=source,
    )


def _fallback_public(provider: str) -> dict:
    if provider == "alipay":
        return {
            "app_id": settings.alipay_app_id,
            "seller_id": settings.alipay_seller_id,
            "gateway_url": settings.alipay_gateway_url,
            "notify_url": settings.alipay_notify_url,
        }
    if provider == "wechat":
        return {
            "appid": settings.wechat_pay_appid,
            "mchid": settings.wechat_pay_mchid,
            "serial_no": settings.wechat_pay_serial_no,
            "platform_serial_no": settings.wechat_pay_platform_serial_no,
            "gateway_url": settings.wechat_pay_gateway_url,
            "notify_url": settings.wechat_pay_notify_url,
        }
    return {}


def _fallback_secret(provider: str) -> dict:
    if provider == "alipay":
        return {
            "private_key": settings.alipay_private_key,
            "public_key": settings.alipay_public_key,
        }
    if provider == "wechat":
        return {
            "private_key": settings.wechat_pay_private_key,
            "api_v3_key": settings.wechat_pay_api_v3_key,
            "platform_cert_pem": settings.wechat_pay_platform_cert_pem,
        }
    return {}


def runtime_or_env(db: Session | None, provider: str) -> ProviderRuntimeConfig:
    if db is not None:
        row = get_provider(db, provider)
        if row is not None and _admin_overrides_provider(row):
            try:
                row_cfg = get_runtime_provider(db, provider)
            except PaymentConfigError:
                return _disabled_runtime(provider, source="db")
            if not row_cfg:
                return _disabled_runtime(provider, source="db")
            # Once an admin row exists, DB is the authority. A disabled or
            # invalid DB config must not be silently overridden by env vars.
            if not row_cfg.enabled:
                return row_cfg
            issues = validate_provider(
                provider,
                row_cfg.public,
                row_cfg.secret,
                enabled=row_cfg.enabled,
                mode=row_cfg.mode,
            )
            if issues:
                return _disabled_runtime(provider, source="db")
            return row_cfg
    public = _fallback_public(provider)
    secret = _fallback_secret(provider)
    env_ready = False
    if provider == "alipay":
        env_ready = bool(
            public.get("app_id")
            and public.get("seller_id")
            and secret.get("private_key")
            and secret.get("public_key")
        )
    elif provider == "wechat":
        env_ready = bool(
            public.get("appid")
            and public.get("mchid")
            and public.get("serial_no")
            and secret.get("private_key")
            and secret.get("api_v3_key")
            and secret.get("platform_cert_pem")
        )
    if env_ready:
        issues = validate_provider(provider, public, secret, enabled=True, mode="live")
        if issues:
            return _disabled_runtime(provider)
        return ProviderRuntimeConfig(
            provider=provider,
            enabled=True,
            mode="live",
            public=public,
            secret=secret,
            source="env",
        )
    return _disabled_runtime(provider, source="none")


def validate_provider(provider: str, public: dict | None, secret: dict | None,
                      *, enabled: bool, mode: str) -> list[str]:
    if provider not in VALID_PROVIDERS:
        return ["支付渠道非法"]
    public = public_config_for_provider(provider, public)
    secret = secret or {}
    issues: list[str] = []
    if not enabled:
        return issues
    if mode == "mock":
        return issues
    gateway_url = str(public.get("gateway_url") or "").strip()
    if gateway_url:
        parsed = urlparse(gateway_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in PAYMENT_GATEWAY_HOSTS.get(provider, set()):
            issues.append("支付网关地址必须使用官方 HTTPS 域名")
    notify_url = str(public.get("notify_url") or "").strip()
    if not notify_url:
        notify_url = settings.public_base_url.rstrip("/") + (NOTIFY_PATHS.get(provider) or "")
    parsed_notify = urlparse(notify_url)
    public_base = urlparse(settings.public_base_url)
    notify_host = (parsed_notify.hostname or "").lower()
    base_host = (public_base.hostname or "").lower()
    if parsed_notify.scheme != "https":
        issues.append("支付回调地址必须使用 HTTPS")
    if not base_host or notify_host != base_host:
        issues.append("支付回调地址必须与 PUBLIC_BASE_URL 同域")
    if parsed_notify.path != NOTIFY_PATHS.get(provider):
        issues.append(f"支付回调路径必须为 {NOTIFY_PATHS.get(provider)}")
    if provider == "alipay":
        for key, label in (
            ("app_id", "支付宝 APP_ID"),
            ("seller_id", "支付宝商户 PID / seller_id"),
            ("private_key", "支付宝应用私钥"),
            ("public_key", "支付宝公钥"),
        ):
            source = public if key in {"app_id", "seller_id"} else secret
            if not str(source.get(key) or "").strip():
                issues.append(f"{label} 未配置")
    if provider == "wechat":
        for key, label in (
            ("appid", "微信 APPID"),
            ("mchid", "微信商户号"),
            ("serial_no", "微信商户证书序列号"),
            ("platform_serial_no", "微信平台证书序列号"),
        ):
            if not str(public.get(key) or "").strip():
                issues.append(f"{label} 未配置")
        for key, label in (
            ("private_key", "微信商户私钥"),
            ("api_v3_key", "微信 APIv3 Key"),
            ("platform_cert_pem", "微信平台证书"),
        ):
            if not str(secret.get(key) or "").strip():
                issues.append(f"{label} 未配置")
        api_key = str(secret.get("api_v3_key") or "")
        if api_key and len(api_key.encode()) != 32:
            issues.append("微信 APIv3 Key 必须为 32 字节")
    return issues


def _required_fields_present(provider: str, public: dict | None, secret: dict | None,
                             *, mode: str) -> bool:
    if mode == "mock":
        return False
    if provider == "alipay":
        public_keys = ("app_id", "seller_id")
        secret_keys = ("private_key", "public_key")
    elif provider == "wechat":
        public_keys = ("appid", "mchid", "serial_no", "platform_serial_no")
        secret_keys = ("private_key", "api_v3_key", "platform_cert_pem")
    else:
        return False
    return all(str((public or {}).get(k) or "").strip() for k in public_keys) and all(
        str((secret or {}).get(k) or "").strip() for k in secret_keys
    )


def provider_out(row: PaymentProviderConfig) -> dict:
    provider = row.provider
    issues: list[str] = []
    public = public_config_for_provider(provider, row.public_config)
    try:
        secret = decrypt_secret_config(provider, row.secret_config)
    except PaymentConfigError as e:
        secret = {}
        issues.append(str(e))
    validation_issues = validate_provider(
        provider,
        public,
        secret,
        enabled=bool(row.enabled),
        mode=row.mode or "mock",
    )
    issues.extend(validation_issues)
    required_present = _required_fields_present(
        provider,
        public,
        secret,
        mode=row.mode or "mock",
    )
    return {
        "provider": provider,
        "enabled": bool(row.enabled),
        "mode": row.mode or "mock",
        "public_config": public,
        "secret_config_masked": mask_secret_config(provider, row.secret_config),
        "configured": required_present,
        "ready": bool(row.enabled) and not issues,
        "issues": issues,
    }


def list_providers(db: Session) -> list[dict]:
    seed_defaults(db)
    rows = list(
        db.execute(
            select(PaymentProviderConfig).order_by(PaymentProviderConfig.provider)
        ).scalars()
    )
    return [provider_out(r) for r in rows]


def list_providers_with_runtime(db: Session) -> list[dict]:
    by_provider = {p["provider"]: p for p in list_providers(db)}
    out = []
    for provider in sorted(VALID_PROVIDERS):
        current = dict(by_provider.get(provider) or {
            "provider": provider,
            "enabled": False,
            "mode": "mock",
            "public_config": {},
            "secret_config_masked": {},
            "configured": False,
            "ready": False,
            "issues": [],
        })
        runtime = runtime_or_env(db, provider)
        if runtime.enabled and runtime.source == "env" and not current.get("ready"):
            current.update({
                "enabled": True,
                "mode": runtime.mode,
                "public_config": public_config_for_provider(provider, runtime.public),
                "secret_config_masked": mask_runtime_secret_config(provider, runtime.secret),
                "configured": True,
                "ready": True,
                "issues": [],
                "source": "env",
            })
        else:
            current["source"] = runtime.source
        out.append(current)
    return out


def save_provider(db: Session, provider: str, enabled: bool, mode: str,
                  public_config: dict | None, secret_config: dict | None) -> PaymentProviderConfig:
    if provider not in VALID_PROVIDERS:
        raise PaymentConfigError("支付渠道非法")
    if mode not in {"mock", "live"}:
        raise PaymentConfigError("支付模式非法")
    if enabled and mode == "live" and not settings.payment_config_secret:
        raise PaymentConfigError("PAYMENT_CONFIG_SECRET 未配置,无法保存真实支付密钥")
    if mode == "live" and secret_config and any(str(v or "").strip() for v in secret_config.values()):
        if not settings.payment_config_secret:
            raise PaymentConfigError("PAYMENT_CONFIG_SECRET 未配置,无法保存真实支付密钥")
    row = db.get(PaymentProviderConfig, provider)
    if row is None:
        row = PaymentProviderConfig(provider=provider)
        db.add(row)
    public = public_config_for_provider(provider, public_config, reject_unknown=True)
    public[_ADMIN_OVERRIDE_KEY] = True
    if public.get("gateway_url"):
        public["gateway_url"] = str(public["gateway_url"]).strip().rstrip("/")
    if public.get("notify_url"):
        public["notify_url"] = str(public["notify_url"]).strip()
    secret = _normalise_secret(provider, row.secret_config, secret_config)
    if enabled and mode == "live":
        runtime_secret = decrypt_secret_config(provider, secret)
        issues = validate_provider(provider, public, runtime_secret, enabled=True, mode=mode)
        if issues:
            raise PaymentConfigError("; ".join(issues))
    row.enabled = bool(enabled)
    row.mode = mode
    row.public_config = public
    row.secret_config = secret
    db.commit()
    db.refresh(row)
    return row


def export_public_status(db: Session) -> dict:
    return {
        "packages": [package_to_dict(p) for p in list_packages(db, enabled_only=True)],
        "providers": list_providers_with_runtime(db),
    }
