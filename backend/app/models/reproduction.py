"""Auto-generated from models.py split. DO NOT edit the line ranges manually."""
from __future__ import annotations

from datetime import datetime

from ._base import (
    Base,
    BigInteger,
    BigIntPK,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSONType,
    Mapped,
    String,
    Text,
    func,
    mapped_column,
    text,
)


class ReproductionAssessment(Base):
    """Explainable comparison between a source and one generated asset."""

    __tablename__ = "reproduction_assessments"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'canceled')",
            name="ck_reproduction_assessments_status_valid",
        ),
        CheckConstraint(
            "media_type in ('image', 'video')",
            name="ck_reproduction_assessments_media_type_valid",
        ),
        CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_reproduction_assessments_progress_range",
        ),
        CheckConstraint(
            "cost_credits >= 0",
            name="ck_reproduction_assessments_cost_nonnegative",
        ),
        Index(
            "uq_reproduction_assessments_user_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_reproduction_assessments_user_created",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_reproduction_assessments_status_updated",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    generated_asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    generated_asset_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("gen_assets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    media_type: Mapped[str] = mapped_column(String(8), nullable=False)
    reverse_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_operations.id", ondelete="SET NULL"),
        index=True,
    )
    reverse_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id", ondelete="SET NULL"),
        index=True,
    )
    generation_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("gen_tasks.id", ondelete="SET NULL"),
        index=True,
    )
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id", ondelete="SET NULL"),
        index=True,
    )
    capability_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_capability_versions.id", ondelete="SET NULL"),
        index=True,
    )
    price_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_price_versions.id", ondelete="SET NULL"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    phase: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_credits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="reproduction.v1", server_default="reproduction.v1", nullable=False
    )
    asset_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    lineage_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    analyzers: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
class ReproductionFinding(Base):
    __tablename__ = "reproduction_findings"
    __table_args__ = (
        CheckConstraint(
            "severity in ('low', 'medium', 'high', 'critical')",
            name="ck_reproduction_findings_severity_valid",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_reproduction_findings_confidence_range",
        ),
        Index(
            "uq_reproduction_findings_assessment_key",
            "assessment_id",
            "finding_key",
            unique=True,
        ),
        Index(
            "ix_reproduction_findings_assessment_position",
            "assessment_id",
            "position",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reproduction_assessments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    finding_key: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dimension: Mapped[str] = mapped_column(String(48), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column()
    message: Mapped[str] = mapped_column(Text, nullable=False)
    bbox: Mapped[dict | None] = mapped_column(JSONType)
    time_range: Mapped[dict | None] = mapped_column(JSONType)
    shot_id: Mapped[str | None] = mapped_column(String(128))
    evidence: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
class ReproductionCorrection(Base):
    __tablename__ = "reproduction_corrections"
    __table_args__ = (
        CheckConstraint(
            "status in ('created', 'applied')",
            name="ck_reproduction_corrections_status_valid",
        ),
        Index(
            "uq_reproduction_corrections_assessment_idempotency",
            "assessment_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "uq_reproduction_corrections_edited_revision",
            "edited_revision_id",
            unique=True,
        ),
        Index(
            "uq_reproduction_corrections_applied_revision",
            "applied_revision_id",
            unique=True,
            postgresql_where=text("applied_revision_id IS NOT NULL"),
            sqlite_where=text("applied_revision_id IS NOT NULL"),
        ),
        Index(
            "ix_reproduction_corrections_user_created",
            "user_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reproduction_assessments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_revision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        nullable=False,
        index=True,
    )
    edited_revision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        nullable=False,
    )
    applied_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
    )
    selected_finding_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    structured_patch: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    prompt_patch: Mapped[str | None] = mapped_column(Text)
    negative_prompt_patch: Mapped[str | None] = mapped_column(Text)
    mask_patch: Mapped[dict | None] = mapped_column(JSONType)
    apply_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="created", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
class ReproductionRemediation(Base):
    """Durable orchestration state for one approved reproduction correction."""

    __tablename__ = "reproduction_remediations"
    __table_args__ = (
        CheckConstraint(
            "mode in ('image_inpaint', 'video_shot_regenerate')",
            name="ck_reproduction_remediations_mode_valid",
        ),
        CheckConstraint(
            "status in ('planned', 'generating', 'composing', 'reassessing', "
            "'succeeded', 'partial', 'failed', 'canceled')",
            name="ck_reproduction_remediations_status_valid",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_reproduction_remediations_fingerprint_length",
        ),
        CheckConstraint(
            "length(plan_hash) = 64",
            name="ck_reproduction_remediations_plan_hash_length",
        ),
        CheckConstraint(
            "parent_remediation_id IS NULL OR parent_remediation_id <> id",
            name="ck_reproduction_remediations_parent_not_self",
        ),
        CheckConstraint(
            "composition_workflow_run_id IS NULL OR composition_tool_run_id IS NOT NULL",
            name="ck_reproduction_remediations_composition_pair",
        ),
        Index(
            "uq_reproduction_remediations_assessment_idempotency",
            "assessment_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "uq_reproduction_remediations_correction",
            "correction_id",
            unique=True,
        ),
        Index(
            "uq_reproduction_remediations_applied_revision",
            "applied_revision_id",
            unique=True,
        ),
        Index(
            "uq_reproduction_remediations_composition_tool_run",
            "composition_tool_run_id",
            unique=True,
            postgresql_where=text("composition_tool_run_id IS NOT NULL"),
            sqlite_where=text("composition_tool_run_id IS NOT NULL"),
        ),
        Index(
            "uq_reproduction_remediations_composition_workflow",
            "composition_workflow_run_id",
            unique=True,
            postgresql_where=text("composition_workflow_run_id IS NOT NULL"),
            sqlite_where=text("composition_workflow_run_id IS NOT NULL"),
        ),
        Index(
            "uq_reproduction_remediations_final_asset",
            "final_asset_id",
            unique=True,
            postgresql_where=text("final_asset_id IS NOT NULL"),
            sqlite_where=text("final_asset_id IS NOT NULL"),
        ),
        Index(
            "uq_reproduction_remediations_successor_assessment",
            "successor_assessment_id",
            unique=True,
            postgresql_where=text("successor_assessment_id IS NOT NULL"),
            sqlite_where=text("successor_assessment_id IS NOT NULL"),
        ),
        Index(
            "ix_reproduction_remediations_assessment_created",
            "assessment_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_reproduction_remediations_user_status_created",
            "user_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_reproduction_remediations_parent",
            "parent_remediation_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reproduction_assessments.id", ondelete="CASCADE"),
        nullable=False,
    )
    correction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reproduction_corrections.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_remediation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reproduction_remediations.id", ondelete="SET NULL"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default="planned", server_default="planned", nullable=False
    )
    selected_finding_ids: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    selected_shot_ids: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    source_asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    target_asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    applied_revision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        nullable=False,
    )
    plan_snapshot: Mapped[list] = mapped_column(JSONType, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    video_composition: Mapped[dict | None] = mapped_column(JSONType)
    generation_task_ids: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    composition_tool_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tool_runs.id", ondelete="SET NULL"),
    )
    composition_workflow_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("workflow_runs.id", ondelete="SET NULL"),
    )
    final_asset_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("gen_assets.id", ondelete="SET NULL"),
    )
    successor_assessment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reproduction_assessments.id", ondelete="SET NULL"),
    )
    auto_reassess: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
