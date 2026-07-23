"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._base import (
    UtcDateTime,
    _validate_json_payload_size,
)


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
