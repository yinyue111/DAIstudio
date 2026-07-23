"""Studio prompt optimization contracts.

These contracts are intentionally separate from the legacy prompt endpoint. A
lineage-backed request identifies immutable server data; it never carries a
client-authored copy of reverse context.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

PromptOptimizationMode = Literal[
    "faithful",
    "concise",
    "expand",
    "commercial",
    "cinematic",
    "constraints",
    "translate",
    "model_adaptation",
    "target_model_adaptation",
]
PromptOptimizationStatus = Literal[
    "proposed",
    "accepted",
    "partially_accepted",
    "rejected",
    "expired",
]
ConstraintType = Literal[
    "aspect_ratio",
    "duration",
    "negative_list",
    "exact_text",
    "protected_evidence",
    "semantic",
]


class ProtectedConstraintIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ConstraintType
    value: str | int | list[str]

    @field_validator("value")
    @classmethod
    def _bounded_value(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 1000:
                raise ValueError("约束内容不能为空且不得超过 1000 字符")
        elif isinstance(value, list):
            if not value or len(value) > 50:
                raise ValueError("约束列表必须包含 1-50 项")
            value = [str(item).strip() for item in value]
            if any(not item or len(item) > 300 for item in value):
                raise ValueError("约束列表项无效")
        return value


class StudioPromptOptimizationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str | None = Field(default=None, max_length=4000)
    reverse_operation_id: int | None = Field(default=None, gt=0)
    reverse_revision_id: int | None = Field(default=None, gt=0)
    mode: PromptOptimizationMode = "faithful"
    target_language: Literal["zh-CN", "en"] | None = None
    target_model_config_id: int = Field(gt=0)
    optimizer_model_config_id: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=8, max_length=128)
    quote_id: int | None = Field(default=None, gt=0)
    protected_constraints: list[ProtectedConstraintIn] = Field(
        default_factory=list,
        max_length=50,
    )

    # Ordinary prompts may declare their current Studio settings. Lineage-backed
    # requests must use the immutable revision and server operation context.
    category: Literal["image", "video"] | None = None
    product_mode: bool | None = None
    duration: int | None = Field(default=None, ge=1, le=3600)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    resolution: str | None = Field(default=None, max_length=32)

    @field_validator("prompt", "aspect_ratio", "resolution", mode="before")
    @classmethod
    def _strip_text(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _lineage_or_ordinary(self):
        has_operation = self.reverse_operation_id is not None
        has_revision = self.reverse_revision_id is not None
        if has_operation != has_revision:
            raise ValueError("reverse_operation_id 和 reverse_revision_id 必须同时提供")
        if has_operation:
            forged_context = {
                "prompt": self.prompt,
                "category": self.category,
                "product_mode": self.product_mode,
                "duration": self.duration,
                "aspect_ratio": self.aspect_ratio,
                "resolution": self.resolution,
            }
            if any(value is not None for value in forged_context.values()):
                raise ValueError("血缘提示词和生成上下文由服务端从反推版本加载")
        elif not self.prompt:
            raise ValueError("普通提示词优化必须提供 prompt")
        if self.mode == "translate" and self.target_language is None:
            raise ValueError("翻译模式必须指定 target_language")
        return self


class PromptDiffSegment(BaseModel):
    id: str
    field_path: str
    label: str
    original: Any
    suggestion: Any
    changed: bool


class ConstraintCoverage(BaseModel):
    constraint_id: str
    type: ConstraintType
    source_field: str
    value: Any
    status: Literal["verified", "mapped", "unverified"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warning: str | None = None


class CompilerWarning(BaseModel):
    code: str
    field: str
    action: Literal["dropped", "transformed", "unverified", "info"]
    message: str


class CatalogProfileSnapshot(BaseModel):
    model_config_id: int
    model_id: str
    provider: str | None = None
    capability_version_id: int
    capability_version: int
    schema_version: str
    capabilities: dict[str, Any]


class StudioPromptOptimizationOut(BaseModel):
    proposal_id: int
    proposal_version: int
    mode: PromptOptimizationMode
    optimization_kind: Literal["rewrite", "model_compile"]
    original: dict[str, Any]
    suggestion: dict[str, Any]
    segments: list[PromptDiffSegment]
    constraint_coverage: list[ConstraintCoverage]
    warnings: list[CompilerWarning]
    provenance: dict[str, Any]
    compiler_profile: CatalogProfileSnapshot
    charged_credits: int = 0


class StudioPromptOptimizationDetailOut(StudioPromptOptimizationOut):
    status: PromptOptimizationStatus
    category: Literal["image", "video"]
    target_model_config_id: int
    source_operation_id: int | None = None
    source_revision_id: int | None = None
    accepted_segment_ids: list[str] = Field(default_factory=list)
    rejected_segment_ids: list[str] = Field(default_factory=list)
    decision_result: dict[str, Any] | None = None
    expires_at: datetime
    decided_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    can_decide: bool


class StudioPromptOptimizationSummaryOut(BaseModel):
    proposal_id: int
    proposal_version: int
    status: PromptOptimizationStatus
    category: Literal["image", "video"]
    mode: PromptOptimizationMode
    optimization_kind: Literal["rewrite", "model_compile"]
    target_model_config_id: int
    target_model_id: str | None = None
    original_preview: str
    suggestion_preview: str
    changed_segment_count: int = Field(ge=0)
    charged_credits: int = Field(default=0, ge=0)
    expires_at: datetime
    decided_at: datetime | None = None
    created_at: datetime
    can_decide: bool


class StudioPromptOptimizationPageOut(BaseModel):
    items: list[StudioPromptOptimizationSummaryOut]
    limit: int = Field(ge=1, le=50)
    offset: int = Field(ge=0)
    has_more: bool


class StudioPromptOptimizationDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    accepted_segment_ids: list[str] = Field(default_factory=list, max_length=200)
    rejected_segment_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("accepted_segment_ids", "rejected_segment_ids")
    @classmethod
    def _unique_ids(cls, value: list[str]):
        cleaned = [str(item).strip() for item in value]
        if any(not item or len(item) > 128 for item in cleaned):
            raise ValueError("分段 ID 无效")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("分段 ID 不得重复")
        return cleaned

    @model_validator(mode="after")
    def _disjoint(self):
        if set(self.accepted_segment_ids).intersection(self.rejected_segment_ids):
            raise ValueError("同一分段不能同时接受和拒绝")
        return self


class StudioPromptOptimizationDecisionOut(BaseModel):
    proposal_id: int
    proposal_version: int
    status: Literal["accepted", "partially_accepted", "rejected"]
    accepted_segment_ids: list[str] = Field(default_factory=list)
    rejected_segment_ids: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    revision: dict[str, Any] | None = None
