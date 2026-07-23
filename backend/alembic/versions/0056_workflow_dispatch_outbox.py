"""add durable workflow dispatch outbox

Revision ID: 0056_workflow_dispatch_outbox
Revises: 0055_project_asset_governance
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0056_workflow_dispatch_outbox"
down_revision = "0055_project_asset_governance"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "workflow_dispatches",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "workflow_run_id",
            sa.BigInteger(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tool_node_runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("celery_task_id", sa.String(128), nullable=False),
        sa.Column("dispatch_token", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind in ('orchestrate', 'node')",
            name="ck_workflow_dispatches_kind_valid",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'publishing', 'published', 'unknown', "
            "'completed', 'failed', 'needs_review')",
            name="ck_workflow_dispatches_status_valid",
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_workflow_dispatches_publish_attempts_nonnegative",
        ),
    )
    op.create_index(
        "ix_workflow_dispatches_workflow_run_id",
        "workflow_dispatches",
        ["workflow_run_id"],
    )
    op.create_index(
        "ix_workflow_dispatches_node_run_id",
        "workflow_dispatches",
        ["node_run_id"],
    )
    op.create_index(
        "uq_workflow_dispatches_dedupe_key",
        "workflow_dispatches",
        ["dedupe_key"],
        unique=True,
    )
    op.create_index(
        "uq_workflow_dispatches_celery_task_id",
        "workflow_dispatches",
        ["celery_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_workflow_dispatches_reconcile",
        "workflow_dispatches",
        ["status", "next_attempt_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("workflow_dispatches")
