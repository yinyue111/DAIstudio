"""add recipe moderation, sharing, and usage attribution

Revision ID: 0054_recipe_content_governance
Revises: 0053_tool_workflow_engine
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0054_recipe_content_governance"
down_revision = "0053_tool_workflow_engine"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _recipe_columns() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns("creation_recipes")
    }


def _recipe_indexes() -> set[str]:
    return {
        str(index.get("name") or "")
        for index in sa.inspect(op.get_bind()).get_indexes("creation_recipes")
    }


def _recipe_checks() -> set[str]:
    return {
        str(check.get("name") or "")
        for check in sa.inspect(op.get_bind()).get_check_constraints("creation_recipes")
    }


def upgrade() -> None:
    if "creation_recipes" not in _tables():
        return
    columns = _recipe_columns()
    checks = _recipe_checks()
    with op.batch_alter_table("creation_recipes") as batch:
        if "moderation_status" not in columns:
            batch.add_column(
                sa.Column(
                    "moderation_status",
                    sa.String(16),
                    nullable=False,
                    server_default="draft",
                )
            )
        if "approved_version" not in columns:
            batch.add_column(sa.Column("approved_version", sa.Integer(), nullable=True))
        if "submitted_at" not in columns:
            batch.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
        if "reviewed_at" not in columns:
            batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        if "reviewed_by" not in columns:
            batch.add_column(sa.Column("reviewed_by", sa.BigInteger(), nullable=True))
            batch.create_foreign_key(
                "fk_creation_recipes_reviewed_by_users",
                "users",
                ["reviewed_by"],
                ["id"],
                ondelete="SET NULL",
            )
        if "review_note" not in columns:
            batch.add_column(sa.Column("review_note", sa.String(500), nullable=True))
        if "ck_creation_recipes_moderation_status_valid" not in checks:
            batch.create_check_constraint(
                "ck_creation_recipes_moderation_status_valid",
                "moderation_status in ('draft', 'pending', 'approved', 'rejected')",
            )
        if "ck_creation_recipes_approved_version_positive" not in checks:
            batch.create_check_constraint(
                "ck_creation_recipes_approved_version_positive",
                "approved_version IS NULL OR approved_version >= 1",
            )

    indexes = _recipe_indexes()
    if "ix_creation_recipes_reviewed_by" not in indexes:
        op.create_index(
            "ix_creation_recipes_reviewed_by",
            "creation_recipes",
            ["reviewed_by"],
        )
    if "ix_creation_recipes_moderation_updated" not in indexes:
        op.create_index(
            "ix_creation_recipes_moderation_updated",
            "creation_recipes",
            ["moderation_status", "updated_at"],
        )

    op.execute(
        sa.text(
            "UPDATE creation_recipes "
            "SET moderation_status = 'approved', "
            "approved_version = current_version, "
            "submitted_at = COALESCE(submitted_at, updated_at), "
            "reviewed_at = COALESCE(reviewed_at, updated_at) "
            "WHERE visibility = 'public'"
        )
    )

    if "creation_recipe_shares" not in _tables():
        op.create_table(
            "creation_recipe_shares",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "recipe_id",
                sa.BigInteger(),
                sa.ForeignKey("creation_recipes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "owner_user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "version >= 1",
                name="ck_creation_recipe_shares_version_positive",
            ),
            sa.CheckConstraint(
                "status in ('active', 'revoked')",
                name="ck_creation_recipe_shares_status_valid",
            ),
        )
        op.create_index(
            "uq_creation_recipe_shares_slug",
            "creation_recipe_shares",
            ["slug"],
            unique=True,
        )
        op.create_index(
            "ix_creation_recipe_shares_recipe_status_created",
            "creation_recipe_shares",
            ["recipe_id", "status", "created_at"],
        )
        op.create_index(
            "ix_creation_recipe_shares_owner_created",
            "creation_recipe_shares",
            ["owner_user_id", "created_at"],
        )

    if "creation_recipe_usage_events" not in _tables():
        op.create_table(
            "creation_recipe_usage_events",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "recipe_id",
                sa.BigInteger(),
                sa.ForeignKey("creation_recipes.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("recipe_version", sa.Integer(), nullable=False),
            sa.Column(
                "user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("event_type", sa.String(24), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column(
                "share_id",
                sa.BigInteger(),
                sa.ForeignKey("creation_recipe_shares.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "derived_recipe_id",
                sa.BigInteger(),
                sa.ForeignKey("creation_recipes.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "generation_task_id",
                sa.BigInteger(),
                sa.ForeignKey("gen_tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("client_event_id", sa.String(128), nullable=True),
            sa.Column("context", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "event_type in ('apply', 'clone', 'generation_prepare', 'generation_submit')",
                name="ck_creation_recipe_usage_events_type_valid",
            ),
            sa.CheckConstraint(
                "source in ('owner', 'public', 'share')",
                name="ck_creation_recipe_usage_events_source_valid",
            ),
            sa.CheckConstraint(
                "recipe_version >= 1",
                name="ck_creation_recipe_usage_events_version_positive",
            ),
        )
        op.create_index(
            "uq_creation_recipe_usage_events_user_client",
            "creation_recipe_usage_events",
            ["user_id", "client_event_id"],
            unique=True,
        )
        op.create_index(
            "ix_creation_recipe_usage_events_recipe_created",
            "creation_recipe_usage_events",
            ["recipe_id", "created_at"],
        )
        op.create_index(
            "ix_creation_recipe_usage_events_generation_task",
            "creation_recipe_usage_events",
            ["generation_task_id"],
        )
        for column in ("user_id", "share_id", "derived_recipe_id"):
            op.create_index(
                f"ix_creation_recipe_usage_events_{column}",
                "creation_recipe_usage_events",
                [column],
            )


def downgrade() -> None:
    tables = _tables()
    if "creation_recipe_usage_events" in tables:
        op.drop_table("creation_recipe_usage_events")
    if "creation_recipe_shares" in tables:
        op.drop_table("creation_recipe_shares")
    if "creation_recipes" not in tables:
        return
    indexes = _recipe_indexes()
    if "ix_creation_recipes_moderation_updated" in indexes:
        op.drop_index(
            "ix_creation_recipes_moderation_updated",
            table_name="creation_recipes",
        )
    if "ix_creation_recipes_reviewed_by" in indexes:
        op.drop_index("ix_creation_recipes_reviewed_by", table_name="creation_recipes")
    columns = _recipe_columns()
    checks = _recipe_checks()
    with op.batch_alter_table("creation_recipes") as batch:
        if "ck_creation_recipes_approved_version_positive" in checks:
            batch.drop_constraint(
                "ck_creation_recipes_approved_version_positive",
                type_="check",
            )
        if "ck_creation_recipes_moderation_status_valid" in checks:
            batch.drop_constraint(
                "ck_creation_recipes_moderation_status_valid",
                type_="check",
            )
        if "reviewed_by" in columns:
            batch.drop_constraint(
                "fk_creation_recipes_reviewed_by_users",
                type_="foreignkey",
            )
        for column in (
            "review_note",
            "reviewed_by",
            "reviewed_at",
            "submitted_at",
            "approved_version",
            "moderation_status",
        ):
            if column in columns:
                batch.drop_column(column)
