"""Pydantic request/response models."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from math import isfinite
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

MAX_MODEL_COST_CREDITS = 1_000_000
MAX_PAYMENT_AMOUNT_CENTS = 1_000_000_00  # 1,000,000 CNY
MAX_PAYMENT_PACKAGE_CREDITS = 100_000_000
MAX_PAYMENT_CREDITS_PER_CENT = 10_000
MAX_REVERSE_SOURCE_RANGES = 8
MAX_REVERSE_SELECTED_DURATION_SECONDS = 300

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
PromptOptimizationDirection = Literal[
    "faithful",
    "concise",
    "expand",
    "commercial",
    "cinematic",
    "model_adaptation",
    "constraints",
    "translate",
]


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
    project_id: int | None = Field(default=None, gt=0)


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
    duration: float | None = Field(default=None, ge=0)
    thumb_width: int | None = None
    thumb_height: int | None = None
    asset_ref: str | None = None


class ParseOut(BaseModel):
    id: int
    status: str
    url: str
    assets: list[Asset] | None = None
    error: str | None = None


# --- Reverse prompt ---
ReverseTarget = Literal["image", "video", "product_profile", "portrait_profile"]
ReverseAnalysisPrecision = Literal["fast", "standard", "fine"]
ReverseAnalysisFocus = Literal[
    "comprehensive",
    "replica",
    "style",
    "product_ad",
    "portrait",
    "composition_lighting",
    "poster_layout",
    "camera_motion",
    "subject_action",
    "storyboard",
    "editing_rhythm",
    "audio_script",
]
ReverseOutputPurpose = Literal[
    "generation",
    "style_transfer",
    "edit",
    "storyboard",
    "analysis_report",
]
ReverseReferenceRole = Literal[
    "primary",
    "subject",
    "product",
    "style",
    "composition",
    "lighting",
    "text_layout",
    "negative",
    "motion",
    "first_frame",
    "last_frame",
]
ReverseOperationStatus = Literal[
    "queued",
    "running",
    "needs_confirmation",
    "succeeded",
    "failed",
    "canceled",
]
ReverseBatchStatus = Literal[
    "queued",
    "running",
    "needs_confirmation",
    "partial",
    "succeeded",
    "failed",
    "canceled",
]

ReverseImageEvidenceType = Literal[
    "visual_field",
    "ocr",
    "logo",
    "packaging",
    "subject_protection",
]
ReverseImageFactStatus = Literal["visible", "inferred", "unknown"]
_DANGEROUS_JSON_KEYS = frozenset({"__proto__", "prototype", "constructor"})
_ROUTE_SECRET_JSON_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "proxy_authorization",
    "access_token",
    "refresh_token",
    "secret",
    "client_secret",
    "password",
    "cookie",
    "set_cookie",
})


def _reject_dangerous_json_keys(value: Any, path: str = "image_evidence") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _DANGEROUS_JSON_KEYS:
                raise ValueError(f"{path} 包含危险字段 {key}")
            _reject_dangerous_json_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_dangerous_json_keys(item, f"{path}[{index}]")


def _reject_route_secret_json_keys(value: Any, path: str = "model_route.extra") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _ROUTE_SECRET_JSON_KEYS:
                raise ValueError(f"{path} 不能包含密钥字段 {key},请使用专用 API Key 字段")
            _reject_route_secret_json_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_route_secret_json_keys(item, f"{path}[{index}]")


class ReverseImageEvidenceBBox(BaseModel):
    """Normalized top-left image coordinates in the inclusive 0..1 range."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @field_validator("x", "y", "width", "height", mode="before")
    @classmethod
    def _strict_finite_number(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("bbox 坐标必须是有限数值")
        number = float(value)
        if not isfinite(number):
            raise ValueError("bbox 坐标必须是有限数值")
        return round(number, 6)

    @model_validator(mode="after")
    def _inside_source_image(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("bbox 必须完整位于源图范围内")
        return self


class ReverseImageEvidencePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)

    @field_validator("x", "y", mode="before")
    @classmethod
    def _strict_finite_number(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("polygon 坐标必须是有限数值")
        number = float(value)
        if not isfinite(number):
            raise ValueError("polygon 坐标必须是有限数值")
        return round(number, 6)


class ReverseImageEvidence(BaseModel):
    """One field-level, source-addressable image evidence region."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: ReverseImageEvidenceType
    bbox: ReverseImageEvidenceBBox | None = None
    polygon: list[ReverseImageEvidencePoint] | None = Field(
        default=None, min_length=3, max_length=128,
    )
    field_key: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    evidence_text: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    source_index: int = Field(ge=1, le=12)
    fact_status: ReverseImageFactStatus
    protected: bool
    editable: bool
    evidence_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    source_fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    analyzer_source: str | None = Field(default=None, min_length=1, max_length=128)
    analyzer_status: Literal["analyzed", "unsupported", "degraded", "failed"] | None = None
    analyzer_version: str | None = Field(default=None, min_length=1, max_length=128)
    review_status: Literal["pending", "confirmed", "rejected"] | None = None
    conflict_status: Literal["none", "conflict"] | None = None
    conflicts_with: list[str] = Field(default_factory=list, max_length=128)
    mask_extraction: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_dangerous_keys_and_coercion(cls, value: Any):
        _reject_dangerous_json_keys(value)
        if not isinstance(value, dict):
            raise ValueError("image_evidence 条目必须是 JSON 对象")
        confidence = value.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("confidence 必须是有限数值")
        if not isfinite(float(confidence)):
            raise ValueError("confidence 必须是有限数值")
        source_index = value.get("source_index")
        if isinstance(source_index, bool) or not isinstance(source_index, int):
            raise ValueError("source_index 必须是整数")
        for key in ("protected", "editable"):
            if not isinstance(value.get(key), bool):
                raise ValueError(f"{key} 必须是布尔值")
        return value

    @field_validator("field_key", "evidence_text")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("证据文本和关联字段不能为空")
        return normalized

    @field_validator("confidence")
    @classmethod
    def _normalize_confidence(cls, value: float) -> float:
        return round(float(value), 6)

    @model_validator(mode="after")
    def _validate_evidence_semantics(self):
        if self.bbox is not None and self.polygon is not None:
            raise ValueError("bbox 和 polygon 必须且只能提供一种")
        has_region = self.bbox is not None or self.polygon is not None
        if self.fact_status == "visible" and not has_region:
            raise ValueError("visible 事实必须提供 bbox 或 polygon")
        if self.protected and self.editable:
            raise ValueError("同一证据区域不能同时为 protected 和 editable")
        if self.protected and (self.fact_status != "visible" or not has_region):
            raise ValueError("只有可见且可定位的事实才能标记 protected")
        if self.evidence_type == "subject_protection" and (
            not self.protected or self.editable or self.fact_status != "visible" or not has_region
        ):
            raise ValueError("subject_protection 必须是可见、可定位且不可编辑的保护区")
        return self


class ReverseIn(BaseModel):
    client_request_id: str | None = Field(default=None, min_length=8, max_length=128)
    project_id: int | None = Field(default=None, gt=0)
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


class ReverseSourceRange(BaseModel):
    start_seconds: float = Field(default=0, ge=0, le=86_400)
    end_seconds: float = Field(gt=0, le=86_400)

    @model_validator(mode="after")
    def _validate_range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("视频结束时间必须大于开始时间")
        return self


class ReverseSourceReference(BaseModel):
    asset_url: str = Field(min_length=1, max_length=4096)
    source_type: Literal["image", "video"] = "image"
    role: ReverseReferenceRole = "primary"
    asset_id: int | None = Field(default=None, gt=0)
    label: str | None = Field(default=None, max_length=64)


class ReverseOperationCreate(BaseModel):
    # Required by the execution route. It remains optional in this reusable
    # intent schema so the same contract can be validated by POST /api/quotes.
    quote_id: int | None = Field(default=None, gt=0)
    client_request_id: str = Field(min_length=8, max_length=128)
    project_id: int | None = Field(default=None, gt=0)
    asset_url: str | None = Field(default=None, min_length=1, max_length=4096)
    sources: list[ReverseSourceReference] = Field(default_factory=list, max_length=12)
    target: ReverseTarget = "image"
    source_type: Literal["image", "video"] | None = None
    video_analysis_preset: ReverseAnalysisPrecision | None = None
    analysis_precision: ReverseAnalysisPrecision | None = None
    analysis_focus: ReverseAnalysisFocus = "comprehensive"
    output_purpose: ReverseOutputPurpose = "generation"
    custom_instruction: str | None = Field(default=None, max_length=500)
    source_range: ReverseSourceRange | None = None
    source_ranges: list[ReverseSourceRange] = Field(
        default_factory=list,
        max_length=MAX_REVERSE_SOURCE_RANGES,
    )
    custom_keyframes: list[float] = Field(default_factory=list, max_length=24)
    include_audio: bool = False
    fallback_image: str | None = Field(default=None, max_length=4096)
    workspace_snapshot_v2: dict[str, Any] | None = None
    workspace_snapshot_v3: dict[str, Any] | None = None
    model_config_id: int | None = Field(default=None, gt=0)

    @field_validator("client_request_id")
    @classmethod
    def _normalize_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not 8 <= len(normalized) <= 128:
            raise ValueError("client_request_id 去除首尾空白后长度必须为 8-128")
        return normalized

    @field_validator("custom_instruction")
    @classmethod
    def _normalize_custom_instruction(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("custom_keyframes")
    @classmethod
    def _normalize_keyframes(cls, value: list[float]) -> list[float]:
        return sorted({round(float(item), 3) for item in value if float(item) >= 0})

    @field_validator("workspace_snapshot_v2", "workspace_snapshot_v3")
    @classmethod
    def _workspace_snapshot_size(cls, value: dict[str, Any] | None):
        if value is not None:
            _validate_workspace_snapshot(value)
            _validate_json_payload_size(value, 64 * 1024, "工作区快照")
        return value

    @model_validator(mode="after")
    def _normalize_reverse_request(self):
        sources = list(self.sources or [])
        if not sources:
            if not self.asset_url:
                raise ValueError("asset_url 与 sources 至少提供一个")
            asset_path = self.asset_url.split("?", 1)[0].lower()
            inferred_source_type = (
                self.source_type
                or ("video" if asset_path.endswith((".mp4", ".webm", ".mov", ".m3u8")) else "image")
            )
            sources = [ReverseSourceReference(
                asset_url=self.asset_url,
                source_type=inferred_source_type,
                role="primary",
            )]
        primary = [item for item in sources if item.role == "primary"]
        if not primary:
            sources[0] = sources[0].model_copy(update={"role": "primary"})
            primary = [sources[0]]
        if len(primary) != 1:
            raise ValueError("反推请求必须且只能有一个主参考素材")
        self.sources = sources
        self.asset_url = primary[0].asset_url
        self.source_type = self.source_type or primary[0].source_type

        precision = self.analysis_precision or self.video_analysis_preset or "standard"
        if (
            self.analysis_precision is not None
            and self.video_analysis_preset is not None
            and self.analysis_precision != self.video_analysis_preset
        ):
            raise ValueError("analysis_precision 与 video_analysis_preset 不一致")
        self.analysis_precision = precision
        self.video_analysis_preset = precision

        video_request = self.target == "video" and self.source_type == "video"
        if (
            self.source_range
            or self.source_ranges
            or self.custom_keyframes
            or self.include_audio
        ) and not video_request:
            raise ValueError("视频区间、关键帧和音频分析仅适用于视频素材反推")

        ranges = list(self.source_ranges or [])
        if self.source_range is not None:
            if ranges and not (
                len(ranges) == 1
                and ranges[0].start_seconds == self.source_range.start_seconds
                and ranges[0].end_seconds == self.source_range.end_seconds
            ):
                raise ValueError("source_range 与 source_ranges 不能表示不同的分析范围")
            if not ranges:
                ranges = [self.source_range]
        ranges.sort(key=lambda item: (item.start_seconds, item.end_seconds))
        merged_ranges: list[ReverseSourceRange] = []
        for item in ranges:
            if merged_ranges and item.start_seconds <= merged_ranges[-1].end_seconds:
                previous = merged_ranges[-1]
                merged_ranges[-1] = ReverseSourceRange(
                    start_seconds=previous.start_seconds,
                    end_seconds=max(previous.end_seconds, item.end_seconds),
                )
            else:
                merged_ranges.append(item)
        ranges = merged_ranges
        total_selected_duration = sum(
            item.end_seconds - item.start_seconds
            for item in ranges
        )
        if total_selected_duration > MAX_REVERSE_SELECTED_DURATION_SECONDS + 1e-6:
            raise ValueError(
                f"视频片段累计分析时长不能超过 "
                f"{MAX_REVERSE_SELECTED_DURATION_SECONDS} 秒"
            )
        self.source_ranges = ranges
        # Legacy readers keep receiving the old shape only when it is exact.
        self.source_range = ranges[0] if len(ranges) == 1 else None

        if ranges:
            for timestamp in self.custom_keyframes:
                if not any(
                    item.start_seconds <= timestamp <= item.end_seconds
                    for item in ranges
                ):
                    raise ValueError("自定义关键帧必须位于某个已选分析片段内")
        for source in self.sources:
            if source.role != "primary" and source.source_type != "image":
                raise ValueError("辅助参考当前仅支持图片素材")
        image_only_focus = {
            "replica", "style", "portrait", "composition_lighting", "poster_layout"
        }
        video_only_focus = {
            "camera_motion", "subject_action", "storyboard", "editing_rhythm", "audio_script"
        }
        if self.target == "video" and self.analysis_focus in image_only_focus:
            raise ValueError("当前分析目标不适用于视频反推")
        if self.target != "video" and self.analysis_focus in video_only_focus:
            raise ValueError("当前分析目标不适用于图片反推")
        if self.include_audio and self.analysis_focus == "audio_script":
            self.output_purpose = "analysis_report"
        return self


class ReverseBatchItemCreate(BaseModel):
    asset_url: str = Field(min_length=1, max_length=4096)
    source_type: Literal["image", "video"] = "image"
    sources: list[ReverseSourceReference] = Field(default_factory=list, max_length=12)
    fallback_image: str | None = Field(default=None, max_length=4096)
    workspace_snapshot_v3: dict[str, Any] | None = None
    target: ReverseTarget | None = None
    analysis_precision: ReverseAnalysisPrecision | None = None
    source_ranges: list[ReverseSourceRange] = Field(
        default_factory=list, max_length=MAX_REVERSE_SOURCE_RANGES,
    )
    custom_keyframes: list[float] = Field(default_factory=list, max_length=24)
    audio_policy: Literal["inherit", "exclude", "analyze"] = "inherit"

    @field_validator("custom_keyframes")
    @classmethod
    def _normalize_keyframes(cls, value: list[float]) -> list[float]:
        return sorted({round(float(item), 3) for item in value if float(item) >= 0})

    @field_validator("workspace_snapshot_v3")
    @classmethod
    def _workspace_snapshot_size(cls, value: dict[str, Any] | None):
        if value is not None:
            _validate_workspace_snapshot(value)
            _validate_json_payload_size(value, 64 * 1024, "工作区快照")
        return value

    @model_validator(mode="after")
    def _normalize_sources(self):
        sources = list(self.sources or [])
        if not sources:
            self.sources = [ReverseSourceReference(
                asset_url=self.asset_url,
                source_type=self.source_type,
                role="primary",
            )]
            return self

        primary = [source for source in sources if source.role == "primary"]
        if len(primary) != 1:
            raise ValueError("批量反推单项必须且只能有一个主参考素材")
        if primary[0].asset_url != self.asset_url:
            raise ValueError("批量反推单项的主参考地址必须与 asset_url 一致")
        if primary[0].source_type != self.source_type:
            raise ValueError("批量反推单项的主参考类型必须与 source_type 一致")
        if any(
            source.role != "primary" and source.source_type != "image"
            for source in sources
        ):
            raise ValueError("批量反推的辅助参考当前仅支持图片素材")
        self.sources = sources
        return self


class ReverseBatchCreate(BaseModel):
    quote_id: int | None = Field(default=None, gt=0)
    client_request_id: str = Field(min_length=8, max_length=128)
    project_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, max_length=128)
    target: ReverseTarget = "image"
    analysis_focus: ReverseAnalysisFocus = "comprehensive"
    analysis_precision: ReverseAnalysisPrecision = "standard"
    output_purpose: ReverseOutputPurpose = "generation"
    custom_instruction: str | None = Field(default=None, max_length=500)
    include_audio: bool = False
    model_config_id: int | None = Field(default=None, gt=0)
    items: list[ReverseBatchItemCreate] = Field(min_length=1, max_length=20)

    @field_validator("client_request_id")
    @classmethod
    def _normalize_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not 8 <= len(normalized) <= 128:
            raise ValueError("client_request_id 去除首尾空白后长度必须为 8-128")
        return normalized

    @field_validator("name", "custom_instruction")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_operation_contracts(self):
        common = {
            "target": self.target,
            "analysis_focus": self.analysis_focus,
            "analysis_precision": self.analysis_precision,
            "output_purpose": self.output_purpose,
            "custom_instruction": self.custom_instruction,
            "include_audio": self.include_audio,
            "model_config_id": self.model_config_id,
        }
        for index, item in enumerate(self.items):
            try:
                item_payload = item.model_dump(mode="json", exclude_none=True)
                item_target = item_payload.pop("target", None) or self.target
                item_precision = item_payload.pop("analysis_precision", None) or self.analysis_precision
                audio_policy = item_payload.pop("audio_policy", "inherit")
                ReverseOperationCreate.model_validate({
                    **common,
                    "client_request_id": f"batch-item-{index:02d}",
                    **item_payload,
                    "target": item_target,
                    "analysis_precision": item_precision,
                    "include_audio": (
                        self.include_audio if audio_policy == "inherit"
                        else audio_policy == "analyze"
                    ),
                })
            except ValueError as exc:
                raise ValueError(f"批次第 {index + 1} 项配置无效: {exc}") from exc
        return self


class ReverseShotTimelineEditIn(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    action: Literal["split", "merge", "reorder", "lock", "boundary"]
    shot_id: str | None = Field(default=None, min_length=1, max_length=128)
    shot_ids: list[str] = Field(default_factory=list, max_length=128)
    split_seconds: float | None = Field(default=None, ge=0, le=86_400)
    boundary_seconds: float | None = Field(default=None, ge=0, le=86_400)
    ordered_shot_ids: list[str] = Field(default_factory=list, max_length=128)
    locked: bool | None = None

    @model_validator(mode="after")
    def _validate_action(self):
        if self.action == "split" and (not self.shot_id or self.split_seconds is None):
            raise ValueError("split 需要 shot_id 和 split_seconds")
        if self.action == "merge" and len(self.shot_ids) < 2:
            raise ValueError("merge 至少需要两个 shot_ids")
        if self.action == "reorder" and not self.ordered_shot_ids:
            raise ValueError("reorder 需要完整 ordered_shot_ids")
        if self.action == "lock" and (not self.shot_id or self.locked is None):
            raise ValueError("lock 需要 shot_id 和 locked")
        if self.action == "boundary" and (
            len(self.shot_ids) != 2 or self.boundary_seconds is None
        ):
            raise ValueError("boundary 需要两个相邻 shot_ids 和 boundary_seconds")
        return self


class ReverseShotReanalyzeIn(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    shot_id: str = Field(min_length=1, max_length=128)
    analysis_precision: ReverseAnalysisPrecision | None = None
    include_audio: bool | None = None


class ReverseShotGenerationPrepareIn(BaseModel):
    shot_id: str = Field(min_length=1, max_length=128)
    revision_id: int | None = Field(default=None, gt=0)
    client_request_id: str = Field(min_length=8, max_length=128)
    model_config_id: int | None = Field(default=None, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)


class ReverseShotGenerationPrepareOut(BaseModel):
    reverse_operation_id: int
    source_revision_id: int
    shot_id: str
    quote_endpoint: str = "/api/quotes"
    generation_endpoint: str = "/api/generate"
    request: dict[str, Any]


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
    quote_id: int | None = None
    model_config_id: int | None = None
    model_name: str | None = None
    model_id: str | None = None
    target: ReverseTarget
    source_type: Literal["image", "video"] | None = None
    analysis_focus: ReverseAnalysisFocus = "comprehensive"
    analysis_precision: ReverseAnalysisPrecision = "standard"
    output_purpose: ReverseOutputPurpose = "generation"
    include_audio: bool = False
    source_range: dict[str, Any] | None = None
    source_ranges: list[dict[str, Any]] = Field(default_factory=list)
    status: ReverseOperationStatus
    phase: str | None = None
    progress: int = Field(default=0, ge=0, le=100)
    result: dict[str, Any] | None = None
    video_analysis: dict[str, Any] | None = None
    request_context: dict[str, Any] | None = None
    workspace_snapshot_v2: dict[str, Any] | None = None
    workspace_snapshot_v3: dict[str, Any] | None = None
    result_schema_version: str = "reverse.v2"
    applied_result_version: int | None = None
    retry_of_operation_id: int | None = None
    reference_count: int = Field(default=1, ge=0)
    charged_credits: int = Field(default=0, ge=0)
    cost_frozen: int = 0
    cost_settled: int = 0
    confirmation_expires_at: UtcDateTime | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    error: str | None = None
    expired: bool = False
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


class ReverseBatchStatusCounts(BaseModel):
    queued: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    needs_confirmation: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    canceled: int = Field(default=0, ge=0)


class ReverseBatchItemOut(BaseModel):
    id: int
    index: int = Field(ge=0)
    operation_id: int
    operation: ReverseOperationOut


class ReverseBatchOut(BaseModel):
    id: int
    quote_id: int | None = None
    client_request_id: str
    name: str | None = None
    target: ReverseTarget
    shared_config_snapshot: dict[str, Any]
    capabilities: dict[str, Any] = Field(default_factory=dict)
    status: ReverseBatchStatus
    status_counts: ReverseBatchStatusCounts
    total_count: int = Field(ge=1, le=20)
    cancel_requested: bool = False
    items: list[ReverseBatchItemOut] = Field(default_factory=list)
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None


class ReverseOperationRetryIn(BaseModel):
    quote_id: int = Field(gt=0)
    client_request_id: str = Field(min_length=8, max_length=128)
    model_config_id: int | None = Field(default=None, gt=0)


class ReverseResultRevisionIn(BaseModel):
    source: Literal["user_edit", "applied"]
    payload: dict[str, Any]
    parent_revision_id: int | None = Field(default=None, gt=0)
    clear_image_evidence: bool = False

    @field_validator("payload")
    @classmethod
    def _payload_size(cls, value: dict[str, Any]):
        _validate_json_payload_size(value, 128 * 1024, "反推结果版本")
        return value


class ReverseResultRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    operation_id: int
    version: int
    source: Literal[
        "provider_raw",
        "normalized",
        "user_edit",
        "applied",
        "model_compiled",
        "generation",
    ]
    payload: dict[str, Any]
    parent_revision_id: int | None = None
    source_content_hash: str | None = None
    source_fingerprints: list[dict[str, Any]] | None = None
    payload_hash: str | None = None
    lineage_status: Literal["verified", "legacy_unverified"] = "legacy_unverified"
    evidence_review_action: Literal[
        "not_applicable", "inherited", "updated", "cleared"
    ] | None = None
    created_at: UtcDateTime | None = None


class ReverseResultApplyIn(BaseModel):
    payload: dict[str, Any]
    parent_revision_id: int = Field(gt=0)
    clear_image_evidence: bool = False

    @field_validator("payload")
    @classmethod
    def _payload_size(cls, value: dict[str, Any]):
        _validate_json_payload_size(value, 128 * 1024, "反推结果应用")
        return value


class ReverseResultApplyOut(BaseModel):
    user_edit: ReverseResultRevisionOut
    applied: ReverseResultRevisionOut


class ReverseOperationFeedbackIn(BaseModel):
    rating: Literal["useful", "not_useful"]
    issue_types: list[Literal[
        "subject_error",
        "style_error",
        "action_missing",
        "shot_missing",
        "camera_error",
        "text_error",
        "audio_error",
        "hallucination",
        "other",
    ]] = Field(default_factory=list, max_length=8)
    note: str | None = Field(default=None, max_length=500)


class ReverseOperationFeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_id: int
    rating: Literal["useful", "not_useful"]
    issue_types: list[str] = Field(default_factory=list)
    note: str | None = None
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None


class ReverseOut(BaseModel):
    structured: dict[str, Any]
    final_text: str
    # The deprecated synchronous endpoint preserves its original dictionary
    # shape; the async operation result carries the extended evidence contract.
    image_evidence: list[dict[str, Any]] = Field(default_factory=list)
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
    direction: PromptOptimizationDirection = "faithful"
    target_language: Literal["zh-CN", "en"] = "en"

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
    source_prompt: str
    compiled_prompt: str | None = None
    direction: PromptOptimizationDirection = "faithful"
    target_language: Literal["zh-CN", "en"] | None = None
    optimization_kind: Literal["rewrite", "model_compile"] = "rewrite"
    change_summary: list[str] = Field(default_factory=list)
    preserved_requirements: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    estimated_credits: int = 0
    charged_credits: int = 0
    optimizer_model_id: str | None = None
    compiler_metadata: dict[str, Any] | None = None
    context_metadata: dict[str, Any] | None = None
    optimizer_model_config_id: int | None = None
    optimizer_model_name: str | None = None


# --- Generate ---
class GenerationShotContext(BaseModel):
    """Internal storyboard lineage that must never be sent as provider params."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    shot_id: str = Field(min_length=1, max_length=128)
    source_segment_index: int = Field(default=1, ge=1, le=MAX_REVERSE_SOURCE_RANGES)
    source_range: ReverseSourceRange


class GenerationRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # optional: pure text-to-image needs no reference; only set when generating
    # from a scraped reference asset (same-style / first-frame).
    client_request_id: str | None = Field(default=None, min_length=8, max_length=128)
    project_id: int | None = Field(default=None, gt=0)
    creation_recipe_id: int | None = Field(default=None, gt=0)
    creation_recipe_version: int | None = Field(default=None, gt=0)
    creation_recipe_share_slug: str | None = Field(
        default=None,
        min_length=20,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    reverse_operation_id: int | None = Field(default=None, gt=0)
    reverse_revision_id: int | None = Field(default=None, gt=0)
    reproduction_remediation_id: int | None = Field(default=None, gt=0)
    reproduction_plan_item_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
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
    shot_context: GenerationShotContext | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    model_config_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_reverse_lineage(self):
        if (self.reverse_operation_id is None) != (self.reverse_revision_id is None):
            raise ValueError("reverse_operation_id 和 reverse_revision_id 必须同时提供")
        if (self.reproduction_remediation_id is None) != (
            self.reproduction_plan_item_id is None
        ):
            raise ValueError(
                "reproduction_remediation_id 和 reproduction_plan_item_id 必须同时提供"
            )
        if (self.creation_recipe_id is None) != (self.creation_recipe_version is None):
            raise ValueError("creation_recipe_id 和 creation_recipe_version 必须同时提供")
        if self.creation_recipe_share_slug is not None and self.creation_recipe_id is None:
            raise ValueError("creation_recipe_share_slug 必须与创作配方一起提供")
        if self.shot_context is not None and self.category != "video":
            raise ValueError("shot_context 仅支持视频生成")
        return self


class GenerationQuoteIn(GenerationRequestBase):
    """Generation intent accepted by the quote endpoint before a quote exists."""


class ExecutionQuoteIn(BaseModel):
    """Unified quote envelope for non-legacy generation and executable tools."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "generation",
        "reverse",
        "reverse_batch",
        "workflow",
        "prompt_optimization",
        "asset_unlock",
    ]
    client_request_id: str = Field(min_length=8, max_length=128)
    request: dict[str, Any]

    @field_validator("client_request_id")
    @classmethod
    def _normalize_quote_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not 8 <= len(normalized) <= 128:
            raise ValueError("client_request_id 去除首尾空白后长度必须为 8-128")
        return normalized

    @field_validator("request")
    @classmethod
    def _bounded_quote_request(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_payload_size(value, 256 * 1024, "报价请求")
        return value


class GenerateIn(GenerationRequestBase):
    """Executable generation request bound to one server-authoritative quote."""

    quote_id: int = Field(gt=0)


class RetryRequoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=8, max_length=128)
    model_config_id: int | None = Field(default=None, gt=0)


class GenerationQuoteOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: int
    quote_id: int
    kind: Literal[
        "generation",
        "reverse",
        "reverse_batch",
        "workflow",
        "prompt_optimization",
        "asset_unlock",
    ] = "generation"
    status: Literal["active", "consumed", "expired", "canceled"]
    category: Literal["image", "video", "workflow"]
    stage: Literal["preview", "final"]
    model_config_id: int | None = None
    model_name: str | None = None
    model_id: str | None = None
    model_provider: str | None = None
    route_id: int | None = None
    route_key: str | None = None
    route_name: str | None = None
    route_selection_state: Literal["closed", "half_open"] | None = None
    route_health_at_quote: dict[str, Any] | None = None
    creation_recipe_id: int | None = None
    creation_recipe_version: int | None = None
    creation_recipe_source: Literal["owner", "public", "share"] | None = None
    creation_recipe_share_id: int | None = None
    capability_version_id: int | None = None
    capability_version: int | None = None
    price_version_id: int | None = None
    price_version: int | None = None
    estimated_credits: int = Field(ge=0)
    total_credits: int = Field(ge=0)
    price_breakdown: dict[str, Any] = Field(default_factory=dict)
    balance_after_estimate: int | None = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    subject_snapshot: dict[str, Any] = Field(default_factory=dict)
    client_request_id: str | None = None
    tool_version_id: int | None = None
    request_fingerprint: str
    expires_at: UtcDateTime
    consumed_at: UtcDateTime | None = None
    task_id: int | None = None
    consumed_ref_type: str | None = None
    consumed_ref_id: int | None = None
    created_at: UtcDateTime | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    category: str
    stage: str
    parent_task_id: int | None = None
    retry_of_task_id: int | None = None
    status: str
    model_use: str | None = None
    model_config_id: int | None = None
    quote_id: int | None = None
    creation_recipe_id: int | None = None
    creation_recipe_version: int | None = None
    creation_recipe_source: Literal["owner", "public", "share"] | None = None
    creation_recipe_share_id: int | None = None
    reverse_operation_id: int | None = None
    source_revision_id: int | None = None
    compiled_revision_id: int | None = None
    generation_revision_id: int | None = None
    dispatch_attempt: int | None = None
    dispatch_status: str | None = None
    dispatch_task_id: str | None = None
    dispatch_publish_attempts: int = 0
    dispatch_reconciliation_required: bool = False
    capability_version_id: int | None = None
    price_version_id: int | None = None
    quote_estimated_credits: int | None = None
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
    prompt_optimization_direction: str | None = None
    prompt_optimization_kind: str | None = None
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


UnifiedTaskKind = Literal["generation", "reverse", "parse", "workflow"]
UnifiedTaskStatusGroup = Literal[
    "active",
    "succeeded",
    "failed",
    "canceled",
    "needs_attention",
]


class UnifiedWorkflowNodeOut(BaseModel):
    key: str
    type: str
    status: str
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    compensation_status: str = "none"


class UnifiedTaskFailureSuggestionOut(BaseModel):
    error_type: str
    title: str
    message: str
    recommended_action: str


class UnifiedTaskRetryModelOut(BaseModel):
    model_config_id: int = Field(gt=0)
    display_name: str
    model_id: str
    provider: str
    estimated_credits: int = Field(default=0, ge=0)
    route_status: str
    reason: str


class UnifiedTaskItemOut(BaseModel):
    key: str
    kind: UnifiedTaskKind
    id: int
    status: str
    status_group: UnifiedTaskStatusGroup
    category: str | None = None
    stage: str | None = None
    retry_of_task_id: int | None = None
    phase: str | None = None
    progress: int = Field(default=0, ge=0, le=100)
    title: str
    summary: str | None = None
    model_config_id: int | None = None
    model_name: str | None = None
    model_id: str | None = None
    model_provider: str | None = None
    dispatch_attempt: int | None = None
    dispatch_status: str | None = None
    dispatch_task_id: str | None = None
    dispatch_publish_attempts: int = 0
    dispatch_reconciliation_required: bool = False
    cost_frozen: int = Field(default=0, ge=0)
    cost_settled: int = Field(default=0, ge=0)
    project_ids: list[int] = Field(default_factory=list)
    result_refs: list[str] = Field(default_factory=list)
    available_actions: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    failure_suggestion: UnifiedTaskFailureSuggestionOut | None = None
    compatible_retry_models: list[UnifiedTaskRetryModelOut] = Field(default_factory=list)
    workflow_tool_slug: str | None = None
    workflow_entry_path: str | None = None
    workflow_current_node_key: str | None = None
    workflow_failed_node_key: str | None = None
    workflow_nodes: list[UnifiedWorkflowNodeOut] = Field(default_factory=list)
    created_at: UtcDateTime
    updated_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None


class UnifiedTaskPageOut(BaseModel):
    items: list[UnifiedTaskItemOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    total: int = Field(default=0, ge=0)
    counts: dict[str, int] = Field(default_factory=dict)


class AssetOut(BaseModel):
    id: int
    task_id: int | None = None
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


class AssetUnlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: int = Field(gt=0)


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
    folder_id: int | None = None
    folder_name: str | None = None
    download_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    content_sha256: str | None = None
    perceptual_hash: str | None = None
    analysis_status: Literal["pending", "ready", "degraded"] = "pending"
    analysis_error: str | None = None


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


# --- Versioned creation recipes ---
class CreationRecipePayloadV1(BaseModel):
    """Stable, replayable Recipe envelope.

    The nested Studio snapshots remain extensible, but the persisted envelope is
    deliberately strict so arbitrary JSON cannot masquerade as a replayable
    Recipe.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["creation-recipe.v1"]
    prompt: str = Field(default="", max_length=100_000)
    negative: str = Field(default="", max_length=50_000)
    structured: dict[str, Any] = Field(default_factory=dict)
    analysis_focus: str | None = Field(default=None, max_length=64)
    generation_params: dict[str, Any] | None = None
    reverse_snapshot_v3: dict[str, Any] | None = None
    reverse_result: dict[str, Any] | None = None
    generation: dict[str, Any] | None = None
    derived_from_recipe_id: int | None = Field(default=None, gt=0)
    derived_from_recipe_version: int | None = Field(default=None, gt=0)
    public_asset_access: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _replayable_content(self):
        snapshot_text = ""
        if self.reverse_snapshot_v3:
            version = self.reverse_snapshot_v3.get("version")
            if version != 3:
                raise ValueError("reverse_snapshot_v3.version 必须为 3")
            target = self.reverse_snapshot_v3.get("target")
            if target is not None and target not in {"image", "video"}:
                raise ValueError("reverse_snapshot_v3.target 非法")
            snapshot_text = str(self.reverse_snapshot_v3.get("final_text") or "").strip()
        if not self.prompt.strip() and not self.structured and not snapshot_text:
            raise ValueError("创作配方必须包含提示词、结构化内容或可恢复的反推结果")
        if (self.derived_from_recipe_id is None) != (self.derived_from_recipe_version is None):
            raise ValueError("派生来源配方和版本必须同时提供")
        return self


def validate_creation_recipe_payload(
    value: dict[str, Any],
    *,
    category: Literal["image", "video"] | None = None,
) -> dict[str, Any]:
    parsed = CreationRecipePayloadV1.model_validate(value)
    snapshot = parsed.reverse_snapshot_v3 or {}
    target = snapshot.get("target")
    if category is not None and target is not None and target != category:
        raise ValueError("配方类型与工作区目标不一致")
    _validate_workspace_snapshot(value)
    _validate_json_payload_size(value, 192 * 1024, "创作配方")
    return value


class CreationRecipeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=128)
    category: Literal["image", "video"]
    visibility: Literal["private", "public"] = "private"
    favorite: bool = False
    source_operation_id: int | None = Field(default=None, gt=0)
    cover_asset_url: str | None = Field(default=None, max_length=4096)
    payload: dict[str, Any]

    @field_validator("title")
    @classmethod
    def _recipe_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("配方标题不能为空")
        return normalized

    @field_validator("payload")
    @classmethod
    def _recipe_payload(cls, value: dict[str, Any]):
        return validate_creation_recipe_payload(value)

    @model_validator(mode="after")
    def _category_matches_payload(self):
        validate_creation_recipe_payload(self.payload, category=self.category)
        return self


class CreationRecipeVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def _recipe_version_payload(cls, value: dict[str, Any]):
        return validate_creation_recipe_payload(value)


class CreationRecipeVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recipe_id: int
    version: int
    schema_version: str
    payload: dict[str, Any]
    metadata_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDateTime | None = None


class CreationRecipeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_operation_id: int | None = None
    title: str
    category: Literal["image", "video"]
    visibility: Literal["private", "public"]
    moderation_status: Literal["draft", "pending", "approved", "rejected"]
    favorite: bool
    current_version: int
    approved_version: int | None = None
    cover_asset_url: str | None = None
    submitted_at: UtcDateTime | None = None
    reviewed_at: UtcDateTime | None = None
    review_note: str | None = None
    version: CreationRecipeVersionOut | None = None
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None


class CreationRecipeShareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _future_expiration(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        normalized = normalized.astimezone(timezone.utc)
        if normalized <= datetime.now(timezone.utc):
            raise ValueError("分享过期时间必须晚于当前时间")
        return normalized


class CreationRecipeShareOut(BaseModel):
    id: int
    recipe_id: int
    version: int
    slug: str
    status: Literal["active", "revoked"]
    expires_at: UtcDateTime | None = None
    revoked_at: UtcDateTime | None = None
    share_url: str
    created_at: UtcDateTime | None = None


class CreationRecipeSharedOut(BaseModel):
    share: CreationRecipeShareOut
    recipe: CreationRecipeOut


class CreationRecipeUsageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["apply", "generation_prepare", "generation_submit"]
    version: int | None = Field(default=None, gt=0)
    share_slug: str | None = Field(default=None, min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    generation_task_id: int | None = Field(default=None, gt=0)
    client_event_id: str | None = Field(default=None, min_length=8, max_length=128)
    context: dict[str, Any] | None = None

    @field_validator("client_event_id")
    @classmethod
    def _client_event_id(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("context")
    @classmethod
    def _usage_context(cls, value: dict[str, Any] | None):
        if value is not None:
            _validate_workspace_snapshot(value)
            _validate_json_payload_size(value, 16 * 1024, "配方使用上下文")
        return value

    @model_validator(mode="after")
    def _generation_link(self):
        if self.event_type == "generation_submit" and self.generation_task_id is None:
            raise ValueError("generation_submit 必须关联生成任务")
        if self.event_type != "generation_submit" and self.generation_task_id is not None:
            raise ValueError("只有 generation_submit 可以关联生成任务")
        return self


class CreationRecipeUsageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recipe_id: int | None = None
    recipe_version: int
    user_id: int | None = None
    event_type: Literal["apply", "clone", "generation_prepare", "generation_submit"]
    source: Literal["owner", "public", "share"]
    share_id: int | None = None
    derived_recipe_id: int | None = None
    generation_task_id: int | None = None
    client_event_id: str | None = None
    context: dict[str, Any] | None = None
    created_at: UtcDateTime | None = None


class CreationRecipeUsageSummaryOut(BaseModel):
    recipe_id: int
    total: int
    unique_users: int
    by_event: dict[str, int]
    last_used_at: UtcDateTime | None = None


class AdminCreationRecipeReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _review_note(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def _rejection_reason(self):
        if self.action == "reject" and not self.note:
            raise ValueError("驳回审核必须填写原因")
        return self


# --- Projects and unified asset folders ---
def _normalize_asset_tags(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(str(raw or "").split()).strip("# ")
        if not value or len(value) > 32 or any(ord(char) < 32 for char in value):
            raise ValueError("素材标签应为 1-32 个可见字符")
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


class AssetRefsIn(BaseModel):
    asset_refs: list[str] = Field(min_length=1, max_length=100)

    @field_validator("asset_refs")
    @classmethod
    def _asset_refs(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value or "").strip() for value in values))
        if not normalized or any(not value or len(value) > 512 for value in normalized):
            raise ValueError("asset_refs 非法")
        return normalized


class AssetFolderCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = Field(default=None, gt=0)
    sort_order: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("文件夹名称不能为空")
        return normalized


class AssetFolderPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    parent_id: int | None = Field(default=None, gt=0)
    move_to_root: bool = False
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("文件夹名称不能为空")
        return normalized

    @model_validator(mode="after")
    def _has_change(self):
        if self.name is None and self.parent_id is None and not self.move_to_root and self.sort_order is None:
            raise ValueError("至少提供一个文件夹修改项")
        if self.parent_id is not None and self.move_to_root:
            raise ValueError("parent_id 与 move_to_root 不能同时提供")
        return self


class AssetFolderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_ref: str
    created_at: UtcDateTime | None = None


class AssetFolderOut(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    sort_order: int = 0
    item_count: int = 0
    items: list[AssetFolderItemOut] = Field(default_factory=list)
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None


class MediaProjectCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    project_type: Literal["image", "video", "mixed"] = "mixed"
    cover_asset_ref: str | None = Field(default=None, max_length=512)
    auto_archive_after_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("项目名称不能为空")
        return normalized

    @field_validator("description", "cover_asset_ref")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class MediaProjectPatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    project_type: Literal["image", "video", "mixed"] | None = None
    status: Literal["active", "archived"] | None = None
    cover_asset_ref: str | None = Field(default=None, max_length=512)
    auto_archive_after_days: int | None = Field(default=None, ge=1, le=3650)
    clear_cover: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("项目名称不能为空")
        return normalized

    @field_validator("description", "cover_asset_ref")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _has_change(self):
        if (
            self.title is None
            and self.description is None
            and self.project_type is None
            and self.status is None
            and self.cover_asset_ref is None
            and "auto_archive_after_days" not in self.model_fields_set
            and not self.clear_cover
        ):
            raise ValueError("至少提供一个项目修改项")
        if self.cover_asset_ref is not None and self.clear_cover:
            raise ValueError("cover_asset_ref 与 clear_cover 不能同时提供")
        return self


class MediaProjectAssetsIn(AssetRefsIn):
    role: str = Field(default="source", min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if not normalized or not normalized.replace("_", "").isalnum():
            raise ValueError("素材角色非法")
        return normalized


class UserAssetTagsIn(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        return _normalize_asset_tags(values)


class UserAssetMetadataOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_ref: str
    media_type: Literal["image", "video"]
    tags: list[str] = Field(default_factory=list)
    content_sha256: str | None = None
    perceptual_hash: str | None = None
    perceptual_hash_algorithm: str | None = None
    analysis_status: Literal["pending", "ready", "degraded"] = "pending"
    analysis_error: str | None = None
    analyzed_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None


class AssetSimilarityMatchOut(BaseModel):
    asset_ref: str
    match_type: Literal["exact", "similar"]
    hamming_distance: int | None = Field(default=None, ge=0, le=64)
    metadata: UserAssetMetadataOut
    asset: UserAssetItem | None = None


class AssetSimilarityOut(BaseModel):
    status: Literal["ready", "degraded"]
    query_asset_ref: str
    exact_available: bool
    perceptual_available: bool
    message: str | None = None
    query: UserAssetMetadataOut
    matches: list[AssetSimilarityMatchOut] = Field(default_factory=list)
    degraded_asset_refs: list[str] = Field(default_factory=list)


class MediaProjectRecipeLinksIn(BaseModel):
    recipe_ids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("recipe_ids")
    @classmethod
    def _recipe_ids(cls, values: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(int(value) for value in values))
        if any(value <= 0 for value in normalized):
            raise ValueError("recipe_ids 非法")
        return normalized


class MediaProjectTaskLinksIn(BaseModel):
    task_kind: Literal["generation", "reverse", "parse", "workflow"]
    task_ids: list[int] = Field(min_length=1, max_length=100)

    @field_validator("task_ids")
    @classmethod
    def _task_ids(cls, values: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(int(value) for value in values))
        if any(value <= 0 for value in normalized):
            raise ValueError("task_ids 非法")
        return normalized


class MediaProjectAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset_ref: str
    role: str
    sort_order: int = 0
    note: str | None = None
    asset: UserAssetItem | None = None
    metadata: UserAssetMetadataOut | None = None
    duplicate_count: int = Field(default=0, ge=0)
    similar_count: int = Field(default=0, ge=0)
    created_at: UtcDateTime | None = None


class MediaProjectRecipeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recipe_id: int
    title: str | None = None
    category: Literal["image", "video"] | None = None
    visibility: Literal["private", "public"] | None = None
    favorite: bool = False
    current_version: int | None = None
    cover_asset_url: str | None = None
    schema_version: str | None = None
    payload: dict[str, Any] | None = None
    created_at: UtcDateTime | None = None


class MediaProjectTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_kind: Literal["generation", "reverse", "parse", "workflow"]
    task_id: int
    status: str | None = None
    category: str | None = None
    stage: str | None = None
    phase: str | None = None
    progress: int = Field(default=0, ge=0, le=100)
    title: str | None = None
    summary: str | None = None
    result_refs: list[str] = Field(default_factory=list)
    cost_frozen: int = Field(default=0, ge=0)
    cost_settled: int = Field(default=0, ge=0)
    error: str | None = None
    workflow_tool_slug: str | None = None
    workflow_entry_path: str | None = None
    workflow_current_node_key: str | None = None
    workflow_nodes: list[UnifiedWorkflowNodeOut] = Field(default_factory=list)
    finished_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None


class MediaProjectOut(BaseModel):
    id: int
    title: str
    description: str | None = None
    project_type: Literal["image", "video", "mixed"]
    status: Literal["active", "archived"]
    cover_asset_ref: str | None = None
    auto_archive_after_days: int | None = Field(default=None, ge=1, le=3650)
    asset_count: int = 0
    recipe_count: int = 0
    task_count: int = 0
    cost_frozen: int = Field(default=0, ge=0)
    cost_settled: int = Field(default=0, ge=0)
    assets: list[MediaProjectAssetOut] = Field(default_factory=list)
    recipes: list[MediaProjectRecipeOut] = Field(default_factory=list)
    tasks: list[MediaProjectTaskOut] = Field(default_factory=list)
    draft_key: str
    draft: dict[str, Any] = Field(default_factory=dict)
    draft_updated_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None


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
        if self.provider_config_id is not None and any((
            self.provider,
            self.base_url,
            self.api_key,
            self.gateway_format,
        )):
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


class ToolVersionPayloadIn(BaseModel):
    schema_version: str = Field(default="tool.v1", min_length=1, max_length=32)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    workflow: dict[str, Any] = Field(default_factory=dict)
    pricing_policy: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _normalize_schema_version(cls, value: str) -> str:
        return value.strip()

    @field_validator("input_schema", "workflow", "pricing_policy", "capabilities")
    @classmethod
    def _validate_tool_json(cls, value: dict[str, Any]):
        _reject_dangerous_json_keys(value, "tool_payload")
        _validate_json_payload_size(value, 64 * 1024, "工具版本配置")
        return value


class ToolDefinitionCreateIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    category: Literal["image", "video", "workflow", "utility"]
    renderer: str = Field(default="studio", min_length=1, max_length=64)
    entry_path: str = Field(min_length=1, max_length=512)
    icon: str | None = Field(default=None, max_length=64)
    sort_order: int = Field(default=0, ge=-100000, le=100000)
    enabled: bool = True
    featured: bool = False
    initial_version: ToolVersionPayloadIn = Field(default_factory=ToolVersionPayloadIn)

    @field_validator("slug", "name", "renderer", "entry_path", "description", "icon")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("entry_path")
    @classmethod
    def _validate_entry_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("工具入口必须是站内绝对路径")
        return value


class ToolDefinitionPatchIn(BaseModel):
    slug: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    category: Literal["image", "video", "workflow", "utility"] | None = None
    renderer: str | None = Field(default=None, min_length=1, max_length=64)
    entry_path: str | None = Field(default=None, min_length=1, max_length=512)
    icon: str | None = Field(default=None, max_length=64)
    sort_order: int | None = Field(default=None, ge=-100000, le=100000)
    enabled: bool | None = None
    featured: bool | None = None

    @field_validator("slug", "name", "renderer", "entry_path", "description", "icon")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str | None:
        return ToolDefinitionCreateIn._normalize_text(value)

    @field_validator("entry_path")
    @classmethod
    def _validate_entry_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ToolDefinitionCreateIn._validate_entry_path(value)

    @model_validator(mode="after")
    def _require_patch_field(self):
        if not self.model_fields_set:
            raise ValueError("至少提交一个待修改字段")
        return self


class SettingsIn(BaseModel):
    reverse_prompt_enabled: bool | None = None
    sms_auth_enabled: bool | None = None
    payment_enabled: bool | None = None
    navigation_states: dict[
        Literal["catalog", "prompts", "projects", "profile", "recharge", "history"],
        Literal["enabled", "disabled", "hidden"],
    ] | None = None
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
