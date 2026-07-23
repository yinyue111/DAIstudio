"""add auditable generation requote retry chain

Revision ID: 0052_generation_retry_chain
Revises: 0051_reverse_fingerprints_jsonb
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0052_generation_retry_chain"
down_revision = "0051_reverse_fingerprints_jsonb"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_columns("gen_tasks")
    }


def _indexes() -> set[str]:
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_indexes("gen_tasks")
    }


def _checks() -> set[str]:
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints("gen_tasks")
    }


def upgrade() -> None:
    if "gen_tasks" not in sa.inspect(op.get_bind()).get_table_names():
        return
    columns = _columns()
    checks = _checks()
    with op.batch_alter_table("gen_tasks") as batch:
        if "retry_of_task_id" not in columns:
            batch.add_column(sa.Column("retry_of_task_id", sa.BigInteger(), nullable=True))
            batch.create_foreign_key(
                "fk_gen_tasks_retry_of_task_id_gen_tasks",
                "gen_tasks",
                ["retry_of_task_id"],
                ["id"],
            )
        if "ck_gen_tasks_retry_not_self" not in checks:
            batch.create_check_constraint(
                "ck_gen_tasks_retry_not_self",
                "retry_of_task_id IS NULL OR retry_of_task_id <> id",
            )
    if "ix_gen_tasks_retry_of_task_id" not in _indexes():
        op.create_index(
            "ix_gen_tasks_retry_of_task_id",
            "gen_tasks",
            ["retry_of_task_id"],
        )


def downgrade() -> None:
    if "gen_tasks" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if "ix_gen_tasks_retry_of_task_id" in _indexes():
        op.drop_index("ix_gen_tasks_retry_of_task_id", table_name="gen_tasks")
    columns = _columns()
    checks = _checks()
    with op.batch_alter_table("gen_tasks") as batch:
        if "ck_gen_tasks_retry_not_self" in checks:
            batch.drop_constraint("ck_gen_tasks_retry_not_self", type_="check")
        if "retry_of_task_id" in columns:
            batch.drop_constraint(
                "fk_gen_tasks_retry_of_task_id_gen_tasks",
                type_="foreignkey",
            )
            batch.drop_column("retry_of_task_id")
