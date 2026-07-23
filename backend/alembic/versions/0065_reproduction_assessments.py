"""add generation-result reproduction assessments and correction lineage

Revision ID: 0065_reproduction_assessments
Revises: 0064_immutable_runtime_versions
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0065_reproduction_assessments"
down_revision = "0064_immutable_runtime_versions"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


_PRECREATED_TABLES = {
    "reproduction_assessments": {
        "columns": {
            "id", "user_id", "idempotency_key", "request_fingerprint",
            "source_asset_ref", "generated_asset_ref", "generated_asset_id",
            "media_type", "reverse_operation_id", "reverse_revision_id",
            "generation_task_id", "model_config_id", "capability_version_id",
            "price_version_id", "status", "phase", "progress",
            "cancel_requested", "cost_credits", "schema_version", "asset_snapshot",
            "lineage_snapshot", "metrics", "analyzers", "warnings", "error_code",
            "error", "started_at", "finished_at", "created_at", "updated_at",
        },
        "indexes": {
            "uq_reproduction_assessments_user_idempotency": (True, ("user_id", "idempotency_key")),
            "ix_reproduction_assessments_user_created": (False, ("user_id", "created_at")),
            "ix_reproduction_assessments_status_updated": (False, ("status", "updated_at")),
            "ix_reproduction_assessments_user_id": (False, ("user_id",)),
            "ix_reproduction_assessments_generated_asset_id": (False, ("generated_asset_id",)),
            "ix_reproduction_assessments_reverse_operation_id": (False, ("reverse_operation_id",)),
            "ix_reproduction_assessments_reverse_revision_id": (False, ("reverse_revision_id",)),
            "ix_reproduction_assessments_generation_task_id": (False, ("generation_task_id",)),
            "ix_reproduction_assessments_model_config_id": (False, ("model_config_id",)),
            "ix_reproduction_assessments_capability_version_id": (False, ("capability_version_id",)),
            "ix_reproduction_assessments_price_version_id": (False, ("price_version_id",)),
        },
        "checks": {
            "ck_reproduction_assessments_status_valid",
            "ck_reproduction_assessments_media_type_valid",
            "ck_reproduction_assessments_progress_range",
            "ck_reproduction_assessments_cost_nonnegative",
        },
        "foreign_keys": {
            (("user_id",), "users", ("id",)),
            (("generated_asset_id",), "gen_assets", ("id",)),
            (("reverse_operation_id",), "reverse_operations", ("id",)),
            (("reverse_revision_id",), "reverse_result_revisions", ("id",)),
            (("generation_task_id",), "gen_tasks", ("id",)),
            (("model_config_id",), "model_configs", ("id",)),
            (("capability_version_id",), "model_capability_versions", ("id",)),
            (("price_version_id",), "model_price_versions", ("id",)),
        },
    },
    "reproduction_findings": {
        "columns": {
            "id", "assessment_id", "finding_key", "position", "dimension", "kind",
            "severity", "confidence", "message", "bbox", "time_range", "shot_id",
            "evidence", "metrics", "created_at",
        },
        "indexes": {
            "uq_reproduction_findings_assessment_key": (True, ("assessment_id", "finding_key")),
            "ix_reproduction_findings_assessment_position": (False, ("assessment_id", "position")),
            "ix_reproduction_findings_assessment_id": (False, ("assessment_id",)),
        },
        "checks": {
            "ck_reproduction_findings_severity_valid",
            "ck_reproduction_findings_confidence_range",
        },
        "foreign_keys": {
            (("assessment_id",), "reproduction_assessments", ("id",)),
        },
    },
    "reproduction_corrections": {
        "columns": {
            "id", "assessment_id", "user_id", "idempotency_key", "request_fingerprint",
            "parent_revision_id", "edited_revision_id", "applied_revision_id",
            "selected_finding_ids", "structured_patch", "prompt_patch",
            "negative_prompt_patch", "mask_patch", "apply_requested", "status", "created_at",
        },
        "indexes": {
            "uq_reproduction_corrections_assessment_idempotency": (True, ("assessment_id", "idempotency_key")),
            "uq_reproduction_corrections_edited_revision": (True, ("edited_revision_id",)),
            "uq_reproduction_corrections_applied_revision": (True, ("applied_revision_id",)),
            "ix_reproduction_corrections_user_created": (False, ("user_id", "created_at")),
            "ix_reproduction_corrections_assessment_id": (False, ("assessment_id",)),
            "ix_reproduction_corrections_user_id": (False, ("user_id",)),
            "ix_reproduction_corrections_parent_revision_id": (False, ("parent_revision_id",)),
        },
        "checks": {"ck_reproduction_corrections_status_valid"},
        "foreign_keys": {
            (("assessment_id",), "reproduction_assessments", ("id",)),
            (("user_id",), "users", ("id",)),
            (("parent_revision_id",), "reverse_result_revisions", ("id",)),
            (("edited_revision_id",), "reverse_result_revisions", ("id",)),
            (("applied_revision_id",), "reverse_result_revisions", ("id",)),
        },
    },
}


def _accept_precreated_tables(bind) -> bool:
    inspector = sa.inspect(bind)
    expected_tables = set(_PRECREATED_TABLES)
    present_tables = {table for table in expected_tables if inspector.has_table(table)}
    if not present_tables:
        return False
    if present_tables != expected_tables:
        raise RuntimeError("partially pre-created reproduction tables cannot be migrated safely")

    for table, expected in _PRECREATED_TABLES.items():
        columns = {column["name"] for column in inspector.get_columns(table)}
        indexes = {
            index.get("name"): (
                bool(index.get("unique")),
                tuple(index.get("column_names") or ()),
            )
            for index in inspector.get_indexes(table)
        }
        checks = {
            constraint.get("name")
            for constraint in inspector.get_check_constraints(table)
        }
        foreign_keys = {
            (
                tuple(constraint.get("constrained_columns") or ()),
                constraint.get("referred_table"),
                tuple(constraint.get("referred_columns") or ()),
            )
            for constraint in inspector.get_foreign_keys(table)
        }
        if columns != expected["columns"]:
            raise RuntimeError(f"pre-created {table} columns do not match migration 0065")
        if any(indexes.get(name) != value for name, value in expected["indexes"].items()):
            raise RuntimeError(f"pre-created {table} indexes do not match migration 0065")
        if not expected["checks"].issubset(checks):
            raise RuntimeError(f"pre-created {table} constraints do not match migration 0065")
        if not expected["foreign_keys"].issubset(foreign_keys):
            raise RuntimeError(f"pre-created {table} foreign keys do not match migration 0065")
    return True


def upgrade() -> None:
    if _accept_precreated_tables(op.get_bind()):
        return
    op.create_table(
        "reproduction_assessments",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_asset_ref", sa.String(length=512), nullable=False),
        sa.Column("generated_asset_ref", sa.String(length=512), nullable=False),
        sa.Column(
            "generated_asset_id",
            sa.BigInteger(),
            sa.ForeignKey("gen_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("media_type", sa.String(length=8), nullable=False),
        sa.Column(
            "reverse_operation_id",
            sa.BigInteger(),
            sa.ForeignKey("reverse_operations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reverse_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("reverse_result_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "generation_task_id",
            sa.BigInteger(),
            sa.ForeignKey("gen_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "model_config_id",
            sa.BigInteger(),
            sa.ForeignKey("model_configs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "capability_version_id",
            sa.BigInteger(),
            sa.ForeignKey("model_capability_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "price_version_id",
            sa.BigInteger(),
            sa.ForeignKey("model_price_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(length=32), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cost_credits", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "schema_version",
            sa.String(length=32),
            nullable=False,
            server_default="reproduction.v1",
        ),
        sa.Column("asset_snapshot", JSON_TYPE, nullable=False, server_default="{}"),
        sa.Column("lineage_snapshot", JSON_TYPE, nullable=False, server_default="{}"),
        sa.Column("metrics", JSON_TYPE, nullable=False, server_default="{}"),
        sa.Column("analyzers", JSON_TYPE, nullable=False, server_default="{}"),
        sa.Column("warnings", JSON_TYPE, nullable=False, server_default="[]"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'canceled')",
            name="ck_reproduction_assessments_status_valid",
        ),
        sa.CheckConstraint(
            "media_type in ('image', 'video')",
            name="ck_reproduction_assessments_media_type_valid",
        ),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_reproduction_assessments_progress_range",
        ),
        sa.CheckConstraint(
            "cost_credits >= 0",
            name="ck_reproduction_assessments_cost_nonnegative",
        ),
    )
    op.create_index(
        "uq_reproduction_assessments_user_idempotency",
        "reproduction_assessments",
        ["user_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_reproduction_assessments_user_created",
        "reproduction_assessments",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_reproduction_assessments_status_updated",
        "reproduction_assessments",
        ["status", "updated_at"],
    )
    op.create_index("ix_reproduction_assessments_user_id", "reproduction_assessments", ["user_id"])
    for column in (
        "user_id",
        "generated_asset_id",
        "reverse_operation_id",
        "reverse_revision_id",
        "generation_task_id",
        "model_config_id",
        "capability_version_id",
        "price_version_id",
    ):
        name = f"ix_reproduction_assessments_{column}"
        if column == "user_id":
            continue
        op.create_index(name, "reproduction_assessments", [column])

    op.create_table(
        "reproduction_findings",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "assessment_id",
            sa.BigInteger(),
            sa.ForeignKey("reproduction_assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("finding_key", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dimension", sa.String(length=48), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("bbox", JSON_TYPE, nullable=True),
        sa.Column("time_range", JSON_TYPE, nullable=True),
        sa.Column("shot_id", sa.String(length=128), nullable=True),
        sa.Column("evidence", JSON_TYPE, nullable=False, server_default="{}"),
        sa.Column("metrics", JSON_TYPE, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "severity in ('low', 'medium', 'high', 'critical')",
            name="ck_reproduction_findings_severity_valid",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_reproduction_findings_confidence_range",
        ),
    )
    op.create_index(
        "uq_reproduction_findings_assessment_key",
        "reproduction_findings",
        ["assessment_id", "finding_key"],
        unique=True,
    )
    op.create_index(
        "ix_reproduction_findings_assessment_position",
        "reproduction_findings",
        ["assessment_id", "position"],
    )
    op.create_index("ix_reproduction_findings_assessment_id", "reproduction_findings", ["assessment_id"])

    op.create_table(
        "reproduction_corrections",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "assessment_id",
            sa.BigInteger(),
            sa.ForeignKey("reproduction_assessments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "parent_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("reverse_result_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "edited_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("reverse_result_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "applied_revision_id",
            sa.BigInteger(),
            sa.ForeignKey("reverse_result_revisions.id"),
            nullable=True,
        ),
        sa.Column("selected_finding_ids", JSON_TYPE, nullable=False, server_default="[]"),
        sa.Column("structured_patch", JSON_TYPE, nullable=False, server_default="{}"),
        sa.Column("prompt_patch", sa.Text(), nullable=True),
        sa.Column("negative_prompt_patch", sa.Text(), nullable=True),
        sa.Column("mask_patch", JSON_TYPE, nullable=True),
        sa.Column("apply_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="created"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('created', 'applied')",
            name="ck_reproduction_corrections_status_valid",
        ),
    )
    op.create_index(
        "uq_reproduction_corrections_assessment_idempotency",
        "reproduction_corrections",
        ["assessment_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_reproduction_corrections_edited_revision",
        "reproduction_corrections",
        ["edited_revision_id"],
        unique=True,
    )
    op.create_index(
        "uq_reproduction_corrections_applied_revision",
        "reproduction_corrections",
        ["applied_revision_id"],
        unique=True,
        postgresql_where=sa.text("applied_revision_id IS NOT NULL"),
        sqlite_where=sa.text("applied_revision_id IS NOT NULL"),
    )
    op.create_index(
        "ix_reproduction_corrections_user_created",
        "reproduction_corrections",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_reproduction_corrections_assessment_id",
        "reproduction_corrections",
        ["assessment_id"],
    )
    op.create_index(
        "ix_reproduction_corrections_user_id",
        "reproduction_corrections",
        ["user_id"],
    )
    op.create_index(
        "ix_reproduction_corrections_parent_revision_id",
        "reproduction_corrections",
        ["parent_revision_id"],
    )


def downgrade() -> None:
    op.drop_table("reproduction_corrections")
    op.drop_table("reproduction_findings")
    op.drop_table("reproduction_assessments")
