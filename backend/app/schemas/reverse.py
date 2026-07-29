"""Auto-generated from schemas.py split."""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._base import (
    MAX_REVERSE_SELECTED_DURATION_SECONDS,
    MAX_REVERSE_SOURCE_RANGES,
    UtcDateTime,
    _reject_dangerous_json_keys,
    _validate_json_payload_size,
    _validate_workspace_snapshot,
)

# --- Reverse prompt ---
ReverseTarget = Literal["image", "video", "product_profile", "portrait_profile"]
ReverseAnalysisPrecision = Literal["fast", "standard", "fine", "ultra"]
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
        default=None,
        min_length=3,
        max_length=128,
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
    video_analysis_preset: ReverseAnalysisPrecision | None = None
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
    custom_keyframes: list[float] = Field(default_factory=list, max_length=36)
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
            inferred_source_type = self.source_type or (
                "video" if asset_path.endswith((".mp4", ".webm", ".mov", ".m3u8")) else "image"
            )
            sources = [
                ReverseSourceReference(
                    asset_url=self.asset_url,
                    source_type=inferred_source_type,
                    role="primary",
                )
            ]
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
            self.source_range or self.source_ranges or self.custom_keyframes or self.include_audio
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
        total_selected_duration = sum(item.end_seconds - item.start_seconds for item in ranges)
        if total_selected_duration > MAX_REVERSE_SELECTED_DURATION_SECONDS + 1e-6:
            raise ValueError(
                f"视频片段累计分析时长不能超过 " f"{MAX_REVERSE_SELECTED_DURATION_SECONDS} 秒"
            )
        self.source_ranges = ranges
        # Legacy readers keep receiving the old shape only when it is exact.
        self.source_range = ranges[0] if len(ranges) == 1 else None

        if ranges:
            for timestamp in self.custom_keyframes:
                if not any(item.start_seconds <= timestamp <= item.end_seconds for item in ranges):
                    raise ValueError("自定义关键帧必须位于某个已选分析片段内")
        for source in self.sources:
            if source.role != "primary" and source.source_type != "image":
                raise ValueError("辅助参考当前仅支持图片素材")
        image_only_focus = {"replica", "style", "portrait", "composition_lighting", "poster_layout"}
        video_only_focus = {
            "camera_motion",
            "subject_action",
            "storyboard",
            "editing_rhythm",
            "audio_script",
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
        default_factory=list,
        max_length=MAX_REVERSE_SOURCE_RANGES,
    )
    custom_keyframes: list[float] = Field(default_factory=list, max_length=36)
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
            self.sources = [
                ReverseSourceReference(
                    asset_url=self.asset_url,
                    source_type=self.source_type,
                    role="primary",
                )
            ]
            return self

        primary = [source for source in sources if source.role == "primary"]
        if len(primary) != 1:
            raise ValueError("批量反推单项必须且只能有一个主参考素材")
        if primary[0].asset_url != self.asset_url:
            raise ValueError("批量反推单项的主参考地址必须与 asset_url 一致")
        if primary[0].source_type != self.source_type:
            raise ValueError("批量反推单项的主参考类型必须与 source_type 一致")
        if any(source.role != "primary" and source.source_type != "image" for source in sources):
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
                item_precision = (
                    item_payload.pop("analysis_precision", None) or self.analysis_precision
                )
                audio_policy = item_payload.pop("audio_policy", "inherit")
                ReverseOperationCreate.model_validate(
                    {
                        **common,
                        "client_request_id": f"batch-item-{index:02d}",
                        **item_payload,
                        "target": item_target,
                        "analysis_precision": item_precision,
                        "include_audio": (
                            self.include_audio
                            if audio_policy == "inherit"
                            else audio_policy == "analyze"
                        ),
                    }
                )
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
        if self.action == "boundary" and (len(self.shot_ids) != 2 or self.boundary_seconds is None):
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
    # 批级费用（各子项 ReverseOperation 求和，serialize_batch 下发）。
    # 缺少这两个字段时 pydantic 会把 serialize_batch 的输出静默剥离。
    cost_frozen: int = 0
    cost_settled: int = 0
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
    evidence_review_action: Literal["not_applicable", "inherited", "updated", "cleared"] | None = (
        None
    )
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
    issue_types: list[
        Literal[
            "subject_error",
            "style_error",
            "action_missing",
            "shot_missing",
            "camera_error",
            "text_error",
            "audio_error",
            "hallucination",
            "other",
        ]
    ] = Field(default_factory=list, max_length=8)
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
