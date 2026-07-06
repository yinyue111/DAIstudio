"""add reverse prompt idempotency operations

Revision ID: 0025_reverse_operations
Revises: 0024_audit_log_indexes
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_reverse_operations"
down_revision = "0024_audit_log_indexes"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "reverse_operations" in _tables():
        return
    op.create_table(
        "reverse_operations",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("client_request_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=32), nullable=False),
        sa.Column("asset_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("result", JSON_TYPE, nullable=True),
        sa.Column("charged_credits", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reference_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status in ('running', 'succeeded', 'failed')",
            name="ck_reverse_operations_status_valid",
        ),
    )
    op.create_index("ix_reverse_operations_user_id", "reverse_operations", ["user_id"])
    op.create_index(
        "uq_reverse_operations_user_client_request_id",
        "reverse_operations",
        ["user_id", "client_request_id"],
        unique=True,
    )
    op.create_index(
        "ix_reverse_operations_user_created",
        "reverse_operations",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    if "reverse_operations" not in _tables():
        return
    op.drop_index("ix_reverse_operations_user_created", table_name="reverse_operations")
    op.drop_index("uq_reverse_operations_user_client_request_id", table_name="reverse_operations")
    op.drop_index("ix_reverse_operations_user_id", table_name="reverse_operations")
    op.drop_table("reverse_operations")
