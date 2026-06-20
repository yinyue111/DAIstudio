"""add persistent admin idempotency keys

Revision ID: 0010_admin_idempotency_keys
Revises: 0009_unique_active_final_tasks
Create Date: 2026-06-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_admin_idempotency_keys"
down_revision = "0009_unique_active_final_tasks"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "admin_idempotency_keys" in set(insp.get_table_names()):
        return
    op.create_table(
        "admin_idempotency_keys",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("admin_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("scope in ('quota_grant')", name="ck_admin_idempotency_scope_valid"),
    )
    op.create_index(
        "uq_admin_idempotency_scope_key",
        "admin_idempotency_keys",
        ["admin_id", "scope", "key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_admin_idempotency_scope_key", table_name="admin_idempotency_keys")
    op.drop_table("admin_idempotency_keys")
