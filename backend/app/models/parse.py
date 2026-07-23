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
    JSONType,
    Mapped,
    String,
    Text,
    func,
    mapped_column,
)


class ParseRecord(Base):
    __tablename__ = "parse_records"
    __table_args__ = (
        CheckConstraint("status in ('queued', 'running', 'done', 'failed')", name="ck_parse_records_status_valid"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    assets: Mapped[list | None] = mapped_column(JSONType)  # [{type,url,thumb}]
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/done/failed
    error: Mapped[str | None] = mapped_column(Text)
    cached_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
