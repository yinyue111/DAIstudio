"""ORM models. Mirrors the schema in the spec, plus two small config tables
(model_configs, app_settings) so the gateway model ids / costs are editable from
the admin UI instead of code."""
from __future__ import annotations

from datetime import datetime

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
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))
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
    )

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    mime: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    original_filename: Mapped[str | None] = mapped_column(String(255))
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
            "status in ('queued', 'running', 'succeeded', 'failed', 'needs_review')",
            name="ck_gen_tasks_status_valid",
        ),
        CheckConstraint(
            "phase IS NULL OR phase in ('submitting', 'polling', 'downloading', 'reconciling')",
            name="ck_gen_tasks_phase_valid",
        ),
        CheckConstraint("cost_frozen >= 0", name="ck_gen_tasks_cost_frozen_nonnegative"),
        CheckConstraint("cost_settled >= 0", name="ck_gen_tasks_cost_settled_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    source_asset_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String(8))  # image/video
    category: Mapped[str] = mapped_column(String(8), nullable=False)  # image/video
    stage: Mapped[str] = mapped_column(String(8), default="preview")  # preview/final
    prompt: Mapped[dict | None] = mapped_column(JSONType)  # structured + final_text
    model_use: Mapped[str | None] = mapped_column(String(16))  # vision/image/video
    params: Mapped[dict | None] = mapped_column(JSONType)
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/succeeded/failed
    cost_frozen: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_settled: Mapped[int] = mapped_column(BigInteger, default=0)
    external_task_id: Mapped[str | None] = mapped_column(Text)  # async video task id
    # video lifecycle sub-state (DB-recoverable): submitting/polling/downloading
    phase: Mapped[str | None] = mapped_column(String(16))
    external_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_task_id: Mapped[int | None] = mapped_column(BigInteger)  # video final -> preview
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("gen_tasks.id"), index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
            "kind in ('reverse', 'image', 'video_submit', 'video_poll', 'video_download')",
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
    kind: Mapped[str] = mapped_column(String(16))  # reverse/image/video_submit/video_poll
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
        Index("ix_model_configs_use", "use", unique=True),
        CheckConstraint("use in ('vision', 'image', 'video')", name="ck_model_configs_use_valid"),
        CheckConstraint(
            "gateway_format IS NULL OR gateway_format in ('openai', 'ark')",
            name="ck_model_configs_gateway_format_valid",
        ),
        CheckConstraint("cost_credits >= 0", name="ck_model_configs_cost_credits_nonnegative"),
        CheckConstraint("unlock_cost >= 0", name="ck_model_configs_unlock_cost_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    use: Mapped[str] = mapped_column(String(16), nullable=False)  # vision/image/video
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    gateway_format: Mapped[str | None] = mapped_column(String(16))  # openai | ark
    cost_credits: Mapped[int] = mapped_column(BigInteger, default=1)  # cost to run / freeze
    unlock_cost: Mapped[int] = mapped_column(BigInteger, default=0)  # extra cost to unlock HD
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    extra: Mapped[dict | None] = mapped_column(JSONType)  # endpoint paths / field maps for video
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
