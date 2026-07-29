"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._base import (
    UtcDateTime,
    _normalize_asset_tags,
)
from .asset import AssetRefsIn, UserAssetItem
from .generation import UnifiedWorkflowNodeOut


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
    media_type: Literal["image", "video", "audio"]
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
