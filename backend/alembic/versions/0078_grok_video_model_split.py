"""separate Grok reference-to-video from Grok 1.5 image-to-video

Revision ID: 0078_grok_video_model_split
Revises: 0077_seedance_15_ref_fix
Create Date: 2026-07-23
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0078_grok_video_model_split"
down_revision = "0077_seedance_15_ref_fix"
branch_labels = None
depends_on = None

_REFERENCE_MODEL_ID = "grok-imagine-video"
_IMAGE_TO_VIDEO_MODEL_ID = "grok-imagine-video-1.5"
_MAPPING_KEYS = (
    "product_images_field",
    "product_images_item_field",
    "product_image_field",
    "product_image_item_field",
    "product_detail_images_field",
    "product_detail_images_item_field",
)


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0078",
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
                model_configs.c.model_id.in_([
                    _REFERENCE_MODEL_ID,
                    _IMAGE_TO_VIDEO_MODEL_ID,
                ]),
                model_configs.c.provider == "grok",
                model_configs.c.gateway_format == "openai",
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        model_row = dict(row)
        extra = (
            dict(model_row["extra"] or {})
            if isinstance(model_row.get("extra"), dict)
            else {}
        )
        current = extra.get("capabilities")
        capabilities = dict(current) if isinstance(current, dict) else {}
        capabilities.update({
            "text_to_video": True,
            "image_to_video": True,
        })
        for key in _MAPPING_KEYS:
            extra.pop(key, None)

        if model_row["model_id"] == _REFERENCE_MODEL_ID:
            capabilities.update({
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 3,
            })
            extra.update({
                "product_images_field": "reference_images",
                "product_images_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
            })
        else:
            capabilities.update({
                "reference_image": False,
                "multi_reference": False,
            })
            capabilities.pop("max_reference_images", None)
            extra.update({
                "first_frame_field": "image",
                "first_frame_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
            })

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
    # Published capability versions may already be referenced by task history.
    pass
