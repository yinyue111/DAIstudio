"""Public-safe model and tool catalog serialization."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    ToolDefinition,
    ToolVersion,
)
from .model_capabilities import effective_model_capabilities
from .model_gateway_config import PROVIDER_PRESETS
from .model_routes import model_route_summary
from .product_edition import feature_enabled, hidden_navigation_keys, is_launch_lite

_BOOLEAN_CAPABILITIES = {
    "text_to_image",
    "image_to_image",
    "mask_edit",
    "reference_image",
    "multi_reference",
    "text_to_video",
    "image_to_video",
    "video_to_video",
    "video_reference",
    "video_edit",
    "audio_reference",
    "first_last_frame",
    "reference_image_mode_exclusive",
    "frame_reference_mode_exclusive",
    "generated_audio",
    "generated_audio_configurable",
    "product_profile",
    "portrait_profile",
    "prompt_optimization",
    "image_analysis",
    "video_analysis",
}
_INTEGER_CAPABILITIES = {
    "max_reference_images",
    "max_reference_videos",
    "max_reference_audio",
    "max_reference_duration_seconds",
    "min_duration_seconds",
    "max_duration_seconds",
}
_LIST_CAPABILITIES = {
    "aspect_ratios",
    "resolutions",
    "durations",
    "product_video_templates",
}

_APP_NAVIGATION_ITEMS = (
    {"key": "studio", "label": "创作", "href": "/", "width": "w-16"},
    {"key": "catalog", "label": "能力", "href": "/catalog", "width": "w-16"},
    {"key": "prompts", "label": "灵感配方", "href": "/prompts", "width": "w-24"},
    {"key": "projects", "label": "项目", "href": "/projects", "width": "w-16"},
    {"key": "profile", "label": "素材", "href": "/profile", "width": "w-16"},
    {"key": "recharge", "label": "充值", "href": "/recharge", "width": "w-16"},
    {"key": "history", "label": "历史", "href": "/history", "width": "w-16"},
    {
        "key": "admin",
        "label": "管理后台",
        "href": "/admin",
        "width": "w-24",
        "permission": "admin",
        "reserve_desktop": True,
    },
)

_NAVIGATION_STATES = frozenset({"enabled", "disabled", "hidden"})


def app_navigation_catalog(
    *,
    is_admin: bool,
    navigation_states: dict | None = None,
) -> dict:
    """Return only navigation entries the current user may discover."""
    configured_states = navigation_states if isinstance(navigation_states, dict) else {}
    feature_hidden_keys = hidden_navigation_keys()
    items = []
    for item in _APP_NAVIGATION_ITEMS:
        if item.get("permission") == "admin" and not is_admin:
            continue
        if item["key"] in feature_hidden_keys:
            continue
        state = str(configured_states.get(item["key"], "enabled"))
        if state not in _NAVIGATION_STATES or item["key"] in {"studio", "admin"}:
            state = "enabled"
        if state == "hidden":
            continue
        resolved_item = dict(item)
        # Labels follow the owning switch, not the edition, so toggling one
        # feature doesn't rename unrelated navigation for existing users.
        if item["key"] == "prompts" and not feature_enabled("recipes_enabled"):
            resolved_item["label"] = "提示词库"
        elif item["key"] == "recharge" and is_launch_lite():
            resolved_item["label"] = "账户"
        items.append(
            {
                **resolved_item,
                "enabled": state == "enabled",
                "visible": True,
                "disabled_reason": (
                    None if state == "enabled" else "该功能已由管理员暂停使用"
                ),
            }
        )
    return {
        "schema_version": 1,
        "product_edition": "launch_lite" if is_launch_lite() else "full",
        "default_key": "studio",
        "items": items,
    }


def public_capabilities(extra: dict | None) -> dict:
    raw = (extra or {}).get("capabilities")
    if not isinstance(raw, dict):
        return {}
    capabilities: dict = {}
    for key in _BOOLEAN_CAPABILITIES:
        if isinstance(raw.get(key), bool):
            capabilities[key] = raw[key]
    for key in _INTEGER_CAPABILITIES:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100000:
            capabilities[key] = value
    for key in _LIST_CAPABILITIES:
        value = raw.get(key)
        if isinstance(value, list) and len(value) <= 100 and all(
            isinstance(item, (str, int)) and not isinstance(item, bool) for item in value
        ):
            if key == "product_video_templates":
                from .video_prompt_compiler import PRODUCT_VIDEO_TEMPLATE_KEYS

                capabilities[key] = list(
                    dict.fromkeys(
                        str(item).strip()
                        for item in value
                        if isinstance(item, str)
                        and str(item).strip() in PRODUCT_VIDEO_TEMPLATE_KEYS
                    )
                )
            else:
                capabilities[key] = value
    return capabilities


def public_model_option(model: ModelConfig) -> dict:
    extra = model.extra if isinstance(model.extra, dict) else {}
    effective_extra = {
        **extra,
        "capabilities": effective_model_capabilities(model),
    }
    preview_cost = (
        int(extra.get("preview_cost", max(1, int(model.cost_credits or 0) // 10)))
        if model.use == "video"
        else None
    )
    provider = str(model.provider or "env")
    provider_label = (PROVIDER_PRESETS.get(provider) or {}).get("label") or provider
    return {
        "id": model.id,
        "use": model.use,
        "name": model.display_name or model.model_id,
        "model_id": model.model_id,
        "provider": provider,
        "provider_label": provider_label,
        "is_default": bool(model.is_default),
        "sort_order": int(model.sort_order or 0),
        "cost_credits": int(model.cost_credits or 0),
        "unlock_cost": int(model.unlock_cost or 0),
        "preview_cost": preview_cost,
        "final_cost": int(model.cost_credits or 0),
        "capabilities": public_capabilities(effective_extra),
    }


def serialize_capability_version(
    row: ModelCapabilityVersion,
    *,
    public: bool = False,
) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "schema_version": row.schema_version,
        "capabilities": (
            public_capabilities({"capabilities": dict(row.capabilities or {})})
            if public
            else dict(row.capabilities or {})
        ),
        "metadata_snapshot": dict(row.metadata_snapshot or {}),
        "status": row.status,
        "is_active": bool(row.is_active),
        "source_version_id": row.source_version_id,
        "activated_at": row.activated_at,
        "disabled_at": row.disabled_at,
        "retired_at": row.retired_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_price_version(row: ModelPriceVersion) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "schema_version": row.schema_version,
        "base_cost_credits": int(row.base_cost_credits or 0),
        "unlock_cost_credits": int(row.unlock_cost_credits or 0),
        "pricing": dict(row.pricing or {}),
        "status": row.status,
        "is_active": bool(row.is_active),
        "source_version_id": row.source_version_id,
        "activated_at": row.activated_at,
        "disabled_at": row.disabled_at,
        "retired_at": row.retired_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def model_catalog_detail(db: Session, model: ModelConfig, *, history: bool = False) -> dict:
    capability_query = select(ModelCapabilityVersion).where(
        ModelCapabilityVersion.model_config_id == model.id
    )
    price_query = select(ModelPriceVersion).where(ModelPriceVersion.model_config_id == model.id)
    if not history:
        capability_query = capability_query.where(ModelCapabilityVersion.is_active.is_(True))
        price_query = price_query.where(ModelPriceVersion.is_active.is_(True))
    capabilities = list(
        db.scalars(capability_query.order_by(ModelCapabilityVersion.version.desc()))
    )
    prices = list(db.scalars(price_query.order_by(ModelPriceVersion.version.desc())))
    active_capability = next((row for row in capabilities if row.is_active), None)
    active_price = next((row for row in prices if row.is_active), None)
    return {
        **public_model_option(model),
        "enabled": bool(model.enabled),
        "route_availability": model_route_summary(db, model),
        "capability_version": (
            serialize_capability_version(active_capability, public=not history)
            if active_capability is not None
            else None
        ),
        "price_version": serialize_price_version(active_price) if active_price is not None else None,
        **(
            {
                "capability_versions": [serialize_capability_version(row) for row in capabilities],
                "price_versions": [serialize_price_version(row) for row in prices],
            }
            if history
            else {}
        ),
    }


def serialize_tool_version(row: ToolVersion) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "schema_version": row.schema_version,
        "input_schema": dict(row.input_schema or {}),
        "workflow": dict(row.workflow or {}),
        "pricing_policy": dict(row.pricing_policy or {}),
        "capabilities": dict(row.capabilities or {}),
        "metadata_snapshot": dict(row.metadata_snapshot or {}),
        "is_active": bool(row.is_active),
        "activated_at": row.activated_at,
        "retired_at": row.retired_at,
        "created_at": row.created_at,
    }


def tool_catalog_detail(db: Session, tool: ToolDefinition, *, history: bool = False) -> dict:
    version_query = select(ToolVersion).where(ToolVersion.tool_definition_id == tool.id)
    if not history:
        version_query = version_query.where(ToolVersion.is_active.is_(True))
    versions = list(db.scalars(version_query.order_by(ToolVersion.version.desc())))
    active = next((row for row in versions if row.is_active), versions[0] if versions else None)
    return {
        "id": tool.id,
        "slug": tool.slug,
        "name": tool.name,
        "description": tool.description,
        "category": tool.category,
        "renderer": tool.renderer,
        "entry_path": tool.entry_path,
        "icon": tool.icon,
        "sort_order": int(tool.sort_order or 0),
        "enabled": bool(tool.enabled),
        "featured": bool(tool.featured),
        "active_version": serialize_tool_version(active) if active else None,
        "created_at": tool.created_at,
        "updated_at": tool.updated_at,
        **({"versions": [serialize_tool_version(row) for row in versions]} if history else {}),
    }
