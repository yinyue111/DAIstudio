"""add Gemini 3.1 Pro High as an Anthropic Messages vision model

Revision ID: 0071_gemini_31_pro_high_vision
Revises: 0070_video_model_capabilities
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0071_gemini_31_pro_high_vision"
down_revision = "0070_video_model_capabilities"
branch_labels = None
depends_on = None

_MODEL_ID = "gemini-3.1-pro-high"
_CAPABILITIES = {
    "image_analysis": True,
    "video_analysis": True,
    "product_profile": True,
    "portrait_profile": True,
}
_MODEL_REFERENCE_COLUMNS = {
    "gen_tasks": ("model_config_id",),
    "reverse_operations": ("model_config_id",),
    "gateway_calls": ("model_config_id",),
    "model_capability_versions": ("model_config_id",),
    "model_price_versions": ("model_config_id",),
    "generation_quotes": ("model_config_id",),
    "prompt_optimization_proposals": (
        "target_model_config_id",
        "optimizer_model_config_id",
    ),
    "model_routes": ("model_config_id",),
    "reproduction_assessments": ("model_config_id",),
}


def _first_model_reference(bind, model_config_id: int) -> str | None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    metadata = sa.MetaData()
    for table_name, column_names in _MODEL_REFERENCE_COLUMNS.items():
        if table_name not in tables:
            continue
        table = sa.Table(table_name, metadata, autoload_with=bind)
        for column_name in column_names:
            if column_name not in table.c:
                continue
            exists = bind.execute(
                sa.select(table.c[column_name])
                .where(table.c[column_name] == model_config_id)
                .limit(1)
            ).first()
            if exists is not None:
                return f"{table_name}.{column_name}"
    return None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    existing = bind.execute(
        sa.select(model_configs.c.id).where(
            model_configs.c.use == "vision",
            model_configs.c.model_id == _MODEL_ID,
        )
    ).first()
    if existing is not None:
        return

    source = bind.execute(
        sa.select(model_configs)
        .where(
            model_configs.c.provider == "antigravity",
            model_configs.c.gateway_format == "anthropic",
            model_configs.c.base_url.is_not(None),
            model_configs.c.api_key_encrypted.is_not(None),
        )
        .order_by(
            sa.case((model_configs.c.use == "prompt", 0), else_=1),
            model_configs.c.sort_order,
            model_configs.c.id,
        )
        .limit(1)
    ).mappings().first()

    bind.execute(
        model_configs.insert().values(
            use="vision",
            model_id=_MODEL_ID,
            display_name="Gemini 3.1 Pro High",
            is_default=False,
            sort_order=20,
            provider="antigravity",
            base_url=source["base_url"] if source is not None else None,
            api_key_encrypted=(
                source["api_key_encrypted"] if source is not None else None
            ),
            gateway_format="anthropic",
            cost_credits=5,
            unlock_cost=0,
            enabled=source is not None,
            extra={
                "catalog_origin": "migration_0071",
                "capabilities": _CAPABILITIES,
            },
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    row = bind.execute(
        sa.select(model_configs).where(
            model_configs.c.use == "vision",
            model_configs.c.model_id == _MODEL_ID,
        )
    ).mappings().first()
    extra = row.get("extra") if row is not None else None
    if (
        row is None
        or not isinstance(extra, dict)
        or extra.get("catalog_origin") != "migration_0071"
    ):
        return
    reference = _first_model_reference(bind, int(row["id"]))
    if reference is not None:
        raise RuntimeError(
            "0071 downgrade blocked: migration-created Gemini vision model "
            f"is referenced by {reference}"
        )
    bind.execute(
        sa.delete(model_configs).where(model_configs.c.id == int(row["id"]))
    )
