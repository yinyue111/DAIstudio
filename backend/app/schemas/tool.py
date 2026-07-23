"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ._base import _reject_dangerous_json_keys, _validate_json_payload_size


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
