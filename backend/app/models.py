"""ORM models. Mirrors the schema in the spec, plus two small config tables
(model_configs, app_settings) so the gateway model ids / costs are editable from
the admin UI instead of code."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Portable types: JSONB / BIGINT on PostgreSQL (production), JSON / INTEGER on
# SQLite (so the same models run in lightweight tests / quick local trials).
JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def _default_workflow_compiled_snapshot(context: Any) -> dict:
    """Keep legacy ORM fixtures insertable while marking their provenance."""
    parameters = context.get_current_parameters()
    workflow = parameters.get("workflow_snapshot")
    if not isinstance(workflow, dict):
        workflow = {}
    return {
        "schema_version": "workflow-compiled.v1",
        "legacy_direct_construct": True,
        "dag": {
            "schema_version": parameters.get("workflow_schema_version"),
            "workflow": workflow,
        },
    }


def _default_workflow_compiled_snapshot_hash(context: Any) -> str:
    parameters = context.get_current_parameters()
    snapshot = parameters.get("compiled_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = _default_workflow_compiled_snapshot(context)
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status in ('active', 'pending', 'disabled')", name="ck_users_status_valid"),
        CheckConstraint("balance_credits >= 0", name="ck_users_balance_nonnegative"),
        CheckConstraint("frozen_credits >= 0", name="ck_users_frozen_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(64))
    avatar: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/pending/disabled
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # bumped on password change / logout-all to invalidate existing JWTs
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    department: Mapped[str | None] = mapped_column(String(64))
    balance_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    frozen_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PhoneWhitelist(Base):
    __tablename__ = "phone_whitelist"

    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    note: Mapped[str | None] = mapped_column(String(128))
    department: Mapped[str | None] = mapped_column(String(64))
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    __table_args__ = (
        CheckConstraint(
            "type in ('grant', 'freeze', 'settle', 'refund', 'unlock', 'consume')",
            name="ck_credit_transactions_type_valid",
        ),
        CheckConstraint("balance_after >= 0", name="ck_credit_transactions_balance_after_nonnegative"),
        CheckConstraint(
            "frozen_after IS NULL OR frozen_after >= 0",
            name="ck_credit_transactions_frozen_after_nonnegative",
        ),
        CheckConstraint(
            "reserved_amount IS NULL OR reserved_amount >= 0",
            name="ck_credit_transactions_reserved_amount_nonnegative",
        ),
        CheckConstraint(
            "real_cost IS NULL OR real_cost >= 0",
            name="ck_credit_transactions_real_cost_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # grant/freeze/settle/refund/unlock/consume
    change: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_delta: Mapped[int | None] = mapped_column(BigInteger)
    frozen_delta: Mapped[int | None] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frozen_after: Mapped[int | None] = mapped_column(BigInteger)
    reserved_amount: Mapped[int | None] = mapped_column(BigInteger)
    real_cost: Mapped[int | None] = mapped_column(BigInteger)
    biz_type: Mapped[str | None] = mapped_column(String(32))  # gen_task / unlock / admin
    biz_ref: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index(
    "uq_credit_transactions_payment_grant",
    CreditTransaction.biz_type,
    CreditTransaction.biz_ref,
    CreditTransaction.type,
    unique=True,
    postgresql_where=text("biz_type = 'payment' AND type = 'grant' AND biz_ref IS NOT NULL"),
    sqlite_where=text("biz_type = 'payment' AND type = 'grant' AND biz_ref IS NOT NULL"),
)


class AdminIdempotencyKey(Base):
    __tablename__ = "admin_idempotency_keys"
    __table_args__ = (
        CheckConstraint("scope in ('quota_grant')", name="ck_admin_idempotency_scope_valid"),
        Index("uq_admin_idempotency_scope_key", "admin_id", "scope", "key", unique=True),
        Index(
            "ix_admin_idempotency_business_window",
            "admin_id",
            "scope",
            "business_fingerprint",
            "fingerprint_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))
    business_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fingerprint_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payment_orders_amount_cents_positive"),
        CheckConstraint("credits > 0", name="ck_payment_orders_credits_positive"),
        CheckConstraint("provider in ('alipay', 'wechat')", name="ck_payment_orders_provider_valid"),
        CheckConstraint(
            "status in ('pending', 'paid', 'closed', 'failed')",
            name="ck_payment_orders_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # alipay/wechat
    package_id: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    code_url: Mapped[str | None] = mapped_column(Text)
    provider_trade_no: Mapped[str | None] = mapped_column(String(128))
    raw: Mapped[dict | None] = mapped_column(JSONType)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index(
    "uq_payment_orders_provider_trade_no",
    PaymentOrder.provider,
    PaymentOrder.provider_trade_no,
    unique=True,
    postgresql_where=text("provider_trade_no IS NOT NULL"),
    sqlite_where=text("provider_trade_no IS NOT NULL"),
)


class PaymentPackage(Base):
    __tablename__ = "payment_packages"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payment_packages_amount_cents_positive"),
        CheckConstraint("credits > 0", name="ck_payment_packages_credits_positive"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    badge: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaymentProviderConfig(Base):
    __tablename__ = "payment_provider_configs"
    __table_args__ = (
        CheckConstraint(
            "provider in ('alipay', 'wechat')",
            name="ck_payment_provider_configs_provider_valid",
        ),
        CheckConstraint("mode in ('mock', 'live')", name="ck_payment_provider_configs_mode_valid"),
    )

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)  # alipay/wechat
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="mock", nullable=False)  # mock/live
    public_config: Mapped[dict | None] = mapped_column(JSONType)
    secret_config: Mapped[dict | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UploadedAsset(Base):
    __tablename__ = "uploaded_assets"
    __table_args__ = (
        CheckConstraint("bytes IS NULL OR bytes >= 0", name="ck_uploaded_assets_bytes_nonnegative"),
        CheckConstraint("duration IS NULL OR duration >= 0", name="ck_uploaded_assets_duration_nonnegative"),
        Index("ix_uploaded_assets_user_created", "user_id", "created_at"),
        Index(
            "ix_uploaded_assets_user_favorite_created",
            "user_id",
            "favorite",
            "created_at",
        ),
        Index("ix_uploaded_assets_user_retained", "user_id", "retained_at"),
    )

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    mime: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ParseRecord(Base):
    __tablename__ = "parse_records"
    __table_args__ = (
        CheckConstraint("status in ('queued', 'running', 'done', 'failed')", name="ck_parse_records_status_valid"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    assets: Mapped[list | None] = mapped_column(JSONType)  # [{type,url,thumb}]
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/done/failed
    error: Mapped[str | None] = mapped_column(Text)
    cached_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReverseOperation(Base):
    __tablename__ = "reverse_operations"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'needs_confirmation', 'succeeded', 'failed', 'canceled')",
            name="ck_reverse_operations_status_valid",
        ),
        CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_reverse_operations_progress_range",
        ),
        CheckConstraint("cost_frozen >= 0", name="ck_reverse_operations_cost_frozen_nonnegative"),
        CheckConstraint("cost_settled >= 0", name="ck_reverse_operations_cost_settled_nonnegative"),
        Index("uq_reverse_operations_user_client_request_id", "user_id", "client_request_id", unique=True),
        Index(
            "uq_reverse_operations_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
        Index("ix_reverse_operations_user_created", "user_id", "created_at"),
        Index("ix_reverse_operations_user_target_created", "user_id", "target", "created_at"),
        Index("ix_reverse_operations_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id"),
        index=True,
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("generation_quotes.id"),
        index=True,
    )
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_url: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_focus: Mapped[str] = mapped_column(
        String(32), default="comprehensive", server_default="comprehensive", nullable=False
    )
    analysis_precision: Mapped[str] = mapped_column(
        String(16), default="standard", server_default="standard", nullable=False
    )
    output_purpose: Mapped[str] = mapped_column(
        String(32), default="generation", server_default="generation", nullable=False
    )
    custom_instruction: Mapped[str | None] = mapped_column(String(500))
    include_audio: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    source_range: Mapped[dict | None] = mapped_column(JSONType)
    source_ranges: Mapped[list | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    phase: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_context: Mapped[dict | None] = mapped_column(JSONType)
    model_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    template_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSONType)
    raw_provider_result: Mapped[dict | None] = mapped_column(JSONType)
    normalized_result: Mapped[dict | None] = mapped_column(JSONType)
    result_schema_version: Mapped[str] = mapped_column(
        String(32), default="reverse.v2", server_default="reverse.v2", nullable=False
    )
    applied_result_version: Mapped[int | None] = mapped_column(Integer)
    retry_of_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id"), index=True
    )
    charged_credits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_frozen: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_settled: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reference_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReverseOperationBatch(Base):
    __tablename__ = "reverse_operation_batches"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'needs_confirmation', 'partial', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_reverse_operation_batches_status_valid",
        ),
        CheckConstraint(
            "total_count >= 1 AND total_count <= 20",
            name="ck_reverse_operation_batches_total_count_range",
        ),
        Index(
            "uq_reverse_operation_batches_user_client_request_id",
            "user_id",
            "client_request_id",
            unique=True,
        ),
        Index("ix_reverse_operation_batches_user_created", "user_id", "created_at"),
        Index("ix_reverse_operation_batches_status_updated", "status", "updated_at"),
        Index(
            "uq_reverse_operation_batches_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("generation_quotes.id"),
        index=True,
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    shared_config_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    status_counts: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReverseOperationBatchItem(Base):
    __tablename__ = "reverse_operation_batch_items"
    __table_args__ = (
        CheckConstraint("item_index >= 0", name="ck_reverse_operation_batch_items_index_nonnegative"),
        Index(
            "uq_reverse_operation_batch_items_batch_index",
            "batch_id",
            "item_index",
            unique=True,
        ),
        Index(
            "uq_reverse_operation_batch_items_operation",
            "operation_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reverse_operation_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reverse_operations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReverseResultRevision(Base):
    __tablename__ = "reverse_result_revisions"
    __table_args__ = (
        CheckConstraint(
            "source in ('provider_raw', 'normalized', 'user_edit', 'applied', "
            "'model_compiled', 'generation')",
            name="ck_reverse_result_revisions_source_valid",
        ),
        Index(
            "uq_reverse_result_revisions_operation_version",
            "operation_id",
            "version",
            unique=True,
        ),
        CheckConstraint(
            "lineage_status in ('verified', 'legacy_unverified')",
            name="ck_reverse_result_revisions_lineage_status_valid",
        ),
        CheckConstraint(
            "evidence_review_action IS NULL OR evidence_review_action in "
            "('not_applicable', 'inherited', 'updated', 'cleared')",
            name="ck_reverse_result_revisions_evidence_action_valid",
        ),
        CheckConstraint(
            "parent_revision_id IS NULL OR parent_revision_id <> id",
            name="ck_reverse_result_revisions_parent_not_self",
        ),
        Index("ix_reverse_result_revisions_user_created", "user_id", "created_at"),
        Index("ix_reverse_result_revisions_parent_revision_id", "parent_revision_id"),
        Index("ix_reverse_result_revisions_lineage_status", "lineage_status"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    parent_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        nullable=True,
    )
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    source_fingerprints: Mapped[list | None] = mapped_column(JSONType)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    lineage_status: Mapped[str] = mapped_column(
        String(24),
        default="legacy_unverified",
        server_default="legacy_unverified",
        nullable=False,
    )
    evidence_review_action: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromptOptimizationProposal(Base):
    __tablename__ = "prompt_optimization_proposals"
    __table_args__ = (
        CheckConstraint(
            "status in ('proposed', 'accepted', 'partially_accepted', 'rejected', 'expired')",
            name="ck_prompt_optimization_proposals_status_valid",
        ),
        CheckConstraint("version >= 1", name="ck_prompt_optimization_proposals_version_positive"),
        CheckConstraint(
            "optimization_kind in ('rewrite', 'model_compile')",
            name="ck_prompt_optimization_proposals_kind_valid",
        ),
        Index(
            "uq_prompt_optimization_proposals_user_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_prompt_optimization_proposals_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
        Index("ix_prompt_optimization_proposals_source_revision", "source_revision_id"),
        Index(
            "uq_prompt_optimization_proposals_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=True
    )
    source_revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_result_revisions.id", ondelete="CASCADE"), nullable=True
    )
    target_model_config_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_configs.id"), nullable=False
    )
    capability_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_capability_versions.id"), nullable=False
    )
    optimizer_model_config_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("model_configs.id"), nullable=True
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("generation_quotes.id"),
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="proposed", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    category: Mapped[str] = mapped_column(String(8), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    optimization_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    original: Mapped[dict] = mapped_column(JSONType, nullable=False)
    suggestion: Mapped[dict] = mapped_column(JSONType, nullable=False)
    diff: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    constraint_coverage: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    warnings: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    catalog_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    accepted_segment_ids: Mapped[list | None] = mapped_column(JSONType)
    rejected_segment_ids: Mapped[list | None] = mapped_column(JSONType)
    decision_result: Mapped[dict | None] = mapped_column(JSONType)
    charged_credits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReverseOperationFeedback(Base):
    __tablename__ = "reverse_operation_feedback"
    __table_args__ = (
        CheckConstraint(
            "rating in ('useful', 'not_useful')",
            name="ck_reverse_operation_feedback_rating_valid",
        ),
        Index("uq_reverse_operation_feedback_operation", "operation_id", unique=True),
        Index("ix_reverse_operation_feedback_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    issue_types: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GenTask(Base):
    __tablename__ = "gen_tasks"
    __table_args__ = (
        CheckConstraint(
            "source_type IS NULL OR source_type in ('image', 'video')",
            name="ck_gen_tasks_source_type_valid",
        ),
        CheckConstraint("category in ('image', 'video')", name="ck_gen_tasks_category_valid"),
        CheckConstraint("stage in ('preview', 'final')", name="ck_gen_tasks_stage_valid"),
        CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed', 'needs_review', 'canceled')",
            name="ck_gen_tasks_status_valid",
        ),
        CheckConstraint(
            "phase IS NULL OR phase in ('rendering', 'submitting', 'polling', 'downloading', 'reconciling')",
            name="ck_gen_tasks_phase_valid",
        ),
        CheckConstraint("cost_frozen >= 0", name="ck_gen_tasks_cost_frozen_nonnegative"),
        CheckConstraint("cost_settled >= 0", name="ck_gen_tasks_cost_settled_nonnegative"),
        CheckConstraint(
            "(reverse_operation_id IS NULL AND source_revision_id IS NULL "
            "AND compiled_revision_id IS NULL AND generation_revision_id IS NULL) OR "
            "(reverse_operation_id IS NOT NULL AND source_revision_id IS NOT NULL "
            "AND compiled_revision_id IS NOT NULL AND generation_revision_id IS NOT NULL)",
            name="ck_gen_tasks_reverse_lineage_complete",
        ),
        CheckConstraint(
            "retry_of_task_id IS NULL OR retry_of_task_id <> id",
            name="ck_gen_tasks_retry_not_self",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id"),
        index=True,
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "generation_quotes.id",
            name="fk_gen_tasks_quote_id_generation_quotes",
            use_alter=True,
        ),
        index=True,
    )
    reverse_operation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_operations.id"),
        index=True,
    )
    source_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        index=True,
    )
    compiled_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        index=True,
    )
    generation_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reverse_result_revisions.id"),
        index=True,
    )
    source_asset_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String(8))  # image/video
    category: Mapped[str] = mapped_column(String(8), nullable=False)  # image/video
    stage: Mapped[str] = mapped_column(String(8), default="preview")  # preview/final
    prompt: Mapped[dict | None] = mapped_column(JSONType)  # structured + final_text
    model_use: Mapped[str | None] = mapped_column(String(16))  # vision/image/video
    params: Mapped[dict | None] = mapped_column(JSONType)
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/succeeded/failed/canceled
    cost_frozen: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_settled: Mapped[int] = mapped_column(BigInteger, default=0)
    external_task_id: Mapped[str | None] = mapped_column(Text)  # async video task id
    # DB-recoverable generation sub-state: image rendering, video submit/poll/download, reconciliation.
    phase: Mapped[str | None] = mapped_column(String(16))
    external_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_task_id: Mapped[int | None] = mapped_column(BigInteger)  # video final -> preview
    retry_of_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("gen_tasks.id"),
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationDispatch(Base):
    """Durable Celery publication intent for one generation task attempt."""

    __tablename__ = "generation_dispatches"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_generation_dispatches_attempt_positive"),
        CheckConstraint(
            "publish_attempts >= 0",
            name="ck_generation_dispatches_publish_attempts_nonnegative",
        ),
        CheckConstraint(
            "status in ('pending', 'publishing', 'published', 'unknown', "
            "'failed', 'completed', 'needs_review')",
            name="ck_generation_dispatches_status_valid",
        ),
        Index(
            "uq_generation_dispatches_task_attempt",
            "task_id",
            "attempt",
            unique=True,
        ),
        Index(
            "uq_generation_dispatches_celery_task_id",
            "celery_task_id",
            unique=True,
        ),
        Index(
            "ix_generation_dispatches_reconcile",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("gen_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    task_name: Mapped[str] = mapped_column(String(64), nullable=False)
    celery_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index(
    "uq_gen_tasks_active_final_per_preview",
    GenTask.user_id,
    GenTask.parent_task_id,
    unique=True,
    postgresql_where=text(
        "category = 'video' AND stage = 'final' "
        "AND status IN ('queued','running','needs_review') "
        "AND parent_task_id IS NOT NULL"
    ),
    sqlite_where=text(
        "category = 'video' AND stage = 'final' "
        "AND status IN ('queued','running','needs_review') "
        "AND parent_task_id IS NOT NULL"
    ),
)

Index(
    "uq_gen_tasks_user_client_request_id",
    GenTask.user_id,
    GenTask.client_request_id,
    unique=True,
    postgresql_where=text("client_request_id IS NOT NULL"),
    sqlite_where=text("client_request_id IS NOT NULL"),
)

Index(
    "uq_gen_tasks_quote_id",
    GenTask.quote_id,
    unique=True,
    postgresql_where=text("quote_id IS NOT NULL"),
    sqlite_where=text("quote_id IS NOT NULL"),
)

Index(
    "uq_gen_tasks_compiled_revision_id",
    GenTask.compiled_revision_id,
    unique=True,
    postgresql_where=text("compiled_revision_id IS NOT NULL"),
    sqlite_where=text("compiled_revision_id IS NOT NULL"),
)

Index(
    "uq_gen_tasks_generation_revision_id",
    GenTask.generation_revision_id,
    unique=True,
    postgresql_where=text("generation_revision_id IS NOT NULL"),
    sqlite_where=text("generation_revision_id IS NOT NULL"),
)


class GenAsset(Base):
    __tablename__ = "gen_assets"
    __table_args__ = (
        CheckConstraint("type in ('image', 'video')", name="ck_gen_assets_type_valid"),
        CheckConstraint(
            "moderation_status in ('active', 'takedown')",
            name="ck_gen_assets_moderation_status_valid",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="ck_gen_assets_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_gen_assets_height_positive"),
        CheckConstraint("duration IS NULL OR duration >= 0", name="ck_gen_assets_duration_nonnegative"),
        CheckConstraint("bytes IS NULL OR bytes >= 0", name="ck_gen_assets_bytes_nonnegative"),
        Index("ix_gen_assets_user_created", "user_id", "created_at"),
        Index("ix_gen_assets_user_favorite_created", "user_id", "favorite", "created_at"),
        Index("ix_gen_assets_user_retained", "user_id", "retained_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("gen_tasks.id"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(8))  # image/video
    preview_url: Mapped[str | None] = mapped_column(Text)
    hd_url: Mapped[str | None] = mapped_column(Text)
    watermarked: Mapped[bool] = mapped_column(Boolean, default=True)
    unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    moderation_status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    retained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class UserPrompt(Base):
    __tablename__ = "user_prompts"
    __table_args__ = (
        CheckConstraint("category in ('image', 'video', 'general')", name="ck_user_prompts_category_valid"),
        CheckConstraint(
            "source in ('manual', 'reverse', 'generate', 'library')",
            name="ck_user_prompts_source_valid",
        ),
        CheckConstraint("usage_count >= 0", name="ck_user_prompts_usage_count_nonnegative"),
        Index("ix_user_prompts_user_created", "user_id", "created_at"),
        Index("ix_user_prompts_user_favorite", "user_id", "favorite"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(16), default="general", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreationRecipe(Base):
    __tablename__ = "creation_recipes"
    __table_args__ = (
        CheckConstraint("category in ('image', 'video')", name="ck_creation_recipes_category_valid"),
        CheckConstraint(
            "visibility in ('private', 'public')", name="ck_creation_recipes_visibility_valid"
        ),
        CheckConstraint(
            "moderation_status in ('draft', 'pending', 'approved', 'rejected')",
            name="ck_creation_recipes_moderation_status_valid",
        ),
        CheckConstraint("current_version >= 1", name="ck_creation_recipes_version_positive"),
        CheckConstraint(
            "approved_version IS NULL OR approved_version >= 1",
            name="ck_creation_recipes_approved_version_positive",
        ),
        Index("ix_creation_recipes_user_updated", "user_id", "updated_at"),
        Index("ix_creation_recipes_public_updated", "visibility", "updated_at"),
        Index(
            "ix_creation_recipes_moderation_updated",
            "moderation_status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("reverse_operations.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="private", nullable=False)
    moderation_status: Mapped[str] = mapped_column(
        String(16), default="draft", server_default="draft", nullable=False
    )
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    approved_version: Mapped[int | None] = mapped_column(Integer)
    cover_asset_url: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    review_note: Mapped[str | None] = mapped_column(String(500))
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreationRecipeVersion(Base):
    __tablename__ = "creation_recipe_versions"
    __table_args__ = (
        Index(
            "uq_creation_recipe_versions_recipe_version", "recipe_id", "version", unique=True
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="creation-recipe.v1", nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    metadata_snapshot: Mapped[dict] = mapped_column(
        JSONType,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreationRecipeShare(Base):
    __tablename__ = "creation_recipe_shares"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_creation_recipe_shares_version_positive"),
        CheckConstraint(
            "status in ('active', 'revoked')",
            name="ck_creation_recipe_shares_status_valid",
        ),
        Index("uq_creation_recipe_shares_slug", "slug", unique=True),
        Index(
            "ix_creation_recipe_shares_recipe_status_created",
            "recipe_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_creation_recipe_shares_owner_created",
            "owner_user_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreationRecipeUsageEvent(Base):
    __tablename__ = "creation_recipe_usage_events"
    __table_args__ = (
        CheckConstraint(
            "event_type in ('apply', 'clone', 'generation_prepare', 'generation_submit')",
            name="ck_creation_recipe_usage_events_type_valid",
        ),
        CheckConstraint(
            "source in ('owner', 'public', 'share')",
            name="ck_creation_recipe_usage_events_source_valid",
        ),
        CheckConstraint(
            "recipe_version >= 1",
            name="ck_creation_recipe_usage_events_version_positive",
        ),
        Index(
            "uq_creation_recipe_usage_events_user_client",
            "user_id",
            "client_event_id",
            unique=True,
        ),
        Index(
            "ix_creation_recipe_usage_events_recipe_created",
            "recipe_id",
            "created_at",
        ),
        Index(
            "ix_creation_recipe_usage_events_generation_task",
            "generation_task_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="SET NULL")
    )
    recipe_version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    share_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("creation_recipe_shares.id", ondelete="SET NULL"), index=True
    )
    derived_recipe_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="SET NULL"), index=True
    )
    generation_task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("gen_tasks.id", ondelete="SET NULL")
    )
    client_event_id: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetFolder(Base):
    __tablename__ = "asset_folders"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="ck_asset_folders_sort_order_nonnegative"),
        Index("ix_asset_folders_user_parent_sort", "user_id", "parent_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("asset_folders.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AssetFolderItem(Base):
    __tablename__ = "asset_folder_items"
    __table_args__ = (
        Index("uq_asset_folder_items_user_asset", "user_id", "asset_ref", unique=True),
        Index("ix_asset_folder_items_folder_created", "folder_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    folder_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("asset_folders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserAssetMetadata(Base):
    """User-owned metadata shared by generated and uploaded asset refs."""

    __tablename__ = "user_asset_metadata"
    __table_args__ = (
        CheckConstraint(
            "media_type in ('image', 'video')",
            name="ck_user_asset_metadata_media_type_valid",
        ),
        CheckConstraint(
            "analysis_status in ('pending', 'ready', 'degraded')",
            name="ck_user_asset_metadata_analysis_status_valid",
        ),
        Index(
            "uq_user_asset_metadata_user_asset",
            "user_id",
            "asset_ref",
            unique=True,
        ),
        Index(
            "ix_user_asset_metadata_user_content_hash",
            "user_id",
            "content_sha256",
        ),
        Index(
            "ix_user_asset_metadata_user_perceptual_hash",
            "user_id",
            "perceptual_hash",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(8), nullable=False)
    tags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    perceptual_hash: Mapped[str | None] = mapped_column(String(16))
    perceptual_hash_algorithm: Mapped[str | None] = mapped_column(String(32))
    analysis_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    analysis_error: Mapped[str | None] = mapped_column(String(500))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MediaProject(Base):
    __tablename__ = "media_projects"
    __table_args__ = (
        CheckConstraint(
            "project_type in ('image', 'video', 'mixed')",
            name="ck_media_projects_type_valid",
        ),
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_media_projects_status_valid",
        ),
        CheckConstraint(
            "auto_archive_after_days IS NULL OR "
            "(auto_archive_after_days >= 1 AND auto_archive_after_days <= 3650)",
            name="ck_media_projects_auto_archive_days_valid",
        ),
        Index("ix_media_projects_user_status_updated", "user_id", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    project_type: Mapped[str] = mapped_column(String(16), default="mixed", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    cover_asset_ref: Mapped[str | None] = mapped_column(String(512))
    auto_archive_after_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MediaProjectAsset(Base):
    __tablename__ = "media_project_assets"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="ck_media_project_assets_sort_order_nonnegative"),
        Index("uq_media_project_assets_project_asset", "project_id", "asset_ref", unique=True),
        Index("ix_media_project_assets_project_sort", "project_id", "sort_order", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="source", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaProjectRecipe(Base):
    __tablename__ = "media_project_recipes"
    __table_args__ = (
        Index("uq_media_project_recipes_project_recipe", "project_id", "recipe_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("creation_recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaProjectTask(Base):
    __tablename__ = "media_project_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_kind in ('generation', 'reverse', 'parse', 'workflow')",
            name="ck_media_project_tasks_kind_valid",
        ),
        Index(
            "uq_media_project_tasks_project_kind_task",
            "project_id",
            "task_kind",
            "task_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("media_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserDraft(Base):
    __tablename__ = "user_drafts"
    __table_args__ = (
        Index("uq_user_drafts_user_key", "user_id", "key", unique=True),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AssetReport(Base):
    __tablename__ = "asset_reports"
    __table_args__ = (
        CheckConstraint(
            "status in ('open', 'dismissed', 'takedown')",
            name="ck_asset_reports_status_valid",
        ),
        CheckConstraint(
            "reason in ('copyright', 'sensitive', 'illegal', 'privacy', 'other')",
            name="ck_asset_reports_reason_valid",
        ),
        Index("ix_asset_reports_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    asset_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("gen_assets.id"), index=True)
    reporter_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    handled_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    handle_note: Mapped[str | None] = mapped_column(String(500))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GatewayCall(Base):
    """Per-invocation gateway call log (real cost / usage accounting).

    Records every model-gateway call so spend can be reconciled against the
    provider's actual consumption (token usage where the provider reports it),
    not just the internal credit estimate."""

    __tablename__ = "gateway_calls"
    __table_args__ = (
        CheckConstraint(
            "kind in ('reverse', 'prompt_optimize', 'image', 'video_submit', 'video_poll', 'video_download')",
            name="ck_gateway_calls_kind_valid",
        ),
        CheckConstraint("status IS NULL OR status in ('ok', 'failed')", name="ck_gateway_calls_status_valid"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_gateway_calls_latency_nonnegative"),
        CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0", name="ck_gateway_calls_prompt_tokens_nonnegative"),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_gateway_calls_completion_tokens_nonnegative",
        ),
        CheckConstraint("total_tokens IS NULL OR total_tokens >= 0", name="ck_gateway_calls_total_tokens_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16))  # reverse/prompt_optimize/image/video_*
    model_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(16))  # ok/failed
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_id_id", "user_id", "id"),
        Index("ix_audit_logs_action_id", "action", "id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    biz_type: Mapped[str | None] = mapped_column(String(32))
    biz_id: Mapped[int | None] = mapped_column(BigInteger)
    ip: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Runtime-editable config (seeded from models.yaml) ---


class ModelConfig(Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        Index("ix_model_configs_use", "use"),
        Index(
            "uq_model_configs_use_model_id",
            "use",
            "model_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_model_configs_default_per_use",
            "use",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
        CheckConstraint("use in ('vision', 'image', 'video', 'prompt')", name="ck_model_configs_use_valid"),
        CheckConstraint(
            "gateway_format IS NULL OR gateway_format in ('openai', 'ark', 'anthropic')",
            name="ck_model_configs_gateway_format_valid",
        ),
        CheckConstraint("cost_credits >= 0", name="ck_model_configs_cost_credits_nonnegative"),
        CheckConstraint("unlock_cost >= 0", name="ck_model_configs_unlock_cost_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    use: Mapped[str] = mapped_column(String(16), nullable=False)  # vision/image/video/prompt
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    gateway_format: Mapped[str | None] = mapped_column(String(16))  # openai | ark | anthropic
    cost_credits: Mapped[int] = mapped_column(BigInteger, default=1)  # cost to run / freeze
    unlock_cost: Mapped[int] = mapped_column(BigInteger, default=0)  # extra cost to unlock HD
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    extra: Mapped[dict | None] = mapped_column(JSONType)  # endpoint paths / field maps for video
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelCapabilityVersion(Base):
    __tablename__ = "model_capability_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_model_capability_versions_version_positive"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_model_capability_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            name="ck_model_capability_versions_active_status",
        ),
        Index(
            "uq_model_capability_versions_model_version",
            "model_config_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_model_capability_versions_active",
            "model_config_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="capability.v1", server_default="capability.v1", nullable=False
    )
    capabilities: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    metadata_snapshot: Mapped[dict] = mapped_column(
        JSONType,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    status: Mapped[str] = mapped_column(
        String(16), default="published", server_default="published", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "model_capability_versions.id",
            name="fk_capability_versions_source",
            ondelete="SET NULL",
        ),
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelPriceVersion(Base):
    __tablename__ = "model_price_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_model_price_versions_version_positive"),
        CheckConstraint("base_cost_credits >= 0", name="ck_model_price_versions_base_cost_nonnegative"),
        CheckConstraint("unlock_cost_credits >= 0", name="ck_model_price_versions_unlock_cost_nonnegative"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_model_price_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            name="ck_model_price_versions_active_status",
        ),
        Index(
            "uq_model_price_versions_model_version",
            "model_config_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_model_price_versions_active",
            "model_config_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="credit-price.v1", server_default="credit-price.v1", nullable=False
    )
    base_cost_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unlock_cost_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pricing: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="published", server_default="published", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "model_price_versions.id",
            name="fk_price_versions_source",
            ondelete="SET NULL",
        ),
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelRoute(Base):
    __tablename__ = "model_routes"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="ck_model_routes_priority_nonnegative"),
        CheckConstraint("config_revision >= 1", name="ck_model_routes_revision_positive"),
        CheckConstraint(
            "health_status in ('closed', 'open', 'half_open')",
            name="ck_model_routes_health_status_valid",
        ),
        CheckConstraint(
            "failure_threshold >= 1", name="ck_model_routes_failure_threshold_positive"
        ),
        CheckConstraint("window_seconds >= 1", name="ck_model_routes_window_positive"),
        CheckConstraint(
            "cooldown_seconds >= 1", name="ck_model_routes_cooldown_positive"
        ),
        Index("uq_model_routes_model_key", "model_config_id", "route_key", unique=True),
        Index(
            "ix_model_routes_select",
            "model_config_id",
            "enabled",
            "priority",
            "id",
        ),
        Index("ix_model_routes_health", "health_status", "cooldown_until", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    gateway_format: Mapped[str | None] = mapped_column(String(16))
    extra: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    managed_by_model_config: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    failure_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="closed"
    )
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    half_open_claimed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ema_ms: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelRouteVersion(Base):
    __tablename__ = "model_route_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_model_route_versions_version_positive"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_model_route_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'disabled', 'retired'))",
            name="ck_model_route_versions_active_status",
        ),
        Index(
            "uq_model_route_versions_route_version",
            "route_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_model_route_versions_active",
            "route_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="model-route.v2", server_default="model-route.v2", nullable=False
    )
    config_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="published", server_default="published", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "model_route_versions.id",
            name="fk_route_versions_source",
            ondelete="SET NULL",
        ),
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelRouteHealthEvent(Base):
    __tablename__ = "model_route_health_events"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('success', 'failure', 'ignored')",
            name="ck_model_route_health_events_outcome_valid",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_model_route_health_events_latency_nonnegative",
        ),
        Index("ix_model_route_health_events_route_created", "route_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    counts_toward_circuit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolDefinition(Base):
    __tablename__ = "tool_definitions"
    __table_args__ = (
        CheckConstraint(
            "category in ('image', 'video', 'workflow', 'utility')",
            name="ck_tool_definitions_category_valid",
        ),
        Index("uq_tool_definitions_slug", "slug", unique=True),
        Index("ix_tool_definitions_enabled_sort", "enabled", "sort_order", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    renderer: Mapped[str] = mapped_column(String(64), default="studio", nullable=False)
    entry_path: Mapped[str] = mapped_column(String(512), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ToolVersion(Base):
    __tablename__ = "tool_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_tool_versions_version_positive"),
        CheckConstraint(
            "status in ('draft', 'published', 'disabled', 'retired')",
            name="ck_tool_versions_status_valid",
        ),
        CheckConstraint(
            "(is_active AND status = 'published') OR "
            "(NOT is_active AND status in ('draft', 'published', 'disabled', 'retired'))",
            name="ck_tool_versions_active_status",
        ),
        Index(
            "uq_tool_versions_definition_version",
            "tool_definition_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_tool_versions_active",
            "tool_definition_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    tool_definition_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="tool.v1", server_default="tool.v1", nullable=False
    )
    input_schema: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    workflow: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    pricing_policy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    capabilities: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    metadata_snapshot: Mapped[dict] = mapped_column(
        JSONType,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    status: Mapped[str] = mapped_column(
        String(16), default="published", server_default="published", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tool_versions.id",
            name="fk_tool_versions_source",
            ondelete="SET NULL",
        ),
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ToolRun(Base):
    __tablename__ = "tool_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'succeeded', "
            "'failed', 'canceled', 'compensating')",
            name="ck_tool_runs_status_valid",
        ),
        Index(
            "uq_tool_runs_user_client_request_id",
            "user_id",
            "client_request_id",
            unique=True,
        ),
        Index("ix_tool_runs_user_created", "user_id", "created_at", "id"),
        Index("ix_tool_runs_status_updated", "status", "updated_at", "id"),
        Index(
            "uq_tool_runs_quote_id",
            "quote_id",
            unique=True,
            postgresql_where=text("quote_id IS NOT NULL"),
            sqlite_where=text("quote_id IS NOT NULL"),
        ),
        CheckConstraint("cost_frozen >= 0", name="ck_tool_runs_cost_frozen_nonnegative"),
        CheckConstraint("cost_settled >= 0", name="ck_tool_runs_cost_settled_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_definition_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_definitions.id"),
        nullable=False,
        index=True,
    )
    tool_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_versions.id"),
        nullable=False,
        index=True,
    )
    quote_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("generation_quotes.id"),
        index=True,
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    pricing_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    cost_frozen: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_settled: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'succeeded', "
            "'failed', 'canceled', 'compensating')",
            name="ck_workflow_runs_status_valid",
        ),
        CheckConstraint("revision >= 0", name="ck_workflow_runs_revision_nonnegative"),
        CheckConstraint(
            "length(compiled_snapshot_hash) = 64",
            name="ck_workflow_runs_compiled_snapshot_hash_length",
        ),
        Index("uq_workflow_runs_tool_run_id", "tool_run_id", unique=True),
        Index("ix_workflow_runs_user_created", "user_id", "created_at", "id"),
        Index("ix_workflow_runs_status_updated", "status", "updated_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    tool_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    compiled_snapshot: Mapped[dict] = mapped_column(
        JSONType,
        nullable=False,
        default=_default_workflow_compiled_snapshot,
    )
    compiled_snapshot_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=_default_workflow_compiled_snapshot_hash,
    )
    compiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    current_node_key: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ToolNodeRun(Base):
    __tablename__ = "tool_node_runs"
    __table_args__ = (
        CheckConstraint(
            "node_type in ('parse', 'reverse', 'manual_review', 'compile', "
            "'generate', 'compose', 'export')",
            name="ck_tool_node_runs_type_valid",
        ),
        CheckConstraint(
            "status in ('queued', 'running', 'waiting_review', 'waiting_external', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_tool_node_runs_status_valid",
        ),
        CheckConstraint(
            "compensation_status in ('none', 'pending', 'running', 'succeeded', 'failed')",
            name="ck_tool_node_runs_compensation_status_valid",
        ),
        CheckConstraint("topological_index >= 0", name="ck_tool_node_runs_topology_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="ck_tool_node_runs_attempt_count_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="ck_tool_node_runs_max_attempts_positive"),
        CheckConstraint("revision >= 0", name="ck_tool_node_runs_revision_nonnegative"),
        Index(
            "uq_tool_node_runs_workflow_node_key",
            "workflow_run_id",
            "node_key",
            unique=True,
        ),
        Index("ix_tool_node_runs_workflow_topology", "workflow_run_id", "topological_index"),
        Index("ix_tool_node_runs_status_available", "status", "available_at", "id"),
        Index("ix_tool_node_runs_external", "external_kind", "external_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    topological_index: Mapped[int] = mapped_column(Integer, nullable=False)
    depends_on: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    config_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    compensation_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    input_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    external_kind: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(256))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatch_token: Mapped[str | None] = mapped_column(String(64))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compensation_status: Mapped[str] = mapped_column(
        String(16), default="none", nullable=False
    )
    compensation_error: Mapped[str | None] = mapped_column(Text)
    compensation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compensation_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ToolNodeAttempt(Base):
    __tablename__ = "tool_node_attempts"
    __table_args__ = (
        CheckConstraint(
            "kind in ('execution', 'compensation')",
            name="ck_tool_node_attempts_kind_valid",
        ),
        CheckConstraint(
            "status in ('running', 'waiting_review', 'waiting_external', "
            "'succeeded', 'failed', 'canceled')",
            name="ck_tool_node_attempts_status_valid",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_tool_node_attempts_number_positive"),
        Index(
            "uq_tool_node_attempts_node_kind_number",
            "node_run_id",
            "kind",
            "attempt_number",
            unique=True,
        ),
        Index("uq_tool_node_attempts_dispatch_token", "dispatch_token", unique=True),
        Index("ix_tool_node_attempts_node_created", "node_run_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    node_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_node_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), default="execution", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dispatch_token: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    input_snapshot: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    external_kind: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(256))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkflowDispatch(Base):
    """Durable broker publication intent for orchestration and node execution."""

    __tablename__ = "workflow_dispatches"
    __table_args__ = (
        CheckConstraint(
            "kind in ('orchestrate', 'node')",
            name="ck_workflow_dispatches_kind_valid",
        ),
        CheckConstraint(
            "status in ('pending', 'publishing', 'published', 'unknown', "
            "'completed', 'failed', 'needs_review')",
            name="ck_workflow_dispatches_status_valid",
        ),
        CheckConstraint(
            "publish_attempts >= 0",
            name="ck_workflow_dispatches_publish_attempts_nonnegative",
        ),
        Index("uq_workflow_dispatches_dedupe_key", "dedupe_key", unique=True),
        Index("uq_workflow_dispatches_celery_task_id", "celery_task_id", unique=True),
        Index(
            "ix_workflow_dispatches_reconcile",
            "status",
            "next_attempt_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tool_node_runs.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    celery_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dispatch_token: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class GenerationQuote(Base):
    __tablename__ = "generation_quotes"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'consumed', 'expired', 'canceled')",
            name="ck_generation_quotes_status_valid",
        ),
        CheckConstraint(
            "category in ('image', 'video', 'workflow')",
            name="ck_generation_quotes_category_valid",
        ),
        CheckConstraint(
            "kind in ('generation', 'reverse', 'reverse_batch', 'workflow', "
            "'prompt_optimization', 'asset_unlock')",
            name="ck_generation_quotes_kind_valid",
        ),
        CheckConstraint(
            "stage in ('preview', 'final')",
            name="ck_generation_quotes_stage_valid",
        ),
        CheckConstraint(
            "estimated_credits >= 0",
            name="ck_generation_quotes_estimated_credits_nonnegative",
        ),
        Index("ix_generation_quotes_user_created", "user_id", "created_at"),
        Index("ix_generation_quotes_user_status_expires", "user_id", "status", "expires_at"),
        Index(
            "ix_generation_quotes_user_kind_client_request",
            "user_id",
            "kind",
            "client_request_id",
            "created_at",
        ),
        Index(
            "uq_generation_quotes_consumed_ref",
            "kind",
            "consumed_ref_type",
            "consumed_ref_id",
            unique=True,
            postgresql_where=text("consumed_ref_id IS NOT NULL"),
            sqlite_where=text("consumed_ref_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_configs.id"),
        index=True,
    )
    capability_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_capability_versions.id"),
        index=True,
    )
    price_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_price_versions.id"),
        index=True,
    )
    tool_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tool_versions.id"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(24), default="generation", server_default="generation", nullable=False
    )
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(8), nullable=False)
    request_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    model_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    pricing_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    price_breakdown: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    subject_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    estimated_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        unique=True,
    )
    consumed_ref_type: Mapped[str | None] = mapped_column(String(32))
    consumed_ref_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
