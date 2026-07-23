"""enable verified Grok and Gemini image editing adapters

Revision ID: 0072_image_edit_models
Revises: 0071_gemini_31_pro_high_vision
Create Date: 2026-07-22
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0072_image_edit_models"
down_revision = "0071_gemini_31_pro_high_vision"
branch_labels = None
depends_on = None

_MODEL_SPECS = {
    "gemini-3.1-flash-image": {
        "display_name": "Gemini 3.1 Flash Image",
        "provider": "antigravity",
        "gateway_format": "anthropic",
        "sort_order": 30,
        "extra": {
            "image_transport": "anthropic_messages",
            "edit_path": "/messages",
            "multi_image_edit_enabled": True,
            "capabilities": {
                "text_to_image": True,
                "image_to_image": True,
                "image_edit": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 2,
            },
        },
    },
    "grok-imagine-image": {
        "display_name": "Grok Imagine Image",
        "provider": "grok",
        "gateway_format": "openai",
        "sort_order": 40,
        "extra": {
            "image_transport": "grok_images",
            "response_format": "b64_json",
            "edit_path": "/images/edits",
            "edit_payload_format": "json",
            "multi_image_edit_enabled": True,
            "capabilities": {
                "text_to_image": True,
                "image_to_image": True,
                "image_edit": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 3,
            },
        },
    },
}


def _provider_source(bind, model_configs, spec: dict) -> dict | None:
    row = (
        bind.execute(
            sa.select(model_configs)
            .where(
                model_configs.c.provider == spec["provider"],
                model_configs.c.gateway_format == spec["gateway_format"],
                model_configs.c.base_url.is_not(None),
                model_configs.c.api_key_encrypted.is_not(None),
            )
            .order_by(model_configs.c.sort_order, model_configs.c.id)
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0072",
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


def _ensure_model(bind, model_configs, model_id: str, spec: dict) -> dict | None:
    existing = (
        bind.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "image",
                model_configs.c.model_id == model_id,
            )
        )
        .mappings()
        .first()
    )
    if existing is None:
        source = _provider_source(bind, model_configs, spec)
        if source is None:
            return None
        bind.execute(
            model_configs.insert().values(
                use="image",
                model_id=model_id,
                display_name=spec["display_name"],
                is_default=False,
                sort_order=spec["sort_order"],
                provider=spec["provider"],
                base_url=source["base_url"] if source is not None else None,
                api_key_encrypted=(
                    source["api_key_encrypted"] if source is not None else None
                ),
                gateway_format=spec["gateway_format"],
                cost_credits=8,
                unlock_cost=0,
                enabled=source is not None,
                extra=spec["extra"],
            )
        )
    else:
        row = dict(existing)
        if (
            str(row.get("provider") or "").strip().lower() != spec["provider"]
            or str(row.get("gateway_format") or "").strip().lower()
            != spec["gateway_format"]
        ):
            return row
        extra = dict(row["extra"] or {}) if isinstance(row["extra"], dict) else {}
        capabilities = (
            dict(extra["capabilities"])
            if isinstance(extra.get("capabilities"), dict)
            else {}
        )
        capabilities.update(spec["extra"]["capabilities"])
        extra.update(spec["extra"])
        extra["capabilities"] = capabilities
        values = {"extra": extra}
        if not row.get("display_name") or row["display_name"] == model_id:
            values["display_name"] = spec["display_name"]
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.id == int(row["id"]))
            .values(**values)
        )

    row = (
        bind.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "image",
                model_configs.c.model_id == model_id,
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    model_configs = sa.Table("model_configs", metadata, autoload_with=bind)
    capability_versions = sa.Table(
        "model_capability_versions",
        metadata,
        autoload_with=bind,
    )
    for model_id, spec in _MODEL_SPECS.items():
        row = _ensure_model(bind, model_configs, model_id, spec)
        if row is None or (
            str(row.get("provider") or "").strip().lower() != spec["provider"]
            or str(row.get("gateway_format") or "").strip().lower()
            != spec["gateway_format"]
        ):
            continue
        capabilities = dict((row.get("extra") or {}).get("capabilities") or {})
        _publish_capability_version(
            bind,
            capability_versions,
            model_row=row,
            capabilities=capabilities,
        )


def downgrade() -> None:
    # Capability versions are immutable. A later administrator correction must
    # not be replaced with the pre-adapter false capability declaration.
    pass
