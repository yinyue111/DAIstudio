"""normalise model config use uniqueness to an index

Revision ID: 0020_model_use_index
Revises: 0019_asset_moderation_status
Create Date: 2026-06-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_model_use_index"
down_revision = "0019_asset_moderation_status"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)}


def _unique_constraints(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints(table)}


def upgrade() -> None:
    if "model_configs" not in _tables():
        return
    constraints = _unique_constraints("model_configs")
    indexes = _indexes("model_configs")
    with op.batch_alter_table("model_configs") as batch:
        if "model_configs_use_key" in constraints:
            batch.drop_constraint("model_configs_use_key", type_="unique")
        if "ix_model_configs_use" not in indexes:
            batch.create_index("ix_model_configs_use", ["use"], unique=True)


def downgrade() -> None:
    if "model_configs" not in _tables():
        return
    constraints = _unique_constraints("model_configs")
    indexes = _indexes("model_configs")
    with op.batch_alter_table("model_configs") as batch:
        if "ix_model_configs_use" in indexes:
            batch.drop_index("ix_model_configs_use")
        if "model_configs_use_key" not in constraints:
            batch.create_unique_constraint("model_configs_use_key", ["use"])
