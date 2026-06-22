"""add structured credit ledger deltas

Revision ID: 0017_structured_credit_ledger
Revises: 0016_core_integrity_constraints
Create Date: 2026-06-21
"""

import re

import sqlalchemy as sa

from alembic import op

revision = "0017_structured_credit_ledger"
down_revision = "0016_core_integrity_constraints"
branch_labels = None
depends_on = None


_RESERVED_RE = re.compile(r"(?:^|\s)reserved=(\d+)")
_REAL_RE = re.compile(r"(?:^|\s)real=(\d+)")


def _column_names(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _check_names(table: str) -> set[str]:
    try:
        return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table)}
    except NotImplementedError:
        return set()


def _add_columns() -> None:
    existing = _column_names("credit_transactions")
    additions = [
        ("balance_delta", sa.Column("balance_delta", sa.BigInteger(), nullable=True)),
        ("frozen_delta", sa.Column("frozen_delta", sa.BigInteger(), nullable=True)),
        ("frozen_after", sa.Column("frozen_after", sa.BigInteger(), nullable=True)),
        ("reserved_amount", sa.Column("reserved_amount", sa.BigInteger(), nullable=True)),
        ("real_cost", sa.Column("real_cost", sa.BigInteger(), nullable=True)),
    ]
    with op.batch_alter_table("credit_transactions") as batch:
        for name, column in additions:
            if name not in existing:
                batch.add_column(column)


def _parse_amount(pattern: re.Pattern[str], note: str | None) -> int | None:
    if not note:
        return None
    match = pattern.search(note)
    if not match:
        return None
    return max(0, int(match.group(1)))


def _backfill_existing_rows() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT id, type, change, note, reserved_amount, real_cost,
                   balance_delta, frozen_delta
            FROM credit_transactions
            ORDER BY id
            """
        )
    ).mappings()
    for row in rows:
        change = int(row["change"] or 0)
        tx_type = str(row["type"] or "")
        reserved = row["reserved_amount"]
        real_cost = row["real_cost"]
        if tx_type == "settle":
            reserved = reserved if reserved is not None else _parse_amount(_RESERVED_RE, row["note"])
            real_cost = real_cost if real_cost is not None else _parse_amount(_REAL_RE, row["note"])
        if tx_type == "freeze":
            frozen_delta = -change
        elif tx_type == "settle":
            frozen_delta = -int(reserved or 0)
        elif tx_type == "refund" and change > 0:
            frozen_delta = -change
        else:
            frozen_delta = 0
        conn.execute(
            sa.text(
                """
                UPDATE credit_transactions
                SET balance_delta = COALESCE(balance_delta, :balance_delta),
                    frozen_delta = COALESCE(frozen_delta, :frozen_delta),
                    reserved_amount = COALESCE(reserved_amount, :reserved_amount),
                    real_cost = COALESCE(real_cost, :real_cost)
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "balance_delta": change,
                "frozen_delta": frozen_delta,
                "reserved_amount": reserved,
                "real_cost": real_cost,
            },
        )


def _create_checks() -> None:
    existing = _check_names("credit_transactions")
    with op.batch_alter_table("credit_transactions") as batch:
        if "ck_credit_transactions_frozen_after_nonnegative" not in existing:
            batch.create_check_constraint(
                "ck_credit_transactions_frozen_after_nonnegative",
                "frozen_after IS NULL OR frozen_after >= 0",
            )
        if "ck_credit_transactions_reserved_amount_nonnegative" not in existing:
            batch.create_check_constraint(
                "ck_credit_transactions_reserved_amount_nonnegative",
                "reserved_amount IS NULL OR reserved_amount >= 0",
            )
        if "ck_credit_transactions_real_cost_nonnegative" not in existing:
            batch.create_check_constraint(
                "ck_credit_transactions_real_cost_nonnegative",
                "real_cost IS NULL OR real_cost >= 0",
            )


def upgrade() -> None:
    _add_columns()
    _backfill_existing_rows()
    _create_checks()


def downgrade() -> None:
    columns = _column_names("credit_transactions")
    checks = _check_names("credit_transactions")
    with op.batch_alter_table("credit_transactions") as batch:
        if "ck_credit_transactions_real_cost_nonnegative" in checks:
            batch.drop_constraint("ck_credit_transactions_real_cost_nonnegative", type_="check")
        if "ck_credit_transactions_reserved_amount_nonnegative" in checks:
            batch.drop_constraint("ck_credit_transactions_reserved_amount_nonnegative", type_="check")
        if "ck_credit_transactions_frozen_after_nonnegative" in checks:
            batch.drop_constraint("ck_credit_transactions_frozen_after_nonnegative", type_="check")
        for name in ("real_cost", "reserved_amount", "frozen_after", "frozen_delta", "balance_delta"):
            if name in columns:
                batch.drop_column(name)
