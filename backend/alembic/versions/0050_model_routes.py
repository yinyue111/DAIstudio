"""add model routes and circuit health history

Revision ID: 0050_model_routes
Revises: 0049_prompt_opt_proposals
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050_model_routes"
down_revision = "0049_prompt_opt_proposals"
branch_labels = None
depends_on = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "model_routes" not in tables:
        op.create_table(
            "model_routes",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "model_config_id",
                sa.BigInteger(),
                sa.ForeignKey("model_configs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("route_key", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("model_id", sa.String(128), nullable=True),
            sa.Column("provider", sa.String(32), nullable=True),
            sa.Column("base_url", sa.String(512), nullable=True),
            sa.Column("api_key_encrypted", sa.Text(), nullable=True),
            sa.Column("gateway_format", sa.String(16), nullable=True),
            sa.Column("extra", JSON_TYPE, nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "managed_by_model_config",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("failure_threshold", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("health_status", sa.String(16), nullable=False, server_default="closed"),
            sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("window_requests", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("window_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("half_open_claimed_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("latency_ema_ms", sa.Integer(), nullable=True),
            sa.Column("last_error_code", sa.String(64), nullable=True),
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
            sa.CheckConstraint("priority >= 0", name="ck_model_routes_priority_nonnegative"),
            sa.CheckConstraint(
                "config_revision >= 1", name="ck_model_routes_revision_positive"
            ),
            sa.CheckConstraint(
                "health_status in ('closed', 'open', 'half_open')",
                name="ck_model_routes_health_status_valid",
            ),
            sa.CheckConstraint(
                "failure_threshold >= 1",
                name="ck_model_routes_failure_threshold_positive",
            ),
            sa.CheckConstraint(
                "window_seconds >= 1", name="ck_model_routes_window_positive"
            ),
            sa.CheckConstraint(
                "cooldown_seconds >= 1", name="ck_model_routes_cooldown_positive"
            ),
        )
        op.create_index("ix_model_routes_model_config_id", "model_routes", ["model_config_id"])
        op.create_index(
            "uq_model_routes_model_key",
            "model_routes",
            ["model_config_id", "route_key"],
            unique=True,
        )
        op.create_index(
            "ix_model_routes_select",
            "model_routes",
            ["model_config_id", "enabled", "priority", "id"],
        )
        op.create_index(
            "ix_model_routes_health",
            "model_routes",
            ["health_status", "cooldown_until", "id"],
        )

    tables = _tables()
    if "model_route_health_events" not in tables:
        op.create_table(
            "model_route_health_events",
            sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
            sa.Column(
                "route_id",
                sa.BigInteger(),
                sa.ForeignKey("model_routes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("operation", sa.String(32), nullable=False),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column(
                "counts_toward_circuit",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("detail", JSON_TYPE, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "outcome in ('success', 'failure', 'ignored')",
                name="ck_model_route_health_events_outcome_valid",
            ),
            sa.CheckConstraint(
                "latency_ms IS NULL OR latency_ms >= 0",
                name="ck_model_route_health_events_latency_nonnegative",
            ),
        )
        op.create_index(
            "ix_model_route_health_events_route_id",
            "model_route_health_events",
            ["route_id"],
        )
        op.create_index(
            "ix_model_route_health_events_route_created",
            "model_route_health_events",
            ["route_id", "created_at", "id"],
        )

    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    model_routes = sa.Table("model_routes", metadata, autoload_with=bind)
    for model in bind.execute(sa.select(model_configs)).mappings():
        exists = bind.scalar(
            sa.select(model_routes.c.id).where(
                model_routes.c.model_config_id == model["id"],
                model_routes.c.route_key == "legacy-default",
            )
        )
        if exists is not None:
            continue
        bind.execute(
            model_routes.insert().values(
                model_config_id=model["id"],
                route_key="legacy-default",
                name=f"{model.get('display_name') or model.get('model_id')} 默认路由",
                model_id=None,
                provider=model.get("provider"),
                base_url=model.get("base_url"),
                api_key_encrypted=model.get("api_key_encrypted"),
                gateway_format=model.get("gateway_format"),
                extra={},
                priority=100,
                enabled=bool(model.get("enabled", True)),
                managed_by_model_config=True,
                config_revision=1,
                health_status="closed",
            )
        )


def downgrade() -> None:
    tables = _tables()
    if "model_route_health_events" in tables:
        op.drop_table("model_route_health_events")
    if "model_routes" in tables:
        op.drop_table("model_routes")
