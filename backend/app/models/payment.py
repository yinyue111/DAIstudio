"""Auto-generated from models.py split. DO NOT edit the line ranges manually."""
from __future__ import annotations

from datetime import datetime

from ._base import (
    Base,
    BigInteger,
    BigIntPK,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSONType,
    Mapped,
    String,
    Text,
    func,
    mapped_column,
    text,
)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payment_orders_amount_cents_positive"),
        CheckConstraint("credits > 0", name="ck_payment_orders_credits_positive"),
        CheckConstraint("provider in ('alipay', 'wechat')", name="ck_payment_orders_provider_valid"),
        CheckConstraint(
            "status in ('pending', 'paid', 'closed', 'failed')",
            name="ck_payment_orders_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # alipay/wechat
    package_id: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    code_url: Mapped[str | None] = mapped_column(Text)
    provider_trade_no: Mapped[str | None] = mapped_column(String(128))
    raw: Mapped[dict | None] = mapped_column(JSONType)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index(
    "uq_payment_orders_provider_trade_no",
    PaymentOrder.provider,
    PaymentOrder.provider_trade_no,
    unique=True,
    postgresql_where=text("provider_trade_no IS NOT NULL"),
    sqlite_where=text("provider_trade_no IS NOT NULL"),
)

class PaymentPackage(Base):
    __tablename__ = "payment_packages"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payment_packages_amount_cents_positive"),
        CheckConstraint("credits > 0", name="ck_payment_packages_credits_positive"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    badge: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

class PaymentProviderConfig(Base):
    __tablename__ = "payment_provider_configs"
    __table_args__ = (
        CheckConstraint(
            "provider in ('alipay', 'wechat')",
            name="ck_payment_provider_configs_provider_valid",
        ),
        CheckConstraint("mode in ('mock', 'live')", name="ck_payment_provider_configs_mode_valid"),
    )

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)  # alipay/wechat
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="mock", nullable=False)  # mock/live
    public_config: Mapped[dict | None] = mapped_column(JSONType)
    secret_config: Mapped[dict | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
