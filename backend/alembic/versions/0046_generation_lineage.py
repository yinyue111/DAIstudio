"""add transactional reverse-to-generation lineage

Revision ID: 0046_generation_lineage
Revises: 0045_tool_catalog
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0046_generation_lineage"
down_revision = "0045_tool_catalog"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {str(item.get("name") or "") for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _checks(table: str) -> dict[str, str]:
    return {
        str(item.get("name") or ""): str(item.get("sqltext") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints(table)
    }


def upgrade() -> None:
    tables = _tables()
    if "reverse_result_revisions" in tables:
        constraint_name = "ck_reverse_result_revisions_source_valid"
        checks = _checks("reverse_result_revisions")
        if "model_compiled" not in checks.get(constraint_name, ""):
            with op.batch_alter_table("reverse_result_revisions") as batch:
                if constraint_name in checks:
                    batch.drop_constraint(constraint_name, type_="check")
                batch.create_check_constraint(
                    constraint_name,
                    "source in ('provider_raw', 'normalized', 'user_edit', 'applied', "
                    "'model_compiled', 'generation')",
                )

    if "gen_tasks" not in tables:
        return

    columns = _columns("gen_tasks")
    checks = _checks("gen_tasks")
    additions = (
        (
            "reverse_operation_id",
            sa.Column("reverse_operation_id", sa.BigInteger(), nullable=True),
            "fk_gen_tasks_reverse_operation_id_reverse_operations",
            "reverse_operations",
        ),
        (
            "source_revision_id",
            sa.Column("source_revision_id", sa.BigInteger(), nullable=True),
            "fk_gen_tasks_source_revision_id_reverse_result_revisions",
            "reverse_result_revisions",
        ),
        (
            "compiled_revision_id",
            sa.Column("compiled_revision_id", sa.BigInteger(), nullable=True),
            "fk_gen_tasks_compiled_revision_id_reverse_result_revisions",
            "reverse_result_revisions",
        ),
        (
            "generation_revision_id",
            sa.Column("generation_revision_id", sa.BigInteger(), nullable=True),
            "fk_gen_tasks_generation_revision_id_reverse_result_revisions",
            "reverse_result_revisions",
        ),
    )
    with op.batch_alter_table("gen_tasks") as batch:
        for name, column, foreign_key_name, remote_table in additions:
            if name not in columns:
                batch.add_column(column)
                batch.create_foreign_key(
                    foreign_key_name,
                    remote_table,
                    [name],
                    ["id"],
                )
        if "ck_gen_tasks_reverse_lineage_complete" not in checks:
            batch.create_check_constraint(
                "ck_gen_tasks_reverse_lineage_complete",
                "(reverse_operation_id IS NULL AND source_revision_id IS NULL "
                "AND compiled_revision_id IS NULL AND generation_revision_id IS NULL) OR "
                "(reverse_operation_id IS NOT NULL AND source_revision_id IS NOT NULL "
                "AND compiled_revision_id IS NOT NULL AND generation_revision_id IS NOT NULL)",
            )

    indexes = _indexes("gen_tasks")
    for name, column in (
        ("ix_gen_tasks_reverse_operation_id", "reverse_operation_id"),
        ("ix_gen_tasks_source_revision_id", "source_revision_id"),
        ("ix_gen_tasks_compiled_revision_id", "compiled_revision_id"),
        ("ix_gen_tasks_generation_revision_id", "generation_revision_id"),
    ):
        if name not in indexes:
            op.create_index(name, "gen_tasks", [column])
    if "uq_gen_tasks_compiled_revision_id" not in indexes:
        op.create_index(
            "uq_gen_tasks_compiled_revision_id",
            "gen_tasks",
            ["compiled_revision_id"],
            unique=True,
            postgresql_where=sa.text("compiled_revision_id IS NOT NULL"),
            sqlite_where=sa.text("compiled_revision_id IS NOT NULL"),
        )
    if "uq_gen_tasks_generation_revision_id" not in indexes:
        op.create_index(
            "uq_gen_tasks_generation_revision_id",
            "gen_tasks",
            ["generation_revision_id"],
            unique=True,
            postgresql_where=sa.text("generation_revision_id IS NOT NULL"),
            sqlite_where=sa.text("generation_revision_id IS NOT NULL"),
        )


def downgrade() -> None:
    tables = _tables()
    if "gen_tasks" in tables:
        indexes = _indexes("gen_tasks")
        for name in (
            "uq_gen_tasks_generation_revision_id",
            "uq_gen_tasks_compiled_revision_id",
            "ix_gen_tasks_generation_revision_id",
            "ix_gen_tasks_compiled_revision_id",
            "ix_gen_tasks_source_revision_id",
            "ix_gen_tasks_reverse_operation_id",
        ):
            if name in indexes:
                op.drop_index(name, table_name="gen_tasks")

        columns = _columns("gen_tasks")
        checks = _checks("gen_tasks")
        with op.batch_alter_table("gen_tasks") as batch:
            if "ck_gen_tasks_reverse_lineage_complete" in checks:
                batch.drop_constraint("ck_gen_tasks_reverse_lineage_complete", type_="check")
            for name, _column, foreign_key_name, _remote_table in (
                (
                    "generation_revision_id",
                    None,
                    "fk_gen_tasks_generation_revision_id_reverse_result_revisions",
                    None,
                ),
                (
                    "compiled_revision_id",
                    None,
                    "fk_gen_tasks_compiled_revision_id_reverse_result_revisions",
                    None,
                ),
                (
                    "source_revision_id",
                    None,
                    "fk_gen_tasks_source_revision_id_reverse_result_revisions",
                    None,
                ),
                (
                    "reverse_operation_id",
                    None,
                    "fk_gen_tasks_reverse_operation_id_reverse_operations",
                    None,
                ),
            ):
                if name in columns:
                    batch.drop_constraint(foreign_key_name, type_="foreignkey")
                    batch.drop_column(name)

    if "reverse_result_revisions" in tables:
        op.get_bind().execute(
            sa.text("DELETE FROM reverse_result_revisions WHERE source = 'model_compiled'")
        )
        constraint_name = "ck_reverse_result_revisions_source_valid"
        checks = _checks("reverse_result_revisions")
        if "model_compiled" in checks.get(constraint_name, ""):
            with op.batch_alter_table("reverse_result_revisions") as batch:
                batch.drop_constraint(constraint_name, type_="check")
                batch.create_check_constraint(
                    constraint_name,
                    "source in ('provider_raw', 'normalized', 'user_edit', 'applied', 'generation')",
                )
