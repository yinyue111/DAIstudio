"""add persistent reverse operation batches

Revision ID: 0042_reverse_operation_batches
Revises: 0041_reverse_video_source_ranges
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042_reverse_operation_batches"
down_revision = "0041_reverse_video_source_ranges"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "reverse_operations" not in tables:
        return
    if "reverse_operation_batches" not in tables:
        op.create_table(
            "reverse_operation_batches",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("client_request_id", sa.String(128), nullable=False),
            sa.Column("request_fingerprint", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=True),
            sa.Column("target", sa.String(32), nullable=False),
            sa.Column("shared_config_snapshot", JSON_TYPE, nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("status_counts", JSON_TYPE, nullable=False),
            sa.Column("total_count", sa.Integer(), nullable=False),
            sa.Column(
                "cancel_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "status in ('queued', 'running', 'needs_confirmation', 'partial', "
                "'succeeded', 'failed', 'canceled')",
                name="ck_reverse_operation_batches_status_valid",
            ),
            sa.CheckConstraint(
                "total_count >= 1 AND total_count <= 20",
                name="ck_reverse_operation_batches_total_count_range",
            ),
        )
        op.create_index(
            "uq_reverse_operation_batches_user_client_request_id",
            "reverse_operation_batches",
            ["user_id", "client_request_id"],
            unique=True,
        )
        op.create_index(
            "ix_reverse_operation_batches_user_id",
            "reverse_operation_batches",
            ["user_id"],
        )
        op.create_index(
            "ix_reverse_operation_batches_user_created",
            "reverse_operation_batches",
            ["user_id", "created_at"],
        )
        op.create_index(
            "ix_reverse_operation_batches_status_updated",
            "reverse_operation_batches",
            ["status", "updated_at"],
        )

    if "reverse_operation_batch_items" not in _tables():
        op.create_table(
            "reverse_operation_batch_items",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "batch_id",
                sa.BigInteger(),
                sa.ForeignKey("reverse_operation_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("item_index", sa.Integer(), nullable=False),
            sa.Column(
                "operation_id",
                sa.BigInteger(),
                sa.ForeignKey("reverse_operations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "item_index >= 0",
                name="ck_reverse_operation_batch_items_index_nonnegative",
            ),
        )
        op.create_index(
            "ix_reverse_operation_batch_items_batch_id",
            "reverse_operation_batch_items",
            ["batch_id"],
        )
        op.create_index(
            "uq_reverse_operation_batch_items_batch_index",
            "reverse_operation_batch_items",
            ["batch_id", "item_index"],
            unique=True,
        )
        op.create_index(
            "uq_reverse_operation_batch_items_operation",
            "reverse_operation_batch_items",
            ["operation_id"],
            unique=True,
        )


def downgrade() -> None:
    tables = _tables()
    if "reverse_operation_batch_items" in tables:
        op.drop_table("reverse_operation_batch_items")
    if "reverse_operation_batches" in tables:
        op.drop_table("reverse_operation_batches")
