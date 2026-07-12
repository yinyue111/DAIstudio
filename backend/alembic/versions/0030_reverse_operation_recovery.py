"""make every reverse call recoverable

Revision ID: 0030_reverse_operation_recovery
Revises: 0029_default_image_n_one
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030_reverse_operation_recovery"
down_revision = "0029_default_image_n_one"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_reverse_operations_status_updated"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("reverse_operations")
    }


def upgrade() -> None:
    if "reverse_operations" not in _tables():
        return
    with op.batch_alter_table("reverse_operations") as batch:
        batch.alter_column(
            "client_request_id",
            existing_type=sa.String(length=128),
            nullable=True,
        )
    if INDEX_NAME not in _index_names():
        op.create_index(
            INDEX_NAME,
            "reverse_operations",
            ["status", "updated_at"],
        )


def downgrade() -> None:
    if "reverse_operations" not in _tables():
        return
    bind = op.get_bind()
    null_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM reverse_operations "
            "WHERE client_request_id IS NULL"
        )
    ).scalar_one()
    if int(null_count or 0) > 0:
        raise RuntimeError(
            "0030 downgrade blocked: reverse operations without client_request_id exist"
        )
    if INDEX_NAME in _index_names():
        op.drop_index(INDEX_NAME, table_name="reverse_operations")
    with op.batch_alter_table("reverse_operations") as batch:
        batch.alter_column(
            "client_request_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
