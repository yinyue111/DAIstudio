"""correct verified video model capabilities

Revision ID: 0070_video_model_capabilities
Revises: 0069_reproduction_remediations
Create Date: 2026-07-21
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0070_video_model_capabilities"
down_revision = "0069_reproduction_remediations"
branch_labels = None
depends_on = None

_SEEDANCE_15_MODEL_ID = "doubao-seedance-1-5-pro-251215"
_GROK_VIDEO_15_MODEL_ID = "grok-imagine-video-1.5"
_CAPABILITY_PATCHES = {
    _SEEDANCE_15_MODEL_ID: {
        "text_to_video": True,
        "image_to_video": True,
        "first_last_frame": True,
        "multi_reference": False,
        "max_reference_images": 2,
    },
    _GROK_VIDEO_15_MODEL_ID: {
        "text_to_video": True,
        "image_to_video": True,
        "multi_reference": False,
    },
}


def _is_verified_adapter(row: dict) -> bool:
    model_id = str(row.get("model_id") or "").strip().lower()
    provider = str(row.get("provider") or "").strip().lower()
    gateway_format = str(row.get("gateway_format") or "").strip().lower()
    if model_id == _SEEDANCE_15_MODEL_ID:
        return provider == "volcengine_ark" or gateway_format == "ark"
    if model_id == _GROK_VIDEO_15_MODEL_ID:
        return provider == "grok" and gateway_format == "openai"
    return False


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0070",
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
    rows = bind.execute(
        sa.select(model_configs).where(
            model_configs.c.use == "video",
            model_configs.c.model_id.in_(tuple(_CAPABILITY_PATCHES)),
        )
    ).mappings()
    for row in rows:
        model_row = dict(row)
        if not _is_verified_adapter(model_row):
            continue
        model_id = str(model_row["model_id"]).strip().lower()
        extra = dict(model_row["extra"] or {}) if isinstance(model_row["extra"], dict) else {}
        current = extra.get("capabilities")
        capabilities = dict(current) if isinstance(current, dict) else {}
        capabilities.update(_CAPABILITY_PATCHES[model_id])
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
    # Model capability versions are immutable and administrators may publish a
    # newer correction after this migration. Reverting data here could replace
    # that later decision with a known-invalid capability declaration.
    pass
