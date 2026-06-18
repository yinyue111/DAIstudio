"""Pydantic request/response models."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Auth (phone + password) ---
class RegisterIn(BaseModel):
    phone: str
    password: str
    sms_code: str
    nickname: str | None = None


class SmsCodeIn(BaseModel):
    phone: str


class LoginIn(BaseModel):
    phone: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


class ResetPasswordIn(BaseModel):
    password: str
    admin_password: str | None = None


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
    # For a video asset_url with target=video: a cover/keyframe image to fall
    # back to when server-side keyframe sampling is unavailable.
    fallback_image: str | None = None


class ReverseOut(BaseModel):
    structured: dict[str, Any]
    final_text: str


# --- Generate ---
class GenerateIn(BaseModel):
    # optional: pure text-to-image needs no reference; only set when generating
    # from a scraped reference asset (same-style / first-frame).
    source_asset_url: str | None = None
    source_type: Literal["image", "video"] = "image"
    category: Literal["image", "video"] = "image"
    stage: Literal["preview", "final"] = "preview"
    parent_task_id: int | None = None
    # Either a structured prompt + final_text (reverse-prompt mode) ...
    prompt: dict[str, Any] | None = None
    # ... or a plain instruction (image+instruction -> image mode, reverse off).
    instruction: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    category: str
    stage: str
    status: str
    model_use: str | None = None
    cost_frozen: int
    cost_settled: int
    error: str | None = None
    partial: bool = False
    requested_count: int | None = None
    saved_count: int | None = None
    skipped_count: int | None = None
    progress: int = 0
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
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    created_at: datetime | None = None
    # populated by the profile gallery (retention)
    expires_at: datetime | None = None
    days_left: int | None = None
    category: str | None = None

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
    package_id: str


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
    admin_password: str | None = None


class WhitelistDeleteIn(BaseModel):
    admin_password: str | None = None


class QuotaGrantIn(BaseModel):
    user_id: int
    amount: int = Field(gt=0)
    note: str | None = None
    admin_password: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class UserStatusIn(BaseModel):
    status: Literal["active", "pending", "disabled"]
    admin_password: str | None = None


class ModelConfigIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    use: Literal["vision", "image", "video"]
    model_id: str = Field(min_length=1)
    cost_credits: int = Field(ge=0)
    unlock_cost: int = Field(default=0, ge=0)
    enabled: bool = True
    extra: dict[str, Any] | None = None
    admin_password: str | None = None

    @field_validator("extra")
    @classmethod
    def _validate_extra_costs(cls, v: dict[str, Any] | None):
        if v is None:
            return v
        if "preview_cost" in v:
            try:
                preview_cost = int(v["preview_cost"])
            except (TypeError, ValueError):
                raise ValueError("extra.preview_cost 必须是非负整数")
            if preview_cost < 0:
                raise ValueError("extra.preview_cost 必须是非负整数")
            v = {**v, "preview_cost": preview_cost}
        return v


class SettingsIn(BaseModel):
    reverse_prompt_enabled: bool | None = None
    image_n: int | None = Field(default=None, ge=1, le=8)
    image_size: str | None = None
    asset_retention_days: int | None = Field(default=None, ge=1, le=3650)
    audit_retention_days: int | None = Field(default=None, ge=1, le=3650)
    admin_password: str | None = None


class PaymentPackageIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(gt=0)
    credits: int = Field(gt=0)
    badge: str | None = Field(default=None, max_length=32)
    enabled: bool = True
    sort_order: int = 0
    admin_password: str | None = None

    @field_validator("id")
    @classmethod
    def _package_id(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("套餐 ID 不能为空")
        if not re_match_package_id(value):
            raise ValueError("套餐 ID 只能包含字母、数字、下划线和短横线")
        return value


class PaymentPackageDisableIn(BaseModel):
    admin_password: str | None = None


class PaymentProviderConfigIn(BaseModel):
    provider: Literal["alipay", "wechat"]
    enabled: bool = False
    mode: Literal["mock", "live"] = "mock"
    public_config: dict[str, Any] = Field(default_factory=dict)
    secret_config: dict[str, Any] = Field(default_factory=dict)
    admin_password: str | None = None


class PaymentProviderConfigOut(BaseModel):
    provider: str
    enabled: bool
    mode: str
    public_config: dict[str, Any] = Field(default_factory=dict)
    secret_config_masked: dict[str, str] = Field(default_factory=dict)
    configured: bool = False
    ready: bool = False
    issues: list[str] = Field(default_factory=list)


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


TaskOut.model_rebuild()
