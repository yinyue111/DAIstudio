"""add user cloud drafts

Revision ID: 0027_user_drafts
Revises: 0026_clear_terminal_task_phase
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027_user_drafts"
down_revision = "0026_clear_terminal_task_phase"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "user_drafts" in _tables():
        return
    op.create_table(
        "user_drafts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_user_drafts_user_id", "user_drafts", ["user_id"])
    op.create_index("uq_user_drafts_user_key", "user_drafts", ["user_id", "key"], unique=True)


def downgrade() -> None:
    if "user_drafts" not in _tables():
        return
    op.drop_index("uq_user_drafts_user_key", table_name="user_drafts")
    op.drop_index("ix_user_drafts_user_id", table_name="user_drafts")
    op.drop_table("user_drafts")
