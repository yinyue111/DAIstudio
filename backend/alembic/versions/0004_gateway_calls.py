"""add gateway_calls (per-call real-cost / usage accounting)

Defensive: only creates the table when missing, so it is a no-op on a fresh DB
whose metadata create_all already built it.

Revision ID: 0004_gateway_calls
Revises: 0003_user_password_hash
Create Date: 2026-06-17
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_gateway_calls"
down_revision = "0003_user_password_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gateway_calls" in insp.get_table_names():
        return
    op.create_table(
        "gateway_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("gateway_calls")
