"""include needs_review in active final video uniqueness

Revision ID: 0011_final_needs_review
Revises: 0010_admin_idempotency_keys
Create Date: 2026-06-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_final_needs_review"
down_revision = "0010_admin_idempotency_keys"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_gen_tasks_active_final_per_preview"
NEW_WHERE_SQL = (
    "category = 'video' AND stage = 'final' "
    "AND status IN ('queued','running','needs_review') "
    "AND parent_task_id IS NOT NULL"
)
OLD_WHERE_SQL = (
    "category = 'video' AND stage = 'final' "
    "AND status IN ('queued','running') "
    "AND parent_task_id IS NOT NULL"
)


def _index_names(insp, table):
    return {idx["name"] for idx in insp.get_indexes(table) if idx.get("name")}


def _fail_on_duplicate_active_finals(bind, dialect: str) -> None:
    if dialect == "postgresql":
        id_expr = "string_agg(CAST(id AS TEXT), ',')"
    else:
        id_expr = "group_concat(id, ',')"
    rows = bind.execute(sa.text(f"""
        SELECT user_id, parent_task_id, {id_expr} AS task_ids, COUNT(*) AS n
        FROM gen_tasks
        WHERE {NEW_WHERE_SQL}
        GROUP BY user_id, parent_task_id
        HAVING COUNT(*) > 1
        LIMIT 20
    """)).mappings().all()
    if not rows:
        return
    detail = "; ".join(
        f"user_id={r['user_id']}, parent_task_id={r['parent_task_id']}, task_ids={r['task_ids']}"
        for r in rows
    )
    raise RuntimeError(
        "迁移 0011 无法更新 active final 唯一索引: "
        f"存在重复的 queued/running/needs_review final 视频任务,请先人工处理后重试: {detail}"
    )


def _create_index(dialect: str) -> None:
    kwargs = {}
    if dialect == "postgresql":
        kwargs["postgresql_where"] = sa.text(NEW_WHERE_SQL)
    elif dialect == "sqlite":
        kwargs["sqlite_where"] = sa.text(NEW_WHERE_SQL)
    else:
        return
    op.create_index(
        INDEX_NAME,
        "gen_tasks",
        ["user_id", "parent_task_id"],
        unique=True,
        **kwargs,
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gen_tasks" not in set(insp.get_table_names()):
        return
    dialect = bind.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        return
    _fail_on_duplicate_active_finals(bind, dialect)
    if INDEX_NAME in _index_names(insp, "gen_tasks"):
        op.drop_index(INDEX_NAME, table_name="gen_tasks")
    _create_index(dialect)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gen_tasks" not in set(insp.get_table_names()):
        return
    dialect = bind.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        return
    if INDEX_NAME in _index_names(insp, "gen_tasks"):
        op.drop_index(INDEX_NAME, table_name="gen_tasks")
    kwargs = {}
    if dialect == "postgresql":
        kwargs["postgresql_where"] = sa.text(OLD_WHERE_SQL)
    elif dialect == "sqlite":
        kwargs["sqlite_where"] = sa.text(OLD_WHERE_SQL)
    op.create_index(
        INDEX_NAME,
        "gen_tasks",
        ["user_id", "parent_task_id"],
        unique=True,
        **kwargs,
    )
