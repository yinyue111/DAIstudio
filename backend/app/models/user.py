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
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status in ('active', 'pending', 'disabled')", name="ck_users_status_valid"),
        CheckConstraint("balance_credits >= 0", name="ck_users_balance_nonnegative"),
        CheckConstraint("frozen_credits >= 0", name="ck_users_frozen_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(64))
    avatar: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/pending/disabled
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # bumped on password change / logout-all to invalidate existing JWTs
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    department: Mapped[str | None] = mapped_column(String(64))
    balance_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    frozen_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 自助注销时间：非空表示该账号已完成软注销（status 同时为 disabled，
    # phone 已被不可逆脱敏）。财务/审计记录仍保留 user_id 外键但不再指向自然人。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
class PhoneWhitelist(Base):
    __tablename__ = "phone_whitelist"

    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    note: Mapped[str | None] = mapped_column(String(128))
    department: Mapped[str | None] = mapped_column(String(64))
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class UserDraft(Base):
    __tablename__ = "user_drafts"
    __table_args__ = (
        Index("uq_user_drafts_user_key", "user_id", "key", unique=True),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
