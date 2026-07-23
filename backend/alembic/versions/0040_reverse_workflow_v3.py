"""add the complete reverse workflow v3 contract

Revision ID: 0040_reverse_workflow_v3
Revises: 0039_margin_credit_pricing
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0040_reverse_workflow_v3"
down_revision = "0039_margin_credit_pricing"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _check_constraints(table: str) -> dict[str, str]:
    return {
        str(item.get("name") or ""): str(item.get("sqltext") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints(table)
    }


def upgrade() -> None:
    if "reverse_operations" not in _tables():
        return

    columns = _columns("reverse_operations")
    additions = (
        ("analysis_focus", sa.Column("analysis_focus", sa.String(32), nullable=False, server_default="comprehensive")),
        ("analysis_precision", sa.Column("analysis_precision", sa.String(16), nullable=False, server_default="standard")),
        ("output_purpose", sa.Column("output_purpose", sa.String(32), nullable=False, server_default="generation")),
        ("custom_instruction", sa.Column("custom_instruction", sa.String(500), nullable=True)),
        ("include_audio", sa.Column("include_audio", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("source_range", sa.Column("source_range", JSON_TYPE, nullable=True)),
        ("raw_provider_result", sa.Column("raw_provider_result", JSON_TYPE, nullable=True)),
        ("normalized_result", sa.Column("normalized_result", JSON_TYPE, nullable=True)),
        ("result_schema_version", sa.Column("result_schema_version", sa.String(32), nullable=False, server_default="reverse.v2")),
        ("applied_result_version", sa.Column("applied_result_version", sa.Integer(), nullable=True)),
        ("retry_of_operation_id", sa.Column("retry_of_operation_id", sa.BigInteger(), nullable=True)),
    )
    with op.batch_alter_table("reverse_operations") as batch:
        for name, column in additions:
            if name not in columns:
                batch.add_column(column)
        if "retry_of_operation_id" not in columns:
            batch.create_foreign_key(
                "fk_reverse_operations_retry_of",
                "reverse_operations",
                ["retry_of_operation_id"],
                ["id"],
            )

    indexes = _indexes("reverse_operations")
    if "ix_reverse_operations_retry_of_operation_id" not in indexes:
        op.create_index(
            "ix_reverse_operations_retry_of_operation_id",
            "reverse_operations",
            ["retry_of_operation_id"],
        )
    if "ix_reverse_operations_user_target_created" not in indexes:
        op.create_index(
            "ix_reverse_operations_user_target_created",
            "reverse_operations",
            ["user_id", "target", "created_at"],
        )

    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE reverse_operations SET normalized_result = result "
        "WHERE status = 'succeeded' AND normalized_result IS NULL"
    ))

    if "reverse_result_revisions" not in _tables():
        op.create_table(
            "reverse_result_revisions",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("operation_id", sa.BigInteger(), sa.ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("payload", JSON_TYPE, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint(
                "source in ('provider_raw', 'normalized', 'user_edit', 'applied', 'generation')",
                name="ck_reverse_result_revisions_source_valid",
            ),
        )
        op.create_index(
            "uq_reverse_result_revisions_operation_version",
            "reverse_result_revisions",
            ["operation_id", "version"],
            unique=True,
        )
        op.create_index(
            "ix_reverse_result_revisions_operation_id",
            "reverse_result_revisions",
            ["operation_id"],
        )
        op.create_index(
            "ix_reverse_result_revisions_user_created",
            "reverse_result_revisions",
            ["user_id", "created_at"],
        )

    if "reverse_result_revisions" in _tables():
        constraint_name = "ck_reverse_result_revisions_source_valid"
        constraints = _check_constraints("reverse_result_revisions")
        current_sql = constraints.get(constraint_name, "")
        if "provider_raw" not in current_sql:
            with op.batch_alter_table("reverse_result_revisions") as batch:
                if constraint_name in constraints:
                    batch.drop_constraint(constraint_name, type_="check")
                batch.create_check_constraint(
                    constraint_name,
                    "source in ('provider_raw', 'normalized', 'user_edit', 'applied', 'generation')",
                )

    if "reverse_operation_feedback" not in _tables():
        op.create_table(
            "reverse_operation_feedback",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("operation_id", sa.BigInteger(), sa.ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("rating", sa.String(16), nullable=False),
            sa.Column("issue_types", JSON_TYPE, nullable=False),
            sa.Column("note", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint(
                "rating in ('useful', 'not_useful')",
                name="ck_reverse_operation_feedback_rating_valid",
            ),
        )
        op.create_index(
            "uq_reverse_operation_feedback_operation",
            "reverse_operation_feedback",
            ["operation_id"],
            unique=True,
        )
        op.create_index(
            "ix_reverse_operation_feedback_user_updated",
            "reverse_operation_feedback",
            ["user_id", "updated_at"],
        )

    if "creation_recipes" not in _tables():
        op.create_table(
            "creation_recipes",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_operation_id", sa.BigInteger(), sa.ForeignKey("reverse_operations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("title", sa.String(128), nullable=False),
            sa.Column("category", sa.String(16), nullable=False),
            sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
            sa.Column("favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("cover_asset_url", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("category in ('image', 'video')", name="ck_creation_recipes_category_valid"),
            sa.CheckConstraint("visibility in ('private', 'public')", name="ck_creation_recipes_visibility_valid"),
            sa.CheckConstraint("current_version >= 1", name="ck_creation_recipes_version_positive"),
        )
        op.create_index("ix_creation_recipes_user_id", "creation_recipes", ["user_id"])
        op.create_index("ix_creation_recipes_source_operation_id", "creation_recipes", ["source_operation_id"])
        op.create_index("ix_creation_recipes_user_updated", "creation_recipes", ["user_id", "updated_at"])
        op.create_index("ix_creation_recipes_public_updated", "creation_recipes", ["visibility", "updated_at"])

    if "creation_recipe_versions" not in _tables():
        op.create_table(
            "creation_recipe_versions",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("recipe_id", sa.BigInteger(), sa.ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("schema_version", sa.String(32), nullable=False, server_default="creation-recipe.v1"),
            sa.Column("payload", JSON_TYPE, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_creation_recipe_versions_recipe_id", "creation_recipe_versions", ["recipe_id"])
        op.create_index(
            "uq_creation_recipe_versions_recipe_version",
            "creation_recipe_versions",
            ["recipe_id", "version"],
            unique=True,
        )

    if "reverse_result_revisions" in _tables():
        bind.execute(sa.text(
            "INSERT INTO reverse_result_revisions "
            "(operation_id, user_id, version, source, payload, created_at) "
            "SELECT id, user_id, 1, 'normalized', normalized_result, COALESCE(finished_at, updated_at, created_at) "
            "FROM reverse_operations "
            "WHERE status = 'succeeded' AND normalized_result IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM reverse_result_revisions r WHERE r.operation_id = reverse_operations.id)"
        ))


def downgrade() -> None:
    for table in (
        "creation_recipe_versions",
        "creation_recipes",
        "reverse_operation_feedback",
        "reverse_result_revisions",
    ):
        if table in _tables():
            op.drop_table(table)

    if "reverse_operations" not in _tables():
        return
    indexes = _indexes("reverse_operations")
    for index in (
        "ix_reverse_operations_user_target_created",
        "ix_reverse_operations_retry_of_operation_id",
    ):
        if index in indexes:
            op.drop_index(index, table_name="reverse_operations")

    columns = _columns("reverse_operations")
    with op.batch_alter_table("reverse_operations") as batch:
        if "retry_of_operation_id" in columns:
            batch.drop_constraint("fk_reverse_operations_retry_of", type_="foreignkey")
        for column in (
            "retry_of_operation_id",
            "applied_result_version",
            "result_schema_version",
            "normalized_result",
            "raw_provider_result",
            "source_range",
            "include_audio",
            "custom_instruction",
            "output_purpose",
            "analysis_precision",
            "analysis_focus",
        ):
            if column in columns:
                batch.drop_column(column)
