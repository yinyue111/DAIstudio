"""upgrade reverse operations to asynchronous lifecycle

Revision ID: 0035_async_reverse_operations
Revises: 0034_anthropic_gateway_format
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0035_async_reverse_operations"
down_revision = "0034_anthropic_gateway_format"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("reverse_operations")}


def upgrade() -> None:
    if "reverse_operations" not in _tables():
        return
    columns = _columns()
    with op.batch_alter_table("reverse_operations") as batch:
        batch.drop_constraint("ck_reverse_operations_status_valid", type_="check")
        batch.alter_column(
            "status",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            nullable=False,
            server_default="queued",
        )
        additions = (
            ("phase", sa.Column("phase", sa.String(length=32), nullable=True)),
            ("progress", sa.Column("progress", sa.Integer(), nullable=False, server_default="0")),
            ("request_context", sa.Column("request_context", JSON_TYPE, nullable=True)),
            ("model_snapshot", sa.Column("model_snapshot", JSON_TYPE, nullable=True)),
            ("template_snapshot", sa.Column("template_snapshot", JSON_TYPE, nullable=True)),
            ("pricing_snapshot", sa.Column("pricing_snapshot", JSON_TYPE, nullable=True)),
            ("celery_task_id", sa.Column("celery_task_id", sa.String(length=64), nullable=True)),
            ("cost_frozen", sa.Column("cost_frozen", sa.BigInteger(), nullable=False, server_default="0")),
            ("cost_settled", sa.Column("cost_settled", sa.BigInteger(), nullable=False, server_default="0")),
            ("confirmation_expires_at", sa.Column("confirmation_expires_at", sa.DateTime(timezone=True), nullable=True)),
            ("cancel_requested", sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("attempt_count", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")),
            ("error_code", sa.Column("error_code", sa.String(length=64), nullable=True)),
            ("started_at", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)),
            ("finished_at", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)),
        )
        for name, column in additions:
            if name not in columns:
                batch.add_column(column)
        batch.create_check_constraint(
            "ck_reverse_operations_status_valid",
            "status in ('queued', 'running', 'needs_confirmation', 'succeeded', 'failed', 'canceled')",
        )
        batch.create_check_constraint(
            "ck_reverse_operations_progress_range",
            "progress >= 0 AND progress <= 100",
        )
        batch.create_check_constraint(
            "ck_reverse_operations_cost_frozen_nonnegative",
            "cost_frozen >= 0",
        )
        batch.create_check_constraint(
            "ck_reverse_operations_cost_settled_nonnegative",
            "cost_settled >= 0",
        )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE reverse_operations SET cost_settled = charged_credits "
            "WHERE status = 'succeeded' AND cost_settled = 0"
        )
    )
    # Older synchronous operations had only created_at/updated_at. Backfill
    # lifecycle anchors so bounded usage reports can attribute their settled
    # cost to a terminal date and the async API never exposes a terminal row
    # without completion metadata.
    bind.execute(
        sa.text(
            "UPDATE reverse_operations SET "
            "started_at = CASE "
            "WHEN status = 'running' THEN COALESCE(started_at, created_at) "
            "ELSE started_at END, "
            "finished_at = CASE "
            "WHEN status IN ('succeeded', 'failed') "
            "THEN COALESCE(finished_at, updated_at, created_at) "
            "ELSE finished_at END, "
            "progress = CASE "
            "WHEN status IN ('succeeded', 'failed') THEN 100 "
            "ELSE progress END"
        )
    )


def downgrade() -> None:
    if "reverse_operations" not in _tables():
        return
    bind = op.get_bind()
    active = bind.execute(
        sa.text(
            "SELECT count(*) FROM reverse_operations "
            "WHERE status IN ('queued', 'needs_confirmation', 'canceled') OR cost_frozen > 0"
        )
    ).scalar_one()
    if int(active or 0) > 0:
        raise RuntimeError("0035 downgrade blocked: asynchronous reverse operations exist")

    with op.batch_alter_table("reverse_operations") as batch:
        batch.drop_constraint("ck_reverse_operations_cost_settled_nonnegative", type_="check")
        batch.drop_constraint("ck_reverse_operations_cost_frozen_nonnegative", type_="check")
        batch.drop_constraint("ck_reverse_operations_progress_range", type_="check")
        batch.drop_constraint("ck_reverse_operations_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_reverse_operations_status_valid",
            "status in ('running', 'succeeded', 'failed')",
        )
        batch.alter_column(
            "status",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            nullable=False,
            server_default="running",
        )
        for column in (
            "finished_at",
            "started_at",
            "error_code",
            "attempt_count",
            "cancel_requested",
            "confirmation_expires_at",
            "cost_settled",
            "cost_frozen",
            "celery_task_id",
            "pricing_snapshot",
            "template_snapshot",
            "model_snapshot",
            "request_context",
            "progress",
            "phase",
        ):
            batch.drop_column(column)
