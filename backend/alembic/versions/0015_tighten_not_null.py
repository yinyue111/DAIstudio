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

BACKFILL_DEFAULTS = {
    ("admin_idempotency_keys", "created_at"): "CURRENT_TIMESTAMP",
    ("app_settings", "updated_at"): "CURRENT_TIMESTAMP",
    ("audit_logs", "created_at"): "CURRENT_TIMESTAMP",
    ("credit_transactions", "created_at"): "CURRENT_TIMESTAMP",
    ("gateway_calls", "created_at"): "CURRENT_TIMESTAMP",
    ("gen_assets", "watermarked"): "true",
    ("gen_assets", "unlocked"): "false",
    ("gen_assets", "created_at"): "CURRENT_TIMESTAMP",
    ("gen_tasks", "stage"): "'preview'",
    ("gen_tasks", "status"): "'failed'",
    ("gen_tasks", "cost_frozen"): "0",
    ("gen_tasks", "cost_settled"): "0",
    ("gen_tasks", "created_at"): "CURRENT_TIMESTAMP",
    ("model_configs", "cost_credits"): "1",
    ("model_configs", "unlock_cost"): "0",
    ("model_configs", "enabled"): "true",
    ("model_configs", "updated_at"): "CURRENT_TIMESTAMP",
    ("parse_records", "status"): "'failed'",
    ("parse_records", "created_at"): "CURRENT_TIMESTAMP",
    ("payment_orders", "status"): "'pending'",
    ("payment_orders", "created_at"): "CURRENT_TIMESTAMP",
    ("payment_orders", "updated_at"): "CURRENT_TIMESTAMP",
    ("payment_packages", "created_at"): "CURRENT_TIMESTAMP",
    ("payment_packages", "updated_at"): "CURRENT_TIMESTAMP",
    ("payment_provider_configs", "updated_at"): "CURRENT_TIMESTAMP",
    ("phone_whitelist", "created_at"): "CURRENT_TIMESTAMP",
    ("uploaded_assets", "created_at"): "CURRENT_TIMESTAMP",
    ("users", "status"): "'active'",
    ("users", "is_admin"): "false",
    ("users", "balance_credits"): "0",
    ("users", "frozen_credits"): "0",
    ("users", "created_at"): "CURRENT_TIMESTAMP",
}


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _quote(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _column_names(table_name: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table_name)}


def _backfill_defaults() -> None:
    existing = _existing_tables()
    for (table_name, column_name), expr in BACKFILL_DEFAULTS.items():
        if table_name not in existing or column_name not in _column_names(table_name):
            continue
        op.execute(
            sa.text(
                f"UPDATE {_quote(table_name)} "
                f"SET {_quote(column_name)} = {expr} "
                f"WHERE {_quote(column_name)} IS NULL"
            )
        )


def _preflight_not_null() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = _existing_tables()
    for table_name, columns in NOT_NULL_COLUMNS.items():
        if table_name not in existing:
            continue
        existing_columns = _column_names(table_name)
        pk_cols = insp.get_pk_constraint(table_name).get("constrained_columns") or []
        pk = pk_cols[0] if pk_cols else None
        for name, _col_type in columns:
            if name not in existing_columns:
                continue
            if pk:
                dirty = bind.execute(
                    sa.text(
                        f"SELECT {_quote(pk)} FROM {_quote(table_name)} "
                        f"WHERE {_quote(name)} IS NULL LIMIT 5"
                    )
                ).scalars().all()
                if dirty:
                    raise RuntimeError(
                        f"0015_tighten_not_null blocked: {table_name}.{name} "
                        f"still has NULL rows, sample {dirty}"
                    )
            else:
                count = bind.execute(
                    sa.text(
                        f"SELECT COUNT(*) FROM {_quote(table_name)} "
                        f"WHERE {_quote(name)} IS NULL"
                    )
                ).scalar()
                if count:
                    raise RuntimeError(
                        f"0015_tighten_not_null blocked: {table_name}.{name} "
                        f"still has {count} NULL rows"
                    )


def _set_nullable(nullable: bool) -> None:
    existing = _existing_tables()
    for table_name, columns in NOT_NULL_COLUMNS.items():
        if table_name not in existing:
            continue
        existing_columns = _column_names(table_name)
        with op.batch_alter_table(table_name) as batch:
            for name, col_type in columns:
                if name not in existing_columns:
                    continue
                batch.alter_column(
                    name,
                    existing_type=col_type,
                    nullable=nullable,
                    existing_nullable=not nullable,
                )


def upgrade() -> None:
    _backfill_defaults()
    _preflight_not_null()
    _set_nullable(False)


def downgrade() -> None:
    _set_nullable(True)
