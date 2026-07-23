"""enable two-image references for Seedance 1.5 Pro

Revision ID: 0073_seedance_15_multi_reference
Revises: 0072_image_edit_models
Create Date: 2026-07-22
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0073_seedance_15_multi_reference"
down_revision = "0072_image_edit_models"
branch_labels = None
depends_on = None

_MODEL_ID = "doubao-seedance-1-5-pro-251215"
_CAPABILITY_PATCH = {
    "text_to_video": True,
    "image_to_video": True,
    "first_last_frame": True,
    "multi_reference": True,
    "max_reference_images": 2,
}


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0073",
        "model_id": row["model_id"],
        "display_name": row["display_name"],
        "is_default": bool(row["is_default"]),
        "sort_order": int(row["sort_order"] or 0),
        "enabled": bool(row["enabled"]),
    }


def _publish_capability_version(
    bind,
    capability_versions,
    *,
    model_row: dict,
    capabilities: dict,
) -> None:
    active = (
        bind.execute(
            sa.select(capability_versions)
            .where(
                capability_versions.c.model_config_id == int(model_row["id"]),
                capability_versions.c.is_active.is_(True),
            )
            .order_by(capability_versions.c.version.desc())
        )
        .mappings()
        .first()
    )
    if (
        active is not None
        and active["status"] == "published"
        and dict(active["capabilities"] or {}) == capabilities
    ):
        return

    now = datetime.now(timezone.utc)
    source_version_id = None
    schema_version = "capability.v1"
    metadata_snapshot = _metadata_snapshot(model_row)
    if active is not None:
        source_version_id = int(active["id"])
        schema_version = str(active["schema_version"] or schema_version)
        if isinstance(active["metadata_snapshot"], dict):
            metadata_snapshot = dict(active["metadata_snapshot"])
        bind.execute(
            sa.update(capability_versions)
            .where(capability_versions.c.id == source_version_id)
            .values(
                status="disabled",
                is_active=False,
                disabled_at=now,
                updated_at=now,
            )
        )

    max_version = bind.scalar(
        sa.select(sa.func.max(capability_versions.c.version)).where(
            capability_versions.c.model_config_id == int(model_row["id"])
        )
    )
    bind.execute(
        capability_versions.insert().values(
            model_config_id=int(model_row["id"]),
            version=int(max_version or 0) + 1,
            schema_version=schema_version,
            capabilities=capabilities,
            metadata_snapshot=metadata_snapshot,
            status="published",
            is_active=True,
            source_version_id=source_version_id,
            activated_at=now,
            disabled_at=None,
            retired_at=None,
            updated_at=now,
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=bind,
    )
    row = (
        bind.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "video",
                model_configs.c.model_id == _MODEL_ID,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return
    model_row = dict(row)
    provider = str(model_row.get("provider") or "").strip().lower()
    gateway_format = str(model_row.get("gateway_format") or "").strip().lower()
    if provider != "volcengine_ark" and gateway_format != "ark":
        return

    extra = (
        dict(model_row["extra"] or {})
        if isinstance(model_row.get("extra"), dict)
        else {}
    )
    current = extra.get("capabilities")
    capabilities = dict(current) if isinstance(current, dict) else {}
    capabilities.update(_CAPABILITY_PATCH)
    extra["capabilities"] = capabilities
    bind.execute(
        sa.update(model_configs)
        .where(model_configs.c.id == int(model_row["id"]))
        .values(extra=extra)
    )
    _publish_capability_version(
        bind,
        capability_versions,
        model_row=model_row,
        capabilities=capabilities,
    )


def downgrade() -> None:
    # Published capability versions are immutable and may already be referenced
    # by generation history, so a downgrade must not rewrite them in place.
    pass
