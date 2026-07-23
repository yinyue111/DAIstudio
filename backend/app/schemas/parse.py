"""Auto-generated from schemas.py split."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
