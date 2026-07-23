"""add durable generation dispatch outbox

Revision ID: 0048_generation_dispatch_outbox
Revises: 0047_reverse_revision_lineage
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0048_generation_dispatch_outbox"
down_revision = "0047_reverse_revision_lineage"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    if "generation_dispatches" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "generation_dispatches",
        sa.Column("id", BIGINT_PK, nullable=False, autoincrement=True),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("task_name", sa.String(length=64), nullable=False),
        sa.Column("celery_task_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("attempt >= 1", name="ck_generation_dispatches_attempt_positive"),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_generation_dispatches_publish_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'publishing', 'published', 'unknown', "
            "'failed', 'completed', 'needs_review')",
            name="ck_generation_dispatches_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["gen_tasks.id"],
            name="fk_generation_dispatches_task_id_gen_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generation_dispatches_task_id",
        "generation_dispatches",
        ["task_id"],
    )
    op.create_index(
        "uq_generation_dispatches_task_attempt",
        "generation_dispatches",
        ["task_id", "attempt"],
        unique=True,
    )
    op.create_index(
        "uq_generation_dispatches_celery_task_id",
        "generation_dispatches",
        ["celery_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_generation_dispatches_reconcile",
        "generation_dispatches",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    if "generation_dispatches" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("generation_dispatches")
