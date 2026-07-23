"""allow unified generated assets without a generation task

Revision ID: 0062_gen_assets_task_nullable
Revises: 0061_paid_action_quotes
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0062_gen_assets_task_nullable"
down_revision = "0061_paid_action_quotes"
branch_labels = None
depends_on = None


def _table_recreate(bind) -> str:
    return "always" if bind.dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table(
        "gen_assets",
        recreate=_table_recreate(bind),
    ) as batch:
        batch.alter_column(
            "task_id",
            existing_type=sa.BigInteger(),
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    asset_ids = bind.execute(
        sa.text(
            "SELECT id FROM gen_assets "
            "WHERE task_id IS NULL ORDER BY id LIMIT 10"
        )
    ).scalars().all()
    if asset_ids:
        samples = ",".join(str(asset_id) for asset_id in asset_ids)
        raise RuntimeError(
            "0062_gen_assets_task_nullable downgrade blocked: "
            "gen_assets.task_id contains NULL rows, "
            f"asset_ids={samples}"
        )

    with op.batch_alter_table(
        "gen_assets",
        recreate=_table_recreate(bind),
    ) as batch:
        batch.alter_column(
            "task_id",
            existing_type=sa.BigInteger(),
            existing_nullable=True,
            nullable=False,
        )
