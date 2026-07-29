"""Auto-generated from schemas.py split."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._base import (
    UtcDateTime,
    _validate_json_payload_size,
    _validate_workspace_snapshot,
)


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


class CreationRecipeUsageStatsOut(BaseModel):
    total: int = 0
    unique_users: int = 0
    by_event: dict[str, int] = Field(default_factory=dict)
    last_used_at: UtcDateTime | None = None


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
    usage: CreationRecipeUsageStatsOut | None = None
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
    share_slug: str | None = Field(
        default=None, min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
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
