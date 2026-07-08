"""allow image rendering task phase

Revision ID: 0028_image_rendering_phase
Revises: 0027_user_drafts
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0028_image_rendering_phase"
down_revision = "0027_user_drafts"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _check_names(table: str) -> set[str]:
    try:
        return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table)}
    except NotImplementedError:
        return set()


def _replace_gen_task_phase_check(condition: str) -> None:
    if "gen_tasks" not in _tables():
        return
    checks = _check_names("gen_tasks")
    with op.batch_alter_table("gen_tasks") as batch:
        if "ck_gen_tasks_phase_valid" in checks:
            batch.drop_constraint("ck_gen_tasks_phase_valid", type_="check")
        batch.create_check_constraint("ck_gen_tasks_phase_valid", condition)


def upgrade() -> None:
    _replace_gen_task_phase_check(
        "phase IS NULL OR phase in ('rendering', 'submitting', 'polling', 'downloading', 'reconciling')"
    )


def downgrade() -> None:
    if "gen_tasks" in _tables():
        count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM gen_tasks WHERE phase = 'rendering'")
        ).scalar()
        if count:
            raise RuntimeError("0028 downgrade blocked: rendering phase tasks exist")
    _replace_gen_task_phase_check(
        "phase IS NULL OR phase in ('submitting', 'polling', 'downloading', 'reconciling')"
    )
