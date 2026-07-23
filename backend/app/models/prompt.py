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


class UserPrompt(Base):
    __tablename__ = "user_prompts"
    __table_args__ = (
        CheckConstraint("category in ('image', 'video', 'general')", name="ck_user_prompts_category_valid"),
        CheckConstraint(
            "source in ('manual', 'reverse', 'generate', 'library')",
            name="ck_user_prompts_source_valid",
        ),
        CheckConstraint("usage_count >= 0", name="ck_user_prompts_usage_count_nonnegative"),
        Index("ix_user_prompts_user_created", "user_id", "created_at"),
        Index("ix_user_prompts_user_favorite", "user_id", "favorite"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(16), default="general", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
