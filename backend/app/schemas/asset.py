"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._base import (
    UtcDateTime,
)


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
    origin: Literal["generated", "uploaded", "fetched"]
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
    fetched: int = 0
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
        if (
            self.name is None
            and self.parent_id is None
            and not self.move_to_root
            and self.sort_order is None
        ):
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
