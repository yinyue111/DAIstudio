"""add media projects and unified asset folders

Revision ID: 0043_projects_and_asset_folders
Revises: 0042_reverse_operation_batches
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043_projects_and_asset_folders"
down_revision = "0042_reverse_operation_batches"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "asset_folders" not in _tables():
        op.create_table(
            "asset_folders",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("parent_id", sa.BigInteger(), sa.ForeignKey("asset_folders.id", ondelete="CASCADE"), nullable=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("sort_order >= 0", name="ck_asset_folders_sort_order_nonnegative"),
        )
        op.create_index("ix_asset_folders_user_id", "asset_folders", ["user_id"])
        op.create_index("ix_asset_folders_parent_id", "asset_folders", ["parent_id"])
        op.create_index(
            "ix_asset_folders_user_parent_sort",
            "asset_folders",
            ["user_id", "parent_id", "sort_order"],
        )

    if "asset_folder_items" not in _tables():
        op.create_table(
            "asset_folder_items",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("folder_id", sa.BigInteger(), sa.ForeignKey("asset_folders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("asset_ref", sa.String(512), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_asset_folder_items_folder_id", "asset_folder_items", ["folder_id"])
        op.create_index("ix_asset_folder_items_user_id", "asset_folder_items", ["user_id"])
        op.create_index(
            "uq_asset_folder_items_user_asset",
            "asset_folder_items",
            ["user_id", "asset_ref"],
            unique=True,
        )
        op.create_index(
            "ix_asset_folder_items_folder_created",
            "asset_folder_items",
            ["folder_id", "created_at"],
        )

    if "media_projects" not in _tables():
        op.create_table(
            "media_projects",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(128), nullable=False),
            sa.Column("description", sa.String(1000), nullable=True),
            sa.Column("project_type", sa.String(16), nullable=False, server_default="mixed"),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("cover_asset_ref", sa.String(512), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("project_type in ('image', 'video', 'mixed')", name="ck_media_projects_type_valid"),
            sa.CheckConstraint("status in ('active', 'archived')", name="ck_media_projects_status_valid"),
        )
        op.create_index("ix_media_projects_user_id", "media_projects", ["user_id"])
        op.create_index(
            "ix_media_projects_user_status_updated",
            "media_projects",
            ["user_id", "status", "updated_at"],
        )

    if "media_project_assets" not in _tables():
        op.create_table(
            "media_project_assets",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("asset_ref", sa.String(512), nullable=False),
            sa.Column("role", sa.String(32), nullable=False, server_default="source"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("note", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("sort_order >= 0", name="ck_media_project_assets_sort_order_nonnegative"),
        )
        op.create_index("ix_media_project_assets_project_id", "media_project_assets", ["project_id"])
        op.create_index(
            "uq_media_project_assets_project_asset",
            "media_project_assets",
            ["project_id", "asset_ref"],
            unique=True,
        )
        op.create_index(
            "ix_media_project_assets_project_sort",
            "media_project_assets",
            ["project_id", "sort_order", "created_at"],
        )

    if "media_project_recipes" not in _tables():
        op.create_table(
            "media_project_recipes",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("recipe_id", sa.BigInteger(), sa.ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_media_project_recipes_project_id", "media_project_recipes", ["project_id"])
        op.create_index("ix_media_project_recipes_recipe_id", "media_project_recipes", ["recipe_id"])
        op.create_index(
            "uq_media_project_recipes_project_recipe",
            "media_project_recipes",
            ["project_id", "recipe_id"],
            unique=True,
        )

    if "media_project_tasks" not in _tables():
        op.create_table(
            "media_project_tasks",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_kind", sa.String(16), nullable=False),
            sa.Column("task_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("task_kind in ('generation', 'reverse', 'parse')", name="ck_media_project_tasks_kind_valid"),
        )
        op.create_index("ix_media_project_tasks_project_id", "media_project_tasks", ["project_id"])
        op.create_index(
            "uq_media_project_tasks_project_kind_task",
            "media_project_tasks",
            ["project_id", "task_kind", "task_id"],
            unique=True,
        )


def downgrade() -> None:
    tables = _tables()
    for table in (
        "media_project_tasks",
        "media_project_recipes",
        "media_project_assets",
        "media_projects",
        "asset_folder_items",
        "asset_folders",
    ):
        if table in tables:
            op.drop_table(table)
