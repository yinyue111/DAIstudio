"""add executable tool workflow runs

Revision ID: 0053_tool_workflow_engine
Revises: 0052_generation_retry_chain
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0053_tool_workflow_engine"
down_revision = "0052_generation_retry_chain"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "tool_runs",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tool_definition_id",
            sa.BigInteger(),
            sa.ForeignKey("tool_definitions.id"),
            nullable=False,
        ),
        sa.Column(
            "tool_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tool_versions.id"),
            nullable=False,
        ),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("input_snapshot", JSON_TYPE, nullable=False),
        sa.Column("output", JSON_TYPE, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'succeeded', "
            "'failed', 'canceled', 'compensating')",
            name="ck_tool_runs_status_valid",
        ),
    )
    op.create_index("ix_tool_runs_user_id", "tool_runs", ["user_id"])
    op.create_index("ix_tool_runs_tool_definition_id", "tool_runs", ["tool_definition_id"])
    op.create_index("ix_tool_runs_tool_version_id", "tool_runs", ["tool_version_id"])
    op.create_index(
        "uq_tool_runs_user_client_request_id",
        "tool_runs",
        ["user_id", "client_request_id"],
        unique=True,
    )
    op.create_index("ix_tool_runs_user_created", "tool_runs", ["user_id", "created_at", "id"])
    op.create_index("ix_tool_runs_status_updated", "tool_runs", ["status", "updated_at", "id"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "tool_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tool_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_schema_version", sa.String(32), nullable=False),
        sa.Column("workflow_snapshot", JSON_TYPE, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("current_node_key", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'succeeded', "
            "'failed', 'canceled', 'compensating')",
            name="ck_workflow_runs_status_valid",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_workflow_runs_revision_nonnegative"),
    )
    op.create_index("uq_workflow_runs_tool_run_id", "workflow_runs", ["tool_run_id"], unique=True)
    op.create_index("ix_workflow_runs_user_id", "workflow_runs", ["user_id"])
    op.create_index(
        "ix_workflow_runs_user_created", "workflow_runs", ["user_id", "created_at", "id"]
    )
    op.create_index(
        "ix_workflow_runs_status_updated", "workflow_runs", ["status", "updated_at", "id"]
    )

    op.create_table(
        "tool_node_runs",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "workflow_run_id",
            sa.BigInteger(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_key", sa.String(64), nullable=False),
        sa.Column("node_type", sa.String(32), nullable=False),
        sa.Column("topological_index", sa.Integer(), nullable=False),
        sa.Column("depends_on", JSON_TYPE, nullable=False),
        sa.Column("config_snapshot", JSON_TYPE, nullable=False),
        sa.Column("compensation_snapshot", JSON_TYPE, nullable=True),
        sa.Column("input_snapshot", JSON_TYPE, nullable=True),
        sa.Column("output", JSON_TYPE, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("external_kind", sa.String(32), nullable=True),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_token", sa.String(64), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compensation_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("compensation_error", sa.Text(), nullable=True),
        sa.Column("compensation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compensation_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "node_type in ('parse', 'reverse', 'manual_review', 'compile', "
            "'generate', 'compose', 'export')",
            name="ck_tool_node_runs_type_valid",
        ),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'waiting_external', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_tool_node_runs_status_valid",
        ),
        sa.CheckConstraint(
            "compensation_status in ('none', 'pending', 'running', 'succeeded', 'failed')",
            name="ck_tool_node_runs_compensation_status_valid",
        ),
        sa.CheckConstraint("topological_index >= 0", name="ck_tool_node_runs_topology_nonnegative"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_tool_node_runs_attempt_count_nonnegative"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_tool_node_runs_max_attempts_positive"),
        sa.CheckConstraint("revision >= 0", name="ck_tool_node_runs_revision_nonnegative"),
    )
    op.create_index("ix_tool_node_runs_workflow_run_id", "tool_node_runs", ["workflow_run_id"])
    op.create_index(
        "uq_tool_node_runs_workflow_node_key",
        "tool_node_runs",
        ["workflow_run_id", "node_key"],
        unique=True,
    )
    op.create_index(
        "ix_tool_node_runs_workflow_topology",
        "tool_node_runs",
        ["workflow_run_id", "topological_index"],
    )
    op.create_index(
        "ix_tool_node_runs_status_available", "tool_node_runs", ["status", "available_at", "id"]
    )
    op.create_index(
        "ix_tool_node_runs_external", "tool_node_runs", ["external_kind", "external_id"]
    )

    op.create_table(
        "tool_node_attempts",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "node_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tool_node_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False, server_default="execution"),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("dispatch_token", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("input_snapshot", JSON_TYPE, nullable=True),
        sa.Column("output", JSON_TYPE, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("external_kind", sa.String(32), nullable=True),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind in ('execution', 'compensation')",
            name="ck_tool_node_attempts_kind_valid",
        ),
        sa.CheckConstraint(
            "status in ('running', 'waiting_review', 'waiting_external', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_tool_node_attempts_status_valid",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_tool_node_attempts_number_positive"),
    )
    op.create_index("ix_tool_node_attempts_node_run_id", "tool_node_attempts", ["node_run_id"])
    op.create_index(
        "uq_tool_node_attempts_node_kind_number",
        "tool_node_attempts",
        ["node_run_id", "kind", "attempt_number"],
        unique=True,
    )
    op.create_index(
        "uq_tool_node_attempts_dispatch_token",
        "tool_node_attempts",
        ["dispatch_token"],
        unique=True,
    )
    op.create_index(
        "ix_tool_node_attempts_node_created",
        "tool_node_attempts",
        ["node_run_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("tool_node_attempts")
    op.drop_table("tool_node_runs")
    op.drop_table("workflow_runs")
    op.drop_table("tool_runs")
