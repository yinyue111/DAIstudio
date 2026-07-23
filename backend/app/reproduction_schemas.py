"""Contracts for generation-result reproduction assessments."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

AssessmentStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "partial",
    "failed",
    "canceled",
]
RemediationMode = Literal["image_inpaint", "video_shot_regenerate"]
RemediationStatus = Literal[
    "planned",
    "generating",
    "composing",
    "reassessing",
    "succeeded",
    "partial",
    "failed",
    "canceled",
]
RemediationPlanItemStatus = Literal[
    "planned",
    "queued",
    "running",
    "succeeded",
    "failed",
    "needs_review",
    "canceled",
]


def _json_size(value: Any, *, limit: int, label: str) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError(f"{label}超出大小上限")
    return value


class ReproductionAssessmentCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_asset_ref: str = Field(min_length=3, max_length=512)
    generated_asset_ref: str = Field(min_length=3, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=128)
    reverse_operation_id: int | None = Field(default=None, gt=0)
    reverse_revision_id: int | None = Field(default=None, gt=0)
    generation_task_id: int | None = Field(default=None, gt=0)

    @field_validator("source_asset_ref", "generated_asset_ref", "idempotency_key")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @model_validator(mode="after")
    def _reverse_pair(self):
        if (self.reverse_operation_id is None) != (self.reverse_revision_id is None):
            raise ValueError("reverse_operation_id 和 reverse_revision_id 必须同时提供")
        return self


class ReproductionCorrectionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    parent_revision_id: int = Field(gt=0)
    selected_finding_ids: list[int] = Field(min_length=1, max_length=100)
    structured_patch: dict[str, Any] = Field(default_factory=dict)
    prompt_patch: str | None = Field(default=None, max_length=12_000)
    negative_prompt_patch: str | None = Field(default=None, max_length=8_000)
    mask_patch: dict[str, Any] | None = None
    apply: bool = False

    @field_validator("idempotency_key")
    @classmethod
    def _strip_idempotency_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("prompt_patch", "negative_prompt_patch", mode="before")
    @classmethod
    def _strip_optional_text(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value or None

    @field_validator("selected_finding_ids")
    @classmethod
    def _unique_positive_findings(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or int(item) <= 0 for item in value):
            raise ValueError("selected_finding_ids 只能包含正整数")
        if len(set(value)) != len(value):
            raise ValueError("selected_finding_ids 不能重复")
        return value

    @field_validator("structured_patch")
    @classmethod
    def _bounded_structured_patch(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_size(value, limit=64 * 1024, label="structured_patch")

    @field_validator("mask_patch")
    @classmethod
    def _bounded_mask_patch(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _json_size(value, limit=64 * 1024, label="mask_patch")


class ReproductionRemediationPlanItem(BaseModel):
    """One immutable, independently quoted generation request in a remediation plan."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    kind: RemediationMode
    shot_id: str | None = Field(default=None, max_length=128)
    finding_ids: list[int] = Field(default_factory=list, max_length=100)
    request: dict[str, Any] = Field(min_length=1)

    @field_validator("item_id")
    @classmethod
    def _strip_item_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("item_id 不能为空")
        return value

    @field_validator("shot_id", mode="before")
    @classmethod
    def _strip_shot_id(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value or None

    @field_validator("finding_ids")
    @classmethod
    def _unique_positive_finding_ids(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("finding_ids 只能包含正整数")
        if len(set(value)) != len(value):
            raise ValueError("finding_ids 不能重复")
        return value

    @field_validator("request")
    @classmethod
    def _bounded_public_request(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(str(key).startswith("_") for key in value):
            raise ValueError("request 不能包含内部字段")
        return _json_size(value, limit=256 * 1024, label="request")

    @model_validator(mode="after")
    def _shot_matches_kind(self):
        if self.kind == "image_inpaint" and self.shot_id is not None:
            raise ValueError("image_inpaint 计划项不能包含 shot_id")
        if self.kind == "video_shot_regenerate" and self.shot_id is None:
            raise ValueError("video_shot_regenerate 计划项必须包含 shot_id")
        return self


class ReproductionRemediationCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    parent_revision_id: int = Field(gt=0)
    parent_remediation_id: int | None = Field(default=None, gt=0)
    selected_finding_ids: list[int] = Field(min_length=1, max_length=100)
    selected_shot_ids: list[str] = Field(default_factory=list, max_length=100)
    mode: RemediationMode
    model_config_id: int = Field(gt=0)
    prompt_patch: str | None = Field(default=None, max_length=12_000)
    negative_prompt_patch: str | None = Field(default=None, max_length=8_000)
    params: dict[str, Any] = Field(default_factory=dict)
    video_composition: dict[str, Any] | None = None
    auto_reassess: bool = True

    @field_validator("idempotency_key")
    @classmethod
    def _strip_remediation_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 8:
            raise ValueError("idempotency_key 至少需要 8 个字符")
        return value

    @field_validator("prompt_patch", "negative_prompt_patch", mode="before")
    @classmethod
    def _strip_remediation_prompt_patch(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value or None

    @field_validator("selected_finding_ids")
    @classmethod
    def _unique_selected_findings(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("selected_finding_ids 只能包含正整数")
        if len(set(value)) != len(value):
            raise ValueError("selected_finding_ids 不能重复")
        return value

    @field_validator("selected_shot_ids")
    @classmethod
    def _unique_selected_shots(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("selected_shot_ids 不能包含空值")
        if len(set(normalized)) != len(normalized):
            raise ValueError("selected_shot_ids 不能重复")
        return normalized

    @field_validator("params")
    @classmethod
    def _bounded_public_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(str(key).startswith("_") for key in value):
            raise ValueError("params 不能包含内部字段")
        return _json_size(value, limit=64 * 1024, label="params")

    @field_validator("video_composition")
    @classmethod
    def _bounded_video_composition(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return _json_size(value, limit=256 * 1024, label="video_composition")

    @model_validator(mode="after")
    def _mode_specific_inputs(self):
        if self.mode == "image_inpaint":
            if self.selected_shot_ids:
                raise ValueError("image_inpaint 不能指定 selected_shot_ids")
            if self.video_composition is not None:
                raise ValueError("image_inpaint 不能包含 video_composition")
        return self


class ReproductionRemediationPlanItemOut(BaseModel):
    """Mutable execution projection for one immutable remediation plan item."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    kind: RemediationMode
    shot_id: str | None = Field(default=None, max_length=128)
    finding_ids: list[int] = Field(default_factory=list, max_length=100)
    status: RemediationPlanItemStatus
    generation_task_id: int | None = Field(default=None, gt=0)
    asset_id: int | None = Field(default=None, gt=0)
    asset_ref: str | None = Field(default=None, min_length=3, max_length=512)
    error_code: str | None = Field(default=None, max_length=64)
    error: str | None = None

    @field_validator("item_id")
    @classmethod
    def _strip_execution_item_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("item_id 不能为空")
        return value

    @field_validator("shot_id", "asset_ref", "error_code", "error", mode="before")
    @classmethod
    def _strip_optional_execution_text(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value or None

    @field_validator("finding_ids")
    @classmethod
    def _unique_execution_finding_ids(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("finding_ids 只能包含正整数")
        if len(set(value)) != len(value):
            raise ValueError("finding_ids 不能重复")
        return value

    @model_validator(mode="after")
    def _execution_shot_matches_kind(self):
        if self.kind == "image_inpaint" and self.shot_id is not None:
            raise ValueError("image_inpaint 计划项不能包含 shot_id")
        if self.kind == "video_shot_regenerate" and self.shot_id is None:
            raise ValueError("video_shot_regenerate 计划项必须包含 shot_id")
        return self


class ReproductionFindingOut(BaseModel):
    id: int
    finding_key: str
    position: int
    dimension: str
    kind: str
    severity: Literal["low", "medium", "high", "critical"]
    confidence: float | None = None
    message: str
    bbox: dict[str, Any] | None = None
    time_range: dict[str, Any] | None = None
    shot_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ReproductionAssessmentOut(BaseModel):
    id: int
    status: AssessmentStatus
    phase: str | None = None
    progress: int = Field(ge=0, le=100)
    media_type: Literal["image", "video"]
    source_asset_ref: str
    generated_asset_ref: str
    cost_credits: int = Field(ge=0)
    cancel_requested: bool
    schema_version: str
    asset_snapshot: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    analyzers: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error: str | None = None
    findings: list[ReproductionFindingOut] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("metrics")
    @classmethod
    def _bounded_dimension_scores(cls, value: dict[str, Any]) -> dict[str, Any]:
        dimensions = value.get("dimensions") if isinstance(value, dict) else None
        rows = dimensions.values() if isinstance(dimensions, dict) else dimensions or []
        for row in rows:
            if not isinstance(row, dict) or row.get("score") is None:
                continue
            score = row["score"]
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValueError("评估维度 score 必须是 0..1 数字")
            if not 0 <= float(score) <= 1:
                raise ValueError("评估维度 score 必须在 0..1 之间")
        return value


class ReproductionAssessmentPageOut(BaseModel):
    items: list[ReproductionAssessmentOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ReverseRevisionSummaryOut(BaseModel):
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
    payload_hash: str | None = None
    lineage_status: Literal["verified", "legacy_unverified"]
    created_at: datetime | None = None


class ReproductionCorrectionOut(BaseModel):
    id: int
    assessment_id: int
    status: Literal["created", "applied"]
    idempotency_key: str
    parent_revision_id: int
    selected_finding_ids: list[int]
    structured_patch: dict[str, Any]
    prompt_patch: str | None = None
    negative_prompt_patch: str | None = None
    mask_patch: dict[str, Any] | None = None
    apply_requested: bool
    edited_revision: ReverseRevisionSummaryOut
    applied_revision: ReverseRevisionSummaryOut | None = None
    created_at: datetime


class ReproductionRemediationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int = Field(gt=0)
    assessment_id: int = Field(gt=0)
    correction_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    parent_remediation_id: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=8, max_length=128)
    request_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    mode: RemediationMode
    status: RemediationStatus
    selected_finding_ids: list[int]
    selected_shot_ids: list[str]
    source_asset_ref: str = Field(min_length=3, max_length=512)
    target_asset_ref: str = Field(min_length=3, max_length=512)
    parent_revision_id: int = Field(gt=0)
    applied_revision_id: int = Field(gt=0)
    plan_snapshot: list[ReproductionRemediationPlanItem] = Field(
        min_length=1,
        max_length=100,
    )
    plan_items: list[ReproductionRemediationPlanItemOut] = Field(
        min_length=1,
        max_length=100,
    )
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    video_composition: dict[str, Any] | None = None
    generation_task_ids: list[int] = Field(default_factory=list)
    composition_tool_run_id: int | None = Field(default=None, gt=0)
    composition_workflow_run_id: int | None = Field(default=None, gt=0)
    final_asset_id: int | None = Field(default=None, gt=0)
    final_asset_ref: str | None = Field(default=None, min_length=3, max_length=512)
    successor_assessment_id: int | None = Field(default=None, gt=0)
    auto_reassess: bool
    error_code: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("selected_finding_ids", "generation_task_ids")
    @classmethod
    def _positive_unique_output_ids(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("ID 列表只能包含正整数")
        if len(set(value)) != len(value):
            raise ValueError("ID 列表不能重复")
        return value

    @field_validator("selected_shot_ids")
    @classmethod
    def _unique_output_shot_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("selected_shot_ids 不能包含空值")
        if len(set(normalized)) != len(normalized):
            raise ValueError("selected_shot_ids 不能重复")
        return normalized

    @field_validator("video_composition")
    @classmethod
    def _bounded_output_video_composition(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return _json_size(value, limit=256 * 1024, label="video_composition")

    @model_validator(mode="after")
    def _validate_plan_consistency(self):
        item_ids = [item.item_id for item in self.plan_snapshot]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("plan_snapshot item_id 不能重复")
        execution_item_ids = [item.item_id for item in self.plan_items]
        if len(set(execution_item_ids)) != len(execution_item_ids):
            raise ValueError("plan_items item_id 不能重复")
        if set(item_ids) != set(execution_item_ids):
            raise ValueError("plan_items 必须完整映射 plan_snapshot")
        execution_by_id = {item.item_id: item for item in self.plan_items}
        for snapshot_item in self.plan_snapshot:
            execution_item = execution_by_id[snapshot_item.item_id]
            if (
                execution_item.kind != snapshot_item.kind
                or execution_item.shot_id != snapshot_item.shot_id
                or execution_item.finding_ids != snapshot_item.finding_ids
            ):
                raise ValueError("plan_items 的 kind、shot_id 和 finding_ids 不得偏离快照")
        if any(item.kind != self.mode for item in self.plan_snapshot):
            raise ValueError("plan_snapshot kind 必须与 remediation mode 一致")
        if any(item.kind != self.mode for item in self.plan_items):
            raise ValueError("plan_items kind 必须与 remediation mode 一致")
        selected_findings = set(self.selected_finding_ids)
        if any(not set(item.finding_ids) <= selected_findings for item in self.plan_snapshot):
            raise ValueError("plan_snapshot finding_ids 必须来自 selected_finding_ids")
        if self.mode == "image_inpaint":
            if self.selected_shot_ids:
                raise ValueError("image_inpaint 不能包含 selected_shot_ids")
            if self.video_composition is not None:
                raise ValueError("image_inpaint 不能包含 video_composition")
        else:
            plan_shot_ids = [item.shot_id for item in self.plan_snapshot]
            if len(set(plan_shot_ids)) != len(plan_shot_ids):
                raise ValueError("视频 plan_snapshot shot_id 不能重复")
            if set(plan_shot_ids) != set(self.selected_shot_ids):
                raise ValueError("视频 plan_snapshot 必须覆盖 selected_shot_ids")
        return self

    @computed_field(return_type=list[dict[str, Any]])
    @property
    def generation_requests(self) -> list[dict[str, Any]]:
        return [item.request for item in self.plan_snapshot]


class ReproductionRemediationPageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReproductionRemediationOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
