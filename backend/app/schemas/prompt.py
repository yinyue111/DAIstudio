"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ._base import (
    PromptOptimizationDirection,
    _validate_json_payload_size,
)


class PromptOptimizeIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    category: Literal["image", "video"] = "image"
    product_mode: bool = False
    duration: int | None = Field(default=None, ge=1, le=3600)
    subject_mode: Literal["general", "product", "portrait"] | None = None
    reference_type: str | None = Field(default=None, max_length=64)
    subject_profile: dict[str, Any] | str | None = None
    target_model_id: str | None = Field(default=None, max_length=256)
    target_model_provider: str | None = Field(default=None, max_length=128)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    resolution: str | None = Field(default=None, max_length=32)
    product_lock_mode: Literal["free", "locked"] | None = None
    product_video_template: str | None = Field(default=None, max_length=64)
    optimizer_model_config_id: int | None = Field(default=None, gt=0)
    target_model_config_id: int | None = Field(default=None, gt=0)
    direction: PromptOptimizationDirection = "faithful"
    target_language: Literal["zh-CN", "en"] = "en"

    @field_validator(
        "prompt",
        "reference_type",
        "target_model_id",
        "target_model_provider",
        "aspect_ratio",
        "resolution",
        "product_video_template",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("subject_profile")
    @classmethod
    def _validate_subject_profile(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 4000:
                raise ValueError("主体档案过长")
            return value or None
        if isinstance(value, dict):
            _validate_json_payload_size(value, 16 * 1024, "主体档案")
        return value


class PromptOptimizeOut(BaseModel):
    prompt: str
    model_id: str
    source_prompt: str
    compiled_prompt: str | None = None
    direction: PromptOptimizationDirection = "faithful"
    target_language: Literal["zh-CN", "en"] | None = None
    optimization_kind: Literal["rewrite", "model_compile"] = "rewrite"
    change_summary: list[str] = Field(default_factory=list)
    preserved_requirements: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    estimated_credits: int = 0
    charged_credits: int = 0
    optimizer_model_id: str | None = None
    compiler_metadata: dict[str, Any] | None = None
    context_metadata: dict[str, Any] | None = None
    optimizer_model_config_id: int | None = None
    optimizer_model_name: str | None = None
