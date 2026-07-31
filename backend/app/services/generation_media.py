"""Media sizing, reference, and poster helpers for generation tasks."""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Literal

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..config import settings
from ..models import GenTask, UploadedAsset
from . import asset_refs, gateway, locks, reverse_lineage, storage, video_frames
from .image_options import IMAGE_SIZES
from .watermark import make_image_preview

log = logging.getLogger("generation")

VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
EDIT_MASK_SEND_CONFIDENCE = 0.60
EDIT_MASK_ANALYSIS_MAX_SIDE = 512
EDIT_MASK_AUTO_EXPAND_RATIO = 0.010
EDIT_MASK_AUTO_MAX_EXPAND = 7
EditMaskMode = Literal[
    "alpha_subject",
    "auto_subject",
    "center_box",
    "reviewed_evidence",
    "none",
]


@dataclass(frozen=True)
class EditMaskResult:
    data_uri: str | None
    mode: EditMaskMode
    confidence: float
    bbox: tuple[int, int, int, int] | None
    width: int
    height: int
    reason: str | None = None

    @property
    def metadata(self) -> dict:
        return {
            "_edit_mask_mode": self.mode,
            "_edit_mask_confidence": round(float(self.confidence), 3),
            "_edit_mask_bbox": list(self.bbox) if self.bbox else None,
            "_edit_mask_source": self.reason or self.mode,
        }


class SubjectProtectionBusy(RuntimeError):
    pass


def final_prompt(task: GenTask) -> str:
    p = task.prompt or {}
    if p.get("final_text"):
        return p["final_text"]
    if p.get("instruction"):
        return p["instruction"]
    # Reverse-off mode still needs concrete visual constraints when the caller
    # supplied only a reference image.
    return "Match the reference subject, composition, lighting, color palette, and materials."


def gateway_reference_image(
    db,
    task: GenTask,
    url: str | None,
    *,
    min_side: int = 1,
    max_side: int = 384,
    prefer_original_upload: bool = False,
    prefer_original_generated: bool = False,
    quality: int = 82,
    subsampling: int = 2,
    return_content_hash: bool = False,
) -> str | tuple[str, str] | None:
    if not url:
        return None
    if url.startswith("data:image/"):
        if return_content_hash:
            return url, reverse_lineage.data_uri_content_hash(url)
        return url
    try:
        return asset_refs.gateway_ref_for_user_asset(
            db,
            task.user_id,
            url,
            min_side=min_side,
            max_side=max_side,
            prefer_original_upload=prefer_original_upload,
            prefer_original_generated=prefer_original_generated,
            quality=quality,
            subsampling=subsampling,
            return_content_hash=return_content_hash,
        )
    except asset_refs.AssetRefError as e:
        raise RuntimeError(str(e)) from e


def image_data_uri(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _asset_image_bytes_for_mask(db, task: GenTask | int, url: str | None) -> bytes | None:
    if not url:
        return None
    user_id = int(task if isinstance(task, int) else task.user_id)
    key = storage.key_from_url(url)
    if not key:
        return None
    row = db.get(UploadedAsset, key)
    if row:
        if row.user_id != user_id:
            raise RuntimeError("上传素材不存在")
        path = storage.local_path(key)
    elif key.startswith(("upload/", "upload_preview/", "upload_video/", "upload_video_preview/")):
        raise RuntimeError("上传素材不存在")
    else:
        path = asset_refs.generated_asset_reference_path(
            db,
            user_id,
            key,
            prefer_original=True,
        )
    if not path.exists() or not path.is_file():
        raise RuntimeError("素材文件不存在")
    return Path(path).read_bytes()


def _mask_data_uri(mask: Image.Image) -> str:
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return image_data_uri(buf.getvalue(), "image/png")


def _mask_alpha_from_data_uri(data_uri: str | None) -> Image.Image | None:
    if not data_uri or "," not in str(data_uri):
        return None
    try:
        raw = base64.b64decode(str(data_uri).split(",", 1)[1])
        mask = Image.open(io.BytesIO(raw)).convert("RGBA")
        return mask.getchannel("A")
    except Exception:  # noqa: BLE001
        return None


def _resize_for_mask(raw: bytes, *, max_side: int) -> Image.Image:
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    scale = min(1.0, max_side / max(img.size))
    if scale < 1.0:
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return img


def _exclusive_bbox_to_inclusive(
    bbox: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    if not bbox:
        return None
    left, top, right, bottom = bbox
    return left, top, max(left, right - 1), max(top, bottom - 1)


def _scale_bbox(
    bbox: tuple[int, int, int, int] | None,
    *,
    from_size: tuple[int, int],
    to_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    if not bbox:
        return None
    src_w, src_h = from_size
    dst_w, dst_h = to_size
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return None
    sx = dst_w / src_w
    sy = dst_h / src_h
    left, top, right, bottom = bbox
    return (
        max(0, min(dst_w - 1, int(round(left * sx)))),
        max(0, min(dst_h - 1, int(round(top * sy)))),
        max(0, min(dst_w - 1, int(round((right + 1) * sx)) - 1)),
        max(0, min(dst_h - 1, int(round((bottom + 1) * sy)) - 1)),
    )


def _center_box_mask(img: Image.Image, *, reason: str = "center_box") -> EditMaskResult:
    mask = Image.new("RGBA", img.size, (0, 0, 0, 0))
    left = int(round(img.width * 0.12))
    top = int(round(img.height * 0.12))
    right = int(round(img.width * 0.88))
    bottom = int(round(img.height * 0.88))
    bbox = (max(0, left), max(0, top), min(img.width - 1, right), min(img.height - 1, bottom))
    ImageDraw.Draw(mask).rectangle(bbox, fill=(255, 255, 255, 255))
    return EditMaskResult(
        data_uri=_mask_data_uri(mask),
        mode="center_box",
        confidence=0.35,
        bbox=bbox,
        width=img.width,
        height=img.height,
        reason=reason,
    )


def _alpha_subject_mask(img: Image.Image, alpha: Image.Image) -> EditMaskResult:
    mask_alpha = alpha.point(lambda px: 255 if px > 8 else 0)
    bbox = _exclusive_bbox_to_inclusive(mask_alpha.getbbox())
    if not bbox:
        return EditMaskResult(
            data_uri=None,
            mode="none",
            confidence=0.0,
            bbox=None,
            width=img.width,
            height=img.height,
            reason="empty_alpha",
        )
    mask = Image.new("RGBA", img.size, (255, 255, 255, 0))
    mask.putalpha(mask_alpha)
    return EditMaskResult(
        data_uri=_mask_data_uri(mask),
        mode="alpha_subject",
        confidence=0.95,
        bbox=bbox,
        width=img.width,
        height=img.height,
        reason="alpha_channel",
    )


def _has_usable_alpha_cutout(alpha: Image.Image) -> bool:
    """Reject incidental transparent pixels from otherwise ordinary PNGs."""
    w, h = alpha.size
    data = alpha.tobytes()
    total = len(data)
    if total <= 0:
        return False
    transparent = sum(1 for value in data if value <= 8)
    if transparent < max(16, int(round(total * 0.005))):
        return False

    border_total = 0
    border_transparent = 0
    for x in range(w):
        for idx in (x, (h - 1) * w + x):
            border_total += 1
            border_transparent += int(data[idx] <= 8)
    for y in range(1, max(1, h - 1)):
        for idx in (y * w, y * w + (w - 1)):
            border_total += 1
            border_transparent += int(data[idx] <= 8)
    return border_transparent >= max(4, int(round(border_total * 0.02)))


def _border_samples(rgb: Image.Image) -> list[tuple[int, int, int]]:
    w, h = rgb.size
    px = rgb.load()
    margin = max(1, int(round(min(w, h) * 0.04)))
    step = max(1, min(w, h) // 96)
    samples: list[tuple[int, int, int]] = []
    for y in range(0, min(h, margin), step):
        for x in range(0, w, step):
            samples.append(px[x, y])
    for y in range(max(0, h - margin), h, step):
        for x in range(0, w, step):
            samples.append(px[x, y])
    for x in range(0, min(w, margin), step):
        for y in range(0, h, step):
            samples.append(px[x, y])
    for x in range(max(0, w - margin), w, step):
        for y in range(0, h, step):
            samples.append(px[x, y])
    return samples


def _estimate_background(rgb: Image.Image) -> tuple[tuple[int, int, int], float, int]:
    samples = _border_samples(rgb)
    if not samples:
        return (255, 255, 255), 255.0, 0
    bg = tuple(int(median(channel)) for channel in zip(*samples, strict=False))
    distances = [
        max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2]))
        for r, g, b in samples
    ]
    distances.sort()
    p75 = distances[min(len(distances) - 1, int(len(distances) * 0.75))]
    hsv_bg = Image.new("RGB", (1, 1), bg).convert("HSV").getpixel((0, 0))
    return bg, float(p75), int(hsv_bg[1])


def _largest_component_mask(mask: Image.Image) -> tuple[Image.Image | None, tuple[int, int, int, int] | None, int]:
    w, h = mask.size
    data = mask.tobytes()
    visited = bytearray(w * h)
    best: list[int] = []
    best_bbox: tuple[int, int, int, int] | None = None
    for start, value in enumerate(data):
        if not value or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        component: list[int] = []
        min_x = max_x = start % w
        min_y = max_y = start // w
        while stack:
            idx = stack.pop()
            component.append(idx)
            x = idx % w
            y = idx // w
            if x < min_x:
                min_x = x
            elif x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            elif y > max_y:
                max_y = y
            if x > 0:
                nxt = idx - 1
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if x + 1 < w:
                nxt = idx + 1
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if y > 0:
                nxt = idx - w
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if y + 1 < h:
                nxt = idx + w
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
        if len(component) > len(best):
            best = component
            best_bbox = (min_x, min_y, max_x, max_y)
    if not best or not best_bbox:
        return None, None, 0
    out = bytearray(w * h)
    for idx in best:
        out[idx] = 255
    return Image.frombytes("L", (w, h), bytes(out)), best_bbox, len(best)


def _carve_background_regions(
    subject: Image.Image,
    rgb: Image.Image,
    *,
    bg: tuple[int, int, int],
    bg_noise: float,
) -> Image.Image:
    """Remove exterior background leaks and compact see-through product holes."""
    w, h = subject.size
    total = w * h
    if total <= 0:
        return subject
    subject_data = subject.tobytes()
    rgb_data = rgb.tobytes()
    threshold = max(3, min(10, int(round(bg_noise * 0.5)) + 3))
    background_like = bytearray(total)
    for idx in range(total):
        base = idx * 3
        if max(
            abs(rgb_data[base] - bg[0]),
            abs(rgb_data[base + 1] - bg[1]),
            abs(rgb_data[base + 2] - bg[2]),
        ) <= threshold:
            background_like[idx] = 1

    exterior = bytearray(total)
    stack: list[int] = []

    def push(idx: int) -> None:
        if background_like[idx] and not exterior[idx]:
            exterior[idx] = 1
            stack.append(idx)

    for x in range(w):
        push(x)
        push((h - 1) * w + x)
    for y in range(h):
        push(y * w)
        push(y * w + (w - 1))
    while stack:
        idx = stack.pop()
        x = idx % w
        y = idx // w
        if x > 0:
            push(idx - 1)
        if x + 1 < w:
            push(idx + 1)
        if y > 0:
            push(idx - w)
        if y + 1 < h:
            push(idx + w)

    out = bytearray(subject_data)
    for idx, is_exterior in enumerate(exterior):
        if is_exterior:
            out[idx] = 0

    seen = bytearray(exterior)
    min_hole_area = max(24, int(round(total * 0.00008)))
    max_hole_area = max(min_hole_area, int(round(total * 0.012)))
    for start, value in enumerate(background_like):
        if not value or seen[start]:
            continue
        component = [start]
        seen[start] = 1
        cells: list[int] = []
        min_x = max_x = start % w
        min_y = max_y = start // w
        while component:
            idx = component.pop()
            cells.append(idx)
            x = idx % w
            y = idx // w
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            if x > 0:
                nxt = idx - 1
                if background_like[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    component.append(nxt)
            if x + 1 < w:
                nxt = idx + 1
                if background_like[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    component.append(nxt)
            if y > 0:
                nxt = idx - w
                if background_like[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    component.append(nxt)
            if y + 1 < h:
                nxt = idx + w
                if background_like[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    component.append(nxt)
        area = len(cells)
        bbox_w = max_x - min_x + 1
        bbox_h = max_y - min_y + 1
        fill_ratio = area / max(1, bbox_w * bbox_h)
        masked_ratio = sum(1 for idx in cells if out[idx]) / max(1, area)
        aspect = bbox_w / max(1, bbox_h)
        if (
            min_hole_area <= area <= max_hole_area
            and bbox_w >= 5
            and bbox_h >= 5
            and 0.25 <= aspect <= 4.0
            and fill_ratio >= 0.55
            and masked_ratio >= 0.80
        ):
            for idx in cells:
                out[idx] = 0
    return Image.frombytes("L", (w, h), bytes(out))


def _odd_kernel_size(value: int, *, minimum: int = 3, maximum: int | None = None) -> int:
    size = max(minimum, int(value))
    if maximum is not None:
        size = min(size, maximum)
    if size % 2 == 0:
        size += 1 if maximum is None or size < maximum else -1
    return max(1, size)


def _refine_auto_subject_alpha(component: Image.Image) -> Image.Image:
    """Keep RGB auto masks tight so white upload backgrounds are not pasted back.

    Alpha PNG uploads already provide a real cutout. For ordinary JPG/RGB
    uploads we only have a heuristic subject mask, so aggressive dilation turns
    any retained white background into a visible sticker edge after inpainting.
    A tiny trim followed by a small expansion protects product edges without
    preserving a thick background fringe.
    """
    original = component
    if min(component.size) >= 96:
        trimmed = component.filter(ImageFilter.MinFilter(3))
        if trimmed.getbbox():
            component = trimmed
    expand = _odd_kernel_size(
        int(round(min(component.size) * EDIT_MASK_AUTO_EXPAND_RATIO)),
        minimum=3,
        maximum=EDIT_MASK_AUTO_MAX_EXPAND,
    )
    expanded = component.filter(ImageFilter.MaxFilter(expand))
    return expanded if expanded.getbbox() else original


def _edge_enclosed_foreground_mask(rgb: Image.Image, *, bg_noise: float) -> Image.Image | None:
    """Recover white-on-white product interiors by treating strong edges as barriers.

    The color-distance detector misses white packaging on white backgrounds.
    On low-noise studio uploads, product outlines and printed labels usually
    form enough barriers that a border flood-fill can separate the exterior
    background from the enclosed product body.
    """
    if bg_noise > 18:
        return None
    w, h = rgb.size
    if w <= 2 or h <= 2:
        return None
    threshold = max(16, min(58, int(bg_noise * 1.8 + 22)))
    edges = rgb.convert("L").filter(ImageFilter.FIND_EDGES)
    barrier = edges.point(lambda px: 255 if px >= threshold else 0)
    barrier = barrier.filter(ImageFilter.MaxFilter(3))
    barrier_data = barrier.tobytes()
    visited = bytearray(w * h)
    stack: list[int] = []

    def push(idx: int) -> None:
        if not visited[idx] and not barrier_data[idx]:
            visited[idx] = 1
            stack.append(idx)

    for x in range(w):
        push(x)
        push((h - 1) * w + x)
    for y in range(h):
        push(y * w)
        push(y * w + (w - 1))

    while stack:
        idx = stack.pop()
        x = idx % w
        y = idx // w
        if x > 0:
            push(idx - 1)
        if x + 1 < w:
            push(idx + 1)
        if y > 0:
            push(idx - w)
        if y + 1 < h:
            push(idx + w)

    out = bytearray(w * h)
    for idx, edge_value in enumerate(barrier_data):
        if edge_value or not visited[idx]:
            out[idx] = 255
    mask = Image.frombytes("L", (w, h), bytes(out))
    mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    bbox = _exclusive_bbox_to_inclusive(mask.getbbox())
    if not bbox:
        return None
    area = int(mask.point(lambda px: 1 if px else 0).histogram()[1])
    bbox_area = (bbox[2] - bbox[0] + 1) * (bbox[3] - bbox[1] + 1)
    if area / max(1, w * h) > 0.65 or bbox_area / max(1, w * h) > 0.85:
        return None
    if bbox[0] <= 1 or bbox[1] <= 1 or bbox[2] >= w - 2 or bbox[3] >= h - 2:
        return None
    return mask


def _edge_span_subject_mask(
    rgb: Image.Image,
    *,
    bg: tuple[int, int, int],
    bg_noise: float,
    bg_sat: int,
) -> Image.Image | None:
    """Infer a product silhouette from row spans on low-noise white backgrounds.

    White packaging on a white upload background is the hardest cheap-CV case:
    color distance mostly sees only logo/text/black base, while the actual box
    body is near the background color. Product photos still contain faint edges,
    texture, and printed details. For that case, collect reliable per-row detail
    spans and fill between their left/right edges to approximate the full
    product silhouette without falling back to a large rectangle.
    """
    if bg_noise > 18 or bg_sat > 40 or max(bg) < 225:
        return None
    w, h = rgb.size
    if w < 32 or h < 32:
        return None
    gray = rgb.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES).tobytes()
    data = rgb.tobytes()
    raw = bytearray(w * h)
    for y in range(3, h - 3):
        row = y * w
        for x in range(3, w - 3):
            idx = row + x
            base = idx * 3
            r = data[base]
            g = data[base + 1]
            b = data[base + 2]
            dist = max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2]))
            dark = 255 - max(r, g, b)
            if edges[idx] >= 10 or dist >= 12 or dark >= 14:
                raw[idx] = 255
    detail = Image.frombytes("L", (w, h), bytes(raw)).filter(ImageFilter.MaxFilter(3))
    detail_data = detail.tobytes()
    out = bytearray(w * h)
    valid_rows = 0
    for y in range(h):
        row = y * w
        left = None
        right = None
        count = 0
        for x in range(w):
            if detail_data[row + x]:
                count += 1
                if left is None:
                    left = x
                right = x
        if left is None or right is None or count < 4:
            continue
        span_w = right - left + 1
        if left <= 2 or right >= w - 3 or span_w > w * 0.86 or span_w < 8:
            continue
        expand = max(6, int(round(span_w * 0.06)))
        left = max(0, left - expand)
        right = min(w - 1, right + expand)
        for x in range(left, right + 1):
            out[row + x] = 255
        valid_rows += 1
    if valid_rows < max(12, int(h * 0.06)):
        return None
    mask = Image.frombytes("L", (w, h), bytes(out))
    mask = mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(3))
    component, bbox, area = _largest_component_mask(mask)
    if component is None or bbox is None:
        return None
    area_ratio = area / max(1, w * h)
    bbox_ratio = _bbox_area(bbox) / max(1, w * h)
    if not (0.03 <= area_ratio <= 0.60 and 0.06 <= bbox_ratio <= 0.76):
        return None
    if bbox[0] <= 2 or bbox[1] <= 2 or bbox[2] >= w - 3 or bbox[3] >= h - 3:
        return None
    return component


def _bright_neutral_subject_mask(rgb: Image.Image) -> tuple[Image.Image | None, tuple[int, int, int, int] | None, int]:
    """Find bright low-saturation packaging on colored/natural backgrounds."""
    hsv = rgb.convert("HSV")
    w, h = hsv.size
    px = hsv.load()
    raw = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            _hue, sat, val = px[x, y]
            if val >= 150 and sat <= 82:
                raw[row + x] = 255
    candidate = Image.frombytes("L", (w, h), bytes(raw))
    candidate = candidate.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(5))
    data = candidate.tobytes()
    visited = bytearray(w * h)
    best_component: list[int] = []
    best_bbox: tuple[int, int, int, int] | None = None
    best_score = 0.0
    for start, value in enumerate(data):
        if not value or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        component: list[int] = []
        min_x = max_x = start % w
        min_y = max_y = start // w
        while stack:
            idx = stack.pop()
            component.append(idx)
            x = idx % w
            y = idx // w
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            if x > 0:
                nxt = idx - 1
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if x + 1 < w:
                nxt = idx + 1
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if y > 0:
                nxt = idx - w
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
            if y + 1 < h:
                nxt = idx + w
                if data[nxt] and not visited[nxt]:
                    visited[nxt] = 1
                    stack.append(nxt)
        bbox = (min_x, min_y, max_x, max_y)
        area = len(component)
        bbox_w = max_x - min_x + 1
        bbox_h = max_y - min_y + 1
        area_ratio = area / max(1, w * h)
        bbox_ratio = _bbox_area(bbox) / max(1, w * h)
        aspect = bbox_w / max(1, bbox_h)
        center_y = (min_y + max_y) / 2 / max(1, h)
        touches_edge = min_y <= 2 or max_y >= h - 3 or min_x <= 2 or max_x >= w - 3
        if (
            touches_edge
            or area_ratio < 0.008
            or area_ratio > 0.45
            or bbox_ratio > 0.60
            or not (0.45 <= aspect <= 5.5)
            or center_y < 0.22
        ):
            continue
        score = area * (1.0 + center_y)
        if score > best_score:
            best_score = score
            best_component = component
            best_bbox = bbox
    if not best_component or not best_bbox:
        return None, None, 0
    out = bytearray(w * h)
    for idx in best_component:
        out[idx] = 255
    return Image.frombytes("L", (w, h), bytes(out)), best_bbox, len(best_component)


def _auto_subject_mask(img: Image.Image) -> EditMaskResult:
    source_size = img.size
    work = img
    scale = min(1.0, EDIT_MASK_ANALYSIS_MAX_SIDE / max(work.size))
    if scale < 1.0:
        work = work.resize(
            (max(1, int(work.width * scale)), max(1, int(work.height * scale))),
            Image.Resampling.BOX,
        )
    rgb = work.convert("RGB")
    hsv = rgb.convert("HSV")
    w, h = rgb.size
    bg, bg_noise, bg_sat = _estimate_background(rgb)
    diff_threshold = max(18, min(70, int(bg_noise * 2.4 + 16)))
    sat_threshold = max(48, min(120, bg_sat + 32))
    rgb_px = rgb.load()
    hsv_px = hsv.load()
    raw = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = rgb_px[x, y]
            dist = max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2]))
            sat = hsv_px[x, y][1]
            if dist >= diff_threshold or (dist >= diff_threshold * 0.65 and sat >= sat_threshold):
                raw[row + x] = 255
    candidate = Image.frombytes("L", (w, h), bytes(raw))
    enclosed = _edge_enclosed_foreground_mask(rgb, bg_noise=bg_noise)
    if enclosed is not None:
        candidate = ImageChops.lighter(candidate, enclosed)
    span_subject = _edge_span_subject_mask(rgb, bg=bg, bg_noise=bg_noise, bg_sat=bg_sat)
    if span_subject is not None:
        candidate = ImageChops.lighter(candidate, span_subject)
    candidate = candidate.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    component, bbox, area = _largest_component_mask(candidate)
    bright_component = None
    bright_bbox = None
    bright_area = 0
    if component is None or bbox is None:
        bright_component, bright_bbox, bright_area = _bright_neutral_subject_mask(rgb)
        if bright_component is None or bright_bbox is None:
            return EditMaskResult(None, "none", 0.0, None, source_size[0], source_size[1], "no_foreground")
        component, bbox, area = bright_component, bright_bbox, bright_area
    area_ratio = area / max(1, w * h)
    bbox_area = (bbox[2] - bbox[0] + 1) * (bbox[3] - bbox[1] + 1)
    bbox_ratio = bbox_area / max(1, w * h)
    touches_edge = bbox[0] <= 1 or bbox[1] <= 1 or bbox[2] >= w - 2 or bbox[3] >= h - 2
    if area_ratio < 0.008:
        return EditMaskResult(
            None,
            "none",
            0.0,
            _scale_bbox(bbox, from_size=(w, h), to_size=source_size),
            source_size[0],
            source_size[1],
            "foreground_too_small",
        )
    if area_ratio > 0.80 or bbox_ratio > 0.92:
        bright_component, bright_bbox, bright_area = _bright_neutral_subject_mask(rgb)
        if bright_component is not None and bright_bbox is not None:
            component, bbox, area = bright_component, bright_bbox, bright_area
            area_ratio = area / max(1, w * h)
            bbox_area = (bbox[2] - bbox[0] + 1) * (bbox[3] - bbox[1] + 1)
            bbox_ratio = bbox_area / max(1, w * h)
            touches_edge = bbox[0] <= 1 or bbox[1] <= 1 or bbox[2] >= w - 2 or bbox[3] >= h - 2
        else:
            return EditMaskResult(
                None,
                "none",
                0.0,
                _scale_bbox(bbox, from_size=(w, h), to_size=source_size),
                source_size[0],
                source_size[1],
                "foreground_too_large",
            )
    subject_alpha = _refine_auto_subject_alpha(component)
    subject_alpha = _carve_background_regions(
        subject_alpha,
        rgb,
        bg=bg,
        bg_noise=bg_noise,
    )
    clean_component, bbox, area = _largest_component_mask(subject_alpha)
    if clean_component is None or bbox is None:
        return EditMaskResult(None, "none", 0.0, None, source_size[0], source_size[1], "empty_subject")
    subject_alpha = clean_component
    area_ratio = area / max(1, w * h)
    bbox_area = _bbox_area(bbox)
    bbox_ratio = bbox_area / max(1, w * h)
    touches_edge = bbox[0] <= 1 or bbox[1] <= 1 or bbox[2] >= w - 2 or bbox[3] >= h - 2
    bbox_w = bbox[2] - bbox[0] + 1
    bbox_h = bbox[3] - bbox[1] + 1
    solidity = area / max(1, bbox_area)
    aspect = bbox_w / max(1, bbox_h)
    if (
        span_subject is None
        and bg_noise <= 12
        and bg_sat <= 24
        and max(bg) >= 245
        and area_ratio < 0.05
        and bbox_ratio < 0.06
        and solidity >= 0.68
        and aspect >= 1.8
    ):
        return EditMaskResult(
            None,
            "none",
            0.42,
            _scale_bbox(bbox, from_size=(w, h), to_size=source_size),
            source_size[0],
            source_size[1],
            "label_only_candidate",
        )
    confidence = 0.62
    if bg_noise <= 12:
        confidence += 0.12
    if 0.02 <= area_ratio <= 0.55:
        confidence += 0.08
    if 0.03 <= bbox_ratio <= 0.70:
        confidence += 0.06
    if touches_edge:
        confidence -= 0.12
    confidence = max(0.0, min(0.88, confidence))
    final_alpha = subject_alpha
    final_bbox = bbox
    if (w, h) != source_size:
        final_alpha = subject_alpha.resize(source_size, Image.Resampling.NEAREST)
        final_bbox = _exclusive_bbox_to_inclusive(final_alpha.getbbox())
        if not final_bbox:
            return EditMaskResult(None, "none", 0.0, None, source_size[0], source_size[1], "empty_subject")
    if touches_edge or confidence < EDIT_MASK_SEND_CONFIDENCE:
        bright_component, bright_bbox, bright_area = _bright_neutral_subject_mask(rgb)
        if bright_component is not None and bright_bbox is not None:
            bright_alpha = _refine_auto_subject_alpha(bright_component)
            bright_final_bbox = _exclusive_bbox_to_inclusive(bright_alpha.getbbox())
            if bright_final_bbox:
                bright_confidence = max(confidence if not touches_edge else 0.0, 0.68)
                if (w, h) != source_size:
                    bright_alpha = bright_alpha.resize(source_size, Image.Resampling.NEAREST)
                    bright_final_bbox = _exclusive_bbox_to_inclusive(bright_alpha.getbbox())
                if bright_final_bbox:
                    mask = Image.new("RGBA", img.size, (255, 255, 255, 0))
                    mask.putalpha(bright_alpha)
                    return EditMaskResult(
                        data_uri=_mask_data_uri(mask),
                        mode="auto_subject",
                        confidence=min(0.82, bright_confidence),
                        bbox=bright_final_bbox,
                        width=source_size[0],
                        height=source_size[1],
                        reason="bright_neutral_subject",
                    )
    if touches_edge:
        return EditMaskResult(
            None,
            "none",
            min(confidence, 0.55),
            _scale_bbox(bbox, from_size=(w, h), to_size=source_size),
            source_size[0],
            source_size[1],
            "edge_connected_foreground",
        )
    if confidence < EDIT_MASK_SEND_CONFIDENCE:
        return EditMaskResult(
            None,
            "none",
            confidence,
            final_bbox,
            source_size[0],
            source_size[1],
            "low_confidence_subject",
        )
    mask = Image.new("RGBA", img.size, (255, 255, 255, 0))
    mask.putalpha(final_alpha)
    return EditMaskResult(
        data_uri=_mask_data_uri(mask),
        mode="auto_subject",
        confidence=confidence,
        bbox=final_bbox,
        width=source_size[0],
        height=source_size[1],
        reason="border_background_heuristic",
    )


def gateway_image_edit_mask(
    db,
    task: GenTask | int,
    url: str | None,
    *,
    max_side: int = 1024,
    edit_mask_mode: str = "protect_subject",
) -> EditMaskResult | None:
    """Return an OpenAI-compatible PNG mask result for product-protecting edits.

    Transparent pixels are editable; opaque pixels are preserved. Alpha uploads
    are high-confidence subject masks. Ordinary RGB uploads first try cheap
    foreground detection; center-box protection is only a compatibility
    fallback and should not be presented as true subject recognition.
    """
    slot = locks.RedisSemaphore(
        "semaphore:subject-protection",
        limit=int(settings.subject_protection_parallelism),
        ttl=120,
        wait_timeout=0,
    )
    try:
        slot.__enter__()
    except TimeoutError:
        raise SubjectProtectionBusy("主体保护处理繁忙,请稍后再试") from None
    try:
        raw = _asset_image_bytes_for_mask(db, task, url)
        if not raw:
            return None
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        scale = min(1.0, max_side / max(img.size))
        if scale < 1.0:
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.Resampling.LANCZOS,
            )
        alpha = img.getchannel("A")
        if _has_usable_alpha_cutout(alpha):
            return _alpha_subject_mask(img, alpha)
        if str(edit_mask_mode or "").lower().strip() == "center_box":
            return _center_box_mask(img, reason="explicit_center_box")
        return _auto_subject_mask(img)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, SubjectProtectionBusy):
            raise
        raise RuntimeError(f"编辑蒙版生成失败:{e}") from e
    finally:
        slot.__exit__(None, None, None)


def _fit_subject_bbox(
    source_size: tuple[int, int],
    target_bbox: tuple[int, int, int, int],
) -> tuple[int, int, int, int] | None:
    source_w, source_h = source_size
    if source_w <= 0 or source_h <= 0:
        return None
    left, top, right, bottom = target_bbox
    target_w = max(1, right - left + 1)
    target_h = max(1, bottom - top + 1)
    scale = min(target_w / source_w, target_h / source_h)
    fitted_w = max(1, int(round(source_w * scale)))
    fitted_h = max(1, int(round(source_h * scale)))
    cx = left + target_w / 2
    cy = top + target_h / 2
    fit_left = int(round(cx - fitted_w / 2))
    fit_top = int(round(cy - fitted_h / 2))
    return fit_left, fit_top, fit_left + fitted_w - 1, fit_top + fitted_h - 1


def _bbox_area(bbox: tuple[int, int, int, int] | None) -> int:
    if not bbox:
        return 0
    left, top, right, bottom = bbox
    return max(0, right - left + 1) * max(0, bottom - top + 1)


def _bbox_iou(
    a: tuple[int, int, int, int] | None,
    b: tuple[int, int, int, int] | None,
) -> float:
    if not a or not b:
        return 0.0
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    intersection = _bbox_area((left, top, right, bottom))
    if intersection <= 0:
        return 0.0
    union = _bbox_area(a) + _bbox_area(b) - intersection
    return intersection / max(1, union)


def _jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=96, subsampling=0, optimize=True)
    return buf.getvalue()


def composite_product_subject_pixels(
    db,
    task: GenTask,
    result_raw: bytes,
    source_url: str | None,
    source_mask: EditMaskResult | None,
    *,
    max_side: int = 1536,
) -> tuple[bytes, dict] | None:
    """Composite the original product pixels back onto a generated scene.

    Image-edit gateways may still redraw protected text/logo areas even when a
    mask is sent. For product mode, the stronger invariant is to reuse the
    original product pixels and let the model contribute the background/style.
    """
    if (
        not source_url
        or not source_mask
        or source_mask.mode not in {"alpha_subject", "auto_subject", "reviewed_evidence"}
    ):
        return None
    source_alpha = _mask_alpha_from_data_uri(source_mask.data_uri)
    if source_alpha is None:
        return None
    try:
        source_img = _resize_for_mask(_asset_image_bytes_for_mask(db, task, source_url) or b"", max_side=max_side)
        result_img = Image.open(io.BytesIO(result_raw)).convert("RGBA")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"产品像素锁定准备失败:{e}") from e
    if source_alpha.size != source_img.size:
        source_alpha = source_alpha.resize(source_img.size, Image.Resampling.NEAREST)
    source_bbox = _exclusive_bbox_to_inclusive(source_alpha.getbbox()) or source_mask.bbox
    if not source_bbox:
        return None

    scaled_source_bbox = _scale_bbox(source_bbox, from_size=source_img.size, to_size=result_img.size)
    if not scaled_source_bbox:
        return None
    # The edit mask defines where the protected product belongs. Re-detecting a
    # subject in the generated scene is unsafe: bright props, mist, stone
    # pedestals, or the model's own duplicate product can become the target and
    # produce a shifted/scaled second package. Keep the source-space placement
    # deterministic and let only the surrounding pixels come from the model.
    target_bbox = scaled_source_bbox
    placement_method = "source_scaled_bbox"
    target_confidence = 0.0

    sx1, sy1, sx2, sy2 = source_bbox
    source_crop = source_img.crop((sx1, sy1, sx2 + 1, sy2 + 1))
    alpha_crop = source_alpha.crop((sx1, sy1, sx2 + 1, sy2 + 1))
    fitted = _fit_subject_bbox(source_crop.size, target_bbox)
    if not fitted:
        return None
    tx1, ty1, tx2, ty2 = fitted
    paste_w = max(1, tx2 - tx1 + 1)
    paste_h = max(1, ty2 - ty1 + 1)
    source_layer = source_crop.resize((paste_w, paste_h), Image.Resampling.LANCZOS)
    alpha_layer = alpha_crop.resize((paste_w, paste_h), Image.Resampling.LANCZOS)
    feather = max(1, int(round(min(paste_w, paste_h) * 0.004)))
    alpha_layer = alpha_layer.filter(ImageFilter.GaussianBlur(feather))
    canvas = result_img.copy()
    canvas.paste(source_layer, (tx1, ty1), alpha_layer)
    return _jpeg_bytes(canvas), {
        "_product_composite_source_bbox": list(source_bbox),
        "_product_composite_target_bbox": list(target_bbox),
        "_product_composite_target_confidence": round(target_confidence, 3),
        "_product_composite_placement": placement_method,
    }


def gateway_video_first_frame(db, task: GenTask) -> str | None:
    key = storage.key_from_url(task.source_asset_url or "")
    if not key:
        return None
    if key.startswith("upload_video/"):
        poster_key = asset_refs.upload_video_poster_key(key)
        if poster_key:
            return gateway_reference_image(db, task, storage.upload_api_url(poster_key))
        path = asset_refs.generated_video_reference_path(db, task.user_id, key)
        poster = video_frames.extract_poster(str(path))
        return image_data_uri(poster) if poster else None
    if key.startswith(("video_preview/", "video_hd/")):
        try:
            path = asset_refs.generated_video_reference_path(db, task.user_id, key)
        except asset_refs.AssetRefError:
            return None
        poster = video_frames.extract_poster(str(path))
        return image_data_uri(poster) if poster else None
    return None


def reference_dimensions(task: GenTask) -> tuple[int | None, int | None]:
    params = task.params or {}
    width = params.get("reference_width") or params.get("width")
    height = params.get("reference_height") or params.get("height")
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        return None, None
    if w <= 0 or h <= 0:
        return None, None
    return w, h


def closest_image_size(width: int | None, height: int | None,
                       fallback: str = "1024x1024") -> str:
    if not width or not height:
        return fallback
    ref_ratio = width / height
    try:
        fallback_w, fallback_h = (int(x) for x in str(fallback).split("x", 1))
        target_area = fallback_w * fallback_h
    except (TypeError, ValueError):
        target_area = 1024 * 1024

    def score(size: str) -> float:
        w, h = (int(x) for x in size.split("x", 1))
        ratio = w / h
        return abs(ratio - ref_ratio) + abs((w * h) - target_area) / 50_000_000

    return min(IMAGE_SIZES, key=score)


def video_ratio(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    ref = width / height
    candidates = {
        "1:1": 1.0,
        "3:4": 3 / 4,
        "4:3": 4 / 3,
        "9:16": 9 / 16,
        "16:9": 16 / 9,
    }
    return min(candidates, key=lambda k: abs(candidates[k] - ref))


def video_target_resolution(params: dict, fallback: str = "720p") -> str:
    selected = params.get("target_resolution") or params.get("resolution") or fallback
    return selected if selected in VIDEO_RESOLUTIONS else fallback


def video_target_duration(params: dict, fallback: int = 5) -> int:
    try:
        duration = int(params.get("target_duration") or params.get("duration") or fallback)
    except (TypeError, ValueError):
        duration = fallback
    return max(1, min(duration, settings.effective_max_video_generation_seconds))


def video_render_duration(params: dict, stage: str | None, fallback: int = 5) -> int:
    """Return the duration actually sent for the requested render stage."""
    target_duration = video_target_duration(params, fallback=fallback)
    return min(target_duration, 5) if str(stage or "").lower() == "preview" else target_duration


def video_preview_resolution(target_resolution: str) -> str:
    # Preview remains the cheap probe render; the selected quality is preserved
    # separately and used for the final render.
    return "480p" if target_resolution != "480p" else target_resolution


def video_poster_url(task: GenTask, params: dict) -> str | None:
    return params.get("reference_image_url") or (
        task.source_asset_url if task.source_type == "image" else None
    )


def _local_uploaded_poster_bytes(db, task: GenTask, url: str) -> bytes | None:
    key = storage.key_from_url(url)
    if not key or not key.startswith(("upload/", "upload_preview/", "upload_video_preview/")):
        return None
    row = db.get(UploadedAsset, key)
    if not row or row.user_id != task.user_id:
        raise RuntimeError("视频封面素材无权访问")
    path = storage.local_path(key)
    if not path.exists() or not path.is_file():
        raise RuntimeError("视频封面素材不存在")
    return Path(path).read_bytes()


def localize_video_poster(db, task: GenTask, url: str | None, written_keys: list[str]) -> str | None:
    if not url:
        return None
    try:
        raw = _local_uploaded_poster_bytes(db, task, url)
        if raw is None:
            raw = gateway.download_bytes_limited(
                url,
                max_bytes=int(settings.parse_localize_image_max_bytes),
                allowed_content_types=("image/",),
            )
        preview_png, _, _ = make_image_preview(
            raw,
            max_pixels=int(settings.parse_localize_image_max_pixels),
        )
        key = storage.save_bytes(preview_png, "preview", "png")
        written_keys.append(key)
        return storage.public_url(key)
    except Exception as e:  # noqa: BLE001
        log.warning("video poster localize failed: %s", e)
        return None


def video_media_meta(key: str | None, fallback_duration: int | None) -> dict:
    if not key:
        return {"width": None, "height": None, "duration": fallback_duration}
    meta = video_frames.probe_media(str(storage.local_path(key)))
    return {
        "width": meta.get("width"),
        "height": meta.get("height"),
        "duration": int(round(meta["duration"])) if meta.get("duration") else fallback_duration,
    }
