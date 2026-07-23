"""add project archive policy and unified asset metadata

Revision ID: 0055_project_asset_governance
Revises: 0054_recipe_content_governance
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0055_project_asset_governance"
down_revision = "0054_recipe_content_governance"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table)
    }


def _checks(table: str) -> set[str]:
    return {
        str(check.get("name") or "")
        for check in sa.inspect(op.get_bind()).get_check_constraints(table)
    }


def upgrade() -> None:
    tables = _tables()
    if "media_projects" in tables:
        columns = _columns("media_projects")
        checks = _checks("media_projects")
        with op.batch_alter_table("media_projects") as batch:
            if "auto_archive_after_days" not in columns:
                batch.add_column(
                    sa.Column("auto_archive_after_days", sa.Integer(), nullable=True)
                )
            if "ck_media_projects_auto_archive_days_valid" not in checks:
                batch.create_check_constraint(
                    "ck_media_projects_auto_archive_days_valid",
                    "auto_archive_after_days IS NULL OR "
                    "(auto_archive_after_days >= 1 AND auto_archive_after_days <= 3650)",
                )

    if "user_asset_metadata" not in tables:
        op.create_table(
            "user_asset_metadata",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("asset_ref", sa.String(512), nullable=False),
            sa.Column("media_type", sa.String(8), nullable=False),
            sa.Column("tags", JSON_TYPE, nullable=False),
            sa.Column("content_sha256", sa.String(64), nullable=True),
            sa.Column("perceptual_hash", sa.String(16), nullable=True),
            sa.Column("perceptual_hash_algorithm", sa.String(32), nullable=True),
            sa.Column(
                "analysis_status",
                sa.String(16),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("analysis_error", sa.String(500), nullable=True),
            sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "media_type in ('image', 'video')",
                name="ck_user_asset_metadata_media_type_valid",
            ),
            sa.CheckConstraint(
                "analysis_status in ('pending', 'ready', 'degraded')",
                name="ck_user_asset_metadata_analysis_status_valid",
            ),
        )
        op.create_index(
            "ix_user_asset_metadata_user_id",
            "user_asset_metadata",
            ["user_id"],
        )
        op.create_index(
            "uq_user_asset_metadata_user_asset",
            "user_asset_metadata",
            ["user_id", "asset_ref"],
            unique=True,
        )
        op.create_index(
            "ix_user_asset_metadata_user_content_hash",
            "user_asset_metadata",
            ["user_id", "content_sha256"],
        )
        op.create_index(
            "ix_user_asset_metadata_user_perceptual_hash",
            "user_asset_metadata",
            ["user_id", "perceptual_hash"],
        )


def downgrade() -> None:
    tables = _tables()
    if "user_asset_metadata" in tables:
        op.drop_table("user_asset_metadata")
    if "media_projects" not in tables:
        return
    columns = _columns("media_projects")
    checks = _checks("media_projects")
    with op.batch_alter_table("media_projects") as batch:
        if "ck_media_projects_auto_archive_days_valid" in checks:
            batch.drop_constraint(
                "ck_media_projects_auto_archive_days_valid",
                type_="check",
            )
        if "auto_archive_after_days" in columns:
            batch.drop_column("auto_archive_after_days")
