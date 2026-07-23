"""allow workflow runs in media project tasks

Revision ID: 0058_workflow_project_tasks
Revises: 0057_storyboard_compose_tool
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0058_workflow_project_tasks"
down_revision = "0057_storyboard_compose_tool"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_media_project_tasks_kind_valid"


def _replace_kind_constraint(expression: str) -> None:
    checks = {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints("media_project_tasks")
    }
    with op.batch_alter_table("media_project_tasks") as batch:
        if _CONSTRAINT in checks:
            batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, expression)


def upgrade() -> None:
    _replace_kind_constraint(
        "task_kind in ('generation', 'reverse', 'parse', 'workflow')"
    )


def downgrade() -> None:
    # The previous schema cannot represent workflow links. The workflow runs
    # themselves remain intact; only their project associations are removed.
    op.execute(
        sa.text("DELETE FROM media_project_tasks WHERE task_kind = 'workflow'")
    )
    _replace_kind_constraint("task_kind in ('generation', 'reverse', 'parse')")
