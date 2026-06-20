"""enforce one active final video task per preview

Revision ID: 0009_unique_active_final_tasks
Revises: 0008_payment_upload_hardening
Create Date: 2026-06-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_unique_active_final_tasks"
down_revision = "0008_payment_upload_hardening"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_gen_tasks_active_final_per_preview"
WHERE_SQL = (
    "category = 'video' AND stage = 'final' "
    "AND status IN ('queued','running','needs_review') "
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
        WHERE {WHERE_SQL}
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
        "迁移 0009 无法创建 active final 唯一索引: "
        f"存在重复的 queued/running/needs_review final 视频任务,请先人工处理后重试: {detail}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gen_tasks" not in set(insp.get_table_names()):
        return
    if INDEX_NAME in _index_names(insp, "gen_tasks"):
        return
    dialect = bind.dialect.name
    kwargs = {}
    if dialect == "postgresql":
        kwargs["postgresql_where"] = sa.text(WHERE_SQL)
    elif dialect == "sqlite":
        kwargs["sqlite_where"] = sa.text(WHERE_SQL)
    else:
        return
    _fail_on_duplicate_active_finals(bind, dialect)
    op.create_index(
        INDEX_NAME,
        "gen_tasks",
        ["user_id", "parent_task_id"],
        unique=True,
        **kwargs,
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "gen_tasks" in set(insp.get_table_names()) and INDEX_NAME in _index_names(insp, "gen_tasks"):
        op.drop_index(INDEX_NAME, table_name="gen_tasks")
