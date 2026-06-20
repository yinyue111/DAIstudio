"""add generation client request id

Revision ID: 0013_gen_task_client_request_id
Revises: 0012_payment_trade_no_unique
Create Date: 2026-06-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0013_gen_task_client_request_id"
down_revision = "0012_payment_trade_no_unique"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_gen_tasks_user_client_request_id"


def _column_names(insp, table: str) -> set[str]:
    return {col["name"] for col in insp.get_columns(table)}


def _index_names(insp, table: str) -> set[str]:
    return {idx["name"] for idx in insp.get_indexes(table) if idx.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gen_tasks" not in set(insp.get_table_names()):
        return
    columns = _column_names(insp, "gen_tasks")
    if "client_request_id" not in columns:
        op.add_column("gen_tasks", sa.Column("client_request_id", sa.String(length=128), nullable=True))
        insp = sa.inspect(bind)
    if INDEX_NAME in _index_names(insp, "gen_tasks"):
        return
    dialect = bind.dialect.name
    kwargs = {}
    where = sa.text("client_request_id IS NOT NULL")
    if dialect == "postgresql":
        kwargs["postgresql_where"] = where
    elif dialect == "sqlite":
        kwargs["sqlite_where"] = where
    else:
        return
    op.create_index(
        INDEX_NAME,
        "gen_tasks",
        ["user_id", "client_request_id"],
        unique=True,
        **kwargs,
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gen_tasks" not in set(insp.get_table_names()):
        return
    if INDEX_NAME in _index_names(insp, "gen_tasks"):
        op.drop_index(INDEX_NAME, table_name="gen_tasks")
    if "client_request_id" in _column_names(insp, "gen_tasks"):
        op.drop_column("gen_tasks", "client_request_id")
