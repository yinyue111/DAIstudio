"""Product-surface policy: per-feature switches with PRODUCT_EDITION defaults.

``PRODUCT_EDITION`` remains the default tier: ``full`` enables every advanced
feature, ``launch_lite`` disables them all. Each feature can additionally be
overridden independently through a ``FEATURE_*_ENABLED`` setting
(``bool | None``): ``None`` follows the edition default, an explicit
``true``/``false`` wins over the edition. Every gate (middleware, router
parameter checks, navigation, public config) must resolve through
``feature_enabled``/``public_feature_flags`` so there is exactly one flag
state.
"""
from __future__ import annotations

from ..config import settings

FULL_EDITION = "full"
LAUNCH_LITE_EDITION = "launch_lite"
SUPPORTED_EDITIONS = frozenset({FULL_EDITION, LAUNCH_LITE_EDITION})

# Public flag name -> settings override field (owned by config.py; read via
# getattr so either side can land first).
FEATURE_SETTING_FIELDS = {
    "projects_enabled": "feature_projects_enabled",
    "recipes_enabled": "feature_recipes_enabled",
    "reverse_batch_enabled": "feature_reverse_batch_enabled",
    "video_composition_enabled": "feature_video_composition_enabled",
    "reproduction_assessment_enabled": "feature_reproduction_assessment_enabled",
    "tool_workflows_enabled": "feature_tool_workflows_enabled",
}

# Navigation keys owned by each feature: hidden when the feature is off.
_FEATURE_HIDDEN_NAVIGATION = {
    "projects_enabled": ("projects",),
    "tool_workflows_enabled": ("catalog",),
}

# Public API prefixes owned by each feature: 404 when the feature is off.
_FEATURE_DISABLED_API_PREFIXES = {
    "projects_enabled": ("/api/projects",),
    "recipes_enabled": ("/api/recipes",),
    "tool_workflows_enabled": ("/api/workflows",),
    "reproduction_assessment_enabled": ("/api/reproduction-assessments",),
    "reverse_batch_enabled": ("/api/prompt/reverse-batches",),
}

# Static launch_lite defaults, kept as documentation/compat: the live values
# must come from hidden_navigation_keys() / api_path_enabled().
LAUNCH_LITE_HIDDEN_NAVIGATION = frozenset(
    key for keys in _FEATURE_HIDDEN_NAVIGATION.values() for key in keys
)
LAUNCH_LITE_DISABLED_API_PREFIXES = tuple(
    prefix
    for feature in FEATURE_SETTING_FIELDS
    for prefix in _FEATURE_DISABLED_API_PREFIXES.get(feature, ())
)


def is_launch_lite() -> bool:
    return settings.product_edition == LAUNCH_LITE_EDITION


def feature_enabled(feature: str) -> bool:
    """Resolve one feature switch.

    Explicit ``FEATURE_*_ENABLED`` setting wins; ``None`` falls back to the
    PRODUCT_EDITION default (full=on, launch_lite=off).
    """
    field = FEATURE_SETTING_FIELDS[feature]
    override = getattr(settings, field, None)
    if override is None:
        return not is_launch_lite()
    return bool(override)


def public_feature_flags() -> dict[str, bool]:
    return {feature: feature_enabled(feature) for feature in FEATURE_SETTING_FIELDS}


def hidden_navigation_keys() -> frozenset[str]:
    """Navigation entries hidden because their owning feature is off."""
    return frozenset(
        key
        for feature, keys in _FEATURE_HIDDEN_NAVIGATION.items()
        if not feature_enabled(feature)
        for key in keys
    )


def api_path_enabled(path: str) -> bool:
    normalized = str(path or "")
    for feature, prefixes in _FEATURE_DISABLED_API_PREFIXES.items():
        for prefix in prefixes:
            if normalized == prefix or normalized.startswith(f"{prefix}/"):
                return feature_enabled(feature)
    return True
