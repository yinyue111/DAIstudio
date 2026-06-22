"""add asset reports for moderation workflow

Revision ID: 0018_asset_reports
Revises: 0017_structured_credit_ledger
Create Date: 2026-06-21
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_asset_reports"
down_revision = "0017_structured_credit_ledger"
branch_labels = None
depends_on = None

BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)}


def _check_names(table: str) -> set[str]:
    return {ck["name"] for ck in sa.inspect(op.get_bind()).get_check_constraints(table)}


def _add_missing_columns() -> None:
    existing = _column_names("asset_reports")
    additions = [
        ("asset_id", sa.Column("asset_id", sa.BigInteger(), sa.ForeignKey("gen_assets.id"), nullable=True)),
        ("reporter_user_id", sa.Column("reporter_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False)),
        ("owner_user_id", sa.Column("owner_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True)),
        ("reason", sa.Column("reason", sa.String(length=16), nullable=False)),
        ("note", sa.Column("note", sa.String(length=500), nullable=True)),
        ("status", sa.Column("status", sa.String(length=16), nullable=False, server_default="open")),
        ("handled_by", sa.Column("handled_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True)),
        ("handle_note", sa.Column("handle_note", sa.String(length=500), nullable=True)),
        ("handled_at", sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True)),
        ("created_at", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)),
    ]
    with op.batch_alter_table("asset_reports") as batch:
        for name, column in additions:
            if name not in existing:
                batch.add_column(column)


def _create_missing_checks() -> None:
    existing = _check_names("asset_reports")
    with op.batch_alter_table("asset_reports") as batch:
        if "ck_asset_reports_status_valid" not in existing:
            batch.create_check_constraint(
                "ck_asset_reports_status_valid",
                "status in ('open', 'dismissed', 'takedown')",
            )
        if "ck_asset_reports_reason_valid" not in existing:
            batch.create_check_constraint(
                "ck_asset_reports_reason_valid",
                "reason in ('copyright', 'sensitive', 'illegal', 'privacy', 'other')",
            )


def _create_missing_indexes() -> None:
    existing = _index_names("asset_reports")
    if "ix_asset_reports_asset_id" not in existing:
        op.create_index("ix_asset_reports_asset_id", "asset_reports", ["asset_id"])
    if "ix_asset_reports_reporter_user_id" not in existing:
        op.create_index("ix_asset_reports_reporter_user_id", "asset_reports", ["reporter_user_id"])
    if "ix_asset_reports_owner_user_id" not in existing:
        op.create_index("ix_asset_reports_owner_user_id", "asset_reports", ["owner_user_id"])
    if "ix_asset_reports_status_created" not in existing:
        op.create_index("ix_asset_reports_status_created", "asset_reports", ["status", "created_at"])


def upgrade() -> None:
    if "asset_reports" in _table_names():
        _add_missing_columns()
        _create_missing_checks()
        _create_missing_indexes()
        return

    op.create_table(
        "asset_reports",
        sa.Column("id", BigIntPK, primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.BigInteger(), sa.ForeignKey("gen_assets.id"), nullable=True),
        sa.Column("reporter_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("handled_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("handle_note", sa.String(length=500), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status in ('open', 'dismissed', 'takedown')",
            name="ck_asset_reports_status_valid",
        ),
        sa.CheckConstraint(
            "reason in ('copyright', 'sensitive', 'illegal', 'privacy', 'other')",
            name="ck_asset_reports_reason_valid",
        ),
    )
    op.create_index("ix_asset_reports_asset_id", "asset_reports", ["asset_id"])
    op.create_index("ix_asset_reports_reporter_user_id", "asset_reports", ["reporter_user_id"])
    op.create_index("ix_asset_reports_owner_user_id", "asset_reports", ["owner_user_id"])
    op.create_index("ix_asset_reports_status_created", "asset_reports", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_asset_reports_status_created", table_name="asset_reports")
    op.drop_index("ix_asset_reports_owner_user_id", table_name="asset_reports")
    op.drop_index("ix_asset_reports_reporter_user_id", table_name="asset_reports")
    op.drop_index("ix_asset_reports_asset_id", table_name="asset_reports")
    op.drop_table("asset_reports")
