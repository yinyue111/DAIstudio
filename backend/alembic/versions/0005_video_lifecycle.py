"""add gen_tasks.phase + external_submitted_at (recoverable video lifecycle)

Defensive: only adds columns that are missing.

Revision ID: 0005_video_lifecycle
Revises: 0004_gateway_calls
Create Date: 2026-06-17
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_video_lifecycle"
down_revision = "0004_gateway_calls"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = _columns(insp, "gen_tasks")
    if "phase" not in cols:
        op.add_column("gen_tasks", sa.Column("phase", sa.String(length=16), nullable=True))
    if "external_submitted_at" not in cols:
        op.add_column(
            "gen_tasks",
            sa.Column("external_submitted_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("gen_tasks", "external_submitted_at")
    op.drop_column("gen_tasks", "phase")
