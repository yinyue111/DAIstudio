"""add model capability/price versions and generation quotes

Revision ID: 0044_model_versions_and_quotes
Revises: 0043_projects_and_asset_folders
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0044_model_versions_and_quotes"
down_revision = "0043_projects_and_asset_folders"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_DEFAULT_PRICING = {
    "image": {"1k": 8, "2k": 8, "4k": 8},
    "image_edit": {"1k": 8, "2k": 8, "4k": 8},
    "video_preview_cost": 50,
    "video_per_second": {"480p": 10, "720p": 10, "1080p": 10},
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _nonnegative_int(value, fallback: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _resolved_pricing(extra: dict | None) -> dict:
    raw = (extra or {}).get("credit_pricing")
    raw = raw if isinstance(raw, dict) else {}
    image = raw.get("image") if isinstance(raw.get("image"), dict) else {}
    image_edit = raw.get("image_edit") if isinstance(raw.get("image_edit"), dict) else {}
    video = raw.get("video_per_second") if isinstance(raw.get("video_per_second"), dict) else {}
    return {
        "image": {
            key: _nonnegative_int(image.get(key), value)
            for key, value in _DEFAULT_PRICING["image"].items()
        },
        "image_edit": {
            key: _nonnegative_int(image_edit.get(key), value)
            for key, value in _DEFAULT_PRICING["image_edit"].items()
        },
        "video_preview_cost": _nonnegative_int(
            raw.get("video_preview_cost"),
            _DEFAULT_PRICING["video_preview_cost"],
        ),
        "video_per_second": {
            key: _nonnegative_int(video.get(key), value)
            for key, value in _DEFAULT_PRICING["video_per_second"].items()
        },
    }


def upgrade() -> None:
    tables = _tables()
    if "model_capability_versions" not in tables:
        op.create_table(
            "model_capability_versions",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "model_config_id",
                sa.BigInteger(),
                sa.ForeignKey("model_configs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("schema_version", sa.String(32), nullable=False, server_default="capability.v1"),
            sa.Column("capabilities", JSON_TYPE, nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("version >= 1", name="ck_model_capability_versions_version_positive"),
        )
        op.create_index(
            "ix_model_capability_versions_model_config_id",
            "model_capability_versions",
            ["model_config_id"],
        )
        op.create_index(
            "uq_model_capability_versions_model_version",
            "model_capability_versions",
            ["model_config_id", "version"],
            unique=True,
        )
        op.create_index(
            "uq_model_capability_versions_active",
            "model_capability_versions",
            ["model_config_id"],
            unique=True,
            postgresql_where=sa.text("is_active"),
            sqlite_where=sa.text("is_active = 1"),
        )

    if "model_price_versions" not in tables:
        op.create_table(
            "model_price_versions",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "model_config_id",
                sa.BigInteger(),
                sa.ForeignKey("model_configs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("schema_version", sa.String(32), nullable=False, server_default="credit-price.v1"),
            sa.Column("base_cost_credits", sa.BigInteger(), nullable=False),
            sa.Column("unlock_cost_credits", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("pricing", JSON_TYPE, nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("version >= 1", name="ck_model_price_versions_version_positive"),
            sa.CheckConstraint("base_cost_credits >= 0", name="ck_model_price_versions_base_cost_nonnegative"),
            sa.CheckConstraint("unlock_cost_credits >= 0", name="ck_model_price_versions_unlock_cost_nonnegative"),
        )
        op.create_index(
            "ix_model_price_versions_model_config_id",
            "model_price_versions",
            ["model_config_id"],
        )
        op.create_index(
            "uq_model_price_versions_model_version",
            "model_price_versions",
            ["model_config_id", "version"],
            unique=True,
        )
        op.create_index(
            "uq_model_price_versions_active",
            "model_price_versions",
            ["model_config_id"],
            unique=True,
            postgresql_where=sa.text("is_active"),
            sqlite_where=sa.text("is_active = 1"),
        )

    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    capability_versions = sa.Table("model_capability_versions", metadata, autoload_with=bind)
    price_versions = sa.Table("model_price_versions", metadata, autoload_with=bind)
    for row in bind.execute(sa.select(model_configs)).mappings():
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        capability_exists = bind.execute(
            sa.select(capability_versions.c.id).where(
                capability_versions.c.model_config_id == row["id"]
            )
        ).first()
        if capability_exists is None:
            bind.execute(
                capability_versions.insert().values(
                    model_config_id=row["id"],
                    version=1,
                    capabilities=extra.get("capabilities")
                    if isinstance(extra.get("capabilities"), dict)
                    else {},
                    is_active=True,
                )
            )
        price_exists = bind.execute(
            sa.select(price_versions.c.id).where(price_versions.c.model_config_id == row["id"])
        ).first()
        if price_exists is None:
            bind.execute(
                price_versions.insert().values(
                    model_config_id=row["id"],
                    version=1,
                    base_cost_credits=_nonnegative_int(row.get("cost_credits"), 0),
                    unlock_cost_credits=_nonnegative_int(row.get("unlock_cost"), 0),
                    pricing=_resolved_pricing(extra),
                    is_active=True,
                )
            )

    if "generation_quotes" not in tables:
        op.create_table(
            "generation_quotes",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("model_config_id", sa.BigInteger(), sa.ForeignKey("model_configs.id"), nullable=False),
            sa.Column("capability_version_id", sa.BigInteger(), sa.ForeignKey("model_capability_versions.id"), nullable=False),
            sa.Column("price_version_id", sa.BigInteger(), sa.ForeignKey("model_price_versions.id"), nullable=False),
            sa.Column("request_fingerprint", sa.String(64), nullable=False),
            sa.Column("category", sa.String(8), nullable=False),
            sa.Column("stage", sa.String(8), nullable=False),
            sa.Column("request_snapshot", JSON_TYPE, nullable=False),
            sa.Column("model_snapshot", JSON_TYPE, nullable=False),
            sa.Column("pricing_snapshot", JSON_TYPE, nullable=False),
            sa.Column("price_breakdown", JSON_TYPE, nullable=False),
            sa.Column("estimated_credits", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("gen_tasks.id"), nullable=True, unique=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "status in ('active', 'consumed', 'expired', 'canceled')",
                name="ck_generation_quotes_status_valid",
            ),
            sa.CheckConstraint("category in ('image', 'video')", name="ck_generation_quotes_category_valid"),
            sa.CheckConstraint("stage in ('preview', 'final')", name="ck_generation_quotes_stage_valid"),
            sa.CheckConstraint("estimated_credits >= 0", name="ck_generation_quotes_estimated_credits_nonnegative"),
        )
        op.create_index("ix_generation_quotes_user_id", "generation_quotes", ["user_id"])
        op.create_index("ix_generation_quotes_model_config_id", "generation_quotes", ["model_config_id"])
        op.create_index("ix_generation_quotes_capability_version_id", "generation_quotes", ["capability_version_id"])
        op.create_index("ix_generation_quotes_price_version_id", "generation_quotes", ["price_version_id"])
        op.create_index("ix_generation_quotes_user_created", "generation_quotes", ["user_id", "created_at"])
        op.create_index(
            "ix_generation_quotes_user_status_expires",
            "generation_quotes",
            ["user_id", "status", "expires_at"],
        )

    if "gen_tasks" in tables and "quote_id" not in _columns("gen_tasks"):
        with op.batch_alter_table("gen_tasks") as batch_op:
            batch_op.add_column(sa.Column("quote_id", sa.BigInteger(), nullable=True))
            batch_op.create_foreign_key(
                "fk_gen_tasks_quote_id_generation_quotes",
                "generation_quotes",
                ["quote_id"],
                ["id"],
            )
        op.create_index("ix_gen_tasks_quote_id", "gen_tasks", ["quote_id"])
        op.create_index(
            "uq_gen_tasks_quote_id",
            "gen_tasks",
            ["quote_id"],
            unique=True,
            postgresql_where=sa.text("quote_id IS NOT NULL"),
            sqlite_where=sa.text("quote_id IS NOT NULL"),
        )


def downgrade() -> None:
    tables = _tables()
    if "gen_tasks" in tables and "quote_id" in _columns("gen_tasks"):
        op.drop_index("uq_gen_tasks_quote_id", table_name="gen_tasks")
        op.drop_index("ix_gen_tasks_quote_id", table_name="gen_tasks")
        with op.batch_alter_table("gen_tasks") as batch_op:
            batch_op.drop_constraint(
                "fk_gen_tasks_quote_id_generation_quotes",
                type_="foreignkey",
            )
            batch_op.drop_column("quote_id")
    for table in ("generation_quotes", "model_price_versions", "model_capability_versions"):
        if table in tables:
            op.drop_table(table)
