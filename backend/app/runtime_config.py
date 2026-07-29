"""Runtime configuration preflight shared by API and workers."""
from __future__ import annotations

import ipaddress
import logging
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
_INSECURE_METRICS_TOKENS = {
    "",
    "metrics-token",
    "change-me",
    "change-me-in-production",
    "please-change-me-to-a-long-random-metrics-token",
}
_INSECURE_SECRET_PREFIXES = ("please-change-me", "replace-with", "changeme")
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
    try:
        assert_safe_url(url)
    except SsrfError as e:
        raise RuntimeError(f"{name} 指向非公网地址或不安全地址:{e}") from e


def _production_deploy_enabled() -> bool:
    return str(getattr(settings, "deploy_env", "") or "").strip().lower() in {"prod", "production"}


def _validate_public_https_url(name: str, url: str) -> None:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(f"{name} 生产部署必须是 HTTPS 公网地址")


def _validate_image_evidence_analyzer_config(capability: str) -> None:
    prefix = f"image_evidence_{capability}"
    env_prefix = f"IMAGE_EVIDENCE_{capability.upper()}"
    url = str(getattr(settings, f"{prefix}_url") or "").strip()
    api_key = str(getattr(settings, f"{prefix}_api_key") or "").strip()
    health_url = str(getattr(settings, f"{prefix}_health_url") or "").strip()
    if not url:
        dangling = []
        if api_key:
            dangling.append(f"{env_prefix}_API_KEY")
        if health_url:
            dangling.append(f"{env_prefix}_HEALTH_URL")
        if dangling:
            raise RuntimeError(
                f"{env_prefix}_URL 未配置时不能单独配置 " + ",".join(dangling)
            )
        return
    for suffix, configured_url in (("URL", url), ("HEALTH_URL", health_url)):
        if not configured_url:
            continue
        parsed = urlparse(configured_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise RuntimeError(f"{env_prefix}_{suffix} 必须是不含用户信息和 fragment 的 HTTP(S) 地址")
        if not settings.debug:
            _validate_analyzer_http_url(f"{env_prefix}_{suffix}", configured_url)
    analyzer_host = (urlparse(url).hostname or "").rstrip(".").lower()
    if (
        not settings.debug
        and analyzer_host in settings.trusted_analyzer_host_list
        and not api_key
    ):
        raise RuntimeError(
            f"{env_prefix}_API_KEY 必须为私网分析器配置 Bearer 凭据"
        )


def _validate_analyzer_http_url(name: str, url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} 必须是不含用户信息和 fragment 的 HTTP(S) 地址")
    host = (parsed.hostname or "").rstrip(".").lower()
    trusted_private = host in settings.trusted_analyzer_host_list
    if not settings.debug and trusted_private:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise RuntimeError(
                f"{name} 的 TRUSTED_ANALYZER_HOSTS 只能使用明确的服务主机名，不能使用 IP"
            )
        if host in {"localhost", "metadata", "metadata.google.internal"}:
            raise RuntimeError(f"{name} 使用了禁止信任的分析器主机名")
        return
    if not settings.debug:
        _validate_configured_egress_url(name, url)


def _validate_audio_analyzer_config() -> None:
    enabled = bool(settings.audio_gateway_enabled)
    base_url = str(settings.audio_gateway_base_url or "").strip()
    api_key = str(settings.audio_gateway_api_key or "").strip()
    model = str(settings.audio_transcription_model or "").strip()
    health_url = str(settings.audio_gateway_health_url or "").strip()
    if bool(base_url) != bool(api_key):
        raise RuntimeError(
            "AUDIO_GATEWAY_BASE_URL 与 AUDIO_GATEWAY_API_KEY 必须同时配置或同时留空"
        )
    if health_url and not base_url:
        raise RuntimeError(
            "AUDIO_GATEWAY_BASE_URL 未配置时不能单独配置 AUDIO_GATEWAY_HEALTH_URL"
        )
    if enabled and not (base_url and api_key and model):
        raise RuntimeError(
            "AUDIO_GATEWAY_ENABLED=true 时必须完整配置 AUDIO_GATEWAY_BASE_URL、"
            "AUDIO_GATEWAY_API_KEY 和 AUDIO_TRANSCRIPTION_MODEL"
        )
    for name, url in (
        ("AUDIO_GATEWAY_BASE_URL", base_url),
        ("AUDIO_GATEWAY_HEALTH_URL", health_url),
    ):
        if url:
            _validate_analyzer_http_url(name, url)


def _validate_video_semantic_analyzer_config() -> None:
    url = str(settings.video_evidence_semantic_url or "").strip()
    api_key = str(settings.video_evidence_semantic_api_key or "").strip()
    health_url = str(settings.video_evidence_semantic_health_url or "").strip()
    if bool(url) != bool(api_key):
        raise RuntimeError(
            "VIDEO_EVIDENCE_SEMANTIC_URL 与 VIDEO_EVIDENCE_SEMANTIC_API_KEY "
            "必须同时配置或同时留空"
        )
    if health_url and not url:
        raise RuntimeError(
            "VIDEO_EVIDENCE_SEMANTIC_URL 未配置时不能单独配置 "
            "VIDEO_EVIDENCE_SEMANTIC_HEALTH_URL"
        )
    for name, configured_url in (
        ("VIDEO_EVIDENCE_SEMANTIC_URL", url),
        ("VIDEO_EVIDENCE_SEMANTIC_HEALTH_URL", health_url),
    ):
        if configured_url:
            _validate_analyzer_http_url(name, configured_url)


def _validate_production_trusted_proxies() -> None:
    for item in settings.trusted_proxy_ip_list:
        if "/" not in item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as e:
            raise RuntimeError(f"TRUSTED_PROXY_IPS 包含非法 CIDR:{item}") from e
        if not settings.allow_broad_trusted_proxy_cidr:
            raise RuntimeError(
                "DEPLOY_ENV=production 时 TRUSTED_PROXY_IPS 默认只允许单个反代 IP。"
                f"请把 {item} 改为具体 IP，或确认风险后设置 ALLOW_BROAD_TRUSTED_PROXY_CIDR=true"
            )
        if network.prefixlen < (24 if network.version == 4 else 120):
            raise RuntimeError(
                "ALLOW_BROAD_TRUSTED_PROXY_CIDR=true 也不能信任过宽代理网段。"
                f"请缩小 TRUSTED_PROXY_IPS={item}"
            )


def _validate_production_deploy_mode() -> None:
    if not _production_deploy_enabled():
        return
    if settings.debug:
        raise RuntimeError("DEPLOY_ENV=production 时必须设置 DEBUG=false")
    _validate_public_https_url("PUBLIC_BASE_URL", settings.public_base_url)
    _validate_public_https_url("PAYMENT_FRONTEND_BASE_URL", settings.payment_frontend_base_url)
    if settings.sms_provider == "mock":
        raise RuntimeError("DEPLOY_ENV=production 时禁止 SMS_PROVIDER=mock")
    localhost_origins = [
        origin for origin in settings.cors_origin_list
        if (urlparse(origin).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    ]
    if localhost_origins:
        raise RuntimeError("DEPLOY_ENV=production 时 CORS_ORIGINS 不能包含 localhost/127.0.0.1")
    _validate_production_trusted_proxies()
    if settings.online_update_enabled and not settings.online_update_require_signed_commits:
        raise RuntimeError(
            "DEPLOY_ENV=production 启用在线升级时必须设置 ONLINE_UPDATE_REQUIRE_SIGNED_COMMITS=true"
        )


def validate_runtime_config() -> None:
    celery_settings = settings.celery
    storage_settings = settings.storage
    _validate_production_deploy_mode()
    if int(celery_settings.task_soft_time_limit_seconds) >= int(celery_settings.task_time_limit_seconds):
        raise RuntimeError("CELERY_TASK_SOFT_TIME_LIMIT_SECONDS 必须小于 CELERY_TASK_TIME_LIMIT_SECONDS")
    if not public_base_is_local() and settings.debug:
        raise RuntimeError("PUBLIC_BASE_URL 非本地域名时禁止 DEBUG=true")
    # Production must never explicitly force local placeholder generations. The
    # concrete gateway credentials may come from env or model_configs; DB-backed
    # model rows are checked in validate_model_gateway_rows() after startup seed.
    if not settings.debug and settings.mock_mode:
        raise RuntimeError("生产环境(DEBUG=false)必须设置 MOCK_MODE=false")
    # 行为翻转显式化:历史版本生产环境默认拒绝无短信验证的公开注册,现在
    # REGISTRATION_ENABLED 默认 true。升级到本版本的运营方必须在启动日志里
    # 看到这一状态,而不是静默开闸;不想开放就设 REGISTRATION_ENABLED=false。
    if not settings.debug and settings.registration_enabled:
        logging.getLogger("runtime_config").warning(
            "自助注册已开放(REGISTRATION_ENABLED=true,当前默认值)。历史版本生产环境"
            "默认关闭无短信验证注册;如不希望开放注册,请设置 REGISTRATION_ENABLED=false,"
            "或在管理后台开启短信验证(sms_auth_enabled)以要求验证码注册。"
        )
    _validate_audio_analyzer_config()
    _validate_video_semantic_analyzer_config()
    for capability in ("detector", "segmenter"):
        _validate_image_evidence_analyzer_config(capability)
    if not settings.debug:
        _validate_configured_egress_url("GATEWAY_BASE_URL", settings.gateway_base_url)
        _validate_configured_egress_url("ANTHROPIC_BASE_URL", settings.anthropic_base_url)
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
    metrics_token = str(settings.metrics_token or "")
    if (
        not settings.debug
        and (observability.metrics_enabled or settings.metrics_token)
        and (
            metrics_token in _INSECURE_METRICS_TOKENS
            or metrics_token.lower().startswith(_INSECURE_SECRET_PREFIXES)
            or len(metrics_token) < _MIN_METRICS_TOKEN_LEN
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
    if str(storage_settings.backend or "local").lower() not in {"local", "s3"}:
        raise RuntimeError("STORAGE_BACKEND 仅支持 local 或 s3")
    if str(storage_settings.backend or "local").lower() == "s3":
        if not str(storage_settings.s3_bucket or "").strip():
            raise RuntimeError("STORAGE_BACKEND=s3 必须配置 STORAGE_S3_BUCKET")
        if bool(storage_settings.s3_access_key_id) != bool(storage_settings.s3_secret_access_key):
            raise RuntimeError(
                "STORAGE_S3_ACCESS_KEY_ID 和 STORAGE_S3_SECRET_ACCESS_KEY 必须同时配置，"
                "或同时留空使用实例角色"
            )
        if storage_settings.s3_endpoint_url:
            endpoint = urlparse(storage_settings.s3_endpoint_url)
            if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
                raise RuntimeError("STORAGE_S3_ENDPOINT_URL 必须是有效的 HTTP(S) 地址")
        public_media = urlparse(str(storage_settings.s3_public_base_url or ""))
        if public_media.scheme not in {"http", "https"} or not public_media.netloc:
            raise RuntimeError(
                "STORAGE_BACKEND=s3 必须配置可访问的 STORAGE_S3_PUBLIC_BASE_URL"
            )
        if not settings.debug and public_media.scheme != "https":
            raise RuntimeError("生产环境 STORAGE_S3_PUBLIC_BASE_URL 必须使用 HTTPS")
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
    """Fail fast on incomplete or unsafe production model configuration."""
    from sqlalchemy import select

    from .models import ModelConfig

    rows = list(
        db.execute(select(ModelConfig).where(ModelConfig.deleted_at.is_(None))).scalars()
    )
    if settings.debug:
        return
    anthropic_base_configured = bool(str(settings.anthropic_base_url or "").strip())
    anthropic_token_configured = bool(str(settings.anthropic_auth_token or "").strip())
    if anthropic_base_configured != anthropic_token_configured:
        raise RuntimeError(
            "生产环境提示词优化网关配置不完整。"
            "ANTHROPIC_BASE_URL 和 ANTHROPIC_AUTH_TOKEN 必须同时配置,或同时清空后使用后台模型配置。"
        )
    required_uses = {"vision", "image", "video", "prompt"}
    configured_uses = {str(row.use or "") for row in rows if row.enabled}
    missing_uses = sorted(required_uses - configured_uses)
    if missing_uses:
        raise RuntimeError(
            "生产环境缺少必需的模型配置行:"
            f"{','.join(missing_uses)}。请先运行数据库初始化/迁移 seed,"
            "或在后台补齐模型配置后再启动。"
        )

    for row in rows:
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
        if row.use == "prompt":
            has_env_gateway = anthropic_base_configured and anthropic_token_configured
        elif row.use == "video":
            has_env_gateway = bool(settings.video_base and settings.video_key)
        else:
            has_env_gateway = bool(settings.gateway_base_url and settings.gateway_api_key)
        if not (has_db_gateway or has_env_gateway):
            raise RuntimeError(
                f"生产环境模型 {row.use} 未配置真实网关。请在后台配置模型 Base URL/API Key,"
                "或通过环境变量提供兜底网关。"
            )
