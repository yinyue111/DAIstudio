"""add durable prompt optimization proposals

Revision ID: 0049_prompt_opt_proposals
Revises: 0048_generation_dispatch_outbox
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049_prompt_opt_proposals"
down_revision = "0048_generation_dispatch_outbox"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    if "prompt_optimization_proposals" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "prompt_optimization_proposals",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_operation_id", sa.BigInteger(), nullable=True),
        sa.Column("source_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("target_model_config_id", sa.BigInteger(), nullable=False),
        sa.Column("capability_version_id", sa.BigInteger(), nullable=False),
        sa.Column("optimizer_model_config_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("decision_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=8), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("optimization_kind", sa.String(length=16), nullable=False),
        sa.Column("original", JSON_TYPE, nullable=False),
        sa.Column("suggestion", JSON_TYPE, nullable=False),
        sa.Column("diff", JSON_TYPE, nullable=False),
        sa.Column("constraint_coverage", JSON_TYPE, nullable=False),
        sa.Column("warnings", JSON_TYPE, nullable=False),
        sa.Column("provenance", JSON_TYPE, nullable=False),
        sa.Column("catalog_snapshot", JSON_TYPE, nullable=False),
        sa.Column("accepted_segment_ids", JSON_TYPE, nullable=True),
        sa.Column("rejected_segment_ids", JSON_TYPE, nullable=True),
        sa.Column("decision_result", JSON_TYPE, nullable=True),
        sa.Column("charged_credits", sa.BigInteger(), nullable=False),
        sa.Column("metrics", JSON_TYPE, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status in ('proposed', 'accepted', 'partially_accepted', 'rejected', 'expired')",
            name="ck_prompt_optimization_proposals_status_valid",
        ),
        sa.CheckConstraint("version >= 1", name="ck_prompt_optimization_proposals_version_positive"),
        sa.CheckConstraint(
            "optimization_kind in ('rewrite', 'model_compile')",
            name="ck_prompt_optimization_proposals_kind_valid",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_operation_id"], ["reverse_operations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_revision_id"], ["reverse_result_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["target_model_config_id"], ["model_configs.id"]),
        sa.ForeignKeyConstraint(["capability_version_id"], ["model_capability_versions.id"]),
        sa.ForeignKeyConstraint(["optimizer_model_config_id"], ["model_configs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_prompt_optimization_proposals_user_idempotency",
        "prompt_optimization_proposals",
        ["user_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_prompt_optimization_proposals_user_status_created",
        "prompt_optimization_proposals",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_prompt_optimization_proposals_source_revision",
        "prompt_optimization_proposals",
        ["source_revision_id"],
    )
    op.create_index(
        "ix_prompt_optimization_proposals_user_id",
        "prompt_optimization_proposals",
        ["user_id"],
    )


def downgrade() -> None:
    if "prompt_optimization_proposals" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("prompt_optimization_proposals")
