"""align reverse source fingerprints with PostgreSQL JSONB models

Revision ID: 0051_reverse_fingerprints_jsonb
Revises: 0050_model_routes
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0051_reverse_fingerprints_jsonb"
down_revision = "0050_model_routes"
branch_labels = None
depends_on = None


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    op.alter_column(
        "reverse_result_revisions",
        "source_fingerprints",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=True,
        postgresql_using='"source_fingerprints"::jsonb',
    )


def downgrade() -> None:
    if not _is_postgresql():
        return
    op.alter_column(
        "reverse_result_revisions",
        "source_fingerprints",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        existing_nullable=True,
        postgresql_using='"source_fingerprints"::json',
    )
