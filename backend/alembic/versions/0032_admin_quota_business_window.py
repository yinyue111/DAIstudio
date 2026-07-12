"""add persistent admin quota business replay window

Revision ID: 0032_admin_quota_business_window
Revises: 0031_jsonb_model_alignment
Create Date: 2026-07-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0032_admin_quota_business_window"
down_revision = "0031_jsonb_model_alignment"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_admin_idempotency_business_window"


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("admin_idempotency_keys")}
    with op.batch_alter_table("admin_idempotency_keys") as batch:
        if "business_fingerprint" not in columns:
            batch.add_column(sa.Column("business_fingerprint", sa.String(length=64), nullable=True))
        if "fingerprint_expires_at" not in columns:
            batch.add_column(sa.Column("fingerprint_expires_at", sa.DateTime(timezone=True), nullable=True))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("admin_idempotency_keys")}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "admin_idempotency_keys",
            ["admin_id", "scope", "business_fingerprint", "fingerprint_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("admin_idempotency_keys")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="admin_idempotency_keys")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("admin_idempotency_keys")}
    with op.batch_alter_table("admin_idempotency_keys") as batch:
        if "fingerprint_expires_at" in columns:
            batch.drop_column("fingerprint_expires_at")
        if "business_fingerprint" in columns:
            batch.drop_column("business_fingerprint")
