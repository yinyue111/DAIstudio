"""Runtime configuration preflight shared by API and workers."""
from __future__ import annotations

from urllib.parse import urlparse

from . import observability
from .config import settings
from .services.model_gateway_config import validate_base_url
from .services.payments import mock_payments_allowed, public_base_is_local
from .services.ssrf import SsrfError, assert_safe_url

# Known placeholder / weak secrets that must never reach production. Includes the
# .env.example sample so "copy example + set DEBUG=false" still refuses to boot.
_INSECURE_JWT_SECRETS = {
    "",
    "change-me-in-production",
    "please-change-me-to-a-long-random-string",
}
_MIN_JWT_SECRET_LEN = 32
_MIN_METRICS_TOKEN_LEN = 16
_INSECURE_METRICS_TOKENS = {"", "metrics-token", "change-me", "change-me-in-production"}
_ALLOWED_JWT_ALGORITHMS = {"HS256", "HS384", "HS512"}


def _validate_configured_egress_url(name: str, url: str, *, require_https: bool = True) -> None:
    if not url:
        return
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise RuntimeError(f"{name} 配置非法:缺少主机名")
    trusted = host in settings.trusted_egress_host_list
    if require_https and parsed.scheme != "https" and not trusted:
        raise RuntimeError(f"{name} 必须使用 HTTPS,或将主机加入 TRUSTED_EGRESS_HOSTS")
    if trusted:
        return
    try:
        assert_safe_url(url)
    except SsrfError as e:
        raise RuntimeError(f"{name} 指向非公网地址或不安全地址:{e}") from e


def validate_runtime_config() -> None:
    if not public_base_is_local() and settings.debug:
        raise RuntimeError("PUBLIC_BASE_URL 非本地域名时禁止 DEBUG=true")
    # Production must never explicitly force local placeholder generations. The
    # concrete gateway credentials may come from env or model_configs; DB-backed
    # model rows are checked in validate_model_gateway_rows() after startup seed.
    if not settings.debug and settings.mock_mode:
        raise RuntimeError("生产环境(DEBUG=false)必须设置 MOCK_MODE=false")
    if not settings.debug:
        _validate_configured_egress_url("GATEWAY_BASE_URL", settings.gateway_base_url)
        _validate_configured_egress_url("VIDEO_GATEWAY_BASE_URL", settings.video_gateway_base_url)
        if settings.sms_provider == "http":
            _validate_configured_egress_url("SMS_HTTP_URL", settings.sms_http_url)
    # Fail fast: a production deploy must not run with a default/weak JWT secret,
    # otherwise anyone can forge tokens for any user (incl. admin).
    if not settings.debug and (
        settings.jwt_secret in _INSECURE_JWT_SECRETS
        or len(settings.jwt_secret) < _MIN_JWT_SECRET_LEN
    ):
        raise RuntimeError(
            "JWT_SECRET 不安全(为占位值或过短)。生产环境(DEBUG=false)拒绝启动,"
            f"请设置长度 >= {_MIN_JWT_SECRET_LEN} 的强随机密钥"
            "(如 python -c \"import secrets;print(secrets.token_urlsafe(48))\")。"
        )
    if (
        not settings.debug
        and (observability.metrics_enabled or settings.metrics_token)
        and (
            settings.metrics_token in _INSECURE_METRICS_TOKENS
            or len(settings.metrics_token) < _MIN_METRICS_TOKEN_LEN
        )
    ):
        raise RuntimeError(
            "生产环境(DEBUG=false)启用 /metrics 或 /api/health/detail 时必须设置强 METRICS_TOKEN"
        )
    if settings.jwt_algorithm not in _ALLOWED_JWT_ALGORITHMS:
        raise RuntimeError(
            "JWT_ALGORITHM 不支持。仅允许 "
            f"{','.join(sorted(_ALLOWED_JWT_ALGORITHMS))}"
        )
    if not settings.debug and settings.payment_mock_enabled:
        raise RuntimeError("生产环境(DEBUG=false)必须设置 PAYMENT_MOCK_ENABLED=false")
    if settings.payment_mock_enabled and not mock_payments_allowed():
        raise RuntimeError(
            "PAYMENT_MOCK_ENABLED=true 仅允许 PUBLIC_BASE_URL 和 "
            "PAYMENT_FRONTEND_BASE_URL 同时为 localhost/127.0.0.1"
        )
    if settings.payment_config_secret and len(settings.payment_config_secret) < 32:
        raise RuntimeError("PAYMENT_CONFIG_SECRET 过短,请使用长度 >= 32 的强随机密钥")
    if settings.model_config_secret and len(settings.model_config_secret) < 32:
        raise RuntimeError("MODEL_CONFIG_SECRET 过短,请使用长度 >= 32 的强随机密钥")
    if not settings.debug and not (settings.payment_config_secret or settings.model_config_secret):
        raise RuntimeError(
            "生产环境(DEBUG=false)必须设置 PAYMENT_CONFIG_SECRET 或 MODEL_CONFIG_SECRET,"
            "用于加密后台保存的支付密钥和模型 API Key"
        )


def validate_model_gateway_rows(db) -> None:
    """Fail fast on unsafe DB-configured model base URLs in production."""
    if settings.debug:
        return
    from sqlalchemy import select

    from .models import ModelConfig

    for row in db.execute(select(ModelConfig)).scalars():
        if row.base_url:
            validate_base_url(f"模型 {row.use} Base URL", row.base_url)
        has_partial_db_gateway = bool(row.base_url) != bool(row.api_key_encrypted)
        if has_partial_db_gateway:
            raise RuntimeError(
                f"生产环境模型 {row.use} 的后台网关配置不完整。"
                "模型 Base URL/API Key 必须同时配置,或同时清空后使用环境变量兜底。"
            )
        if not row.enabled:
            continue
        has_db_gateway = bool(row.base_url and row.api_key_encrypted)
        if row.use == "video":
            has_env_gateway = bool(settings.video_base and settings.video_key)
        else:
            has_env_gateway = bool(settings.gateway_base_url and settings.gateway_api_key)
        if not (has_db_gateway or has_env_gateway):
            raise RuntimeError(
                f"生产环境模型 {row.use} 未配置真实网关。请在后台配置模型 Base URL/API Key,"
                "或通过环境变量提供兜底网关。"
            )
