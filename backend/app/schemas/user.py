"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._base import UtcDateTime, _validate_json_payload_size


class UserOut(BaseModel):
    id: int
    phone: str
    nickname: str | None = None
    department: str | None = None
    status: str
    is_admin: bool
    balance_credits: int
    frozen_credits: int

    model_config = ConfigDict(from_attributes=True)


class UserDraftIn(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        _validate_json_payload_size(v, 96 * 1024, "草稿")
        return v


class UserDraftOut(BaseModel):
    key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: UtcDateTime | None = None


# --- Parse ---
