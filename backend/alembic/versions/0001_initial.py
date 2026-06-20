"""initial schema

Frozen base schema for a fresh database. Later migrations stay defensive so old
local databases can still upgrade, but this revision must not import the live
ORM: doing so makes the same migration create different schemas as models drift.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-16
"""
import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON()
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("nickname", sa.String(length=64), nullable=True),
        sa.Column("avatar", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=True),
        sa.Column("department", sa.String(length=64), nullable=True),
        sa.Column("balance_credits", sa.BigInteger(), nullable=True),
        sa.Column("frozen_credits", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)

    op.create_table(
        "phone_whitelist",
        sa.Column("phone", sa.String(length=20), primary_key=True),
        sa.Column("note", sa.String(length=128), nullable=True),
        sa.Column("department", sa.String(length=64), nullable=True),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "credit_transactions",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("change", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("biz_type", sa.String(length=32), nullable=True),
        sa.Column("biz_ref", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_credit_transactions_user_id", "credit_transactions", ["user_id"])

    op.create_table(
        "parse_records",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("assets", JSON_TYPE, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cached_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_parse_records_user_id", "parse_records", ["user_id"])

    op.create_table(
        "gen_tasks",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("source_asset_url", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=8), nullable=True),
        sa.Column("category", sa.String(length=8), nullable=False),
        sa.Column("stage", sa.String(length=8), nullable=True),
        sa.Column("prompt", JSON_TYPE, nullable=True),
        sa.Column("model_use", sa.String(length=16), nullable=True),
        sa.Column("params", JSON_TYPE, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("cost_frozen", sa.BigInteger(), nullable=True),
        sa.Column("cost_settled", sa.BigInteger(), nullable=True),
        sa.Column("external_task_id", sa.Text(), nullable=True),
        sa.Column("parent_task_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_gen_tasks_user_id", "gen_tasks", ["user_id"])

    op.create_table(
        "gen_assets",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("gen_tasks.id"), nullable=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("type", sa.String(length=8), nullable=True),
        sa.Column("preview_url", sa.Text(), nullable=True),
        sa.Column("hd_url", sa.Text(), nullable=True),
        sa.Column("watermarked", sa.Boolean(), nullable=True),
        sa.Column("unlocked", sa.Boolean(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_gen_assets_task_id", "gen_assets", ["task_id"])
    op.create_index("ix_gen_assets_user_id", "gen_assets", ["user_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("biz_type", sa.String(length=32), nullable=True),
        sa.Column("biz_id", sa.BigInteger(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("detail", JSON_TYPE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "model_configs",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("use", sa.String(length=16), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("cost_credits", sa.BigInteger(), nullable=True),
        sa.Column("unlock_cost", sa.BigInteger(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("extra", JSON_TYPE, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_model_configs_use", "model_configs", ["use"], unique=True)

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", JSON_TYPE, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    for table in (
        "app_settings",
        "model_configs",
        "audit_logs",
        "gen_assets",
        "gen_tasks",
        "parse_records",
        "credit_transactions",
        "phone_whitelist",
        "users",
    ):
        op.drop_table(table)
