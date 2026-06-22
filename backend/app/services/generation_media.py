"""Media sizing, reference, and poster helpers for generation tasks."""
from __future__ import annotations

import base64
import logging

from ..config import settings
from ..models import GenTask
from . import asset_refs, gateway, storage, video_frames
from .image_options import IMAGE_SIZES
from .watermark import make_image_preview

log = logging.getLogger("generation")

VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")


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
        )
    except asset_refs.AssetRefError as e:
        raise RuntimeError(str(e)) from e


def image_data_uri(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


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
    return max(1, min(duration, settings.max_video_seconds))


def video_preview_resolution(target_resolution: str) -> str:
    # Preview remains the cheap probe render; the selected quality is preserved
    # separately and used for the final render.
    return "480p" if target_resolution != "480p" else target_resolution


def video_poster_url(task: GenTask, params: dict) -> str | None:
    return params.get("reference_image_url") or (
        task.source_asset_url if task.source_type == "image" else None
    )


def localize_video_poster(url: str | None, written_keys: list[str]) -> str | None:
    if not url:
        return None
    try:
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
