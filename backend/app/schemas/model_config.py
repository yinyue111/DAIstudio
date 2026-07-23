"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._base import (
    MAX_MODEL_COST_CREDITS,
    ModelGatewayFormat,
    ModelProvider,
    ModelUse,
    _reject_dangerous_json_keys,
    _reject_route_secret_json_keys,
    _validate_json_payload_size,
)


class ModelConfigIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    use: ModelUse
    model_id: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    is_default: bool | None = None
    sort_order: int = Field(default=0, ge=-100000, le=100000)
    provider_config_id: int | None = Field(default=None, ge=1)
    provider: ModelProvider | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    api_key_clear: bool = False
    gateway_format: ModelGatewayFormat | None = None
    cost_credits: int = Field(ge=1, le=MAX_MODEL_COST_CREDITS)
    unlock_cost: int = Field(default=0, ge=0, le=MAX_MODEL_COST_CREDITS)
    enabled: bool = True
    extra: dict[str, Any] | None = None

    @field_validator("extra")
    @classmethod
    def _validate_extra_costs(cls, v: dict[str, Any] | None):
        if v is None:
            return v
        _validate_json_payload_size(v, 16 * 1024, "extra")
        if "preview_cost" in v:
            try:
                preview_cost = int(v["preview_cost"])
            except (TypeError, ValueError):
                raise ValueError("extra.preview_cost 必须是非负整数")
            if preview_cost < 0:
                raise ValueError("extra.preview_cost 必须是非负整数")
            if preview_cost > MAX_MODEL_COST_CREDITS:
                raise ValueError(f"extra.preview_cost 不能超过 {MAX_MODEL_COST_CREDITS}")
            v = {**v, "preview_cost": preview_cost}
        return v

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, v: str | None):
        if v is None:
            return None
        text = v.strip().rstrip("/")
        return text or None

    @field_validator("api_key")
    @classmethod
    def _normalise_api_key(cls, v: str | None):
        if v is None:
            return None
        return v.strip()

    @field_validator("model_id", "display_name")
    @classmethod
    def _normalise_model_labels(cls, v: str | None):
        if v is None:
            return None
        text = v.strip()
        if not text:
            raise ValueError("模型标识和展示名称不能为空")
        return text

    @model_validator(mode="after")
    def _validate_gateway_source(self):
        if self.provider_config_id is not None and any(
            (
                self.provider,
                self.base_url,
                self.api_key,
                self.api_key_clear,
                self.gateway_format,
            )
        ):
            raise ValueError("已有供应商不能与 Base URL、API Key 或网关格式同时提交")
        return self


class ModelConfigPatchIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    use: ModelUse | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    provider: ModelProvider | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    api_key_clear: bool = False
    gateway_format: ModelGatewayFormat | None = None
    cost_credits: int | None = Field(default=None, ge=1, le=MAX_MODEL_COST_CREDITS)
    unlock_cost: int | None = Field(default=None, ge=0, le=MAX_MODEL_COST_CREDITS)
    enabled: bool | None = None
    is_default: bool | None = None
    sort_order: int | None = Field(default=None, ge=-100000, le=100000)
    extra: dict[str, Any] | None = None

    @field_validator("extra")
    @classmethod
    def _validate_extra_costs(cls, v: dict[str, Any] | None):
        return ModelConfigIn._validate_extra_costs(v)

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, v: str | None):
        return ModelConfigIn._normalise_base_url(v)

    @field_validator("api_key")
    @classmethod
    def _normalise_api_key(cls, v: str | None):
        return ModelConfigIn._normalise_api_key(v)

    @field_validator("model_id", "display_name")
    @classmethod
    def _normalise_model_labels(cls, v: str | None):
        return ModelConfigIn._normalise_model_labels(v)

    @model_validator(mode="after")
    def _reject_conflicting_key_actions(self):
        if self.api_key_clear and self.api_key not in (None, ""):
            raise ValueError("不能同时提交 api_key 和 api_key_clear")
        if not self.model_fields_set:
            raise ValueError("至少提交一个待修改字段")
        return self


class ModelProbeIn(BaseModel):
    model_config_id: int | None = Field(default=None, ge=1)
    provider_config_id: int | None = Field(default=None, ge=1)
    use: ModelUse | None = None
    provider: ModelProvider | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    gateway_format: ModelGatewayFormat | None = None

    @field_validator("base_url")
    @classmethod
    def _normalise_probe_base_url(cls, v: str | None):
        if v is None:
            return None
        text = v.strip().rstrip("/")
        return text or None

    @field_validator("api_key")
    @classmethod
    def _normalise_probe_api_key(cls, v: str | None):
        if v is None:
            return None
        return v.strip()

    @model_validator(mode="after")
    def _validate_probe_source(self):
        if self.model_config_id is not None and self.provider_config_id is not None:
            raise ValueError("模型配置与已有供应商连接只能选择一个")
        if self.provider_config_id is not None and any(
            (
                self.provider,
                self.base_url,
                self.api_key,
                self.gateway_format,
            )
        ):
            raise ValueError("使用已有供应商探测时不能临时覆盖网关参数")
        return self


class ModelCatalogImportItemIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    use: ModelUse
    model_id: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    cost_credits: int = Field(ge=1, le=MAX_MODEL_COST_CREDITS)
    unlock_cost: int = Field(default=0, ge=0, le=MAX_MODEL_COST_CREDITS)
    enabled: bool = True
    sort_order: int = Field(default=0, ge=-100000, le=100000)
    extra: dict[str, Any] | None = None

    @field_validator("model_id", "display_name")
    @classmethod
    def _normalise_model_labels(cls, v: str | None):
        return ModelConfigIn._normalise_model_labels(v)

    @field_validator("extra")
    @classmethod
    def _validate_extra_costs(cls, v: dict[str, Any] | None):
        return ModelConfigIn._validate_extra_costs(v)


class ModelCatalogImportIn(BaseModel):
    provider: ModelProvider
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=1, max_length=4096)
    gateway_format: ModelGatewayFormat
    models: list[ModelCatalogImportItemIn] = Field(min_length=1, max_length=50)

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, v: str):
        value = ModelConfigIn._normalise_base_url(v)
        if not value:
            raise ValueError("批量导入必须填写 Base URL")
        return value

    @field_validator("api_key")
    @classmethod
    def _normalise_api_key(cls, v: str):
        value = ModelConfigIn._normalise_api_key(v)
        if not value:
            raise ValueError("批量导入必须填写 API Key")
        return value


class ModelVersionActivateIn(BaseModel):
    kind: Literal["capability", "price"]
    version: int = Field(ge=1)


class ModelVersionDraftCreateIn(BaseModel):
    kind: Literal["capability", "price"]
    schema_version: str | None = Field(default=None, min_length=1, max_length=32)
    capabilities: dict[str, Any] | None = None
    base_cost_credits: int | None = Field(
        default=None,
        ge=0,
        le=MAX_MODEL_COST_CREDITS,
    )
    unlock_cost_credits: int | None = Field(
        default=None,
        ge=0,
        le=MAX_MODEL_COST_CREDITS,
    )
    pricing: dict[str, Any] | None = None

    @field_validator("schema_version")
    @classmethod
    def _normalize_schema_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("版本 Schema 不能为空")
        return normalized

    @field_validator("capabilities", "pricing")
    @classmethod
    def _validate_version_json(cls, value: dict[str, Any] | None):
        if value is None:
            return None
        _reject_dangerous_json_keys(value, "model_version")
        _validate_json_payload_size(value, 64 * 1024, "模型版本")
        return value

    @model_validator(mode="after")
    def _validate_kind_payload(self):
        if self.kind == "capability":
            if self.capabilities is None:
                raise ValueError("能力版本必须提供 capabilities")
            if any(
                value is not None
                for value in (
                    self.base_cost_credits,
                    self.unlock_cost_credits,
                    self.pricing,
                )
            ):
                raise ValueError("能力版本不能包含价格字段")
        else:
            if self.base_cost_credits is None or self.unlock_cost_credits is None:
                raise ValueError("价格版本必须提供调用和解锁积分")
            if self.pricing is None:
                raise ValueError("价格版本必须提供 pricing")
            if self.capabilities is not None:
                raise ValueError("价格版本不能包含 capabilities")
        return self


class ModelVersionDraftPatchIn(BaseModel):
    schema_version: str | None = Field(default=None, min_length=1, max_length=32)
    capabilities: dict[str, Any] | None = None
    base_cost_credits: int | None = Field(
        default=None,
        ge=0,
        le=MAX_MODEL_COST_CREDITS,
    )
    unlock_cost_credits: int | None = Field(
        default=None,
        ge=0,
        le=MAX_MODEL_COST_CREDITS,
    )
    pricing: dict[str, Any] | None = None

    @field_validator("schema_version")
    @classmethod
    def _normalize_schema_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("版本 Schema 不能为空")
        return normalized

    @field_validator("capabilities", "pricing")
    @classmethod
    def _validate_version_json(cls, value: dict[str, Any] | None):
        if value is None:
            return None
        _reject_dangerous_json_keys(value, "model_version")
        _validate_json_payload_size(value, 64 * 1024, "模型版本")
        return value

    @model_validator(mode="after")
    def _require_patch_field(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个草稿字段")
        return self


class ModelRouteCreateIn(BaseModel):
    route_key: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    name: str = Field(min_length=1, max_length=128)
    model_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=32)
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    gateway_format: str | None = Field(default=None, max_length=16)
    extra: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=100000)
    enabled: bool = True
    failure_threshold: int = Field(default=5, ge=1, le=100)
    window_seconds: int = Field(default=60, ge=1, le=86400)
    cooldown_seconds: int = Field(default=60, ge=1, le=86400)

    @field_validator(
        "route_key",
        "name",
        "model_id",
        "provider",
        "base_url",
        "api_key",
        "gateway_format",
    )
    @classmethod
    def _normalize_route_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("extra")
    @classmethod
    def _validate_route_extra(cls, value: dict[str, Any]):
        _reject_dangerous_json_keys(value, "model_route.extra")
        _reject_route_secret_json_keys(value)
        _validate_json_payload_size(value, 64 * 1024, "模型路由配置")
        if {"capabilities", "credit_pricing"}.intersection(value):
            raise ValueError("模型路由不能覆盖能力版本或价格版本")
        return value


class ModelRoutePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    model_id: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=32)
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    api_key_clear: bool = False
    gateway_format: str | None = Field(default=None, max_length=16)
    extra: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=100000)
    enabled: bool | None = None
    managed_by_model_config: bool | None = None
    failure_threshold: int | None = Field(default=None, ge=1, le=100)
    window_seconds: int | None = Field(default=None, ge=1, le=86400)
    cooldown_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator(
        "name",
        "model_id",
        "provider",
        "base_url",
        "api_key",
        "gateway_format",
    )
    @classmethod
    def _normalize_route_text(cls, value: str | None) -> str | None:
        return ModelRouteCreateIn._normalize_route_text(value)

    @field_validator("extra")
    @classmethod
    def _validate_route_extra(cls, value: dict[str, Any] | None):
        return value if value is None else ModelRouteCreateIn._validate_route_extra(value)

    @model_validator(mode="after")
    def _require_route_patch(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个路由修改字段")
        return self
