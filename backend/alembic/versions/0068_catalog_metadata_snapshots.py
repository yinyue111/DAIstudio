"""version catalog definition metadata snapshots

Revision ID: 0068_catalog_metadata_snapshots
Revises: 0067_recipe_revision_governance
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0068_catalog_metadata_snapshots"
down_revision = "0067_recipe_revision_governance"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "model_capability_versions",
        sa.Column(
            "metadata_snapshot",
            JSON_TYPE,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "tool_versions",
        sa.Column(
            "metadata_snapshot",
            JSON_TYPE,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    bind = op.get_bind()
    models = sa.table(
        "model_configs",
        sa.column("id", sa.BigInteger()),
        sa.column("model_id", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("is_default", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
    )
    capability_versions = sa.table(
        "model_capability_versions",
        sa.column("model_config_id", sa.BigInteger()),
        sa.column("metadata_snapshot", JSON_TYPE),
    )
    for row in bind.execute(sa.select(models)).mappings():
        bind.execute(
            sa.update(capability_versions)
            .where(capability_versions.c.model_config_id == int(row["id"]))
            .values(
                metadata_snapshot={
                    "schema_version": "model-catalog-metadata.v1",
                    "origin": "legacy_backfill",
                    "model_id": row["model_id"],
                    "display_name": row["display_name"],
                    "is_default": bool(row["is_default"]),
                    "sort_order": int(row["sort_order"] or 0),
                    "enabled": bool(row["enabled"]),
                }
            )
        )

    tools = sa.table(
        "tool_definitions",
        sa.column("id", sa.BigInteger()),
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("category", sa.String()),
        sa.column("renderer", sa.String()),
        sa.column("entry_path", sa.String()),
        sa.column("icon", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
        sa.column("featured", sa.Boolean()),
    )
    tool_versions = sa.table(
        "tool_versions",
        sa.column("tool_definition_id", sa.BigInteger()),
        sa.column("metadata_snapshot", JSON_TYPE),
    )
    for row in bind.execute(sa.select(tools)).mappings():
        bind.execute(
            sa.update(tool_versions)
            .where(tool_versions.c.tool_definition_id == int(row["id"]))
            .values(
                metadata_snapshot={
                    "schema_version": "tool-catalog-metadata.v1",
                    "origin": "legacy_backfill",
                    "slug": row["slug"],
                    "name": row["name"],
                    "description": row["description"],
                    "category": row["category"],
                    "renderer": row["renderer"],
                    "entry_path": row["entry_path"],
                    "icon": row["icon"],
                    "sort_order": int(row["sort_order"] or 0),
                    "enabled": bool(row["enabled"]),
                    "featured": bool(row["featured"]),
                }
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_versions") as batch_op:
        batch_op.drop_column("metadata_snapshot")
    with op.batch_alter_table("model_capability_versions") as batch_op:
        batch_op.drop_column("metadata_snapshot")
