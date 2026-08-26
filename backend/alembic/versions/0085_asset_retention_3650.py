"""set the default asset retention period to 3650 days

Revision ID: 0085_asset_retention_3650
Revises: 0084_video_mode_constraints
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0085_asset_retention_3650"
down_revision = "0084_video_mode_constraints"
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
            SET value = '{"v": 3650}'::jsonb
            WHERE key = 'asset_retention_days'
              AND value->>'v' = '30'
        """))
        return
    bind.execute(sa.text("""
        UPDATE app_settings
        SET value = '{"v": 3650}'
        WHERE key = 'asset_retention_days'
          AND (
            json_extract(value, '$.v') = 30
            OR json_extract(value, '$.v') = '30'
          )
    """))


def downgrade() -> None:
    # Do not rewrite 3650 back to 30: an admin may have chosen 3650 after upgrade.
    pass
