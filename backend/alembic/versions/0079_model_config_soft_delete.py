"""allow model configs to be removed without breaking historical references

Revision ID: 0079_model_config_soft_delete
Revises: 0078_grok_video_model_split
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0079_model_config_soft_delete"
down_revision = "0078_grok_video_model_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_configs") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.drop_index("uq_model_configs_use_model_id")
        batch.create_index("ix_model_configs_deleted_at", ["deleted_at"], unique=False)
    op.create_index(
        "uq_model_configs_use_model_id",
        "model_configs",
        ["use", "model_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT use, model_id FROM model_configs "
            "GROUP BY use, model_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "0079 downgrade blocked: deleted model identifiers have been reused; "
            "remove duplicates before downgrade"
        )
    with op.batch_alter_table("model_configs") as batch:
        batch.drop_index("uq_model_configs_use_model_id")
        batch.drop_index("ix_model_configs_deleted_at")
        batch.drop_column("deleted_at")
        batch.create_index(
            "uq_model_configs_use_model_id",
            ["use", "model_id"],
            unique=True,
        )
