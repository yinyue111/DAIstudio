"""Auto-generated from models.py split. DO NOT edit the line ranges manually."""
from __future__ import annotations

from datetime import datetime

from ._base import (
    Base,
    BigInteger,
    BigIntPK,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Mapped,
    String,
    func,
    mapped_column,
    text,
)


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    __table_args__ = (
        CheckConstraint(
            "type in ('grant', 'freeze', 'settle', 'refund', 'unlock', 'consume')",
            name="ck_credit_transactions_type_valid",
        ),
        CheckConstraint("balance_after >= 0", name="ck_credit_transactions_balance_after_nonnegative"),
        CheckConstraint(
            "frozen_after IS NULL OR frozen_after >= 0",
            name="ck_credit_transactions_frozen_after_nonnegative",
        ),
        CheckConstraint(
            "reserved_amount IS NULL OR reserved_amount >= 0",
            name="ck_credit_transactions_reserved_amount_nonnegative",
        ),
        CheckConstraint(
            "real_cost IS NULL OR real_cost >= 0",
            name="ck_credit_transactions_real_cost_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # grant/freeze/settle/refund/unlock/consume
    change: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_delta: Mapped[int | None] = mapped_column(BigInteger)
    frozen_delta: Mapped[int | None] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frozen_after: Mapped[int | None] = mapped_column(BigInteger)
    reserved_amount: Mapped[int | None] = mapped_column(BigInteger)
    real_cost: Mapped[int | None] = mapped_column(BigInteger)
    biz_type: Mapped[str | None] = mapped_column(String(32))  # gen_task / unlock / admin
    biz_ref: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index(
    "uq_credit_transactions_payment_grant",
    CreditTransaction.biz_type,
    CreditTransaction.biz_ref,
    CreditTransaction.type,
    unique=True,
    postgresql_where=text("biz_type = 'payment' AND type = 'grant' AND biz_ref IS NOT NULL"),
    sqlite_where=text("biz_type = 'payment' AND type = 'grant' AND biz_ref IS NOT NULL"),
)

class AdminIdempotencyKey(Base):
    __tablename__ = "admin_idempotency_keys"
    __table_args__ = (
        CheckConstraint("scope in ('quota_grant')", name="ck_admin_idempotency_scope_valid"),
        Index("uq_admin_idempotency_scope_key", "admin_id", "scope", "key", unique=True),
        Index(
            "ix_admin_idempotency_business_window",
            "admin_id",
            "scope",
            "business_fingerprint",
            "fingerprint_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))
    business_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fingerprint_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
