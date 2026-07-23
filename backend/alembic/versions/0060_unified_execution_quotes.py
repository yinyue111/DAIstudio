"""extend generation quotes to all executable operations

Revision ID: 0060_unified_execution_quotes
Revises: 0059_recipe_usage_context_jsonb
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0060_unified_execution_quotes"
down_revision = "0059_recipe_usage_context_jsonb"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _partial_not_null(column: str) -> dict:
    predicate = sa.text(f"{column} IS NOT NULL")
    return {"postgresql_where": predicate, "sqlite_where": predicate}


def _quote_table_recreate(bind) -> str:
    # SQLite cannot alter checks/types in place. PostgreSQL must not recreate
    # this table because several existing tables already reference quote IDs.
    return "always" if bind.dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    bind = op.get_bind()
    task_fk = next(
        (
            row
            for row in sa.inspect(bind).get_foreign_keys("generation_quotes")
            if row.get("constrained_columns") == ["task_id"]
        ),
        None,
    )
    naming_convention = {
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    }
    with op.batch_alter_table(
        "generation_quotes",
        recreate=_quote_table_recreate(bind),
        naming_convention=naming_convention,
    ) as batch:
        if task_fk is not None:
            batch.drop_constraint(
                task_fk.get("name") or "fk_generation_quotes_task_id_gen_tasks",
                type_="foreignkey",
            )
        batch.drop_constraint("ck_generation_quotes_category_valid", type_="check")
        batch.alter_column(
            "model_config_id", existing_type=sa.BigInteger(), nullable=True
        )
        batch.alter_column(
            "capability_version_id", existing_type=sa.BigInteger(), nullable=True
        )
        batch.alter_column(
            "price_version_id", existing_type=sa.BigInteger(), nullable=True
        )
        batch.alter_column(
            "category",
            existing_type=sa.String(length=8),
            type_=sa.String(length=16),
            nullable=False,
        )
        batch.add_column(
            sa.Column(
                "kind",
                sa.String(length=16),
                nullable=False,
                server_default="generation",
            )
        )
        batch.add_column(sa.Column("client_request_id", sa.String(length=128)))
        batch.add_column(sa.Column("tool_version_id", sa.BigInteger()))
        batch.add_column(
            sa.Column(
                "subject_snapshot",
                JSON_TYPE,
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column(
                "warnings",
                JSON_TYPE,
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(sa.Column("consumed_ref_type", sa.String(length=32)))
        batch.add_column(sa.Column("consumed_ref_id", sa.BigInteger()))
        batch.create_foreign_key(
            "fk_generation_quotes_tool_version_id_tool_versions",
            "tool_versions",
            ["tool_version_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "ck_generation_quotes_category_valid",
            "category in ('image', 'video', 'workflow')",
        )
        batch.create_check_constraint(
            "ck_generation_quotes_kind_valid",
            "kind in ('generation', 'reverse', 'reverse_batch', 'workflow')",
        )

    op.create_index(
        "ix_generation_quotes_tool_version_id",
        "generation_quotes",
        ["tool_version_id"],
    )
    op.create_index(
        "ix_generation_quotes_user_kind_client_request",
        "generation_quotes",
        ["user_id", "kind", "client_request_id", "created_at"],
    )
    op.create_index(
        "uq_generation_quotes_consumed_ref",
        "generation_quotes",
        ["kind", "consumed_ref_type", "consumed_ref_id"],
        unique=True,
        **_partial_not_null("consumed_ref_id"),
    )

    with op.batch_alter_table("reverse_operations") as batch:
        batch.add_column(sa.Column("quote_id", sa.BigInteger()))
        batch.create_foreign_key(
            "fk_reverse_operations_quote_id_generation_quotes",
            "generation_quotes",
            ["quote_id"],
            ["id"],
        )
    op.create_index(
        "ix_reverse_operations_quote_id", "reverse_operations", ["quote_id"]
    )
    op.create_index(
        "uq_reverse_operations_quote_id",
        "reverse_operations",
        ["quote_id"],
        unique=True,
        **_partial_not_null("quote_id"),
    )

    with op.batch_alter_table("reverse_operation_batches") as batch:
        batch.add_column(sa.Column("quote_id", sa.BigInteger()))
        batch.create_foreign_key(
            "fk_reverse_operation_batches_quote_id_generation_quotes",
            "generation_quotes",
            ["quote_id"],
            ["id"],
        )
    op.create_index(
        "ix_reverse_operation_batches_quote_id",
        "reverse_operation_batches",
        ["quote_id"],
    )
    op.create_index(
        "uq_reverse_operation_batches_quote_id",
        "reverse_operation_batches",
        ["quote_id"],
        unique=True,
        **_partial_not_null("quote_id"),
    )

    with op.batch_alter_table("tool_runs") as batch:
        batch.add_column(sa.Column("quote_id", sa.BigInteger()))
        batch.add_column(
            sa.Column(
                "pricing_snapshot",
                JSON_TYPE,
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column("cost_frozen", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("cost_settled", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.create_foreign_key(
            "fk_tool_runs_quote_id_generation_quotes",
            "generation_quotes",
            ["quote_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "ck_tool_runs_cost_frozen_nonnegative", "cost_frozen >= 0"
        )
        batch.create_check_constraint(
            "ck_tool_runs_cost_settled_nonnegative", "cost_settled >= 0"
        )
    op.create_index("ix_tool_runs_quote_id", "tool_runs", ["quote_id"])
    op.create_index(
        "uq_tool_runs_quote_id",
        "tool_runs",
        ["quote_id"],
        unique=True,
        **_partial_not_null("quote_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index("uq_tool_runs_quote_id", table_name="tool_runs")
    op.drop_index("ix_tool_runs_quote_id", table_name="tool_runs")
    with op.batch_alter_table("tool_runs") as batch:
        batch.drop_constraint("ck_tool_runs_cost_settled_nonnegative", type_="check")
        batch.drop_constraint("ck_tool_runs_cost_frozen_nonnegative", type_="check")
        batch.drop_constraint(
            "fk_tool_runs_quote_id_generation_quotes", type_="foreignkey"
        )
        batch.drop_column("cost_settled")
        batch.drop_column("cost_frozen")
        batch.drop_column("pricing_snapshot")
        batch.drop_column("quote_id")

    op.drop_index(
        "uq_reverse_operation_batches_quote_id",
        table_name="reverse_operation_batches",
    )
    op.drop_index(
        "ix_reverse_operation_batches_quote_id",
        table_name="reverse_operation_batches",
    )
    with op.batch_alter_table("reverse_operation_batches") as batch:
        batch.drop_constraint(
            "fk_reverse_operation_batches_quote_id_generation_quotes",
            type_="foreignkey",
        )
        batch.drop_column("quote_id")

    op.drop_index("uq_reverse_operations_quote_id", table_name="reverse_operations")
    op.drop_index("ix_reverse_operations_quote_id", table_name="reverse_operations")
    with op.batch_alter_table("reverse_operations") as batch:
        batch.drop_constraint(
            "fk_reverse_operations_quote_id_generation_quotes", type_="foreignkey"
        )
        batch.drop_column("quote_id")

    op.drop_index("uq_generation_quotes_consumed_ref", table_name="generation_quotes")
    op.drop_index(
        "ix_generation_quotes_user_kind_client_request", table_name="generation_quotes"
    )
    op.drop_index("ix_generation_quotes_tool_version_id", table_name="generation_quotes")
    op.execute(sa.text("DELETE FROM generation_quotes WHERE kind <> 'generation'"))
    with op.batch_alter_table(
        "generation_quotes",
        recreate=_quote_table_recreate(bind),
    ) as batch:
        batch.drop_constraint("ck_generation_quotes_kind_valid", type_="check")
        batch.drop_constraint("ck_generation_quotes_category_valid", type_="check")
        batch.drop_constraint(
            "fk_generation_quotes_tool_version_id_tool_versions", type_="foreignkey"
        )
        batch.drop_column("consumed_ref_id")
        batch.drop_column("consumed_ref_type")
        batch.drop_column("warnings")
        batch.drop_column("subject_snapshot")
        batch.drop_column("tool_version_id")
        batch.drop_column("client_request_id")
        batch.drop_column("kind")
        batch.alter_column(
            "category",
            existing_type=sa.String(length=16),
            type_=sa.String(length=8),
            nullable=False,
        )
        batch.alter_column(
            "price_version_id", existing_type=sa.BigInteger(), nullable=False
        )
        batch.alter_column(
            "capability_version_id", existing_type=sa.BigInteger(), nullable=False
        )
        batch.alter_column(
            "model_config_id", existing_type=sa.BigInteger(), nullable=False
        )
        batch.create_check_constraint(
            "ck_generation_quotes_category_valid", "category in ('image', 'video')"
        )
        batch.create_foreign_key(
            "fk_generation_quotes_task_id_gen_tasks",
            "gen_tasks",
            ["task_id"],
            ["id"],
        )
