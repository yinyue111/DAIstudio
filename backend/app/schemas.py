"""Pydantic request/response models."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

MAX_MODEL_COST_CREDITS = 1_000_000
MAX_PAYMENT_AMOUNT_CENTS = 1_000_000_00  # 1,000,000 CNY
MAX_PAYMENT_PACKAGE_CREDITS = 100_000_000
MAX_PAYMENT_CREDITS_PER_CENT = 10_000

ModelUse = Literal["vision", "image", "video", "prompt"]
ModelProvider = Literal[
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
]
ModelGatewayFormat = Literal["openai", "ark", "anthropic"]


def _serialize_utc_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


UtcDateTime = Annotated[
    datetime,
    PlainSerializer(_serialize_utc_datetime, return_type=str, when_used="json"),
]


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


class AdminTaskRefundIn(BaseModel):
    note: str | None = Field(default=None, max_length=255)


class AdminTaskSettleIn(BaseModel):
    result_url: str | None = Field(default=None, max_length=2048)
    external_task_id: str | None = Field(default=None, max_length=256)
    note: str | None = Field(default=None, max_length=255)


class AdminAssetReportHandleIn(BaseModel):
    action: Literal["dismiss", "takedown"]
    note: str | None = Field(default=None, max_length=500)


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


class UserDraftIn(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        _validate_json_payload_size(v, 96 * 1024, "草稿")
        return v


class UserDraftOut(BaseModel):
    key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: UtcDateTime | None = None


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
ReverseTarget = Literal["image", "video", "product_profile", "portrait_profile"]
ReverseOperationStatus = Literal[
    "queued",
    "running",
    "needs_confirmation",
    "succeeded",
    "failed",
    "canceled",
]


class ReverseIn(BaseModel):
    client_request_id: str | None = Field(default=None, min_length=8, max_length=128)
    asset_url: str
    target: ReverseTarget = "image"  # selects prompt dimensions
    source_type: Literal["image", "video"] | None = None
    video_analysis_preset: Literal["fast", "standard", "fine"] | None = None
    # Candidate cover used only after explicit fallback confirmation when
    # server-side keyframe sampling is unavailable.
    fallback_image: str | None = None
    model_config_id: int | None = Field(default=None, gt=0)

    @field_validator("client_request_id")
    @classmethod
    def _normalize_optional_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not 8 <= len(normalized) <= 128:
            raise ValueError("client_request_id 去除首尾空白后长度必须为 8-128")
        return normalized


class ReverseOperationCreate(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    asset_url: str = Field(min_length=1, max_length=4096)
    target: ReverseTarget = "image"
    source_type: Literal["image", "video"] | None = None
    video_analysis_preset: Literal["fast", "standard", "fine"] | None = None
    fallback_image: str | None = Field(default=None, max_length=4096)
    workspace_snapshot_v2: dict[str, Any] | None = None
    model_config_id: int | None = Field(default=None, gt=0)

    @field_validator("client_request_id")
    @classmethod
    def _normalize_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not 8 <= len(normalized) <= 128:
            raise ValueError("client_request_id 去除首尾空白后长度必须为 8-128")
        return normalized

    @field_validator("workspace_snapshot_v2")
    @classmethod
    def _workspace_snapshot_size(cls, value: dict[str, Any] | None):
        if value is not None:
            _validate_workspace_snapshot(value)
            _validate_json_payload_size(value, 64 * 1024, "工作区快照")
        return value


class ReverseOperationConfirm(BaseModel):
    fallback_image: str | None = Field(default=None, max_length=4096)


class ReverseOperationTimestamps(BaseModel):
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None


class ReverseOperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_config_id: int | None = None
    model_name: str | None = None
    model_id: str | None = None
    target: ReverseTarget
    source_type: Literal["image", "video"] | None = None
    status: ReverseOperationStatus
    phase: str | None = None
    progress: int = Field(default=0, ge=0, le=100)
    result: dict[str, Any] | None = None
    video_analysis: dict[str, Any] | None = None
    request_context: dict[str, Any] | None = None
    workspace_snapshot_v2: dict[str, Any] | None = None
    reference_count: int = Field(default=1, ge=0)
    charged_credits: int = Field(default=0, ge=0)
    cost_frozen: int = 0
    cost_settled: int = 0
    confirmation_expires_at: UtcDateTime | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    error: str | None = None
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    timestamps: ReverseOperationTimestamps = Field(default_factory=ReverseOperationTimestamps)

    @model_validator(mode="after")
    def _sync_timestamps(self):
        # Keep the flat fields for one compatibility window while making the
        # grouped lifecycle contract present on every response.
        self.timestamps = ReverseOperationTimestamps(
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )
        return self


class ReverseOut(BaseModel):
    structured: dict[str, Any]
    final_text: str
    charged_credits: int = 0
    reference_count: int = 1
    video_analysis: dict[str, Any] | None = None
    model_config_id: int | None = None
    model_name: str | None = None


class PromptOptimizeIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    category: Literal["image", "video"] = "image"
    product_mode: bool = False
    duration: int | None = Field(default=None, ge=1, le=3600)
    subject_mode: Literal["general", "product", "portrait"] | None = None
    reference_type: str | None = Field(default=None, max_length=64)
    subject_profile: dict[str, Any] | str | None = None
    target_model_id: str | None = Field(default=None, max_length=256)
    target_model_provider: str | None = Field(default=None, max_length=128)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    resolution: str | None = Field(default=None, max_length=32)
    product_lock_mode: Literal["free", "locked"] | None = None
    product_video_template: str | None = Field(default=None, max_length=64)
    optimizer_model_config_id: int | None = Field(default=None, gt=0)
    target_model_config_id: int | None = Field(default=None, gt=0)

    @field_validator(
        "prompt",
        "reference_type",
        "target_model_id",
        "target_model_provider",
        "aspect_ratio",
        "resolution",
        "product_video_template",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("subject_profile")
    @classmethod
    def _validate_subject_profile(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 4000:
                raise ValueError("主体档案过长")
            return value or None
        if isinstance(value, dict):
            _validate_json_payload_size(value, 16 * 1024, "主体档案")
        return value


class PromptOptimizeOut(BaseModel):
    prompt: str
    model_id: str
    optimizer_model_id: str | None = None
    compiler_metadata: dict[str, Any] | None = None
    context_metadata: dict[str, Any] | None = None
    optimizer_model_config_id: int | None = None
    optimizer_model_name: str | None = None


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
    model_config_id: int | None = Field(default=None, gt=0)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    category: str
    stage: str
    parent_task_id: int | None = None
    status: str
    model_use: str | None = None
    model_config_id: int | None = None
    model_name: str | None = None
    model_id: str | None = None
    model_provider: str | None = None
    prompt_text: str | None = None
    prompt_text_source: Literal["generation", "request"] | None = None
    request_prompt_text: str | None = None
    generation_prompt_text: str | None = None
    raw_prompt_text: str | None = None
    optimized_prompt_text: str | None = None
    assembled_prompt_text: str | None = None
    prompt_optimizer_model_id: str | None = None
    prompt_compiler_version: str | None = None
    prompt_warnings: list[str] = Field(default_factory=list)
    post_overlays: list[str] = Field(default_factory=list)
    voiceover: str | None = None
    sfx: list[str] = Field(default_factory=list)
    sequence_required: bool = False
    cost_frozen: int
    cost_settled: int
    error: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    eta_source: str | None = None
    eta_total_seconds: int | None = None
    eta_remaining_seconds: int | None = None
    eta_sample_count: int | None = None
    partial: bool = False
    requested_count: int | None = None
    saved_count: int | None = None
    skipped_count: int | None = None
    partial_errors: list[str] = Field(default_factory=list)
    progress: int = 0
    final_task_id: int | None = None
    final_status: str | None = None
    final_asset_count: int = 0
    final_cost_estimate: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
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
    created_at: UtcDateTime | None = None
    # populated by the profile gallery (retention)
    expires_at: UtcDateTime | None = None
    days_left: int | None = None
    category: str | None = None
    unlock_cost: int = 0
    quality_status: str = "ok"
    quality_message: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UserAssetItem(BaseModel):
    asset_ref: str
    origin: Literal["generated", "uploaded"]
    type: Literal["image", "video"]
    url: str | None = None
    preview_url: str | None = None
    thumb: str | None = None
    favorite: bool = False
    retained: bool = False
    unlocked: bool = False
    unlock_cost: int = 0
    moderation_status: str = "active"
    available: bool = True
    created_at: UtcDateTime | None = None
    expires_at: UtcDateTime | None = None
    days_left: int | None = None
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    filename: str | None = None
    task_id: int | None = None
    download_url: str | None = None


class UserAssetStats(BaseModel):
    generated: int = 0
    uploaded: int = 0
    images: int = 0
    videos: int = 0
    favorites: int = 0
    retained: int = 0


class UserAssetListOut(BaseModel):
    items: list[UserAssetItem] = Field(default_factory=list)
    total: int = 0
    stats: UserAssetStats = Field(default_factory=UserAssetStats)
    next_cursor: str | None = None


class UserAssetMetadataIn(BaseModel):
    asset_refs: list[str] = Field(min_length=1, max_length=100)
    favorite: bool | None = None
    retained: bool | None = None

    @field_validator("asset_refs")
    @classmethod
    def _normalize_asset_refs(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value or "").strip() for value in values))
        if not normalized or any(not value or len(value) > 512 for value in normalized):
            raise ValueError("asset_refs 非法")
        return normalized

    @model_validator(mode="after")
    def _require_metadata_change(self):
        if self.favorite is None and self.retained is None:
            raise ValueError("favorite 和 retained 至少提供一个")
        return self


class UserAssetBatchDeleteIn(BaseModel):
    asset_refs: list[str] = Field(min_length=1, max_length=100)

    @field_validator("asset_refs")
    @classmethod
    def _normalize_asset_refs(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value or "").strip() for value in values))
        if not normalized or any(not value or len(value) > 512 for value in normalized):
            raise ValueError("asset_refs 非法")
        return normalized


class UserAssetMutationOut(BaseModel):
    asset_refs: list[str] = Field(default_factory=list)


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
    handled_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None

    model_config = ConfigDict(from_attributes=True)


class AssetBatchIn(BaseModel):
    asset_ids: list[int] = Field(min_length=1, max_length=100)


class AssetBatchItemOut(BaseModel):
    id: int
    ok: bool = True
    error: str | None = None


class AssetBatchDeleteOut(BaseModel):
    deleted: list[int] = Field(default_factory=list)
    failed: list[AssetBatchItemOut] = Field(default_factory=list)


# --- Prompt history ---
class UserPromptIn(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    prompt: str = Field(min_length=1, max_length=12000)
    category: Literal["image", "video", "general"] = "general"
    source: Literal["manual", "reverse", "generate", "library"] = "manual"
    favorite: bool = False
    params: dict[str, Any] | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = v.strip()
        return text or None

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        text = v.strip()
        if not text:
            raise ValueError("提示词不能为空")
        return text

    @field_validator("params")
    @classmethod
    def _params_size(cls, v: dict[str, Any] | None):
        if v is not None:
            _validate_json_payload_size(v, 16 * 1024, "提示词参数")
        return v


class UserPromptUpdateIn(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    prompt: str | None = Field(default=None, min_length=1, max_length=12000)
    category: Literal["image", "video", "general"] | None = None
    favorite: bool | None = None
    params: dict[str, Any] | None = None
    increment_usage: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = v.strip()
        return text or None

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = v.strip()
        if not text:
            raise ValueError("提示词不能为空")
        return text

    @field_validator("params")
    @classmethod
    def _params_size(cls, v: dict[str, Any] | None):
        if v is not None:
            _validate_json_payload_size(v, 16 * 1024, "提示词参数")
        return v


class UserPromptOut(BaseModel):
    id: int
    title: str
    prompt: str
    category: str
    source: str
    favorite: bool
    usage_count: int
    params: dict[str, Any] | None = None
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None

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
    expires_at: UtcDateTime | None = None
    paid_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None


# --- Admin ---
class WhitelistIn(BaseModel):
    phone: str
    note: str | None = None
    department: str | None = None


class WhitelistDeleteIn(BaseModel):
    pass


class QuotaGrantIn(BaseModel):
    user_id: int
    amount: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=255)
    idempotency_key: str = Field(min_length=8, max_length=128)


class QuotaGrantItemIn(BaseModel):
    user_id: int
    amount: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=255)


class QuotaBulkGrantIn(BaseModel):
    items: list[QuotaGrantItemIn] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=96)


class QuotaBulkGrantItemOut(BaseModel):
    user_id: int
    ok: bool = True
    balance_credits: int | None = None
    error: str | None = None


class QuotaBulkGrantOut(BaseModel):
    granted: list[QuotaBulkGrantItemOut] = Field(default_factory=list)
    failed: list[QuotaBulkGrantItemOut] = Field(default_factory=list)


class UserStatusIn(BaseModel):
    status: Literal["active", "pending", "disabled"]


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
        if self.provider_config_id is not None and any((
            self.provider,
            self.base_url,
            self.api_key,
            self.api_key_clear,
            self.gateway_format,
        )):
            raise ValueError("已有供应商不能与 Base URL、API Key 或网关格式同时提交")
        if self.use == "vision" and (
            self.provider == "anthropic" or self.gateway_format == "anthropic"
        ):
            raise ValueError("视觉反推不支持 Anthropic 原生协议,请使用 OpenAI-compatible 视觉网关")
        return self


class ModelConfigPatchIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

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
        if self.provider_config_id is not None and any((
            self.provider,
            self.base_url,
            self.api_key,
            self.gateway_format,
        )):
            raise ValueError("使用已有供应商探测时不能临时覆盖网关参数")
        if self.use == "vision" and (
            self.provider == "anthropic" or self.gateway_format == "anthropic"
        ):
            raise ValueError("视觉反推不支持 Anthropic 原生协议,请使用 OpenAI-compatible 视觉网关")
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


class AuditOut(BaseModel):
    id: int
    user_id: int | None = None
    action: str
    biz_type: str | None = None
    biz_id: int | None = None
    ip: str | None = None
    detail: dict[str, Any] | None = None
    created_at: UtcDateTime | None = None

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


_SNAPSHOT_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "auth_key",
)
_SNAPSHOT_BINARY_KEY_PARTS = ("base64", "binary", "blob", "file_content", "raw_bytes")


def _validate_workspace_snapshot(value: dict[str, Any]) -> None:
    """Reject secrets and embedded media while keeping Studio metadata extensible."""

    def walk(node: Any, *, path: str, depth: int) -> None:
        if depth > 12:
            raise ValueError("工作区快照嵌套过深")
        if isinstance(node, dict):
            for raw_key, child in node.items():
                if not isinstance(raw_key, str):
                    raise ValueError("工作区快照的字段名必须是字符串")
                key = raw_key.strip().lower().replace("-", "_")
                child_path = f"{path}.{raw_key}" if path else raw_key
                if any(part in key for part in _SNAPSHOT_SECRET_KEY_PARTS):
                    raise ValueError(f"工作区快照不得包含密钥或凭据字段: {child_path}")
                if any(part in key for part in _SNAPSHOT_BINARY_KEY_PARTS):
                    raise ValueError(f"工作区快照不得包含二进制内容字段: {child_path}")
                walk(child, path=child_path, depth=depth + 1)
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, path=f"{path}[{index}]", depth=depth + 1)
            return
        if isinstance(node, str) and node.lstrip().lower().startswith("data:"):
            raise ValueError(f"工作区快照不得内嵌 data URI: {path}")

    walk(value, path="", depth=0)


TaskOut.model_rebuild()
