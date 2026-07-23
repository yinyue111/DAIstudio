"""add durable reproduction remediation orchestration

Revision ID: 0069_reproduction_remediations
Revises: 0068_catalog_metadata_snapshots
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0069_reproduction_remediations"
down_revision = "0068_catalog_metadata_snapshots"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "reproduction_remediations",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "assessment_id",
            sa.BigInteger(),
            sa.ForeignKey("reproduction_assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "correction_id",
            sa.BigInteger(),
            sa.ForeignKey("reproduction_corrections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_remediation_id",
            sa.BigInteger(),
            sa.ForeignKey("reproduction_remediations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="planned",
        ),
        sa.Column(
            "selected_finding_ids",
            JSON_TYPE,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "selected_shot_ids",
            JSON_TYPE,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("source_asset_ref", sa.String(length=512), nullable=False),
        sa.Column("target_asset_ref", sa.String(length=512), nullable=False),
        sa.Column(
            "applied_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("reverse_result_revisions.id"),
            nullable=False,
        ),
        sa.Column("plan_snapshot", JSON_TYPE, nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("video_composition", JSON_TYPE, nullable=True),
        sa.Column(
            "generation_task_ids",
            JSON_TYPE,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "composition_tool_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tool_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "composition_workflow_run_id",
            sa.BigInteger(),
            sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "final_asset_id",
            sa.BigInteger(),
            sa.ForeignKey("gen_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "successor_assessment_id",
            sa.BigInteger(),
            sa.ForeignKey("reproduction_assessments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "auto_reassess",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "mode in ('image_inpaint', 'video_shot_regenerate')",
            name="ck_reproduction_remediations_mode_valid",
        ),
        sa.CheckConstraint(
            "status in ('planned', 'generating', 'composing', 'reassessing', "
            "'succeeded', 'partial', 'failed', 'canceled')",
            name="ck_reproduction_remediations_status_valid",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_reproduction_remediations_fingerprint_length",
        ),
        sa.CheckConstraint(
            "length(plan_hash) = 64",
            name="ck_reproduction_remediations_plan_hash_length",
        ),
        sa.CheckConstraint(
            "parent_remediation_id IS NULL OR parent_remediation_id <> id",
            name="ck_reproduction_remediations_parent_not_self",
        ),
        sa.CheckConstraint(
            "composition_workflow_run_id IS NULL OR composition_tool_run_id IS NOT NULL",
            name="ck_reproduction_remediations_composition_pair",
        ),
    )
    op.create_index(
        "uq_reproduction_remediations_assessment_idempotency",
        "reproduction_remediations",
        ["assessment_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_reproduction_remediations_correction",
        "reproduction_remediations",
        ["correction_id"],
        unique=True,
    )
    op.create_index(
        "uq_reproduction_remediations_applied_revision",
        "reproduction_remediations",
        ["applied_revision_id"],
        unique=True,
    )
    op.create_index(
        "uq_reproduction_remediations_composition_tool_run",
        "reproduction_remediations",
        ["composition_tool_run_id"],
        unique=True,
        postgresql_where=sa.text("composition_tool_run_id IS NOT NULL"),
        sqlite_where=sa.text("composition_tool_run_id IS NOT NULL"),
    )
    op.create_index(
        "uq_reproduction_remediations_composition_workflow",
        "reproduction_remediations",
        ["composition_workflow_run_id"],
        unique=True,
        postgresql_where=sa.text("composition_workflow_run_id IS NOT NULL"),
        sqlite_where=sa.text("composition_workflow_run_id IS NOT NULL"),
    )
    op.create_index(
        "uq_reproduction_remediations_final_asset",
        "reproduction_remediations",
        ["final_asset_id"],
        unique=True,
        postgresql_where=sa.text("final_asset_id IS NOT NULL"),
        sqlite_where=sa.text("final_asset_id IS NOT NULL"),
    )
    op.create_index(
        "uq_reproduction_remediations_successor_assessment",
        "reproduction_remediations",
        ["successor_assessment_id"],
        unique=True,
        postgresql_where=sa.text("successor_assessment_id IS NOT NULL"),
        sqlite_where=sa.text("successor_assessment_id IS NOT NULL"),
    )
    op.create_index(
        "ix_reproduction_remediations_assessment_created",
        "reproduction_remediations",
        ["assessment_id", "created_at", "id"],
    )
    op.create_index(
        "ix_reproduction_remediations_user_status_created",
        "reproduction_remediations",
        ["user_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_reproduction_remediations_parent",
        "reproduction_remediations",
        ["parent_remediation_id"],
    )


def downgrade() -> None:
    op.drop_table("reproduction_remediations")
