"""add Grok Imagine Image 2.0 with automatic generation and edit defaults

Revision ID: 0086_grok_image_20
Revises: 0085_asset_retention_3650
Create Date: 2026-08-28
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0086_grok_image_20"
down_revision = "0085_asset_retention_3650"
branch_labels = None
depends_on = None

_MODEL_ID = "grok-imagine-image-2.0"
_CAPABILITIES = {
    "text_to_image": True,
    "image_to_image": True,
    "reference_image": True,
    "multi_reference": True,
    "max_reference_images": 2,
    "mask_edit": False,
}
_EXTRA = {
    "image_transport": "grok_images",
    "response_format": "b64_json",
    "edit_path": "/images/edits",
    "edit_payload_format": "json",
    "multi_image_edit_enabled": True,
    "capabilities": _CAPABILITIES,
}
_DEFAULT_PRICING = {
    "image": {"1k": 8, "2k": 8, "4k": 8},
    "image_edit": {"1k": 8, "2k": 8, "4k": 8},
    "video_preview_cost": 50,
    "video_per_second": {"480p": 10, "720p": 10, "1080p": 10},
}


def _metadata_snapshot(row: dict) -> dict:
    return {
        "schema_version": "model-catalog-metadata.v1",
        "origin": "migration_0086",
        "model_id": row["model_id"],
        "display_name": row["display_name"],
        "is_default": bool(row["is_default"]),
        "sort_order": int(row["sort_order"] or 0),
        "enabled": bool(row["enabled"]),
    }


def _provider_source(bind, model_configs) -> dict | None:
    row = (
        bind.execute(
            sa.select(model_configs)
            .where(
                model_configs.c.provider.in_(("grok", "custom_openai")),
                model_configs.c.gateway_format == "openai",
                model_configs.c.base_url.is_not(None),
                model_configs.c.api_key_encrypted.is_not(None),
                model_configs.c.deleted_at.is_(None),
                model_configs.c.model_id != _MODEL_ID,
            )
            .order_by(
                sa.case(
                    (model_configs.c.model_id == "grok-imagine-image", 0),
                    (model_configs.c.model_id == "grok-imagine-image-quality", 1),
                    else_=2,
                ),
                model_configs.c.sort_order,
                model_configs.c.id,
            )
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _merge_extra(value: object) -> dict:
    current = dict(value) if isinstance(value, dict) else {}
    capabilities = (
        dict(current["capabilities"])
        if isinstance(current.get("capabilities"), dict)
        else {}
    )
    capabilities.update(_CAPABILITIES)
    current.update(_EXTRA)
    current["capabilities"] = capabilities
    return current


def _ensure_model(bind, model_configs) -> tuple[dict | None, dict | None]:
    source = _provider_source(bind, model_configs)
    existing = (
        bind.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "image",
                model_configs.c.model_id == _MODEL_ID,
                model_configs.c.deleted_at.is_(None),
            )
        )
        .mappings()
        .first()
    )
    if existing is None:
        if source is None:
            return None, None
        max_sort_order = bind.scalar(
            sa.select(sa.func.max(model_configs.c.sort_order)).where(
                model_configs.c.use == "image",
                model_configs.c.deleted_at.is_(None),
            )
        )
        bind.execute(
            model_configs.insert().values(
                use="image",
                model_id=_MODEL_ID,
                display_name="Grok Imagine Image 2.0",
                is_default=False,
                sort_order=int(max_sort_order or 0) + 10,
                provider=source["provider"],
                base_url=source["base_url"],
                api_key_encrypted=source["api_key_encrypted"],
                gateway_format="openai",
                cost_credits=max(1, int(source.get("cost_credits") or 8)),
                unlock_cost=max(0, int(source.get("unlock_cost") or 0)),
                enabled=True,
                extra=_EXTRA,
                deleted_at=None,
            )
        )
    else:
        row = dict(existing)
        values = {
            "gateway_format": "openai",
            "extra": _merge_extra(row.get("extra")),
        }
        if not row.get("display_name") or row["display_name"] == _MODEL_ID:
            values["display_name"] = "Grok Imagine Image 2.0"
        if source is not None:
            if row.get("provider") not in {"grok", "custom_openai"}:
                values.update(
                    provider=source["provider"],
                    base_url=source["base_url"],
                    api_key_encrypted=source["api_key_encrypted"],
                )
            elif not row.get("base_url"):
                values["base_url"] = source["base_url"]
            if (
                row.get("provider") in {"grok", "custom_openai"}
                and not row.get("api_key_encrypted")
            ):
                values["api_key_encrypted"] = source["api_key_encrypted"]
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.id == int(row["id"]))
            .values(**values)
        )

    row = (
        bind.execute(
            sa.select(model_configs).where(
                model_configs.c.use == "image",
                model_configs.c.model_id == _MODEL_ID,
                model_configs.c.deleted_at.is_(None),
            )
        )
        .mappings()
        .one()
    )
    return dict(row), source


def _publish_capability_version(bind, table, model_row: dict) -> None:
    capabilities = dict((model_row.get("extra") or {}).get("capabilities") or {})
    active = (
        bind.execute(
            sa.select(table).where(
                table.c.model_config_id == int(model_row["id"]),
                table.c.is_active.is_(True),
            )
        )
        .mappings()
        .first()
    )
    metadata_snapshot = _metadata_snapshot(model_row)
    active_metadata = (
        dict(active["metadata_snapshot"] or {}) if active is not None else {}
    )
    if (
        active is not None
        and active["status"] == "published"
        and dict(active["capabilities"] or {}) == capabilities
        and all(
            active_metadata.get(key) == metadata_snapshot[key]
            for key in ("model_id", "display_name", "is_default", "sort_order", "enabled")
        )
    ):
        return
    now = datetime.now(timezone.utc)
    source_version_id = None
    schema_version = "capability.v1"
    if active is not None:
        source_version_id = int(active["id"])
        schema_version = str(active["schema_version"] or schema_version)
        bind.execute(
            sa.update(table)
            .where(table.c.id == source_version_id)
            .values(
                status="disabled",
                is_active=False,
                disabled_at=now,
                updated_at=now,
            )
        )
    max_version = bind.scalar(
        sa.select(sa.func.max(table.c.version)).where(
            table.c.model_config_id == int(model_row["id"])
        )
    )
    bind.execute(
        table.insert().values(
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


def _source_pricing(bind, price_versions, source: dict | None) -> dict:
    if source is None:
        return _DEFAULT_PRICING
    row = (
        bind.execute(
            sa.select(price_versions.c.pricing).where(
                price_versions.c.model_config_id == int(source["id"]),
                price_versions.c.is_active.is_(True),
            )
        )
        .mappings()
        .first()
    )
    return dict(row["pricing"] or {}) if row is not None else _DEFAULT_PRICING


def _publish_price_version(bind, table, model_row: dict, source: dict | None) -> None:
    pricing = _source_pricing(bind, table, source)
    base_cost = max(0, int(model_row.get("cost_credits") or 0))
    unlock_cost = max(0, int(model_row.get("unlock_cost") or 0))
    active = (
        bind.execute(
            sa.select(table).where(
                table.c.model_config_id == int(model_row["id"]),
                table.c.is_active.is_(True),
            )
        )
        .mappings()
        .first()
    )
    if (
        active is not None
        and active["status"] == "published"
        and int(active["base_cost_credits"] or 0) == base_cost
        and int(active["unlock_cost_credits"] or 0) == unlock_cost
        and dict(active["pricing"] or {}) == pricing
    ):
        return
    now = datetime.now(timezone.utc)
    source_version_id = None
    schema_version = "credit-price.v1"
    if active is not None:
        source_version_id = int(active["id"])
        schema_version = str(active["schema_version"] or schema_version)
        bind.execute(
            sa.update(table)
            .where(table.c.id == source_version_id)
            .values(
                status="disabled",
                is_active=False,
                disabled_at=now,
                updated_at=now,
            )
        )
    max_version = bind.scalar(
        sa.select(sa.func.max(table.c.version)).where(
            table.c.model_config_id == int(model_row["id"])
        )
    )
    bind.execute(
        table.insert().values(
            model_config_id=int(model_row["id"]),
            version=int(max_version or 0) + 1,
            schema_version=schema_version,
            base_cost_credits=base_cost,
            unlock_cost_credits=unlock_cost,
            pricing=pricing,
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
        "model_capability_versions", metadata, autoload_with=bind
    )
    price_versions = sa.Table("model_price_versions", metadata, autoload_with=bind)
    model_row, source = _ensure_model(bind, model_configs)
    if model_row is None:
        return
    _publish_capability_version(bind, capability_versions, model_row)
    _publish_price_version(bind, price_versions, model_row, source)


def downgrade() -> None:
    # Do not remove a model that may already be referenced by quotes or tasks.
    pass
