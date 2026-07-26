"""user soft deletion, refunds/invoices, audio metadata, route/tool soft delete

Covers seven schema changes accumulated on feat/audit-remediation:
1. users.deleted_at (soft account deletion timestamp)
2. payment_orders refund + invoice columns
3. payment_orders check constraints (status gains 'refunded', refund/invoice checks)
4. payment_refunds table (channel refund ledger)
5. user_asset_metadata media_type check widened to allow 'audio'
6. model_routes soft delete + partial unique uq_model_routes_model_key
7. tool_definitions soft delete + partial unique uq_tool_definitions_slug

Revision ID: 0081_refunds_soft_delete
Revises: 0080_uploaded_asset_origin
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0081_refunds_soft_delete"
down_revision = "0080_uploaded_asset_origin"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # 1. users: soft-deletion timestamp (non-null means the account is deleted)
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    # 2 & 3. payment_orders: refund + invoice columns and constraints
    with op.batch_alter_table("payment_orders") as batch:
        batch.add_column(
            sa.Column(
                "refunded_amount_cents",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "invoice_status",
                sa.String(length=16),
                nullable=False,
                server_default="none",
            )
        )
        batch.add_column(sa.Column("invoice_type", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("invoice_title", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("invoice_tax_no", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("invoice_email", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("invoice_note", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column("invoice_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("invoice_issued_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.drop_constraint("ck_payment_orders_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_payment_orders_status_valid",
            "status in ('pending', 'paid', 'closed', 'failed', 'refunded')",
        )
        batch.create_check_constraint(
            "ck_payment_orders_refunded_amount_nonnegative",
            "refunded_amount_cents >= 0",
        )
        batch.create_check_constraint(
            "ck_payment_orders_invoice_status_valid",
            "invoice_status in ('none', 'requested', 'issued', 'rejected')",
        )

    # 4. payment_refunds: one row per channel refund attempt
    op.create_table(
        "payment_refunds",
        sa.Column("id", BIGINT_PK, autoincrement=True, primary_key=True),
        sa.Column("refund_no", sa.String(length=64), nullable=False),
        sa.Column(
            "order_id", sa.BigInteger(), sa.ForeignKey("payment_orders.id"), nullable=False
        ),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "credits_reclaimed",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("operator_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("provider_refund_no", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("raw", JSON_TYPE, nullable=True),
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
            "amount_cents > 0", name="ck_payment_refunds_amount_cents_positive"
        ),
        sa.CheckConstraint(
            "credits_reclaimed >= 0",
            name="ck_payment_refunds_credits_reclaimed_nonnegative",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'succeeded', 'failed')",
            name="ck_payment_refunds_status_valid",
        ),
    )
    op.create_index(
        "ix_payment_refunds_refund_no", "payment_refunds", ["refund_no"], unique=True
    )
    op.create_index("ix_payment_refunds_order_id", "payment_refunds", ["order_id"])
    op.create_index("ix_payment_refunds_user_id", "payment_refunds", ["user_id"])
    op.create_index("ix_payment_refunds_status", "payment_refunds", ["status"])

    # 5. user_asset_metadata: allow audio assets
    with op.batch_alter_table("user_asset_metadata") as batch:
        batch.drop_constraint("ck_user_asset_metadata_media_type_valid", type_="check")
        batch.create_check_constraint(
            "ck_user_asset_metadata_media_type_valid",
            "media_type in ('image', 'video', 'audio')",
        )

    # 6. model_routes: soft delete + partial unique (route_key reusable after delete)
    with op.batch_alter_table("model_routes") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.drop_index("uq_model_routes_model_key")
        batch.create_index("ix_model_routes_deleted_at", ["deleted_at"], unique=False)
    op.create_index(
        "uq_model_routes_model_key",
        "model_routes",
        ["model_config_id", "route_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    # 7. tool_definitions: soft delete + partial unique (slug reusable after delete)
    with op.batch_alter_table("tool_definitions") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.drop_index("uq_tool_definitions_slug")
        batch.create_index("ix_tool_definitions_deleted_at", ["deleted_at"], unique=False)
    op.create_index(
        "uq_tool_definitions_slug",
        "tool_definitions",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()

    # 7. tool_definitions: restore full unique on slug
    duplicate = bind.execute(
        sa.text(
            "SELECT slug FROM tool_definitions GROUP BY slug HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "0081 downgrade blocked: deleted tool slugs have been reused; "
            "remove duplicates before downgrade"
        )
    with op.batch_alter_table("tool_definitions") as batch:
        batch.drop_index("uq_tool_definitions_slug")
        batch.drop_index("ix_tool_definitions_deleted_at")
        batch.drop_column("deleted_at")
        batch.create_index("uq_tool_definitions_slug", ["slug"], unique=True)

    # 6. model_routes: restore full unique on (model_config_id, route_key)
    duplicate = bind.execute(
        sa.text(
            "SELECT model_config_id, route_key FROM model_routes "
            "GROUP BY model_config_id, route_key HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "0081 downgrade blocked: deleted route keys have been reused; "
            "remove duplicates before downgrade"
        )
    with op.batch_alter_table("model_routes") as batch:
        batch.drop_index("uq_model_routes_model_key")
        batch.drop_index("ix_model_routes_deleted_at")
        batch.drop_column("deleted_at")
        batch.create_index(
            "uq_model_routes_model_key",
            ["model_config_id", "route_key"],
            unique=True,
        )

    # 5. user_asset_metadata: audio rows would violate the narrower check
    audio_row = bind.execute(
        sa.text("SELECT id FROM user_asset_metadata WHERE media_type = 'audio' LIMIT 1")
    ).first()
    if audio_row is not None:
        raise RuntimeError(
            "0081 downgrade blocked: audio rows exist in user_asset_metadata; "
            "remove them before downgrade"
        )
    with op.batch_alter_table("user_asset_metadata") as batch:
        batch.drop_constraint("ck_user_asset_metadata_media_type_valid", type_="check")
        batch.create_check_constraint(
            "ck_user_asset_metadata_media_type_valid",
            "media_type in ('image', 'video')",
        )

    # 4. payment_refunds
    op.drop_index("ix_payment_refunds_status", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_user_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_order_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_refund_no", table_name="payment_refunds")
    op.drop_table("payment_refunds")

    # 3 & 2. payment_orders: refunded rows would violate the narrower status check
    refunded_row = bind.execute(
        sa.text("SELECT id FROM payment_orders WHERE status = 'refunded' LIMIT 1")
    ).first()
    if refunded_row is not None:
        raise RuntimeError(
            "0081 downgrade blocked: refunded orders exist in payment_orders; "
            "resolve them before downgrade"
        )
    with op.batch_alter_table("payment_orders") as batch:
        batch.drop_constraint("ck_payment_orders_invoice_status_valid", type_="check")
        batch.drop_constraint("ck_payment_orders_refunded_amount_nonnegative", type_="check")
        batch.drop_constraint("ck_payment_orders_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_payment_orders_status_valid",
            "status in ('pending', 'paid', 'closed', 'failed')",
        )
        batch.drop_column("invoice_issued_at")
        batch.drop_column("invoice_requested_at")
        batch.drop_column("invoice_note")
        batch.drop_column("invoice_email")
        batch.drop_column("invoice_tax_no")
        batch.drop_column("invoice_title")
        batch.drop_column("invoice_type")
        batch.drop_column("invoice_status")
        batch.drop_column("refunded_at")
        batch.drop_column("refunded_amount_cents")

    # 1. users
    with op.batch_alter_table("users") as batch:
        batch.drop_column("deleted_at")
