"""add unique provider trade number index

Revision ID: 0012_payment_trade_no_unique
Revises: 0011_final_needs_review
Create Date: 2026-06-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_payment_trade_no_unique"
down_revision = "0011_final_needs_review"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_payment_orders_provider_trade_no"


def _index_names(insp, table):
    return {idx["name"] for idx in insp.get_indexes(table) if idx.get("name")}


def _fail_on_duplicate_trade_no(bind, dialect: str) -> None:
    if dialect == "postgresql":
        id_expr = "string_agg(CAST(id AS TEXT), ',')"
    else:
        id_expr = "group_concat(id, ',')"
    rows = bind.execute(sa.text(f"""
        SELECT provider, provider_trade_no, {id_expr} AS order_ids, COUNT(*) AS n
        FROM payment_orders
        WHERE provider_trade_no IS NOT NULL AND provider_trade_no != ''
        GROUP BY provider, provider_trade_no
        HAVING COUNT(*) > 1
        LIMIT 20
    """)).mappings().all()
    if not rows:
        return
    detail = "; ".join(
        f"provider={r['provider']}, trade_no={r['provider_trade_no']}, order_ids={r['order_ids']}"
        for r in rows
    )
    raise RuntimeError(
        "迁移 0012 无法创建支付流水唯一索引:存在重复三方支付流水,请先人工处理后重试: "
        f"{detail}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "payment_orders" not in set(insp.get_table_names()):
        return
    dialect = bind.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        return
    bind.execute(sa.text("""
        UPDATE payment_orders
        SET provider_trade_no = NULL
        WHERE provider_trade_no = ''
    """))
    _fail_on_duplicate_trade_no(bind, dialect)
    if INDEX_NAME in _index_names(insp, "payment_orders"):
        return
    kwargs = {}
    where = sa.text("provider_trade_no IS NOT NULL")
    if dialect == "postgresql":
        kwargs["postgresql_where"] = where
    elif dialect == "sqlite":
        kwargs["sqlite_where"] = where
    op.create_index(
        INDEX_NAME,
        "payment_orders",
        ["provider", "provider_trade_no"],
        unique=True,
        **kwargs,
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "payment_orders" in set(insp.get_table_names()) and INDEX_NAME in _index_names(
        insp, "payment_orders"
    ):
        op.drop_index(INDEX_NAME, table_name="payment_orders")
