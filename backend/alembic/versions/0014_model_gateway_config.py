"""add per-model gateway configuration

Revision ID: 0014_model_gateway_config
Revises: 0013_gen_task_client_request_id
Create Date: 2026-06-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0014_model_gateway_config"
down_revision = "0013_gen_task_client_request_id"
branch_labels = None
depends_on = None


def _column_names(insp, table: str) -> set[str]:
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "model_configs" not in set(insp.get_table_names()):
        return
    columns = _column_names(insp, "model_configs")
    if "provider" not in columns:
        op.add_column("model_configs", sa.Column("provider", sa.String(length=32), nullable=True))
    if "base_url" not in columns:
        op.add_column("model_configs", sa.Column("base_url", sa.String(length=512), nullable=True))
    if "api_key_encrypted" not in columns:
        op.add_column("model_configs", sa.Column("api_key_encrypted", sa.Text(), nullable=True))
    if "gateway_format" not in columns:
        op.add_column("model_configs", sa.Column("gateway_format", sa.String(length=16), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "model_configs" not in set(insp.get_table_names()):
        return
    columns = _column_names(insp, "model_configs")
    for name in ("gateway_format", "api_key_encrypted", "base_url", "provider"):
        if name in columns:
            op.drop_column("model_configs", name)
