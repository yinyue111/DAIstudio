"""harden payments and track uploaded asset ownership

Revision ID: 0008_payment_upload_hardening
Revises: 0007_payment_config
Create Date: 2026-06-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_payment_upload_hardening"
down_revision = "0007_payment_config"
branch_labels = None
depends_on = None


def _constraint_names(insp, table):
    return {c["name"] for c in insp.get_check_constraints(table) if c.get("name")}


def _add_check_if_missing(insp, table, name, condition):
    if name in _constraint_names(insp, table):
        return
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "uploaded_assets" not in tables:
        op.create_table(
            "uploaded_assets",
            sa.Column("key", sa.String(length=255), primary_key=True),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("mime", sa.String(length=64), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("bytes", sa.BigInteger(), nullable=True),
            sa.Column("original_filename", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_uploaded_assets_user_id", "uploaded_assets", ["user_id"])

    # SQLite cannot add check constraints to existing tables. Test databases
    # get the same constraints through Base.metadata.create_all().
    if bind.dialect.name == "sqlite":
        return
    if "payment_orders" in tables:
        _add_check_if_missing(
            insp,
            "payment_orders",
            "ck_payment_orders_amount_cents_positive",
            "amount_cents > 0",
        )
        _add_check_if_missing(
            insp,
            "payment_orders",
            "ck_payment_orders_credits_positive",
            "credits > 0",
        )
        _add_check_if_missing(
            insp,
            "payment_orders",
            "ck_payment_orders_provider_valid",
            "provider in ('alipay', 'wechat')",
        )
        _add_check_if_missing(
            insp,
            "payment_orders",
            "ck_payment_orders_status_valid",
            "status in ('pending', 'paid', 'closed', 'failed')",
        )
    if "payment_packages" in tables:
        _add_check_if_missing(
            insp,
            "payment_packages",
            "ck_payment_packages_amount_cents_positive",
            "amount_cents > 0",
        )
        _add_check_if_missing(
            insp,
            "payment_packages",
            "ck_payment_packages_credits_positive",
            "credits > 0",
        )
    if "payment_provider_configs" in tables:
        _add_check_if_missing(
            insp,
            "payment_provider_configs",
            "ck_payment_provider_configs_provider_valid",
            "provider in ('alipay', 'wechat')",
        )
        _add_check_if_missing(
            insp,
            "payment_provider_configs",
            "ck_payment_provider_configs_mode_valid",
            "mode in ('mock', 'live')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        for table, names in (
            (
                "payment_provider_configs",
                (
                    "ck_payment_provider_configs_mode_valid",
                    "ck_payment_provider_configs_provider_valid",
                ),
            ),
            (
                "payment_packages",
                (
                    "ck_payment_packages_credits_positive",
                    "ck_payment_packages_amount_cents_positive",
                ),
            ),
            (
                "payment_orders",
                (
                    "ck_payment_orders_status_valid",
                    "ck_payment_orders_provider_valid",
                    "ck_payment_orders_credits_positive",
                    "ck_payment_orders_amount_cents_positive",
                ),
            ),
        ):
            for name in names:
                op.drop_constraint(name, table_name=table, type_="check")
    op.drop_table("uploaded_assets")
