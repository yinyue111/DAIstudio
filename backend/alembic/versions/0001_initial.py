"""initial schema

Bootstraps the full schema from the ORM metadata so `alembic upgrade head`
works on a fresh database. Subsequent changes should be created with
`alembic revision --autogenerate -m "..."`.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-16
"""
from alembic import op

from app.db import Base
import app.models  # noqa: F401  (register all tables)

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
