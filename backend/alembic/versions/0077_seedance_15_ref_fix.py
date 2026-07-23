"""separate Seedance 1.5 frame input from reference-to-video

Revision ID: 0077_seedance_15_ref_fix
Revises: 0076_grok_video_ref_schema
Create Date: 2026-07-22
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0077_seedance_15_ref_fix"
down_revision = "0076_grok_video_ref_schema"
branch_labels = None
depends_on = None

_MODEL_ID = "doubao-seedance-1-5-pro-251215"
_CAPABILITY_PATCH = {
    "text_to_video": True,
    "image_to_video": True,
    "reference_image": False,
    "first_last_frame": True,
    "multi_reference": False,
}


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0077",
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
    rows = (
        bind.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "video",
                model_configs.c.model_id == _MODEL_ID,
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        model_row = dict(row)
        provider = str(model_row.get("provider") or "").strip().lower()
        gateway_format = str(model_row.get("gateway_format") or "").strip().lower()
        if provider != "volcengine_ark" and gateway_format != "ark":
            continue

        extra = (
            dict(model_row["extra"] or {})
            if isinstance(model_row.get("extra"), dict)
            else {}
        )
        current = extra.get("capabilities")
        capabilities = dict(current) if isinstance(current, dict) else {}
        capabilities.update(_CAPABILITY_PATCH)
        capabilities.pop("max_reference_images", None)
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
    # Capability versions can already be referenced by generation history.
    # Keep the corrected published version immutable on downgrade.
    pass
