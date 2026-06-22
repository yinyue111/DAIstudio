"""add soft takedown state for generated assets

Revision ID: 0019_asset_moderation_status
Revises: 0018_asset_reports
Create Date: 2026-06-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0019_asset_moderation_status"
down_revision = "0018_asset_reports"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _check_names(table: str) -> set[str]:
    try:
        return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table)}
    except NotImplementedError:
        return set()


def upgrade() -> None:
    existing = _column_names("gen_assets")
    checks = _check_names("gen_assets")
    with op.batch_alter_table("gen_assets") as batch:
        if "moderation_status" not in existing:
            batch.add_column(
                sa.Column(
                    "moderation_status",
                    sa.String(length=16),
                    nullable=False,
                    server_default="active",
                )
            )
        if "ck_gen_assets_moderation_status_valid" not in checks:
            batch.create_check_constraint(
                "ck_gen_assets_moderation_status_valid",
                "moderation_status in ('active', 'takedown')",
            )
    with op.batch_alter_table("gen_assets") as batch:
        if "moderation_status" in _column_names("gen_assets"):
            batch.alter_column("moderation_status", server_default=None)


def downgrade() -> None:
    columns = _column_names("gen_assets")
    checks = _check_names("gen_assets")
    with op.batch_alter_table("gen_assets") as batch:
        if "ck_gen_assets_moderation_status_valid" in checks:
            batch.drop_constraint("ck_gen_assets_moderation_status_valid", type_="check")
        if "moderation_status" in columns:
            batch.drop_column("moderation_status")
