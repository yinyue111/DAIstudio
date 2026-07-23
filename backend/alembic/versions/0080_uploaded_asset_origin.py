"""distinguish fetched assets from manual uploads

Revision ID: 0080_uploaded_asset_origin
Revises: 0079_model_config_soft_delete
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0080_uploaded_asset_origin"
down_revision = "0079_model_config_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("uploaded_assets") as batch:
        batch.add_column(
            sa.Column(
                "origin",
                sa.String(length=16),
                nullable=False,
                server_default="uploaded",
            )
        )
        batch.create_check_constraint(
            "ck_uploaded_assets_origin_valid",
            "origin in ('uploaded', 'fetched')",
        )
    op.execute(
        sa.text(
            "UPDATE uploaded_assets SET origin = 'fetched' "
            "WHERE original_filename IN "
            "('parsed-image.png', 'parsed-preview.png', 'parsed-model-ref.jpg')"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("uploaded_assets") as batch:
        batch.drop_constraint("ck_uploaded_assets_origin_valid", type_="check")
        batch.drop_column("origin")
