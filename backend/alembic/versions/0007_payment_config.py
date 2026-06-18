"""add configurable payment packages and provider settings

Revision ID: 0007_payment_config
Revises: 0006_payment_orders
Create Date: 2026-06-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_payment_config"
down_revision = "0006_payment_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "payment_packages" not in tables:
        op.create_table(
            "payment_packages",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("title", sa.String(length=64), nullable=False),
            sa.Column("amount_cents", sa.BigInteger(), nullable=False),
            sa.Column("credits", sa.BigInteger(), nullable=False),
            sa.Column("badge", sa.String(length=32), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    if "payment_provider_configs" not in tables:
        op.create_table(
            "payment_provider_configs",
            sa.Column("provider", sa.String(length=16), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("mode", sa.String(length=16), nullable=False, server_default="mock"),
            sa.Column("public_config", sa.JSON(), nullable=True),
            sa.Column("secret_config", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("payment_provider_configs")
    op.drop_table("payment_packages")
