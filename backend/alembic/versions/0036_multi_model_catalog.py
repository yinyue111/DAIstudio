"""turn model configs into a multi-model catalog

Revision ID: 0036_multi_model_catalog
Revises: 0035_async_reverse_operations
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0036_multi_model_catalog"
down_revision = "0035_async_reverse_operations"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _add_model_reference(table: str) -> None:
    if table not in _tables() or "model_config_id" in _columns(table):
        return
    with op.batch_alter_table(table) as batch:
        batch.add_column(sa.Column("model_config_id", sa.BigInteger(), nullable=True))
        batch.create_foreign_key(
            f"fk_{table}_model_config_id_model_configs",
            "model_configs",
            ["model_config_id"],
            ["id"],
        )
        batch.create_index(f"ix_{table}_model_config_id", ["model_config_id"])


def upgrade() -> None:
    if "model_configs" not in _tables():
        return

    columns = _columns("model_configs")
    indexes = _indexes("model_configs")
    with op.batch_alter_table("model_configs") as batch:
        if "ix_model_configs_use" in indexes:
            batch.drop_index("ix_model_configs_use")
        if "display_name" not in columns:
            batch.add_column(sa.Column("display_name", sa.String(length=128), nullable=True))
        if "is_default" not in columns:
            batch.add_column(
                sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        if "sort_order" not in columns:
            batch.add_column(
                sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
            )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE model_configs SET display_name = model_id "
            "WHERE display_name IS NULL OR display_name = ''"
        )
    )
    # 0035 and earlier allowed exactly one row per use, so every legacy row is
    # the unambiguous default after the catalog migration.
    bind.execute(sa.text("UPDATE model_configs SET is_default = TRUE"))

    with op.batch_alter_table("model_configs") as batch:
        batch.alter_column(
            "display_name",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch.create_index("ix_model_configs_use", ["use"], unique=False)
        batch.create_index(
            "uq_model_configs_use_model_id",
            ["use", "model_id"],
            unique=True,
        )
    op.create_index(
        "uq_model_configs_default_per_use",
        "model_configs",
        ["use"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default = 1"),
    )

    for table in ("gen_tasks", "reverse_operations", "gateway_calls"):
        _add_model_reference(table)


def _drop_model_reference(table: str) -> None:
    if table not in _tables() or "model_config_id" not in _columns(table):
        return
    indexes = _indexes(table)
    with op.batch_alter_table(table) as batch:
        if f"ix_{table}_model_config_id" in indexes:
            batch.drop_index(f"ix_{table}_model_config_id")
        batch.drop_constraint(
            f"fk_{table}_model_config_id_model_configs",
            type_="foreignkey",
        )
        batch.drop_column("model_config_id")


def downgrade() -> None:
    if "model_configs" not in _tables():
        return
    duplicate_use = op.get_bind().execute(
        sa.text(
            "SELECT use FROM model_configs GROUP BY use HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate_use is not None:
        raise RuntimeError(
            "0036 downgrade blocked: multiple model configs exist for use "
            f"{duplicate_use!r}; disable or consolidate them before downgrade"
        )

    for table in ("gateway_calls", "reverse_operations", "gen_tasks"):
        _drop_model_reference(table)

    indexes = _indexes("model_configs")
    with op.batch_alter_table("model_configs") as batch:
        if "uq_model_configs_default_per_use" in indexes:
            batch.drop_index("uq_model_configs_default_per_use")
        if "uq_model_configs_use_model_id" in indexes:
            batch.drop_index("uq_model_configs_use_model_id")
        if "ix_model_configs_use" in indexes:
            batch.drop_index("ix_model_configs_use")
        batch.drop_column("sort_order")
        batch.drop_column("is_default")
        batch.drop_column("display_name")
        batch.create_index("ix_model_configs_use", ["use"], unique=True)
