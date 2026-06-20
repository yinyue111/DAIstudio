"""add payment_orders

Revision ID: 0006_payment_orders
Revises: 0005_video_lifecycle
Create Date: 2026-06-18
"""
import sqlalchemy as sa

from alembic import op

revision = "0006_payment_orders"
down_revision = "0005_video_lifecycle"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "payment_orders" in insp.get_table_names():
        return
    op.create_table(
        "payment_orders",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("order_no", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("package_id", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("credits", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("code_url", sa.Text(), nullable=True),
        sa.Column("provider_trade_no", sa.String(length=128), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_payment_orders_order_no", "payment_orders", ["order_no"], unique=True)
    op.create_index("ix_payment_orders_user_id", "payment_orders", ["user_id"])
    op.create_index("ix_payment_orders_status", "payment_orders", ["status"])


def downgrade() -> None:
    op.drop_table("payment_orders")
