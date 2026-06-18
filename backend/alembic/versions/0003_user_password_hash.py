"""add users.password_hash for existing local databases

Revision ID: 0003_user_password_hash
Revises: 0002_account_favorite
Create Date: 2026-06-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_user_password_hash"
down_revision = "0002_account_favorite"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "password_hash" not in _columns(insp, "users"):
        op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
