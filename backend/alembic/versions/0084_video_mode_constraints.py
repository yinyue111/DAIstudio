"""publish verified video duration, resolution, and mode constraints

Revision ID: 0084_video_mode_constraints
Revises: 0083_video_input_semantics
Create Date: 2026-07-31
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0084_video_mode_constraints"
down_revision = "0083_video_input_semantics"
branch_labels = None
depends_on = None

_SEEDANCE_VERSION_RE = re.compile(r"^doubao-seedance-(\d+)-(\d+)(?:-|$)")
_SEEDANCE_20_STANDARD_RE = re.compile(r"^doubao-seedance-2-0-\d+$")
_MANAGED_CAPABILITY_KEYS = {
    "durations",
    "frame_reference_mode_exclusive",
    "generated_audio",
    "generated_audio_configurable",
    "max_duration_seconds",
    "max_reference_duration_seconds",
    "min_duration_seconds",
    "reference_image_mode_exclusive",
    "resolutions",
}


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0084",
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


def _correction(row: dict) -> dict | None:
    use = str(row.get("use") or "").strip().lower()
    model_id = str(row.get("model_id") or "").strip().lower()
    provider = str(row.get("provider") or "").strip().lower()
    gateway_format = str(row.get("gateway_format") or "").strip().lower()
    if use != "video":
        return None

    seedance_version = _seedance_version(model_id)
    is_ark = provider == "volcengine_ark" and gateway_format == "ark"
    if (
        model_id == "doubao-seedance-1-5-pro-251215"
        and seedance_version == (1, 5)
        and is_ark
    ):
        return {
            "durations": list(range(4, 13)),
            "generated_audio": True,
            "generated_audio_configurable": False,
            "max_duration_seconds": 12,
            "min_duration_seconds": 4,
            "resolutions": ["480p", "720p", "1080p"],
        }
    if seedance_version == (2, 0) and is_ark:
        return {
            "durations": list(range(4, 16)),
            "frame_reference_mode_exclusive": True,
            "generated_audio": True,
            "generated_audio_configurable": False,
            "max_duration_seconds": 15,
            "min_duration_seconds": 4,
            "resolutions": (
                ["480p", "720p", "1080p"]
                if _SEEDANCE_20_STANDARD_RE.fullmatch(model_id)
                else ["480p", "720p"]
            ),
        }
    if (
        model_id == "grok-imagine-video"
        and provider == "grok"
        and gateway_format == "openai"
    ):
        return {
            "durations": list(range(1, 16)),
            "max_duration_seconds": 15,
            "max_reference_duration_seconds": 10,
            "min_duration_seconds": 1,
            "reference_image_mode_exclusive": True,
            "resolutions": ["480p", "720p"],
        }
    if (
        model_id == "grok-imagine-video-1.5"
        and provider == "grok"
        and gateway_format == "openai"
    ):
        return {
            "durations": list(range(1, 16)),
            "max_duration_seconds": 15,
            "min_duration_seconds": 1,
            "resolutions": ["480p", "720p", "1080p"],
        }
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
        expected = _correction(model_row)
        if expected is None:
            continue
        original_extra = (
            dict(model_row["extra"] or {})
            if isinstance(model_row.get("extra"), dict)
            else {}
        )
        extra = dict(original_extra)
        current = extra.get("capabilities")
        capabilities = dict(current) if isinstance(current, dict) else {}
        for key in _MANAGED_CAPABILITY_KEYS:
            capabilities.pop(key, None)
        capabilities.update(expected)
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
    # Capability versions can be attached to quotes and tasks. Preserve their
    # immutable history instead of reactivating an older, less precise profile.
    pass
