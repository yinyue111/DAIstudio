"""add audit log query indexes

Revision ID: 0024_audit_log_indexes
Revises: 0023_events_cancel_prompt
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0024_audit_log_indexes"
down_revision = "0023_events_cancel_prompt"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)}


def _create_index_once(name: str, table: str, columns: list[str]) -> None:
    if table not in _tables() or name in _indexes(table):
        return
    op.create_index(name, table, columns)


def _drop_index_once(name: str, table: str) -> None:
    if table not in _tables() or name not in _indexes(table):
        return
    op.drop_index(name, table_name=table)


def upgrade() -> None:
    _create_index_once("ix_audit_logs_user_id_id", "audit_logs", ["user_id", "id"])
    _create_index_once("ix_audit_logs_action_id", "audit_logs", ["action", "id"])
    _create_index_once("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    _drop_index_once("ix_audit_logs_created_at", "audit_logs")
    _drop_index_once("ix_audit_logs_action_id", "audit_logs")
    _drop_index_once("ix_audit_logs_user_id_id", "audit_logs")
