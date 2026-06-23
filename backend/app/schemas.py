"""Pydantic request/response models."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MODEL_COST_CREDITS = 1_000_000
MAX_PAYMENT_AMOUNT_CENTS = 1_000_000_00  # 1,000,000 CNY
MAX_PAYMENT_PACKAGE_CREDITS = 100_000_000
MAX_PAYMENT_CREDITS_PER_CENT = 10_000


# --- Auth (phone + password) ---
class RegisterIn(BaseModel):
    phone: str
    password: str = Field(max_length=128)
    sms_code: str | None = None
    nickname: str | None = None


class SmsCodeIn(BaseModel):
    phone: str


class LoginIn(BaseModel):
    phone: str
    password: str = Field(max_length=128)


class TokenOut(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"


class ChangePasswordIn(BaseModel):
    old_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)


class ResetPasswordIn(BaseModel):
    password: str = Field(max_length=128)
    admin_password: str | None = Field(default=None, max_length=128)


class AdminTaskRefundIn(BaseModel):
    admin_password: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=255)


class AdminTaskSettleIn(BaseModel):
    result_url: str | None = Field(default=None, max_length=2048)
    external_task_id: str | None = Field(default=None, max_length=256)
    admin_password: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=255)


class AdminAssetReportHandleIn(BaseModel):
    action: Literal["dismiss", "takedown"]
    note: str | None = Field(default=None, max_length=500)
    admin_password: str | None = Field(default=None, max_length=128)


class UserOut(BaseModel):
    id: int
    phone: str
    nickname: str | None = None
    department: str | None = None
    status: str
    is_admin: bool
    balance_credits: int
    frozen_credits: int

    model_config = ConfigDict(from_attributes=True)


# --- Parse ---
class ParseIn(BaseModel):
    url: str


class Asset(BaseModel):
    type: str  # image / video
    url: str
    thumb: str | None = None
    original_url: str | None = None
    original_thumb: str | None = None
    source_page_url: str | None = None
    source_captured_at: str | None = None
    width: int | None = None
    height: int | None = None
    thumb_width: int | None = None
    thumb_height: int | None = None


class ParseOut(BaseModel):
    id: int
    status: str
    url: str
    assets: list[Asset] | None = None
    error: str | None = None


# --- Reverse prompt ---
class ReverseIn(BaseModel):
    asset_url: str
    target: Literal["image", "video"] = "image"  # selects prompt dimensions
    source_type: Literal["image", "video"] | None = None
    video_analysis_preset: Literal["fast", "standard", "fine"] | None = None
    # For a video asset_url with target=video: a cover/keyframe image to fall
    # back to when server-side keyframe sampling is unavailable.
    fallback_image: str | None = None


class ReverseOut(BaseModel):
    structured: dict[str, Any]
    final_text: str
    charged_credits: int = 0
    reference_count: int = 1


# --- Generate ---
class GenerateIn(BaseModel):
    # optional: pure text-to-image needs no reference; only set when generating
    # from a scraped reference asset (same-style / first-frame).
    client_request_id: str | None = Field(default=None, min_length=8, max_length=128)
    source_asset_url: str | None = None
    source_type: Literal["image", "video"] = "image"
    category: Literal["image", "video"] = "image"
    stage: Literal["preview", "final"] = "preview"
    parent_task_id: int | None = None
    # Either a structured prompt + final_text (reverse-prompt mode) ...
    prompt: dict[str, Any] | None = None
    # ... or a plain instruction (image+instruction -> image mode, reverse off).
    instruction: str | None = None
    source_asset_meta: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    category: str
    stage: str
    parent_task_id: int | None = None
    status: str
    model_use: str | None = None
    cost_frozen: int
    cost_settled: int
    error: str | None = None
    partial: bool = False
    requested_count: int | None = None
    saved_count: int | None = None
    skipped_count: int | None = None
    partial_errors: list[str] = Field(default_factory=list)
    progress: int = 0
    final_task_id: int | None = None
    final_status: str | None = None
    final_asset_count: int = 0
    created_at: datetime | None = None
    finished_at: datetime | None = None
    assets: list[AssetOut] = Field(default_factory=list)


class AssetOut(BaseModel):
    id: int
    task_id: int
    type: str
    preview_url: str | None = None
    hd_url: str | None = None
    watermarked: bool
    unlocked: bool
    favorite: bool = False
    moderation_status: str = "active"
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    created_at: datetime | None = None
    # populated by the profile gallery (retention)
    expires_at: datetime | None = None
    days_left: int | None = None
    category: str | None = None
    unlock_cost: int = 0

    model_config = ConfigDict(from_attributes=True)


class AssetReportIn(BaseModel):
    reason: Literal["copyright", "sensitive", "illegal", "privacy", "other"]
    note: str | None = Field(default=None, max_length=500)


class AssetReportOut(BaseModel):
    id: int
    asset_id: int | None = None
    reporter_user_id: int
    owner_user_id: int | None = None
    reason: str
    note: str | None = None
    status: str
    handle_note: str | None = None
    handled_by: int | None = None
    handled_at: datetime | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# --- Payments ---
class PaymentPackageOut(BaseModel):
    id: str
    title: str
    amount_cents: int
    credits: int
    badge: str | None = None
    enabled: bool = True
    sort_order: int = 0


class PaymentCreateIn(BaseModel):
    provider: Literal["alipay", "wechat"] = "alipay"
    package_id: str = Field(min_length=1, max_length=32)

    @field_validator("package_id")
    @classmethod
    def _package_id(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("套餐 ID 不能为空")
        if not re_match_package_id(value):
            raise ValueError("套餐 ID 只能包含字母、数字、下划线和短横线")
        return value


class PaymentOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    provider: str
    package_id: str
    amount_cents: int
    credits: int
    status: str
    code_url: str | None = None
    provider_trade_no: str | None = None
    expires_at: datetime | None = None
    paid_at: datetime | None = None
    created_at: datetime | None = None


# --- Admin ---
class WhitelistIn(BaseModel):
    phone: str
    note: str | None = None
    department: str | None = None
    admin_password: str | None = Field(default=None, max_length=128)


class WhitelistDeleteIn(BaseModel):
    admin_password: str | None = Field(default=None, max_length=128)


class QuotaGrantIn(BaseModel):
    user_id: int
    amount: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=255)
    admin_password: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


class UserStatusIn(BaseModel):
    status: Literal["active", "pending", "disabled"]
    admin_password: str | None = Field(default=None, max_length=128)


class ModelConfigIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    use: Literal["vision", "image", "video"]
    model_id: str = Field(min_length=1, max_length=128)
    provider: Literal[
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
        "custom_openai",
    ] | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    api_key_clear: bool = False
    gateway_format: Literal["openai", "ark"] | None = None
    cost_credits: int = Field(ge=1, le=MAX_MODEL_COST_CREDITS)
    unlock_cost: int = Field(default=0, ge=0, le=MAX_MODEL_COST_CREDITS)
    enabled: bool = True
    extra: dict[str, Any] | None = None
    admin_password: str | None = Field(default=None, max_length=128)

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


class ModelProbeIn(BaseModel):
    use: Literal["vision", "image", "video"] | None = None
    provider: Literal[
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
        "custom_openai",
    ] | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    gateway_format: Literal["openai", "ark"] | None = None
    admin_password: str | None = Field(default=None, max_length=128)

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


class SettingsIn(BaseModel):
    reverse_prompt_enabled: bool | None = None
    sms_auth_enabled: bool | None = None
    payment_enabled: bool | None = None
    content_safety_enabled: bool | None = None
    content_safety_banned_terms: str | None = Field(default=None, max_length=4000)
    image_n: int | None = Field(default=None, ge=1, le=8)
    image_size: str | None = None
    asset_retention_days: int | None = Field(default=None, ge=1, le=3650)
    audit_retention_days: int | None = Field(default=None, ge=1, le=3650)
    admin_api_rate_per_hour: int | None = Field(default=None, ge=10, le=100000)
    admin_quota_grant_single_limit: int | None = Field(default=None, ge=1, le=100000000)
    admin_quota_grant_daily_limit: int | None = Field(default=None, ge=1, le=1000000000)
    review_task_sla_minutes: int | None = Field(default=None, ge=1, le=10080)
    admin_password: str | None = Field(default=None, max_length=128)


class PaymentPackageIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(gt=0, le=MAX_PAYMENT_AMOUNT_CENTS)
    credits: int = Field(gt=0, le=MAX_PAYMENT_PACKAGE_CREDITS)
    badge: str | None = Field(default=None, max_length=32)
    enabled: bool = True
    sort_order: int = 0
    admin_password: str | None = Field(default=None, max_length=128)

    @field_validator("id")
    @classmethod
    def _package_id(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("套餐 ID 不能为空")
        if not re_match_package_id(value):
            raise ValueError("套餐 ID 只能包含字母、数字、下划线和短横线")
        return value

    @model_validator(mode="after")
    def _package_ratio(self):
        if self.credits > self.amount_cents * MAX_PAYMENT_CREDITS_PER_CENT:
            raise ValueError("套餐积分/价格比例异常,请核对金额和积分")
        return self


class PaymentPackageDisableIn(BaseModel):
    admin_password: str | None = Field(default=None, max_length=128)


class PaymentProviderConfigIn(BaseModel):
    provider: Literal["alipay", "wechat"]
    enabled: bool = False
    mode: Literal["mock", "live"] = "mock"
    public_config: dict[str, Any] = Field(default_factory=dict)
    secret_config: dict[str, Any] = Field(default_factory=dict)
    admin_password: str | None = Field(default=None, max_length=128)

    @field_validator("public_config", "secret_config")
    @classmethod
    def _validate_provider_config_size(cls, v: dict[str, Any]):
        _validate_json_payload_size(v, 64 * 1024, "支付配置")
        return v


class PaymentProviderConfigOut(BaseModel):
    provider: str
    enabled: bool
    mode: str
    source: str = "db"
    public_config: dict[str, Any] = Field(default_factory=dict)
    secret_config_masked: dict[str, str] = Field(default_factory=dict)
    configured: bool = False
    ready: bool = False
    issues: list[str] = Field(default_factory=list)


class OnlineUpdateStatusOut(BaseModel):
    enabled: bool
    repo_dir: str
    remote: str
    branch: str
    current_branch: str = ""
    current_head: str = ""
    remote_head: str = ""
    dirty: bool = False
    dirty_status: str = ""
    apply_command_configured: bool = False
    allow_dirty: bool = False
    error: str | None = None


class OnlineUpdateRunIn(BaseModel):
    apply: bool = True


class OnlineUpdateRunOut(BaseModel):
    ok: bool
    changed: bool
    applied: bool
    partial_failure: bool = False
    before: str
    after: str
    remote_head: str = ""
    output: str = ""
    error: str = ""


class AuditOut(BaseModel):
    id: int
    user_id: int | None = None
    action: str
    biz_type: str | None = None
    biz_id: int | None = None
    ip: str | None = None
    detail: dict[str, Any] | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


def re_match_package_id(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _validate_json_payload_size(value: dict[str, Any], max_bytes: int, label: str) -> None:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise ValueError(f"{label} 必须是可序列化 JSON") from e
    if len(raw) > max_bytes:
        raise ValueError(f"{label} 不能超过 {max_bytes // 1024}KB")


TaskOut.model_rebuild()
