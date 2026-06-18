"""Supported image-size presets for UI defaults and reference adaptation."""
from __future__ import annotations

# The gateway accepts arbitrary WxH strings within the global 4096px edge cap.
# Keep presets explicit so the UI/admin defaults and backend auto-adaptation stay
# predictable across common social, poster, product and panoramic crops.
IMAGE_SIZES = (
    # square
    "1024x1024", "2048x2048", "4096x4096",
    # 4:5 / 5:4
    "1024x1280", "2048x2560", "3276x4096",
    "1280x1024", "2560x2048", "4080x3264",
    # 3:4 / 4:3
    "768x1024", "1536x2048", "3072x4096",
    "1024x768", "2048x1536", "4096x3072",
    # 2:3 / 3:2
    "1024x1536", "2048x3072", "2720x4080",
    "1536x1024", "3072x2048", "4080x2720",
    # 9:16 / 16:9
    "720x1280", "1080x1920", "2304x4096",
    "1280x720", "1920x1080", "4096x2304",
    # ultra-wide / vertical poster
    "1792x768", "3584x1536", "4088x1752",
    "768x1792", "1536x3584", "1752x4088",
    # legacy presets kept so older settings/tasks still round-trip naturally
    "512x512", "768x768", "1536x1536",
    "832x1216", "1216x832",
)
