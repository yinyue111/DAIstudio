"""Supported image-size presets for UI defaults and reference adaptation."""
from __future__ import annotations

# Current production image traffic is routed through the Codex/OAuth-compatible
# gateway path, which cannot honor explicit 4K requests reliably. Keep 4K hidden,
# but allow 2K presets and save whatever image the gateway actually returns.
IMAGE_SIZES = (
    # square
    "1024x1024", "2048x2048",
    # 4:5 / 5:4
    "1024x1280", "1632x2048",
    "1280x1024", "2048x1632",
    # 3:4 / 4:3
    "768x1024", "1536x2048",
    "1024x768", "2048x1536",
    # 2:3 / 3:2
    "1024x1536", "1360x2048",
    "1536x1024", "2048x1360",
    # 9:16 / 16:9
    "720x1280", "1152x2048",
    "1280x720", "2048x1152",
    # ultra-wide / vertical poster
    "1280x544", "2048x864",
    "544x1280", "864x2048",
    # legacy presets kept so older settings/tasks still round-trip naturally
    "512x512", "768x768", "832x1216", "1216x832",
)
