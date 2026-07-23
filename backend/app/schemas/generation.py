"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._base import (
    MAX_REVERSE_SOURCE_RANGES,
    UtcDateTime,
    _validate_json_payload_size,
)
from .asset import AssetOut
from .reverse import ReverseSourceRange


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
        if (self.reproduction_remediation_id is None) != (self.reproduction_plan_item_id is None):
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
