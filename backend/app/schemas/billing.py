"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._base import (
    UtcDateTime,
    re_match_package_id,
)


class PaymentPackageOut(BaseModel):
    id: str
    title: str
    amount_cents: int
    credits: int
    badge: str | None = None
    enabled: bool = True
    sort_order: int = 0


class PaymentCreateIn(BaseModel):
    provider: Literal["alipay", "wechat"] = "alipay"
    package_id: str = Field(min_length=1, max_length=32)

    @field_validator("package_id")
    @classmethod
    def _package_id(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("套餐 ID 不能为空")
        if not re_match_package_id(value):
            raise ValueError("套餐 ID 只能包含字母、数字、下划线和短横线")
        return value


class PaymentOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    provider: str
    package_id: str
    amount_cents: int
    credits: int
    status: str
    code_url: str | None = None
    provider_trade_no: str | None = None
    expires_at: UtcDateTime | None = None
    paid_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None


# --- Admin ---
class WhitelistIn(BaseModel):
    phone: str
    note: str | None = None
    department: str | None = None


class WhitelistDeleteIn(BaseModel):
    pass


class QuotaGrantIn(BaseModel):
    user_id: int
    amount: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=255)
    idempotency_key: str = Field(min_length=8, max_length=128)


class QuotaGrantItemIn(BaseModel):
    user_id: int
    amount: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=255)


class QuotaBulkGrantIn(BaseModel):
    items: list[QuotaGrantItemIn] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=96)


class QuotaBulkGrantItemOut(BaseModel):
    user_id: int
    ok: bool = True
    balance_credits: int | None = None
    error: str | None = None


class QuotaBulkGrantOut(BaseModel):
    granted: list[QuotaBulkGrantItemOut] = Field(default_factory=list)
    failed: list[QuotaBulkGrantItemOut] = Field(default_factory=list)


class UserStatusIn(BaseModel):
    status: Literal["active", "pending", "disabled"]
