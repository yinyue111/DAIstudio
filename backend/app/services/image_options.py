"""Supported image-size presets for UI defaults and reference adaptation."""
from __future__ import annotations

# OpenAI-compatible gpt-image-2 accepts custom 4K sizes when each side is a
# multiple of 16, the long edge is at most 3840px, the total area is no more
# than 3840x2160, and the aspect ratio stays within 1:3..3:1.
# Keep presets explicit so UI/admin defaults and backend auto-adaptation stay
# predictable across common social, poster, product and panoramic crops.
IMAGE_SIZES = (
    # square
    "1024x1024", "2048x2048", "2880x2880",
    # 4:5 / 5:4
    "1024x1280", "2048x2560", "2576x3216",
    "1280x1024", "2560x2048", "3216x2576",
    # 3:4 / 4:3
    "768x1024", "1536x2048", "2480x3312",
    "1024x768", "2048x1536", "3312x2480",
    # 2:3 / 3:2
    "1024x1536", "2048x3072", "2352x3520",
    "1536x1024", "3072x2048", "3520x2352",
    # 9:16 / 16:9
    "720x1280", "1080x1920", "2160x3840",
    "1280x720", "1920x1080", "3840x2160",
    # ultra-wide / vertical poster
    "1792x768", "3584x1536", "3840x1648",
    "768x1792", "1536x3584", "1648x3840",
    # legacy presets kept so older settings/tasks still round-trip naturally
    "512x512", "768x768", "1536x1536",
    "832x1216", "1216x832",
)
