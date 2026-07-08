"""set default image generation count to one

Revision ID: 0029_default_image_n_one
Revises: 0028_image_rendering_phase
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_default_image_n_one"
down_revision = "0028_image_rendering_phase"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "app_settings" not in _tables():
        return
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("""
            UPDATE app_settings
            SET value = '{"v": 1}'::jsonb
            WHERE key = 'image_n'
              AND value->>'v' = '4'
        """))
        return
    bind.execute(sa.text("""
        UPDATE app_settings
        SET value = '{"v": 1}'
        WHERE key = 'image_n'
          AND (
            json_extract(value, '$.v') = 4
            OR json_extract(value, '$.v') = '4'
          )
    """))


def downgrade() -> None:
    # Do not rewrite image_n back to 4 on rollback: after this migration runs,
    # an admin may legitimately choose 1 as the current production default.
    pass
