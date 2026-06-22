"""Local gateway placeholders used when mock mode is enabled."""
from __future__ import annotations

import io


def mock_image(prompt: str, size: str, idx: int) -> bytes:
    from PIL import Image, ImageDraw

    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except Exception:
        w, h = 1024, 1024
    img = Image.new("RGB", (w, h))
    px = img.load()
    seed = (hash(prompt) + idx * 97) & 0xFFFFFF
    r0, g0, b0 = (seed >> 16) & 255, (seed >> 8) & 255, seed & 255
    for y in range(h):
        for x in range(0, w, 4):  # step for speed
            r = (r0 + x * 255 // w) % 256
            g = (g0 + y * 255 // h) % 256
            b = (b0 + (x + y) * 255 // (w + h)) % 256
            for dx in range(4):
                if x + dx < w:
                    px[x + dx, y] = (r, g, b)
    d = ImageDraw.Draw(img)
    d.text((24, 24), f"MOCK #{idx + 1}", fill=(255, 255, 255))
    d.text((24, 44), prompt[:60], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def mock_video_preview_image() -> bytes:
    """Placeholder still used as a stand-in for mock video preview/final."""
    return mock_image("video preview", "640x360", 0)
