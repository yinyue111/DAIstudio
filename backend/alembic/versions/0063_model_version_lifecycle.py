"""add draft/publish/disable/retire lifecycle to model versions

Revision ID: 0063_model_version_lifecycle
Revises: 0062_gen_assets_task_nullable
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0063_model_version_lifecycle"
down_revision = "0062_gen_assets_task_nullable"
branch_labels = None
depends_on = None

_TABLES = ("model_capability_versions", "model_price_versions")
_SOURCE_FK_NAMES = {
    "model_capability_versions": "fk_capability_versions_source",
    "model_price_versions": "fk_price_versions_source",
}


def _check_name(table: str, suffix: str) -> str:
    return f"ck_{table}_{suffix}"


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "status",
                    sa.String(length=16),
                    nullable=False,
                    server_default="published",
                )
            )
            batch_op.add_column(
                sa.Column(
                    "source_version_id",
                    sa.BigInteger(),
                    sa.ForeignKey(
                        f"{table}.id",
                        name=_SOURCE_FK_NAMES[table],
                        ondelete="SET NULL",
                    ),
                    nullable=True,
                )
            )
            batch_op.add_column(
                sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch_op.add_column(
                sa.Column(
                    "updated_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.now(),
                )
            )
            batch_op.alter_column(
                "activated_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=True,
                existing_server_default=sa.func.now(),
                server_default=None,
            )

        op.execute(
            sa.text(
                f"UPDATE {table} "
                "SET status = 'disabled', disabled_at = retired_at, retired_at = NULL "
                "WHERE NOT is_active"
            )
        )

        with op.batch_alter_table(table) as batch_op:
            batch_op.create_index(
                f"ix_{table}_source_version_id",
                ["source_version_id"],
                unique=False,
            )
            batch_op.create_check_constraint(
                _check_name(table, "status_valid"),
                "status in ('draft', 'published', 'disabled', 'retired')",
            )
            batch_op.create_check_constraint(
                _check_name(table, "active_status"),
                "(is_active AND status = 'published') OR "
                "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            )


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(_check_name(table, "active_status"), type_="check")
            batch_op.drop_constraint(_check_name(table, "status_valid"), type_="check")
            batch_op.drop_index(f"ix_{table}_source_version_id")

        op.execute(
            sa.text(
                f"UPDATE {table} "
                "SET activated_at = COALESCE(activated_at, created_at, CURRENT_TIMESTAMP), "
                "retired_at = CASE WHEN is_active THEN NULL "
                "ELSE COALESCE(retired_at, disabled_at, updated_at, created_at, CURRENT_TIMESTAMP) END"
            )
        )

        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "activated_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
                existing_server_default=None,
                server_default=sa.func.now(),
            )
            batch_op.drop_column("updated_at")
            batch_op.drop_column("disabled_at")
            batch_op.drop_column("source_version_id")
            batch_op.drop_column("status")
