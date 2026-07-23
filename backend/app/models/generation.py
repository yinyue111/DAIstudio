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


class GenTask(Base):
    __tablename__ = "gen_tasks"
    __table_args__ = (
        CheckConstraint(
            "source_type IS NULL OR source_type in ('image', 'video')",
            name="ck_gen_tasks_source_type_valid",
        ),
        CheckConstraint("category in ('image', 'video')", name="ck_gen_tasks_category_valid"),
        CheckConstraint("stage in ('preview', 'final')", name="ck_gen_tasks_stage_valid"),
        CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed', 'needs_review', 'canceled')",
            name="ck_gen_tasks_status_valid",
        ),
        CheckConstraint(
            "phase IS NULL OR phase in ('rendering', 'submitting', 'polling', 'downloading', 'reconciling')",
            name="ck_gen_tasks_phase_valid",
        ),
        CheckConstraint("cost_frozen >= 0", name="ck_gen_tasks_cost_frozen_nonnegative"),
        CheckConstraint("cost_settled >= 0", name="ck_gen_tasks_cost_settled_nonnegative"),
        CheckConstraint(
            "(reverse_operation_id IS NULL AND source_revision_id IS NULL "
            "AND compiled_revision_id IS NULL AND generation_revision_id IS NULL) OR "
            "(reverse_operation_id IS NOT NULL AND source_revision_id IS NOT NULL "
            "AND compiled_revision_id IS NOT NULL AND generation_revision_id IS NOT NULL)",
            name="ck_gen_tasks_reverse_lineage_complete",
        ),
        CheckConstraint(
            "retry_of_task_id IS NULL OR retry_of_task_id <> id",
            name="ck_gen_tasks_retry_not_self",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id"),
        index=True,
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "generation_quotes.id",
            name="fk_gen_tasks_quote_id_generation_quotes",
            use_alter=True,
        ),
        index=True,
    )
    reverse_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_operations.id"),
        index=True,
    )
    source_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        index=True,
    )
    compiled_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        index=True,
    )
    generation_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        index=True,
    )
    source_asset_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String(8))  # image/video
    category: Mapped[str] = mapped_column(String(8), nullable=False)  # image/video
    stage: Mapped[str] = mapped_column(String(8), default="preview")  # preview/final
    prompt: Mapped[dict | None] = mapped_column(JSONType)  # structured + final_text
    model_use: Mapped[str | None] = mapped_column(String(16))  # vision/image/video
    params: Mapped[dict | None] = mapped_column(JSONType)
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/succeeded/failed/canceled
    cost_frozen: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_settled: Mapped[int] = mapped_column(BigInteger, default=0)
    external_task_id: Mapped[str | None] = mapped_column(Text)  # async video task id
    # DB-recoverable generation sub-state: image rendering, video submit/poll/download, reconciliation.
    phase: Mapped[str | None] = mapped_column(String(16))
    external_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_task_id: Mapped[int | None] = mapped_column(BigInteger)  # video final -> preview
    retry_of_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("gen_tasks.id"),
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationDispatch(Base):
    """Durable Celery publication intent for one generation task attempt."""

    __tablename__ = "generation_dispatches"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_generation_dispatches_attempt_positive"),
        CheckConstraint(
            "publish_attempts >= 0",
            name="ck_generation_dispatches_publish_attempts_nonnegative",
        ),
        CheckConstraint(
            "status in ('pending', 'publishing', 'published', 'unknown', "
            "'failed', 'completed', 'needs_review')",
            name="ck_generation_dispatches_status_valid",
        ),
        Index(
            "uq_generation_dispatches_task_attempt",
            "task_id",
            "attempt",
            unique=True,
        ),
        Index(
            "uq_generation_dispatches_celery_task_id",
            "celery_task_id",
            unique=True,
        ),
        Index(
            "ix_generation_dispatches_reconcile",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("gen_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    task_name: Mapped[str] = mapped_column(String(64), nullable=False)
    celery_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index(
    "uq_gen_tasks_active_final_per_preview",
    GenTask.user_id,
    GenTask.parent_task_id,
    unique=True,
    postgresql_where=text(
        "category = 'video' AND stage = 'final' "
        "AND status IN ('queued','running','needs_review') "
        "AND parent_task_id IS NOT NULL"
    ),
    sqlite_where=text(
        "category = 'video' AND stage = 'final' "
        "AND status IN ('queued','running','needs_review') "
        "AND parent_task_id IS NOT NULL"
    ),
)

Index(
    "uq_gen_tasks_user_client_request_id",
    GenTask.user_id,
    GenTask.client_request_id,
    unique=True,
    postgresql_where=text("client_request_id IS NOT NULL"),
    sqlite_where=text("client_request_id IS NOT NULL"),
)

Index(
    "uq_gen_tasks_quote_id",
    GenTask.quote_id,
    unique=True,
    postgresql_where=text("quote_id IS NOT NULL"),
    sqlite_where=text("quote_id IS NOT NULL"),
)

Index(
    "uq_gen_tasks_compiled_revision_id",
    GenTask.compiled_revision_id,
    unique=True,
    postgresql_where=text("compiled_revision_id IS NOT NULL"),
    sqlite_where=text("compiled_revision_id IS NOT NULL"),
)

Index(
    "uq_gen_tasks_generation_revision_id",
    GenTask.generation_revision_id,
    unique=True,
    postgresql_where=text("generation_revision_id IS NOT NULL"),
    sqlite_where=text("generation_revision_id IS NOT NULL"),
)

class GenAsset(Base):
    __tablename__ = "gen_assets"
    __table_args__ = (
        CheckConstraint("type in ('image', 'video')", name="ck_gen_assets_type_valid"),
        CheckConstraint(
            "moderation_status in ('active', 'takedown')",
            name="ck_gen_assets_moderation_status_valid",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="ck_gen_assets_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_gen_assets_height_positive"),
        CheckConstraint("duration IS NULL OR duration >= 0", name="ck_gen_assets_duration_nonnegative"),
        CheckConstraint("bytes IS NULL OR bytes >= 0", name="ck_gen_assets_bytes_nonnegative"),
        Index("ix_gen_assets_user_created", "user_id", "created_at"),
        Index("ix_gen_assets_user_favorite_created", "user_id", "favorite", "created_at"),
        Index("ix_gen_assets_user_retained", "user_id", "retained_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("gen_tasks.id"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(8))  # image/video
    preview_url: Mapped[str | None] = mapped_column(Text)
    hd_url: Mapped[str | None] = mapped_column(Text)
    watermarked: Mapped[bool] = mapped_column(Boolean, default=True)
    unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    moderation_status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    retained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class GenerationQuote(Base):
    __tablename__ = "generation_quotes"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'consumed', 'expired', 'canceled')",
            name="ck_generation_quotes_status_valid",
        ),
        CheckConstraint(
            "category in ('image', 'video', 'workflow')",
            name="ck_generation_quotes_category_valid",
        ),
        CheckConstraint(
            "kind in ('generation', 'reverse', 'reverse_batch', 'workflow', "
            "'prompt_optimization', 'asset_unlock')",
            name="ck_generation_quotes_kind_valid",
        ),
        CheckConstraint(
            "stage in ('preview', 'final')",
            name="ck_generation_quotes_stage_valid",
        ),
        CheckConstraint(
            "estimated_credits >= 0",
            name="ck_generation_quotes_estimated_credits_nonnegative",
        ),
        Index("ix_generation_quotes_user_created", "user_id", "created_at"),
        Index("ix_generation_quotes_user_status_expires", "user_id", "status", "expires_at"),
        Index(
            "ix_generation_quotes_user_kind_client_request",
            "user_id",
            "kind",
            "client_request_id",
            "created_at",
        ),
        Index(
            "uq_generation_quotes_consumed_ref",
            "kind",
            "consumed_ref_type",
            "consumed_ref_id",
            unique=True,
            postgresql_where=text("consumed_ref_id IS NOT NULL"),
            sqlite_where=text("consumed_ref_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id"),
        index=True,
    )
    capability_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_capability_versions.id"),
        index=True,
    )
    price_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_price_versions.id"),
        index=True,
    )
    tool_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tool_versions.id"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(24), default="generation", server_default="generation", nullable=False
    )
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(8), nullable=False)
    request_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    model_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    pricing_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    price_breakdown: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    subject_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    estimated_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        unique=True,
    )
    consumed_ref_type: Mapped[str | None] = mapped_column(String(32))
    consumed_ref_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
