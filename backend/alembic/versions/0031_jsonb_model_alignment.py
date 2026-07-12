"""align legacy PostgreSQL JSON columns with JSONB models

Revision ID: 0031_jsonb_model_alignment
Revises: 0030_reverse_operation_recovery
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0031_jsonb_model_alignment"
down_revision = "0030_reverse_operation_recovery"
branch_labels = None
depends_on = None

JSON_COLUMNS = (
    ("app_settings", "value"),
    ("audit_logs", "detail"),
    ("gateway_calls", "detail"),
    ("gen_tasks", "prompt"),
    ("gen_tasks", "params"),
    ("model_configs", "extra"),
    ("parse_records", "assets"),
    ("payment_orders", "raw"),
    ("payment_provider_configs", "public_config"),
    ("payment_provider_configs", "secret_config"),
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    for table_name, column_name in JSON_COLUMNS:
        op.alter_column(
            table_name,
            column_name,
            existing_type=postgresql.JSON(astext_type=sa.Text()),
            type_=postgresql.JSONB(astext_type=sa.Text()),
            existing_nullable=True,
            postgresql_using=f'"{column_name}"::jsonb',
        )


def downgrade() -> None:
    if not _is_postgresql():
        return
    for table_name, column_name in reversed(JSON_COLUMNS):
        op.alter_column(
            table_name,
            column_name,
            existing_type=postgresql.JSONB(astext_type=sa.Text()),
            type_=postgresql.JSON(astext_type=sa.Text()),
            existing_nullable=True,
            postgresql_using=f'"{column_name}"::json',
        )
