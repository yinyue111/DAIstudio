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


PAYMENT_CHECKS = {
    "payment_orders": (
        ("ck_payment_orders_amount_cents_positive", "amount_cents > 0"),
        ("ck_payment_orders_credits_positive", "credits > 0"),
        ("ck_payment_orders_provider_valid", "provider in ('alipay', 'wechat')"),
        (
            "ck_payment_orders_status_valid",
            "status in ('pending', 'paid', 'closed', 'failed')",
        ),
    ),
    "payment_packages": (
        ("ck_payment_packages_amount_cents_positive", "amount_cents > 0"),
        ("ck_payment_packages_credits_positive", "credits > 0"),
    ),
    "payment_provider_configs": (
        (
            "ck_payment_provider_configs_provider_valid",
            "provider in ('alipay', 'wechat')",
        ),
        ("ck_payment_provider_configs_mode_valid", "mode in ('mock', 'live')"),
    ),
}


def _add_payment_checks(insp, tables) -> None:
    for table, checks in PAYMENT_CHECKS.items():
        if table not in tables:
            continue
        existing = _constraint_names(insp, table)
        missing = [(name, condition) for name, condition in checks if name not in existing]
        if not missing:
            continue
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(table, recreate="always") as batch:
                for name, condition in missing:
                    batch.create_check_constraint(name, condition)
        else:
            for name, condition in missing:
                _add_check_if_missing(insp, table, name, condition)


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

    _add_payment_checks(insp, tables)


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
