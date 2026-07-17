"""Runtime-editable model gateway configuration.

The admin UI can configure provider/base URL/API key per model use. Secrets are
encrypted in the DB and never returned to the frontend; callers receive a
decrypted in-memory runtime object only when they are about to call a gateway.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings
from ..models import ModelConfig
from .ssrf import SsrfError, assert_safe_url

VALID_PROVIDERS = {
    "openai",
    "volcengine_ark",
    "openrouter",
    "siliconflow",
    "deepseek",
    "moonshot",
    "zhipu",
    "dashscope",
    "baidu_qianfan",
    "tencent_hunyuan",
    "yinyue",
    "anthropic",
    "antigravity",
    "grok",
    "gemini",
    "custom_openai",
}
VALID_GATEWAY_FORMATS = {"openai", "ark", "anthropic"}

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "gateway_format": "openai",
    },
    "volcengine_ark": {
        "label": "火山方舟 Ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "gateway_format": "ark",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "gateway_format": "openai",
    },
    "siliconflow": {
        "label": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "gateway_format": "openai",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "gateway_format": "openai",
    },
    "moonshot": {
        "label": "Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "gateway_format": "openai",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "gateway_format": "openai",
    },
    "dashscope": {
        "label": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "gateway_format": "openai",
    },
    "baidu_qianfan": {
        "label": "百度千帆",
        "base_url": "https://qianfan.baidubce.com/v2",
        "gateway_format": "openai",
    },
    "tencent_hunyuan": {
        "label": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "gateway_format": "openai",
    },
    "yinyue": {
        "label": "yinyue",
        "base_url": "",
        "gateway_format": "openai",
    },
    "anthropic": {
        "label": "Anthropic-Compatible",
        "base_url": "https://api.anthropic.com",
        "gateway_format": "anthropic",
    },
    "antigravity": {
        "label": "Antigravity (Claude / Gemini)",
        "base_url": "https://sub.aiwuq.cn/antigravity",
        "gateway_format": "anthropic",
        "description": "Anthropic Messages 协议；支持 Claude/Gemini 对话和 Gemini 图片模型",
    },
    "grok": {
        "label": "Grok / xAI Compatible",
        "base_url": "https://sub.aiwuq.cn/v1",
        "gateway_format": "openai",
        "description": "OpenAI-Compatible 协议；支持 Grok 对话、图片和异步视频",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "gateway_format": "openai",
        "description": "Gemini OpenAI-Compatible 对话端点；图片/视频需按实际网关接口适配",
    },
    "custom_openai": {
        "label": "自定义 OpenAI-Compatible",
        "base_url": "",
        "gateway_format": "openai",
    },
}


class ModelGatewayConfigError(Exception):
    pass


@dataclass(frozen=True)
class RuntimeGatewayConfig:
    use: str
    provider: str
    base_url: str
    api_key: str
    gateway_format: str
    source: str = "model"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


def gateway_key_fingerprint(config: RuntimeGatewayConfig) -> str:
    """Stable non-secret identity for detecting model gateway key drift.

    Generation tasks persist provider/base_url/model snapshots so video final
    renders and retries can keep using the preview-time model/cost. Persisting
    the API key itself would be unsafe; this HMAC-like digest lets the worker
    detect when a later admin edit would mix an old gateway URL with a new key.
    """
    if not config.api_key:
        return ""
    material = "\x1f".join([
        config.use or "",
        config.provider or "",
        config.base_url or "",
        config.gateway_format or "",
        config.api_key,
    ])
    return hashlib.sha256(material.encode()).hexdigest()


def _secret_seed() -> str:
    secret = settings.model_config_secret or settings.payment_config_secret
    if not secret and settings.debug:
        secret = settings.jwt_secret
    if not secret:
        raise ModelGatewayConfigError("MODEL_CONFIG_SECRET 未配置,无法保存模型 API Key")
    return secret


def _fernet() -> Fernet:
    digest = hashlib.sha256(_secret_seed().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_api_key(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise ModelGatewayConfigError("模型 API Key 解密失败,请检查 MODEL_CONFIG_SECRET") from e


def _is_encrypted(value: str) -> bool:
    return value.startswith("gAAAA")


def mask_secret(value: str | None) -> str:
    text = str(value or "")
    if not text:
        return ""
    return (text[:6] + "..." + text[-4:]) if len(text) > 12 else "已配置"


def provider_preset(provider: str | None) -> dict[str, Any]:
    return PROVIDER_PRESETS.get(provider or "", {})


def normalise_provider(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text not in VALID_PROVIDERS:
        raise ModelGatewayConfigError("模型提供商非法")
    return text


def normalise_gateway_format(value: str | None, provider: str | None, use: str | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        preset = provider_preset(provider).get("gateway_format")
        text = preset or (
            "anthropic"
            if use == "prompt"
            else "ark"
            if use == "video" and provider == "volcengine_ark"
            else "openai"
        )
    if text not in VALID_GATEWAY_FORMATS:
        raise ModelGatewayConfigError("模型网关格式非法")
    return text


def normalise_base_url(value: str | None) -> str | None:
    text = str(value or "").strip().rstrip("/")
    return text or None


def validate_base_url(name: str, url: str | None) -> None:
    if not url:
        return
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ModelGatewayConfigError(f"{name} 配置非法:缺少主机名")
    trusted = host in settings.trusted_egress_host_list
    if not settings.debug and parsed.scheme != "https" and not trusted:
        raise ModelGatewayConfigError(f"{name} 必须使用 HTTPS,或将主机加入 TRUSTED_EGRESS_HOSTS")
    if settings.debug:
        return
    try:
        assert_safe_url(url)
    except SsrfError as e:
        raise ModelGatewayConfigError(f"{name} 指向非公网地址或不安全地址:{e}") from e


def encrypted_key_present(row: ModelConfig | None) -> bool:
    return bool(row and row.api_key_encrypted)


def decrypt_row_api_key(row: ModelConfig | None) -> str:
    value = str(getattr(row, "api_key_encrypted", "") or "")
    if not value:
        return ""
    return decrypt_api_key(value) if _is_encrypted(value) else value


def apply_model_gateway_update(
    row: ModelConfig,
    *,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    api_key_clear: bool,
    gateway_format: str | None,
) -> None:
    current_provider = normalise_provider(getattr(row, "provider", None))
    current_base_url = normalise_base_url(getattr(row, "base_url", None))
    current_gateway_format = normalise_gateway_format(
        getattr(row, "gateway_format", None),
        current_provider,
        row.use,
    )
    provider = normalise_provider(provider)
    base_url = normalise_base_url(base_url)
    gateway_format = normalise_gateway_format(gateway_format, provider, row.use)
    validate_base_url("模型 Base URL", base_url)
    if api_key and not (settings.model_config_secret or settings.payment_config_secret or settings.debug):
        raise ModelGatewayConfigError("MODEL_CONFIG_SECRET 未配置,无法保存模型 API Key")
    existing_key_present = encrypted_key_present(row)
    new_key_supplied = api_key not in (None, "", "__keep__", "已配置")
    gateway_identity_changed = (
        current_provider != provider
        or current_base_url != base_url
        or current_gateway_format != gateway_format
    )
    if existing_key_present and gateway_identity_changed and not api_key_clear and not new_key_supplied:
        raise ModelGatewayConfigError(
            "切换模型提供商、Base URL 或网关格式时必须重新输入 API Key；"
            "如需使用环境变量兜底，请同时清空 Base URL 和 API Key"
        )
    next_key_present = bool(existing_key_present)
    if api_key_clear:
        next_key_present = False
    elif new_key_supplied:
        next_key_present = True
    if bool(base_url) != bool(next_key_present):
        raise ModelGatewayConfigError(
            "模型 Base URL 和 API Key 必须同时配置；如需使用环境变量兜底，请同时清空二者"
        )
    row.provider = provider
    row.base_url = base_url
    row.gateway_format = gateway_format
    if api_key_clear:
        row.api_key_encrypted = None
    elif new_key_supplied:
        row.api_key_encrypted = encrypt_api_key(str(api_key).strip())


def model_to_admin_dict(row: ModelConfig) -> dict[str, Any]:
    return {
        "use": row.use,
        "model_id": row.model_id,
        "provider": row.provider,
        "base_url": row.base_url,
        "gateway_format": row.gateway_format or normalise_gateway_format(None, row.provider, row.use),
        "api_key_configured": encrypted_key_present(row),
        "api_key_masked": "已配置" if encrypted_key_present(row) else "",
        "cost_credits": row.cost_credits,
        "unlock_cost": row.unlock_cost,
        "enabled": row.enabled,
        "extra": row.extra,
    }


def _env_runtime_config(use: str) -> RuntimeGatewayConfig:
    if use == "prompt":
        return RuntimeGatewayConfig(
            use=use,
            provider="anthropic",
            base_url=(settings.anthropic_base_url or "").rstrip("/"),
            api_key=settings.anthropic_auth_token or "",
            gateway_format="anthropic",
            source="env",
        )
    if use == "video":
        return RuntimeGatewayConfig(
            use=use,
            provider="env",
            base_url=(settings.video_base or "").rstrip("/"),
            api_key=settings.video_key or "",
            gateway_format=settings.video_gateway_format or "ark",
            source="env",
        )
    return RuntimeGatewayConfig(
        use=use,
        provider="env",
        base_url=(settings.gateway_base_url or "").rstrip("/"),
        api_key=settings.gateway_api_key or "",
        gateway_format="openai",
        source="env",
    )


def runtime_config_for_model(row: ModelConfig | None, use: str | None = None) -> RuntimeGatewayConfig:
    resolved_use = use or getattr(row, "use", None) or "image"
    if row is not None:
        api_key = decrypt_row_api_key(row)
        base_url = normalise_base_url(getattr(row, "base_url", None)) or ""
        source = getattr(row, "gateway_source", None) or "model"
        has_model_gateway_override = source != "env" and bool(
            base_url or getattr(row, "api_key_encrypted", None)
        )
        if has_model_gateway_override:
            provider = normalise_provider(getattr(row, "provider", None)) or "custom_openai"
            gateway_format = normalise_gateway_format(getattr(row, "gateway_format", None), provider, resolved_use)
            if base_url:
                validate_base_url("模型 Base URL", base_url)
            return RuntimeGatewayConfig(
                use=resolved_use,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                gateway_format=gateway_format or "openai",
                source="model",
            )
    return _env_runtime_config(resolved_use)


def apply_saved_model_gateway(row: ModelConfig, source: ModelConfig | None) -> None:
    """Copy a saved provider connection without exposing its secret to clients."""
    if source is None or not encrypted_key_present(source) or not normalise_base_url(source.base_url):
        raise ModelGatewayConfigError("所选已有供应商未完整配置 Base URL 和 API Key")
    config = runtime_config_for_model(source, use=row.use)
    if config.source != "model" or not config.configured:
        raise ModelGatewayConfigError("所选已有供应商连接不可用")
    apply_model_gateway_update(
        row,
        provider=config.provider,
        base_url=config.base_url,
        api_key=config.api_key,
        api_key_clear=False,
        gateway_format=config.gateway_format,
    )


def runtime_config_from_probe(
    *,
    use: str | None,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    gateway_format: str | None,
    fallback_row: ModelConfig | None = None,
) -> RuntimeGatewayConfig:
    resolved_use = use or getattr(fallback_row, "use", None) or "image"
    supplied_key = str(api_key or "").strip()
    saved_key_reused = bool(fallback_row is not None and encrypted_key_present(fallback_row) and not supplied_key)
    fallback_provider = normalise_provider(getattr(fallback_row, "provider", None))
    fallback_base_url = normalise_base_url(getattr(fallback_row, "base_url", None))
    fallback_gateway_format = normalise_gateway_format(
        getattr(fallback_row, "gateway_format", None),
        fallback_provider,
        resolved_use,
    ) if fallback_row is not None else None
    requested_provider = normalise_provider(provider) if provider else None
    requested_base_url = normalise_base_url(base_url)
    requested_gateway_format = normalise_gateway_format(gateway_format, requested_provider or fallback_provider, resolved_use) \
        if gateway_format else None
    if saved_key_reused:
        if requested_base_url and requested_base_url != fallback_base_url:
            raise ModelGatewayConfigError("使用已保存 API Key 探测时不能临时覆盖 Base URL")
        if requested_provider and requested_provider != fallback_provider:
            raise ModelGatewayConfigError("使用已保存 API Key 探测时不能临时切换模型提供商")
        if requested_gateway_format and requested_gateway_format != fallback_gateway_format:
            raise ModelGatewayConfigError("使用已保存 API Key 探测时不能临时切换网关格式")
    provider = requested_provider or fallback_provider or "custom_openai"
    base_url = (
        requested_base_url
        or fallback_base_url
        or provider_preset(provider).get("base_url")
        or ""
    )
    key = supplied_key
    if not key and fallback_row is not None:
        key = decrypt_row_api_key(fallback_row)
    gateway_format = (
        requested_gateway_format
        or fallback_gateway_format
        or "openai"
    )
    validate_base_url("模型 Base URL", base_url)
    return RuntimeGatewayConfig(
        use=resolved_use,
        provider=provider,
        base_url=base_url,
        api_key=key,
        gateway_format=gateway_format,
        source="probe",
    )
