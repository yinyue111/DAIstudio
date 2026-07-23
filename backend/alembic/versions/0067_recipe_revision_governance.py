"""preserve recipe metadata revisions and soft-delete history

Revision ID: 0067_recipe_revision_governance
Revises: 0066_version_source_fks
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0067_recipe_revision_governance"
down_revision = "0066_version_source_fks"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "creation_recipes",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_creation_recipes_deleted_at",
        "creation_recipes",
        ["deleted_at"],
    )
    op.add_column(
        "creation_recipe_versions",
        sa.Column(
            "metadata_snapshot",
            JSON_TYPE,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    bind = op.get_bind()
    recipes = sa.table(
        "creation_recipes",
        sa.column("id", sa.BigInteger()),
        sa.column("title", sa.String()),
        sa.column("visibility", sa.String()),
        sa.column("cover_asset_url", sa.Text()),
    )
    versions = sa.table(
        "creation_recipe_versions",
        sa.column("recipe_id", sa.BigInteger()),
        sa.column("metadata_snapshot", JSON_TYPE),
    )
    for row in bind.execute(
        sa.select(
            recipes.c.id,
            recipes.c.title,
            recipes.c.visibility,
            recipes.c.cover_asset_url,
        )
    ).mappings():
        bind.execute(
            sa.update(versions)
            .where(versions.c.recipe_id == int(row["id"]))
            .values(
                metadata_snapshot={
                    "schema_version": "creation-recipe-metadata.v1",
                    "origin": "legacy_backfill",
                    "title": row["title"],
                    "visibility": row["visibility"],
                    "cover_asset_url": row["cover_asset_url"],
                }
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    deleted_count = bind.scalar(
        sa.text("SELECT count(*) FROM creation_recipes WHERE deleted_at IS NOT NULL")
    )
    if int(deleted_count or 0):
        raise RuntimeError(
            "cannot downgrade recipe governance while soft-deleted recipes exist"
        )
    with op.batch_alter_table("creation_recipe_versions") as batch_op:
        batch_op.drop_column("metadata_snapshot")
    op.drop_index("ix_creation_recipes_deleted_at", table_name="creation_recipes")
    with op.batch_alter_table("creation_recipes") as batch_op:
        batch_op.drop_column("deleted_at")
