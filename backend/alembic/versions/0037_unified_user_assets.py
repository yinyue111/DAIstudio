"""add unified user asset metadata

Revision ID: 0037_unified_user_assets
Revises: 0036_multi_model_catalog
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "0037_unified_user_assets"
down_revision = "0036_multi_model_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("uploaded_assets") as batch:
        batch.add_column(sa.Column("duration", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "favorite",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("retained_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_uploaded_assets_duration_nonnegative",
            "duration IS NULL OR duration >= 0",
        )
        batch.create_index(
            "ix_uploaded_assets_user_created",
            ["user_id", "created_at"],
        )
        batch.create_index(
            "ix_uploaded_assets_user_favorite_created",
            ["user_id", "favorite", "created_at"],
        )
        batch.create_index(
            "ix_uploaded_assets_user_retained",
            ["user_id", "retained_at"],
        )

    with op.batch_alter_table("gen_assets") as batch:
        batch.add_column(sa.Column("bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("retained_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_gen_assets_bytes_nonnegative",
            "bytes IS NULL OR bytes >= 0",
        )
        batch.create_index("ix_gen_assets_user_created", ["user_id", "created_at"])
        batch.create_index(
            "ix_gen_assets_user_favorite_created",
            ["user_id", "favorite", "created_at"],
        )
        batch.create_index(
            "ix_gen_assets_user_retained",
            ["user_id", "retained_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("gen_assets") as batch:
        batch.drop_index("ix_gen_assets_user_retained")
        batch.drop_index("ix_gen_assets_user_favorite_created")
        batch.drop_index("ix_gen_assets_user_created")
        batch.drop_constraint("ck_gen_assets_bytes_nonnegative", type_="check")
        batch.drop_column("retained_at")
        batch.drop_column("bytes")

    with op.batch_alter_table("uploaded_assets") as batch:
        batch.drop_index("ix_uploaded_assets_user_retained")
        batch.drop_index("ix_uploaded_assets_user_favorite_created")
        batch.drop_index("ix_uploaded_assets_user_created")
        batch.drop_constraint("ck_uploaded_assets_duration_nonnegative", type_="check")
        batch.drop_column("retained_at")
        batch.drop_column("favorite")
        batch.drop_column("duration")
