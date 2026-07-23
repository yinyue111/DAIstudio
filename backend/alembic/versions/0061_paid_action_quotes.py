"""add prompt optimization and asset unlock quotes

Revision ID: 0061_paid_action_quotes
Revises: 0060_unified_execution_quotes
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0061_paid_action_quotes"
down_revision = "0060_unified_execution_quotes"
branch_labels = None
depends_on = None


def _partial_not_null(column: str) -> dict:
    predicate = sa.text(f"{column} IS NOT NULL")
    return {"postgresql_where": predicate, "sqlite_where": predicate}


def _quote_table_recreate(bind) -> str:
    # Preserve inbound quote references on PostgreSQL; SQLite still needs the
    # copy-and-swap path for CHECK and VARCHAR changes.
    return "always" if bind.dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table(
        "generation_quotes",
        recreate=_quote_table_recreate(bind),
    ) as batch:
        batch.drop_constraint("ck_generation_quotes_kind_valid", type_="check")
        batch.alter_column(
            "kind",
            existing_type=sa.String(length=16),
            type_=sa.String(length=24),
            nullable=False,
            existing_server_default="generation",
        )
        batch.create_check_constraint(
            "ck_generation_quotes_kind_valid",
            "kind in ('generation', 'reverse', 'reverse_batch', 'workflow', "
            "'prompt_optimization', 'asset_unlock')",
        )

    with op.batch_alter_table("prompt_optimization_proposals") as batch:
        batch.add_column(sa.Column("quote_id", sa.BigInteger()))
        batch.create_foreign_key(
            "fk_prompt_optimization_proposals_quote_id_generation_quotes",
            "generation_quotes",
            ["quote_id"],
            ["id"],
        )
    op.create_index(
        "ix_prompt_optimization_proposals_quote_id",
        "prompt_optimization_proposals",
        ["quote_id"],
    )
    op.create_index(
        "uq_prompt_optimization_proposals_quote_id",
        "prompt_optimization_proposals",
        ["quote_id"],
        unique=True,
        **_partial_not_null("quote_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index(
        "uq_prompt_optimization_proposals_quote_id",
        table_name="prompt_optimization_proposals",
    )
    op.drop_index(
        "ix_prompt_optimization_proposals_quote_id",
        table_name="prompt_optimization_proposals",
    )
    with op.batch_alter_table("prompt_optimization_proposals") as batch:
        batch.drop_constraint(
            "fk_prompt_optimization_proposals_quote_id_generation_quotes",
            type_="foreignkey",
        )
        batch.drop_column("quote_id")

    op.execute(
        sa.text(
            "DELETE FROM generation_quotes "
            "WHERE kind IN ('prompt_optimization', 'asset_unlock')"
        )
    )
    with op.batch_alter_table(
        "generation_quotes",
        recreate=_quote_table_recreate(bind),
    ) as batch:
        batch.drop_constraint("ck_generation_quotes_kind_valid", type_="check")
        batch.alter_column(
            "kind",
            existing_type=sa.String(length=24),
            type_=sa.String(length=16),
            nullable=False,
            existing_server_default="generation",
        )
        batch.create_check_constraint(
            "ck_generation_quotes_kind_valid",
            "kind in ('generation', 'reverse', 'reverse_batch', 'workflow')",
        )
