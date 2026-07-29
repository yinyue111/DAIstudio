"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ._base import (
    MAX_PAYMENT_AMOUNT_CENTS,
    MAX_PAYMENT_CREDITS_PER_CENT,
    MAX_PAYMENT_PACKAGE_CREDITS,
    _validate_json_payload_size,
    re_match_package_id,
)


class SettingsIn(BaseModel):
    reverse_prompt_enabled: bool | None = None
    sms_auth_enabled: bool | None = None
    payment_enabled: bool | None = None
    navigation_states: (
        dict[
            Literal["catalog", "prompts", "projects", "profile", "recharge", "history"],
            Literal["enabled", "disabled", "hidden"],
        ]
        | None
    ) = None
    content_safety_enabled: bool | None = None
    content_safety_banned_terms: str | None = Field(default=None, max_length=4000)
    media_moderation_enabled: bool | None = None
    media_moderation_fail_open: bool | None = None
    image_n: int | None = Field(default=None, ge=1, le=8)
    image_size: str | None = None
    asset_retention_days: int | None = Field(default=None, ge=1, le=3650)
    audit_retention_days: int | None = Field(default=None, ge=1, le=3650)
    admin_api_rate_per_hour: int | None = Field(default=None, ge=10, le=100000)
    admin_quota_grant_single_limit: int | None = Field(default=None, ge=1, le=100000000)
    admin_quota_grant_daily_limit: int | None = Field(default=None, ge=1, le=1000000000)
    review_task_sla_minutes: int | None = Field(default=None, ge=1, le=10080)


class PaymentPackageIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(gt=0, le=MAX_PAYMENT_AMOUNT_CENTS)
    credits: int = Field(gt=0, le=MAX_PAYMENT_PACKAGE_CREDITS)
    badge: str | None = Field(default=None, max_length=32)
    enabled: bool = True
    sort_order: int = 0

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
    pass


class PaymentProviderConfigIn(BaseModel):
    provider: Literal["alipay", "wechat"]
    enabled: bool = False
    mode: Literal["mock", "live"] = "mock"
    public_config: dict[str, Any] = Field(default_factory=dict)
    secret_config: dict[str, Any] = Field(default_factory=dict)

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
    deployment_mode: str = "unknown"
    update_strategy: str = "manual"
    can_apply_online: bool = False
    next_action: str = ""
    repo_dir: str
    remote: str
    branch: str
    github_token_configured: bool = False
    current_branch: str = ""
    current_head: str = ""
    remote_head: str = ""
    dirty: bool = False
    dirty_status: str = ""
    apply_command_configured: bool = False
    apply_command_safe: bool = False
    apply_command_error: str = ""
    allow_dirty: bool = False
    require_signed_commits: bool = False
    error: str | None = None


class OnlineUpdateRunIn(BaseModel):
    apply: bool = True
    confirm: str = ""
    force_apply: bool = False
    expected_remote_head: str = ""


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
