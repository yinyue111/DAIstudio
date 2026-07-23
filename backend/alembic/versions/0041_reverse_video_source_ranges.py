"""add multi-segment source ranges to reverse operations

Revision ID: 0041_reverse_video_source_ranges
Revises: 0040_reverse_workflow_v3
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0041_reverse_video_source_ranges"
down_revision = "0040_reverse_workflow_v3"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if "reverse_operations" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("reverse_operations")}


def upgrade() -> None:
    columns = _columns()
    if not columns or "source_ranges" in columns:
        return
    with op.batch_alter_table("reverse_operations") as batch:
        batch.add_column(sa.Column("source_ranges", JSON_TYPE, nullable=True))
    op.get_bind().execute(sa.text(
        "UPDATE reverse_operations SET source_ranges = "
        "CASE WHEN source_range IS NULL THEN '[]' ELSE json_array(source_range) END "
        "WHERE source_ranges IS NULL"
    )) if op.get_bind().dialect.name == "sqlite" else op.get_bind().execute(sa.text(
        "UPDATE reverse_operations SET source_ranges = "
        "CASE WHEN source_range IS NULL THEN '[]'::jsonb ELSE jsonb_build_array(source_range) END "
        "WHERE source_ranges IS NULL"
    ))


def downgrade() -> None:
    if "source_ranges" not in _columns():
        return
    with op.batch_alter_table("reverse_operations") as batch:
        batch.drop_column("source_ranges")
