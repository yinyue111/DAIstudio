"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ._base import UtcDateTime


class AuditOut(BaseModel):
    id: int
    user_id: int | None = None
    action: str
    biz_type: str | None = None
    biz_id: int | None = None
    ip: str | None = None
    detail: dict[str, Any] | None = None
    created_at: UtcDateTime | None = None

    model_config = ConfigDict(from_attributes=True)
