"""tighten nullable drift from base migrations

Revision ID: 0015_tighten_not_null
Revises: 0014_model_gateway_config
Create Date: 2026-06-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_tighten_not_null"
down_revision = "0014_model_gateway_config"
branch_labels = None
depends_on = None


NOT_NULL_COLUMNS = {
    "admin_idempotency_keys": [
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "app_settings": [
        ("updated_at", sa.DateTime(timezone=True)),
    ],
    "audit_logs": [
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "credit_transactions": [
        ("user_id", sa.BigInteger()),
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "gateway_calls": [
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "gen_assets": [
        ("task_id", sa.BigInteger()),
        ("user_id", sa.BigInteger()),
        ("type", sa.String(length=8)),
        ("watermarked", sa.Boolean()),
        ("unlocked", sa.Boolean()),
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "gen_tasks": [
        ("user_id", sa.BigInteger()),
        ("stage", sa.String(length=8)),
        ("status", sa.String(length=16)),
        ("cost_frozen", sa.BigInteger()),
        ("cost_settled", sa.BigInteger()),
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "model_configs": [
        ("cost_credits", sa.BigInteger()),
        ("unlock_cost", sa.BigInteger()),
        ("enabled", sa.Boolean()),
        ("updated_at", sa.DateTime(timezone=True)),
    ],
    "parse_records": [
        ("user_id", sa.BigInteger()),
        ("status", sa.String(length=16)),
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "payment_orders": [
        ("status", sa.String(length=16)),
        ("created_at", sa.DateTime(timezone=True)),
        ("updated_at", sa.DateTime(timezone=True)),
    ],
    "payment_packages": [
        ("created_at", sa.DateTime(timezone=True)),
        ("updated_at", sa.DateTime(timezone=True)),
    ],
    "payment_provider_configs": [
        ("updated_at", sa.DateTime(timezone=True)),
    ],
    "phone_whitelist": [
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "uploaded_assets": [
        ("created_at", sa.DateTime(timezone=True)),
    ],
    "users": [
        ("status", sa.String(length=16)),
        ("is_admin", sa.Boolean()),
        ("balance_credits", sa.BigInteger()),
        ("frozen_credits", sa.BigInteger()),
        ("created_at", sa.DateTime(timezone=True)),
    ],
}


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _set_nullable(nullable: bool) -> None:
    existing = _existing_tables()
    for table_name, columns in NOT_NULL_COLUMNS.items():
        if table_name not in existing:
            continue
        with op.batch_alter_table(table_name) as batch:
            for name, col_type in columns:
                batch.alter_column(
                    name,
                    existing_type=col_type,
                    nullable=nullable,
                    existing_nullable=not nullable,
                )


def upgrade() -> None:
    _set_nullable(False)


def downgrade() -> None:
    _set_nullable(True)
