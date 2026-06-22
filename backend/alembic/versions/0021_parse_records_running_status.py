"""allow running parse records for atomic worker claims

Revision ID: 0021_parse_records_running
Revises: 0020_model_use_index
Create Date: 2026-06-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0021_parse_records_running"
down_revision = "0020_model_use_index"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _check_names(table: str) -> set[str]:
    try:
        return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table)}
    except NotImplementedError:
        return set()


def _replace_status_check(condition: str) -> None:
    if "parse_records" not in _tables():
        return
    checks = _check_names("parse_records")
    with op.batch_alter_table("parse_records") as batch:
        if "ck_parse_records_status_valid" in checks:
            batch.drop_constraint("ck_parse_records_status_valid", type_="check")
        batch.create_check_constraint("ck_parse_records_status_valid", condition)


def upgrade() -> None:
    _replace_status_check("status in ('queued', 'running', 'done', 'failed')")


def downgrade() -> None:
    if "parse_records" in _tables():
        count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM parse_records WHERE status = 'running'")
        ).scalar()
        if count:
            raise RuntimeError(
                "0021_parse_records_running downgrade blocked: running parse records exist"
            )
    _replace_status_check("status in ('queued', 'done', 'failed')")
