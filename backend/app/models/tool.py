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
    _default_workflow_compiled_snapshot,
    _default_workflow_compiled_snapshot_hash,
    func,
    mapped_column,
    text,
)


class ToolDefinition(Base):
    __tablename__ = "tool_definitions"
    __table_args__ = (
        CheckConstraint(
            "category in ('image', 'video', 'workflow', 'utility')",
            name="ck_tool_definitions_category_valid",
        ),
        Index("uq_tool_definitions_slug", "slug", unique=True),
        Index("ix_tool_definitions_enabled_sort", "enabled", "sort_order", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    renderer: Mapped[str] = mapped_column(String(64), default="studio", nullable=False)
    entry_path: Mapped[str] = mapped_column(String(512), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ToolVersion(Base):
    __tablename__ = "tool_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_tool_versions_version_positive"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_tool_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'published', 'disabled', 'retired'))",
            name="ck_tool_versions_active_status",
        ),
        Index(
            "uq_tool_versions_definition_version",
            "tool_definition_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_tool_versions_active",
            "tool_definition_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    tool_definition_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="tool.v1", server_default="tool.v1", nullable=False
    )
    input_schema: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    workflow: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    pricing_policy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
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
            "tool_versions.id",
            name="fk_tool_versions_source",
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


class ToolRun(Base):
    __tablename__ = "tool_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'succeeded', "
            "'failed', 'canceled', 'compensating')",
            name="ck_tool_runs_status_valid",
        ),
        Index(
            "uq_tool_runs_user_client_request_id",
            "user_id",
            "client_request_id",
            unique=True,
        ),
        Index("ix_tool_runs_user_created", "user_id", "created_at", "id"),
        Index("ix_tool_runs_status_updated", "status", "updated_at", "id"),
        Index(
            "uq_tool_runs_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
        CheckConstraint("cost_frozen >= 0", name="ck_tool_runs_cost_frozen_nonnegative"),
        CheckConstraint("cost_settled >= 0", name="ck_tool_runs_cost_settled_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_definition_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_definitions.id"),
        nullable=False,
        index=True,
    )
    tool_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_versions.id"),
        nullable=False,
        index=True,
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("generation_quotes.id"),
        index=True,
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    pricing_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    cost_frozen: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_settled: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'succeeded', "
            "'failed', 'canceled', 'compensating')",
            name="ck_workflow_runs_status_valid",
        ),
        CheckConstraint("revision >= 0", name="ck_workflow_runs_revision_nonnegative"),
        CheckConstraint(
            "length(compiled_snapshot_hash) = 64",
            name="ck_workflow_runs_compiled_snapshot_hash_length",
        ),
        Index("uq_workflow_runs_tool_run_id", "tool_run_id", unique=True),
        Index("ix_workflow_runs_user_created", "user_id", "created_at", "id"),
        Index("ix_workflow_runs_status_updated", "status", "updated_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    tool_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    compiled_snapshot: Mapped[dict] = mapped_column(
        JSONType,
        nullable=False,
        default=_default_workflow_compiled_snapshot,
    )
    compiled_snapshot_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=_default_workflow_compiled_snapshot_hash,
    )
    compiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    current_node_key: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ToolNodeRun(Base):
    __tablename__ = "tool_node_runs"
    __table_args__ = (
        CheckConstraint(
            "node_type in ('parse', 'reverse', 'manual_review', 'compile', "
            "'generate', 'compose', 'export')",
            name="ck_tool_node_runs_type_valid",
        ),
        CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'waiting_external', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_tool_node_runs_status_valid",
        ),
        CheckConstraint(
            "compensation_status in ('none', 'pending', 'running', 'succeeded', 'failed')",
            name="ck_tool_node_runs_compensation_status_valid",
        ),
        CheckConstraint("topological_index >= 0", name="ck_tool_node_runs_topology_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="ck_tool_node_runs_attempt_count_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="ck_tool_node_runs_max_attempts_positive"),
        CheckConstraint("revision >= 0", name="ck_tool_node_runs_revision_nonnegative"),
        Index(
            "uq_tool_node_runs_workflow_node_key",
            "workflow_run_id",
            "node_key",
            unique=True,
        ),
        Index("ix_tool_node_runs_workflow_topology", "workflow_run_id", "topological_index"),
        Index("ix_tool_node_runs_status_available", "status", "available_at", "id"),
        Index("ix_tool_node_runs_external", "external_kind", "external_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    topological_index: Mapped[int] = mapped_column(Integer, nullable=False)
    depends_on: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    config_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    compensation_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    input_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    external_kind: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(256))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatch_token: Mapped[str | None] = mapped_column(String(64))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compensation_status: Mapped[str] = mapped_column(
        String(16), default="none", nullable=False
    )
    compensation_error: Mapped[str | None] = mapped_column(Text)
    compensation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compensation_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ToolNodeAttempt(Base):
    __tablename__ = "tool_node_attempts"
    __table_args__ = (
        CheckConstraint(
            "kind in ('execution', 'compensation')",
            name="ck_tool_node_attempts_kind_valid",
        ),
        CheckConstraint(
            "status in ('running', 'waiting_review', 'waiting_external', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_tool_node_attempts_status_valid",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_tool_node_attempts_number_positive"),
        Index(
            "uq_tool_node_attempts_node_kind_number",
            "node_run_id",
            "kind",
            "attempt_number",
            unique=True,
        ),
        Index("uq_tool_node_attempts_dispatch_token", "dispatch_token", unique=True),
        Index("ix_tool_node_attempts_node_created", "node_run_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    node_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_node_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), default="execution", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dispatch_token: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    input_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    external_kind: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(256))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkflowDispatch(Base):
    """Durable broker publication intent for orchestration and node execution."""

    __tablename__ = "workflow_dispatches"
    __table_args__ = (
        CheckConstraint(
            "kind in ('orchestrate', 'node')",
            name="ck_workflow_dispatches_kind_valid",
        ),
        CheckConstraint(
            "status in ('pending', 'publishing', 'published', 'unknown', "
            "'completed', 'failed', 'needs_review')",
            name="ck_workflow_dispatches_status_valid",
        ),
        CheckConstraint(
            "publish_attempts >= 0",
            name="ck_workflow_dispatches_publish_attempts_nonnegative",
        ),
        Index("uq_workflow_dispatches_dedupe_key", "dedupe_key", unique=True),
        Index("uq_workflow_dispatches_celery_task_id", "celery_task_id", unique=True),
        Index(
            "ix_workflow_dispatches_reconcile",
            "status",
            "next_attempt_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tool_node_runs.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    celery_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dispatch_token: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
