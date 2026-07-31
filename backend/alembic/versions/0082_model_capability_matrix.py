"""align public model capabilities with verified provider adapters

Revision ID: 0082_model_capability_matrix
Revises: 0081_refunds_soft_delete
Create Date: 2026-07-30
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0082_model_capability_matrix"
down_revision = "0081_refunds_soft_delete"
branch_labels = None
depends_on = None

_SEEDANCE_VERSION_RE = re.compile(r"^doubao-seedance-(\d+)-(\d+)(?:-|$)")
_IMAGE_CAPABILITY_KEYS = {
    "text_to_image",
    "image_to_image",
    "image_edit",
    "image_mask",
    "inpainting",
    "mask_edit",
    "reference_image",
    "multi_reference",
    "max_reference_images",
}
_VIDEO_CAPABILITY_KEYS = {
    "text_to_video",
    "image_to_video",
    "reference_image",
    "multi_reference",
    "max_reference_images",
    "first_last_frame",
    "video_to_video",
}
_VISION_CAPABILITY_KEYS = {
    "image_analysis",
    "video_analysis",
    "product_profile",
    "portrait_profile",
}
_PROMPT_CAPABILITY_KEYS = {"prompt_optimization"}
_GROK_REFERENCE_MAPPING_KEYS = {
    "product_images_field",
    "product_images_item_field",
    "product_image_field",
    "product_image_item_field",
    "product_detail_images_field",
    "product_detail_images_item_field",
}


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0082",
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


def _seedance_version(model_id: str) -> tuple[int, int] | None:
    match = _SEEDANCE_VERSION_RE.match(model_id)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _correction(row: dict) -> tuple[set[str], dict, dict, set[str]] | None:
    use = str(row.get("use") or "").strip().lower()
    model_id = str(row.get("model_id") or "").strip().lower()
    provider = str(row.get("provider") or "").strip().lower()
    gateway_format = str(row.get("gateway_format") or "").strip().lower()

    if use == "image" and model_id == "gpt-image-2" and gateway_format == "openai":
        return (
            _IMAGE_CAPABILITY_KEYS,
            {
                "text_to_image": True,
                "image_to_image": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 2,
                "mask_edit": True,
            },
            {"edit_path": "/v1/images/edits", "multi_image_edit_enabled": True},
            set(),
        )
    if (
        use == "image"
        and model_id == "gemini-3.1-flash-image"
        and provider == "antigravity"
        and gateway_format == "anthropic"
    ):
        return (
            _IMAGE_CAPABILITY_KEYS,
            {
                "text_to_image": True,
                "image_to_image": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 2,
                "mask_edit": False,
            },
            {
                "image_transport": "anthropic_messages",
                "edit_path": "/messages",
                "multi_image_edit_enabled": True,
            },
            set(),
        )
    if (
        use == "image"
        and model_id in {"grok-imagine-image", "grok-imagine-image-quality"}
        and provider == "grok"
        and gateway_format == "openai"
    ):
        return (
            _IMAGE_CAPABILITY_KEYS,
            {
                "text_to_image": True,
                "image_to_image": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 2,
                "mask_edit": False,
            },
            {
                "image_transport": "grok_images",
                "response_format": "b64_json",
                "edit_path": "/images/edits",
                "edit_payload_format": "json",
                "multi_image_edit_enabled": True,
            },
            set(),
        )

    seedance_version = _seedance_version(model_id)
    is_ark = provider == "volcengine_ark" and gateway_format == "ark"
    if (
        use == "video"
        and model_id == "doubao-seedance-1-5-pro-251215"
        and seedance_version == (1, 5)
        and is_ark
    ):
        return (
            _VIDEO_CAPABILITY_KEYS,
            {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": False,
                "first_last_frame": True,
                "multi_reference": False,
                "video_to_video": False,
            },
            {},
            set(),
        )
    if (
        use == "video"
        and seedance_version == (2, 0)
        and is_ark
    ):
        return (
            _VIDEO_CAPABILITY_KEYS,
            {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "first_last_frame": True,
                "multi_reference": True,
                "max_reference_images": 9,
                "video_to_video": True,
            },
            {},
            set(),
        )
    if (
        use == "video"
        and model_id == "grok-imagine-video"
        and provider == "grok"
        and gateway_format == "openai"
    ):
        return (
            _VIDEO_CAPABILITY_KEYS,
            {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 7,
                "first_last_frame": False,
                "video_to_video": True,
            },
            {
                "video_transport": "grok_videos",
                "product_images_field": "reference_images",
                "product_images_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
                "submit_path": "/v1/videos/generations",
                "poll_path": "/v1/videos/{id}",
            },
            set(),
        )
    if (
        use == "video"
        and model_id == "grok-imagine-video-1.5"
        and provider == "grok"
        and gateway_format == "openai"
    ):
        return (
            _VIDEO_CAPABILITY_KEYS,
            {
                "text_to_video": False,
                "image_to_video": True,
                "reference_image": False,
                "multi_reference": False,
                "first_last_frame": False,
                "video_to_video": False,
            },
            {
                "video_transport": "grok_videos",
                "first_frame_field": "image",
                "first_frame_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
                "submit_path": "/v1/videos/generations",
                "poll_path": "/v1/videos/{id}",
            },
            _GROK_REFERENCE_MAPPING_KEYS,
        )

    if use == "vision" and model_id == "gpt-5.6-sol" and gateway_format == "openai":
        return (
            _VISION_CAPABILITY_KEYS,
            {
                "image_analysis": True,
                "video_analysis": True,
                "product_profile": True,
                "portrait_profile": True,
            },
            {},
            set(),
        )
    if (
        use == "vision"
        and model_id == "gemini-3.1-pro-high"
        and provider == "antigravity"
        and gateway_format == "anthropic"
    ):
        return (
            _VISION_CAPABILITY_KEYS,
            {
                "image_analysis": True,
                "video_analysis": True,
                "product_profile": True,
                "portrait_profile": True,
            },
            {},
            set(),
        )
    if use == "prompt" and (
        (
            model_id
            in {
                "claude-opus-4-6-thinking",
                "gemini-3.5-flash-low",
                "gemini-3.1-pro-high",
            }
            and provider == "antigravity"
            and gateway_format == "anthropic"
        )
        or (
            model_id == "grok-4.5"
            and provider == "grok"
            and gateway_format == "openai"
        )
    ):
        return (
            _PROMPT_CAPABILITY_KEYS,
            {"prompt_optimization": True},
            {},
            set(),
        )
    return None


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
        sa.select(model_configs).where(model_configs.c.deleted_at.is_(None))
    ).mappings().all()
    for row in rows:
        model_row = dict(row)
        correction = _correction(model_row)
        if correction is None:
            continue
        managed_keys, expected, extra_patch, removed_extra_keys = correction
        original_extra = (
            dict(model_row["extra"] or {})
            if isinstance(model_row.get("extra"), dict)
            else {}
        )
        extra = dict(original_extra)
        current = extra.get("capabilities")
        capabilities = dict(current) if isinstance(current, dict) else {}
        for key in managed_keys:
            capabilities.pop(key, None)
        capabilities.update(expected)
        for key in removed_extra_keys:
            extra.pop(key, None)
        extra.update(extra_patch)
        extra["capabilities"] = capabilities
        if extra != original_extra:
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
    # Published capability versions may already be bound to quotes and tasks.
    # Keep the verified version immutable rather than restoring known-bad data.
    pass
