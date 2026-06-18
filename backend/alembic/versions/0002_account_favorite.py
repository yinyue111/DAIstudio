"""add users.token_version + gen_assets.favorite

Defensive: only adds columns that are missing, so it is a no-op on a fresh DB
whose 0001 already created the full current schema.

Revision ID: 0002_account_favorite
Revises: 0001_initial
Create Date: 2026-06-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_account_favorite"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "token_version" not in _columns(insp, "users"):
        op.add_column("users", sa.Column("token_version", sa.Integer(),
                                          nullable=False, server_default="0"))
    if "favorite" not in _columns(insp, "gen_assets"):
        op.add_column("gen_assets", sa.Column("favorite", sa.Boolean(),
                                               nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("gen_assets", "favorite")
    op.drop_column("users", "token_version")
