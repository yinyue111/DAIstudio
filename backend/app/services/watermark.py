"""Make watermarked / low-res previews. HD original is kept untouched so the
internal unlock can serve it without a watermark."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw


def make_image_preview(
    hd_bytes: bytes,
    max_side: int = 768,
    *,
    max_pixels: int | None = None,
) -> tuple[bytes, int, int]:
    """Return (watermarked_preview_png, hd_width, hd_height)."""
    img = Image.open(io.BytesIO(hd_bytes))
    hd_w, hd_h = img.size
    if max_pixels is not None and hd_w * hd_h > max_pixels:
        raise ValueError("图片像素超出上限")
    img = img.convert("RGB")

    # downscale for the preview
    scale = min(1.0, max_side / max(img.size))
    if scale < 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))

    overlay = img.copy()
    d = ImageDraw.Draw(overlay)
    text = "内部预览 · PREVIEW"
    step = 180
    for y in range(0, overlay.height + step, step):
        for x in range(-step, overlay.width, step):
            d.text((x, y), text, fill=(255, 255, 255))
    blended = Image.blend(img, overlay, 0.35)

    buf = io.BytesIO()
    blended.save(buf, format="PNG")
    return buf.getvalue(), hd_w, hd_h


def make_model_reference(
    image_bytes: bytes,
    max_side: int = 768,
    *,
    max_pixels: int | None = None,
) -> tuple[bytes, int, int]:
    """Return a clean, bounded PNG reference for model inputs."""
    img = Image.open(io.BytesIO(image_bytes))
    hd_w, hd_h = img.size
    if max_pixels is not None and hd_w * hd_h > max_pixels:
        raise ValueError("图片像素超出上限")
    img = img.convert("RGB")
    scale = min(1.0, max_side / max(img.size))
    if scale < 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), hd_w, hd_h


def image_ext(img_bytes: bytes) -> str:
    """Best-effort extension for raw gateway image bytes."""
    try:
        fmt = (Image.open(io.BytesIO(img_bytes)).format or "").lower()
    except Exception:  # noqa: BLE001
        return "png"
    return {
        "jpeg": "jpg",
        "jpg": "jpg",
        "png": "png",
        "webp": "webp",
        "gif": "gif",
    }.get(fmt, "png")


def dimensions(img_bytes: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(img_bytes))
    return img.size
