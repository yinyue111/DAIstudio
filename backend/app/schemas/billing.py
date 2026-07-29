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
    refunded_amount_cents: int = 0
    refunded_at: UtcDateTime | None = None
    invoice_status: str = "none"
    expires_at: UtcDateTime | None = None
    paid_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None


class PaymentInvoiceRequestIn(BaseModel):
    """用户侧开票申请。"""

    invoice_type: Literal["personal", "company"]
    title: str = Field(min_length=1, max_length=128)
    tax_no: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=255)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("发票抬头不能为空")
        return value

    @field_validator("tax_no")
    @classmethod
    def _tax_no(cls, v: str | None) -> str | None:
        value = (v or "").strip()
        return value or None


class PaymentInvoiceOrderOut(PaymentOrderOut):
    invoice_type: str | None = None
    invoice_title: str | None = None
    invoice_tax_no: str | None = None
    invoice_email: str | None = None
    invoice_note: str | None = None
    invoice_requested_at: UtcDateTime | None = None
    invoice_issued_at: UtcDateTime | None = None


# --- Admin: 订单查询 / 补单 / 退款 / 开票 ---
class AdminPaymentOrderOut(BaseModel):
    id: int
    order_no: str
    user_id: int
    phone: str | None = None
    provider: str
    package_id: str
    amount_cents: int
    credits: int
    status: str
    provider_trade_no: str | None = None
    refunded_amount_cents: int = 0
    refunded_at: UtcDateTime | None = None
    invoice_status: str = "none"
    invoice_type: str | None = None
    invoice_title: str | None = None
    invoice_tax_no: str | None = None
    invoice_email: str | None = None
    invoice_note: str | None = None
    invoice_requested_at: UtcDateTime | None = None
    invoice_issued_at: UtcDateTime | None = None
    expires_at: UtcDateTime | None = None
    paid_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None


class AdminPaymentOrderPageOut(BaseModel):
    items: list[AdminPaymentOrderOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0


class AdminPaymentOrderSyncOut(BaseModel):
    order: AdminPaymentOrderOut
    outcome: str


class PaymentRefundIn(BaseModel):
    amount_cents: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("请填写退款原因")
        return value


class PaymentRefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    refund_no: str
    order_id: int
    provider: str
    amount_cents: int
    credits_reclaimed: int = 0
    status: str
    reason: str | None = None
    provider_refund_no: str | None = None
    error: str | None = None
    created_at: UtcDateTime | None = None


class AdminInvoiceUpdateIn(BaseModel):
    status: Literal["issued", "rejected"]
    note: str | None = Field(default=None, max_length=255)


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


class QuotaDeductIn(BaseModel):
    """管理端积分扣减/冲正:必须走 credit_transactions 流水。"""

    user_id: int
    amount: int = Field(gt=0)
    note: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=8, max_length=96)
    # 余额不足时:默认拒绝;true 表示扣到 0 为止(余额列带非负约束,不允许负余额)
    allow_partial: bool = False

    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("请填写扣减原因")
        return value


class QuotaDeductOut(BaseModel):
    user_id: int
    requested_amount: int
    deducted_amount: int
    balance_credits: int
    frozen_credits: int


class UserStatusIn(BaseModel):
    status: Literal["active", "pending", "disabled"]
