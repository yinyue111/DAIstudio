"""Versioned, non-secret catalog definition snapshots."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

MODEL_METADATA_SCHEMA_VERSION = "model-catalog-metadata.v1"
TOOL_METADATA_SCHEMA_VERSION = "tool-catalog-metadata.v1"

MODEL_METADATA_FIELDS = (
    "model_id",
    "display_name",
    "is_default",
    "sort_order",
    "enabled",
)
TOOL_METADATA_FIELDS = (
    "slug",
    "name",
    "description",
    "category",
    "renderer",
    "entry_path",
    "icon",
    "sort_order",
    "enabled",
    "featured",
)


def model_metadata_snapshot(model, *, origin: str = "recorded") -> dict[str, Any]:
    return {
        "schema_version": MODEL_METADATA_SCHEMA_VERSION,
        "origin": origin,
        "model_id": str(model.model_id),
        "display_name": str(model.display_name or model.model_id),
        "is_default": bool(model.is_default),
        "sort_order": int(model.sort_order or 0),
        "enabled": bool(model.enabled),
    }


def tool_metadata_snapshot(tool, *, origin: str = "recorded") -> dict[str, Any]:
    return {
        "schema_version": TOOL_METADATA_SCHEMA_VERSION,
        "origin": origin,
        "slug": str(tool.slug),
        "name": str(tool.name),
        "description": tool.description,
        "category": str(tool.category),
        "renderer": str(tool.renderer),
        "entry_path": str(tool.entry_path),
        "icon": tool.icon,
        "sort_order": int(tool.sort_order or 0),
        "enabled": bool(tool.enabled),
        "featured": bool(tool.featured),
    }


def resolved_tool_metadata_snapshot(tool, snapshot: dict | None) -> dict[str, Any]:
    """Return the immutable version metadata, with a legacy runtime fallback."""
    fallback = tool_metadata_snapshot(tool, origin="runtime_fallback")
    if not isinstance(snapshot, dict):
        return fallback
    values = snapshot_fields(snapshot, TOOL_METADATA_FIELDS)
    fallback.update(values)
    schema_version = snapshot.get("schema_version")
    origin = snapshot.get("origin")
    if isinstance(schema_version, str) and schema_version:
        fallback["schema_version"] = schema_version
    if isinstance(origin, str) and origin:
        fallback["origin"] = origin
    return fallback


def snapshot_fields(snapshot: dict | None, fields: tuple[str, ...]) -> dict[str, Any]:
    values = snapshot if isinstance(snapshot, dict) else {}
    return {field: deepcopy(values.get(field)) for field in fields if field in values}


def recorded_snapshot(
    snapshot: dict | None,
    *,
    schema_version: str,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "origin": "recorded",
        **snapshot_fields(snapshot, fields),
    }


def snapshot_matches(
    snapshot: dict | None,
    expected: dict,
    *,
    fields: tuple[str, ...],
) -> bool:
    return snapshot_fields(snapshot, fields) == snapshot_fields(expected, fields)
