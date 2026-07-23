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
    Integer,
    JSONType,
    Mapped,
    String,
    func,
    mapped_column,
)


class GatewayCall(Base):
    """Per-invocation gateway call log (real cost / usage accounting).

    Records every model-gateway call so spend can be reconciled against the
    provider's actual consumption (token usage where the provider reports it),
    not just the internal credit estimate."""

    __tablename__ = "gateway_calls"
    __table_args__ = (
        CheckConstraint(
            "kind in ('reverse', 'prompt_optimize', 'image', 'video_submit', 'video_poll', 'video_download')",
            name="ck_gateway_calls_kind_valid",
        ),
        CheckConstraint("status IS NULL OR status in ('ok', 'failed')", name="ck_gateway_calls_status_valid"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_gateway_calls_latency_nonnegative"),
        CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0", name="ck_gateway_calls_prompt_tokens_nonnegative"),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_gateway_calls_completion_tokens_nonnegative",
        ),
        CheckConstraint("total_tokens IS NULL OR total_tokens >= 0", name="ck_gateway_calls_total_tokens_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16))  # reverse/prompt_optimize/image/video_*
    model_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(16))  # ok/failed
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_id_id", "user_id", "id"),
        Index("ix_audit_logs_action_id", "action", "id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    biz_type: Mapped[str | None] = mapped_column(String(32))
    biz_id: Mapped[int | None] = mapped_column(BigInteger)
    ip: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
