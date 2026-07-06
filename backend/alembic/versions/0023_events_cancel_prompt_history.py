"""events, task cancel status, and user prompt history

Revision ID: 0023_events_cancel_prompt
Revises: 0022_credit_pricing_defaults
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_events_cancel_prompt"
down_revision = "0022_credit_pricing_defaults"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _check_names(table: str) -> set[str]:
    try:
        return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table)}
    except NotImplementedError:
        return set()


def _replace_gen_task_status_check(condition: str) -> None:
    if "gen_tasks" not in _tables():
        return
    checks = _check_names("gen_tasks")
    with op.batch_alter_table("gen_tasks") as batch:
        if "ck_gen_tasks_status_valid" in checks:
            batch.drop_constraint("ck_gen_tasks_status_valid", type_="check")
        batch.create_check_constraint("ck_gen_tasks_status_valid", condition)


def upgrade() -> None:
    _replace_gen_task_status_check(
        "status in ('queued', 'running', 'succeeded', 'failed', 'needs_review', 'canceled')"
    )
    if "user_prompts" not in _tables():
        op.create_table(
            "user_prompts",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("title", sa.String(length=128), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=16), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=False),
            sa.Column("favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("params", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("category in ('image', 'video', 'general')", name="ck_user_prompts_category_valid"),
            sa.CheckConstraint(
                "source in ('manual', 'reverse', 'generate', 'library')",
                name="ck_user_prompts_source_valid",
            ),
            sa.CheckConstraint("usage_count >= 0", name="ck_user_prompts_usage_count_nonnegative"),
        )
        op.create_index("ix_user_prompts_user_id", "user_prompts", ["user_id"])
        op.create_index("ix_user_prompts_user_created", "user_prompts", ["user_id", "created_at"])
        op.create_index("ix_user_prompts_user_favorite", "user_prompts", ["user_id", "favorite"])


def downgrade() -> None:
    if "gen_tasks" in _tables():
        count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM gen_tasks WHERE status = 'canceled'")
        ).scalar()
        if count:
            raise RuntimeError("0023 downgrade blocked: canceled tasks exist")
    if "user_prompts" in _tables():
        op.drop_index("ix_user_prompts_user_favorite", table_name="user_prompts")
        op.drop_index("ix_user_prompts_user_created", table_name="user_prompts")
        op.drop_index("ix_user_prompts_user_id", table_name="user_prompts")
        op.drop_table("user_prompts")
    _replace_gen_task_status_check(
        "status in ('queued', 'running', 'succeeded', 'failed', 'needs_review')"
    )
