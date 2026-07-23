"""Product-surface policy for the controlled launch edition."""
from __future__ import annotations

from ..config import settings

FULL_EDITION = "full"
LAUNCH_LITE_EDITION = "launch_lite"
SUPPORTED_EDITIONS = frozenset({FULL_EDITION, LAUNCH_LITE_EDITION})

LAUNCH_LITE_HIDDEN_NAVIGATION = frozenset({"catalog", "projects"})
LAUNCH_LITE_DISABLED_API_PREFIXES = (
    "/api/projects",
    "/api/recipes",
    "/api/workflows",
    "/api/reproduction-assessments",
    "/api/prompt/reverse-batches",
)

_FULL_FEATURES = {
    "reverse_batch_enabled": True,
    "video_composition_enabled": True,
    "reproduction_assessment_enabled": True,
    "recipes_enabled": True,
    "projects_enabled": True,
    "tool_workflows_enabled": True,
}


def is_launch_lite() -> bool:
    return settings.product_edition == LAUNCH_LITE_EDITION


def public_feature_flags() -> dict[str, bool]:
    if not is_launch_lite():
        return dict(_FULL_FEATURES)
    return {key: False for key in _FULL_FEATURES}


def api_path_enabled(path: str) -> bool:
    if not is_launch_lite():
        return True
    normalized = str(path or "")
    return not any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in LAUNCH_LITE_DISABLED_API_PREFIXES
    )
