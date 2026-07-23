"""User-facing credit ledger and aggregated billing response models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .schemas import UtcDateTime

CreditTransactionType = Literal[
    "grant",
    "freeze",
    "settle",
    "refund",
    "unlock",
    "consume",
]


class CreditTransactionOut(BaseModel):
    id: int
    type: CreditTransactionType
    change: int
    balance_delta: int
    frozen_delta: int
    balance_after: int
    frozen_after: int | None = None
    reserved_amount: int | None = None
    real_cost: int | None = None
    biz_type: str | None = None
    biz_ref: int | None = None
    note: str | None = None
    created_at: UtcDateTime


class CreditTransactionPageOut(BaseModel):
    items: list[CreditTransactionOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    total: int = Field(default=0, ge=0)


class BillingEntryOut(BaseModel):
    key: str
    kind: str
    biz_type: str
    biz_ref: int | None = None
    title: str
    subtitle: str | None = None
    status: str
    status_group: str
    model_config_id: int | None = None
    model_name: str | None = None
    model_id: str | None = None
    model_provider: str | None = None
    quote_id: int | None = None
    quote_kind: str | None = None
    quote_status: str | None = None
    quoted_credits: int | None = Field(default=None, ge=0)
    price_version_id: int | None = None
    related_kind: str | None = None
    related_id: int | None = None
    review_status: str | None = None
    review_reason: str | None = None
    review_note: str | None = None
    reviewed_at: UtcDateTime | None = None
    frozen_credits: int = Field(default=0, ge=0)
    settled_credits: int = Field(default=0, ge=0)
    refunded_credits: int = Field(default=0, ge=0)
    settlement_returned_credits: int = Field(default=0, ge=0)
    reservation_refunded_credits: int = Field(default=0, ge=0)
    consumed_refunded_credits: int = Field(default=0, ge=0)
    outstanding_frozen_credits: int = Field(default=0, ge=0)
    net_consumed_credits: int = Field(default=0, ge=0)
    credited_credits: int = Field(default=0, ge=0)
    net_balance_change: int = 0
    balance_conserved: bool = False
    reservation_conserved: bool = False
    balance_after: int
    frozen_after: int | None = None
    transaction_count: int = Field(default=0, ge=1)
    created_at: UtcDateTime
    updated_at: UtcDateTime


class BillingEntryPageOut(BaseModel):
    items: list[BillingEntryOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    total: int = Field(default=0, ge=0)
