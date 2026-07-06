"""Media sizing, reference, and poster helpers for generation tasks."""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Literal

from PIL import Image, ImageDraw, ImageFilter

from ..config import settings
from ..models import GenTask, UploadedAsset
from . import asset_refs, gateway, storage, video_frames
from .image_options import IMAGE_SIZES
from .watermark import make_image_preview

log = logging.getLogger("generation")

VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
EDIT_MASK_SEND_CONFIDENCE = 0.60
EDIT_MASK_ANALYSIS_MAX_SIDE = 512
EditMaskMode = Literal["alpha_subject", "auto_subject", "center_box", "none"]


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


def final_prompt(task: GenTask) -> str:
    p = task.prompt or {}
    if p.get("final_text"):
        return p["final_text"]
    if p.get("instruction"):
        return p["instruction"]
    # reverse-off mode: instruction + reference hint
    return "generate a new image in the same style as the reference, high quality"


def gateway_reference_image(
    db,
    task: GenTask,
    url: str | None,
    *,
    min_side: int = 1,
    max_side: int = 384,
    prefer_original_upload: bool = False,
    quality: int = 82,
    subsampling: int = 2,
) -> str | None:
    if not url:
        return None
    if url.startswith("data:image/"):
        return url
    try:
        return asset_refs.gateway_ref_for_user_asset(
            db,
            task.user_id,
            url,
            min_side=min_side,
            max_side=max_side,
            prefer_original_upload=prefer_original_upload,
            quality=quality,
            subsampling=subsampling,
        )
    except asset_refs.AssetRefError as e:
        raise RuntimeError(str(e)) from e


def image_data_uri(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _asset_image_bytes_for_mask(db, task: GenTask, url: str | None) -> bytes | None:
    if not url:
        return None
    key = storage.key_from_url(url)
    if not key:
        return None
    row = db.get(UploadedAsset, key)
    if row:
        if row.user_id != task.user_id:
            raise RuntimeError("上传素材不存在")
        path = storage.local_path(key)
    elif key.startswith(("upload/", "upload_preview/", "upload_video/", "upload_video_preview/")):
        raise RuntimeError("上传素材不存在")
    else:
        path = asset_refs.generated_asset_reference_path(db, task.user_id, key)
    if not path.exists() or not path.is_file():
        raise RuntimeError("素材文件不存在")
    return Path(path).read_bytes()


def _mask_data_uri(mask: Image.Image) -> str:
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return image_data_uri(buf.getvalue(), "image/png")


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
    candidate = candidate.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    component, bbox, area = _largest_component_mask(candidate)
    if component is None or bbox is None:
        return EditMaskResult(None, "none", 0.0, None, source_size[0], source_size[1], "no_foreground")
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
        return EditMaskResult(
            None,
            "none",
            0.0,
            _scale_bbox(bbox, from_size=(w, h), to_size=source_size),
            source_size[0],
            source_size[1],
            "foreground_too_large",
        )
    dilation = max(3, int(round(min(w, h) * 0.025)))
    if dilation % 2 == 0:
        dilation += 1
    subject_alpha = component.filter(ImageFilter.MaxFilter(dilation))
    bbox = _exclusive_bbox_to_inclusive(subject_alpha.getbbox())
    if not bbox:
        return EditMaskResult(None, "none", 0.0, None, source_size[0], source_size[1], "empty_subject")
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
    task: GenTask,
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
    raw = _asset_image_bytes_for_mask(db, task, url)
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        scale = min(1.0, max_side / max(img.size))
        if scale < 1.0:
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.Resampling.LANCZOS,
            )
        alpha = img.getchannel("A")
        if alpha.getextrema()[0] < 255:
            return _alpha_subject_mask(img, alpha)
        if str(edit_mask_mode or "").lower().strip() == "center_box":
            return _center_box_mask(img, reason="explicit_center_box")
        result = _auto_subject_mask(img)
        if result.data_uri:
            return result
        return _center_box_mask(img, reason=result.reason or "auto_subject_unavailable")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"编辑蒙版生成失败:{e}") from e


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
