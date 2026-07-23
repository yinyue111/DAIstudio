"""Versioned public reverse-workflow capability declarations."""
from __future__ import annotations

BATCH_CAPABILITIES = {
    "schema_version": "reverse-batch-capabilities.v2",
    "item_overrides": True,
    "supported_override_keys": [
        "target",
        "analysis_precision",
        "source_ranges",
        "custom_keyframes",
        "audio_policy",
    ],
    "audio_policies": ["inherit", "exclude", "analyze"],
}
