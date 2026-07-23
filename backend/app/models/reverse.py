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


class ReverseOperation(Base):
    __tablename__ = "reverse_operations"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'needs_confirmation', 'succeeded', 'failed', 'canceled')",
            name="ck_reverse_operations_status_valid",
        ),
        CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_reverse_operations_progress_range",
        ),
        CheckConstraint("cost_frozen >= 0", name="ck_reverse_operations_cost_frozen_nonnegative"),
        CheckConstraint("cost_settled >= 0", name="ck_reverse_operations_cost_settled_nonnegative"),
        Index("uq_reverse_operations_user_client_request_id", "user_id", "client_request_id", unique=True),
        Index(
            "uq_reverse_operations_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
        Index("ix_reverse_operations_user_created", "user_id", "created_at"),
        Index("ix_reverse_operations_user_target_created", "user_id", "target", "created_at"),
        Index("ix_reverse_operations_status_updated", "status", "updated_at"),
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
        ForeignKey("generation_quotes.id"),
        index=True,
    )
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_url: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_focus: Mapped[str] = mapped_column(
        String(32), default="comprehensive", server_default="comprehensive", nullable=False
    )
    analysis_precision: Mapped[str] = mapped_column(
        String(16), default="standard", server_default="standard", nullable=False
    )
    output_purpose: Mapped[str] = mapped_column(
        String(32), default="generation", server_default="generation", nullable=False
    )
    custom_instruction: Mapped[str | None] = mapped_column(String(500))
    include_audio: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    source_range: Mapped[dict | None] = mapped_column(JSONType)
    source_ranges: Mapped[list | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    phase: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_context: Mapped[dict | None] = mapped_column(JSONType)
    model_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    template_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSONType)
    raw_provider_result: Mapped[dict | None] = mapped_column(JSONType)
    normalized_result: Mapped[dict | None] = mapped_column(JSONType)
    result_schema_version: Mapped[str] = mapped_column(
        String(32), default="reverse.v2", server_default="reverse.v2", nullable=False
    )
    applied_result_version: Mapped[int | None] = mapped_column(Integer)
    retry_of_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id"), index=True
    )
    charged_credits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_frozen: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_settled: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reference_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
class ReverseOperationBatch(Base):
    __tablename__ = "reverse_operation_batches"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'needs_confirmation', 'partial', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_reverse_operation_batches_status_valid",
        ),
        CheckConstraint(
            "total_count >= 1 AND total_count <= 20",
            name="ck_reverse_operation_batches_total_count_range",
        ),
        Index(
            "uq_reverse_operation_batches_user_client_request_id",
            "user_id",
            "client_request_id",
            unique=True,
        ),
        Index("ix_reverse_operation_batches_user_created", "user_id", "created_at"),
        Index("ix_reverse_operation_batches_status_updated", "status", "updated_at"),
        Index(
            "uq_reverse_operation_batches_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("generation_quotes.id"),
        index=True,
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    shared_config_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    status_counts: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
class ReverseOperationBatchItem(Base):
    __tablename__ = "reverse_operation_batch_items"
    __table_args__ = (
        CheckConstraint("item_index >= 0", name="ck_reverse_operation_batch_items_index_nonnegative"),
        Index(
            "uq_reverse_operation_batch_items_batch_index",
            "batch_id",
            "item_index",
            unique=True,
        ),
        Index(
            "uq_reverse_operation_batch_items_operation",
            "operation_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reverse_operation_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reverse_operations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class ReverseResultRevision(Base):
    __tablename__ = "reverse_result_revisions"
    __table_args__ = (
        CheckConstraint(
            "source in ('provider_raw', 'normalized', 'user_edit', 'applied', "
            "'model_compiled', 'generation')",
            name="ck_reverse_result_revisions_source_valid",
        ),
        Index(
            "uq_reverse_result_revisions_operation_version",
            "operation_id",
            "version",
            unique=True,
        ),
        CheckConstraint(
            "lineage_status in ('verified', 'legacy_unverified')",
            name="ck_reverse_result_revisions_lineage_status_valid",
        ),
        CheckConstraint(
            "evidence_review_action IS NULL OR evidence_review_action in "
            "('not_applicable', 'inherited', 'updated', 'cleared')",
            name="ck_reverse_result_revisions_evidence_action_valid",
        ),
        CheckConstraint(
            "parent_revision_id IS NULL OR parent_revision_id <> id",
            name="ck_reverse_result_revisions_parent_not_self",
        ),
        Index("ix_reverse_result_revisions_user_created", "user_id", "created_at"),
        Index("ix_reverse_result_revisions_parent_revision_id", "parent_revision_id"),
        Index("ix_reverse_result_revisions_lineage_status", "lineage_status"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    parent_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        nullable=True,
    )
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    source_fingerprints: Mapped[list | None] = mapped_column(JSONType)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    lineage_status: Mapped[str] = mapped_column(
        String(24),
        default="legacy_unverified",
        server_default="legacy_unverified",
        nullable=False,
    )
    evidence_review_action: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
class PromptOptimizationProposal(Base):
    __tablename__ = "prompt_optimization_proposals"
    __table_args__ = (
        CheckConstraint(
            "status in ('proposed', 'accepted', 'partially_accepted', 'rejected', 'expired')",
            name="ck_prompt_optimization_proposals_status_valid",
        ),
        CheckConstraint("version >= 1", name="ck_prompt_optimization_proposals_version_positive"),
        CheckConstraint(
            "optimization_kind in ('rewrite', 'model_compile')",
            name="ck_prompt_optimization_proposals_kind_valid",
        ),
        Index(
            "uq_prompt_optimization_proposals_user_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_prompt_optimization_proposals_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
        Index("ix_prompt_optimization_proposals_source_revision", "source_revision_id"),
        Index(
            "uq_prompt_optimization_proposals_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=True
    )
    source_revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_result_revisions.id", ondelete="CASCADE"), nullable=True
    )
    target_model_config_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_configs.id"), nullable=False
    )
    capability_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_capability_versions.id"), nullable=False
    )
    optimizer_model_config_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("model_configs.id"), nullable=True
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("generation_quotes.id"),
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="proposed", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    category: Mapped[str] = mapped_column(String(8), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    optimization_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    original: Mapped[dict] = mapped_column(JSONType, nullable=False)
    suggestion: Mapped[dict] = mapped_column(JSONType, nullable=False)
    diff: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    constraint_coverage: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    warnings: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    catalog_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    accepted_segment_ids: Mapped[list | None] = mapped_column(JSONType)
    rejected_segment_ids: Mapped[list | None] = mapped_column(JSONType)
    decision_result: Mapped[dict | None] = mapped_column(JSONType)
    charged_credits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
class ReverseOperationFeedback(Base):
    __tablename__ = "reverse_operation_feedback"
    __table_args__ = (
        CheckConstraint(
            "rating in ('useful', 'not_useful')",
            name="ck_reverse_operation_feedback_rating_valid",
        ),
        Index("uq_reverse_operation_feedback_operation", "operation_id", unique=True),
        Index("ix_reverse_operation_feedback_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    issue_types: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
