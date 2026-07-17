"""declare Seedance 2.0 multi-reference capability

Revision ID: 0038_seedance_multi_reference
Revises: 0037_unified_user_assets
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "0038_seedance_multi_reference"
down_revision = "0037_unified_user_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "model_configs" not in inspector.get_table_names():
        return

    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            model_configs.c.id,
            model_configs.c.model_id,
            model_configs.c.provider,
            model_configs.c.gateway_format,
            model_configs.c.extra,
        ).where(model_configs.c.use == "video")
    ).mappings()
    for row in rows:
        model_id = str(row["model_id"] or "").strip().lower()
        provider = str(row["provider"] or "").strip().lower()
        gateway_format = str(row["gateway_format"] or "").strip().lower()
        if not model_id.startswith("doubao-seedance-2-0-"):
            continue
        if provider != "volcengine_ark" and gateway_format != "ark":
            continue

        extra = dict(row["extra"] or {}) if isinstance(row["extra"], dict) else {}
        capabilities = (
            dict(extra.get("capabilities") or {})
            if isinstance(extra.get("capabilities"), dict)
            else {}
        )
        changed = False
        if "multi_reference" not in capabilities:
            capabilities["multi_reference"] = True
            changed = True
        if "max_reference_images" not in capabilities:
            capabilities["max_reference_images"] = 10
            changed = True
        if changed:
            extra["capabilities"] = capabilities
            bind.execute(
                sa.update(model_configs)
                .where(model_configs.c.id == row["id"])
                .values(extra=extra)
            )


def downgrade() -> None:
    # Capability values may have been edited by an administrator after upgrade.
    # A downgrade cannot safely distinguish those edits from migration defaults.
    pass
