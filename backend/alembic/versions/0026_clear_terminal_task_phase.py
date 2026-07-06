"""clear stale phase on succeeded generation tasks

Revision ID: 0026_clear_terminal_task_phase
Revises: 0025_reverse_operations
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_clear_terminal_task_phase"
down_revision = "0025_reverse_operations"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "gen_tasks" not in _tables():
        return
    op.execute(
        sa.text(
            "UPDATE gen_tasks SET phase = NULL "
            "WHERE status = 'succeeded' AND phase IS NOT NULL"
        )
    )


def downgrade() -> None:
    # Historical lifecycle phases cannot be reconstructed after terminal
    # success, so keep the corrected state on downgrade.
    return
