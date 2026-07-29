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

# --- Runtime-editable config (seeded from models.yaml) ---


class ModelConfig(Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        Index("ix_model_configs_use", "use"),
        Index(
            "uq_model_configs_use_model_id",
            "use",
            "model_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_model_configs_default_per_use",
            "use",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
        CheckConstraint("use in ('vision', 'image', 'video', 'prompt')", name="ck_model_configs_use_valid"),
        CheckConstraint(
            "gateway_format IS NULL OR gateway_format in ('openai', 'ark', 'anthropic')",
            name="ck_model_configs_gateway_format_valid",
        ),
        CheckConstraint("cost_credits >= 0", name="ck_model_configs_cost_credits_nonnegative"),
        CheckConstraint("unlock_cost >= 0", name="ck_model_configs_unlock_cost_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    use: Mapped[str] = mapped_column(String(16), nullable=False)  # vision/image/video/prompt
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    gateway_format: Mapped[str | None] = mapped_column(String(16))  # openai | ark | anthropic
    cost_credits: Mapped[int] = mapped_column(BigInteger, default=1)  # cost to run / freeze
    unlock_cost: Mapped[int] = mapped_column(BigInteger, default=0)  # extra cost to unlock HD
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    extra: Mapped[dict | None] = mapped_column(JSONType)  # endpoint paths / field maps for video
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelCapabilityVersion(Base):
    __tablename__ = "model_capability_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_model_capability_versions_version_positive"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_model_capability_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            name="ck_model_capability_versions_active_status",
        ),
        Index(
            "uq_model_capability_versions_model_version",
            "model_config_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_model_capability_versions_active",
            "model_config_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="capability.v1", server_default="capability.v1", nullable=False
    )
    capabilities: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    metadata_snapshot: Mapped[dict] = mapped_column(
        JSONType,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    status: Mapped[str] = mapped_column(
        String(16), default="published", server_default="published", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "model_capability_versions.id",
            name="fk_capability_versions_source",
            ondelete="SET NULL",
        ),
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelPriceVersion(Base):
    __tablename__ = "model_price_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_model_price_versions_version_positive"),
        CheckConstraint("base_cost_credits >= 0", name="ck_model_price_versions_base_cost_nonnegative"),
        CheckConstraint("unlock_cost_credits >= 0", name="ck_model_price_versions_unlock_cost_nonnegative"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_model_price_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            name="ck_model_price_versions_active_status",
        ),
        Index(
            "uq_model_price_versions_model_version",
            "model_config_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_model_price_versions_active",
            "model_config_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="credit-price.v1", server_default="credit-price.v1", nullable=False
    )
    base_cost_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unlock_cost_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pricing: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="published", server_default="published", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "model_price_versions.id",
            name="fk_price_versions_source",
            ondelete="SET NULL",
        ),
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelRoute(Base):
    __tablename__ = "model_routes"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="ck_model_routes_priority_nonnegative"),
        CheckConstraint("config_revision >= 1", name="ck_model_routes_revision_positive"),
        CheckConstraint(
            "health_status in ('closed', 'open', 'half_open')",
            name="ck_model_routes_health_status_valid",
        ),
        CheckConstraint(
            "failure_threshold >= 1", name="ck_model_routes_failure_threshold_positive"
        ),
        CheckConstraint("window_seconds >= 1", name="ck_model_routes_window_positive"),
        CheckConstraint(
            "cooldown_seconds >= 1", name="ck_model_routes_cooldown_positive"
        ),
        Index(
            "uq_model_routes_model_key",
            "model_config_id",
            "route_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_model_routes_select",
            "model_config_id",
            "enabled",
            "priority",
            "id",
        ),
        Index("ix_model_routes_health", "health_status", "cooldown_until", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    gateway_format: Mapped[str | None] = mapped_column(String(16))
    extra: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    managed_by_model_config: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    failure_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="closed"
    )
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    half_open_claimed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ema_ms: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelRouteVersion(Base):
    __tablename__ = "model_route_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_model_route_versions_version_positive"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_model_route_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            name="ck_model_route_versions_active_status",
        ),
        Index(
            "uq_model_route_versions_route_version",
            "route_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_model_route_versions_active",
            "route_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="model-route.v2", server_default="model-route.v2", nullable=False
    )
    config_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="published", server_default="published", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "model_route_versions.id",
            name="fk_route_versions_source",
            ondelete="SET NULL",
        ),
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelRouteHealthEvent(Base):
    __tablename__ = "model_route_health_events"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('success', 'failure', 'ignored')",
            name="ck_model_route_health_events_outcome_valid",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_model_route_health_events_latency_nonnegative",
        ),
        Index("ix_model_route_health_events_route_created", "route_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    counts_toward_circuit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
