"""Generation request validation and pricing helpers shared by routers."""
from __future__ import annotations

import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenTask, UploadedAsset
from . import asset_refs
from .config_store import get_setting
from .generation_pricing import generation_cost, generation_cost_from_snapshot
from .ssrf import local_storage_key_from_user_asset_url

_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_OPENAI_COMPAT_4K_MAX_PIXELS = 3840 * 2160
_VIDEO_RESOLUTIONS = {"480p", "720p", "1080p"}
_VIDEO_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9"}
_COMMON_PARAM_KEYS = {
    "seed",
    "negative_prompt",
    "negative",
    "reference_width",
    "reference_height",
    "width",
    "height",
    "subject_mode",
}
_IMAGE_PARAM_KEYS = _COMMON_PARAM_KEYS | {
    "n",
    "size",
    "edit_mask_mode",
    "mask_image_url",
    "product_pixel_lock",
    "style_reference_image",
    "character_reference_image",
    "variation_of_asset_id",
}
_VIDEO_PARAM_KEYS = _COMMON_PARAM_KEYS | {
    "duration",
    "target_duration",
    "resolution",
    "target_resolution",
    "ratio",
    "reference_image_url",
    "first_frame_image",
    "last_frame_image",
    "style_reference_image",
    "character_reference_image",
    "product_lock_mode",
    "preview_resolution",
    "preview_duration",
}
_SUBJECT_MODES = {"general", "product", "portrait"}
_PRODUCT_LOCK_MODES = {"locked", "free"}
_EDIT_MASK_MODES = {"off", "protect_subject", "center_box"}
_PRODUCT_PIXEL_LOCK_MODES = {"strict", "on", "true", "1", "off", "false", "0"}


def image_max_pixels() -> int:
    max_dim = max(1, int(settings.max_image_dim or 1))
    return min(_OPENAI_COMPAT_4K_MAX_PIXELS, max_dim * max_dim)


def _normalise_reference_dimensions(params: dict) -> None:
    for key in ("reference_width", "reference_height", "width", "height"):
        if params.get(key) is None:
            continue
        try:
            value = int(params[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} 非法")
        if not (1 <= value <= 20000):
            raise HTTPException(400, f"{key} 超出范围")
        params[key] = value


def validate_generation_params(category: str, params: dict) -> dict:
    """Clamp/reject generation params before gateway dispatch or media allocation."""
    params = dict(params or {})
    try:
        raw_size = len(json.dumps(params, ensure_ascii=False))
    except (TypeError, ValueError):
        raise HTTPException(400, "params 必须是可序列化 JSON")
    if raw_size > int(settings.max_generate_params_bytes):
        raise HTTPException(400, "params 过大")
    allowed = _IMAGE_PARAM_KEYS if category == "image" else _VIDEO_PARAM_KEYS
    unknown = sorted(str(k) for k in params if k not in allowed)
    if unknown:
        raise HTTPException(400, f"不支持的生成参数:{','.join(unknown[:5])}")
    _normalise_reference_dimensions(params)
    if params.get("subject_mode") not in (None, ""):
        subject_mode = str(params["subject_mode"]).strip().lower()
        if subject_mode not in _SUBJECT_MODES:
            raise HTTPException(400, "subject_mode 不支持")
        params["subject_mode"] = subject_mode
    if params.get("seed") not in (None, ""):
        try:
            seed = int(params["seed"])
        except (TypeError, ValueError):
            raise HTTPException(400, "seed 非法")
        if not (0 <= seed <= 2**32 - 1):
            raise HTTPException(400, "seed 超出范围")
        params["seed"] = seed
    if category == "image":
        if params.get("variation_of_asset_id") not in (None, ""):
            try:
                variation_of_asset_id = int(params["variation_of_asset_id"])
            except (TypeError, ValueError):
                raise HTTPException(400, "variation_of_asset_id 非法")
            if variation_of_asset_id <= 0:
                raise HTTPException(400, "variation_of_asset_id 非法")
            params["variation_of_asset_id"] = variation_of_asset_id
        if params.get("edit_mask_mode") not in (None, ""):
            edit_mask_mode = str(params["edit_mask_mode"]).strip().lower()
            if edit_mask_mode not in _EDIT_MASK_MODES:
                raise HTTPException(400, "edit_mask_mode 不支持")
            params["edit_mask_mode"] = edit_mask_mode
        if params.get("product_pixel_lock") not in (None, ""):
            product_pixel_lock = str(params["product_pixel_lock"]).strip().lower()
            if product_pixel_lock not in _PRODUCT_PIXEL_LOCK_MODES:
                raise HTTPException(400, "product_pixel_lock 不支持")
            params["product_pixel_lock"] = product_pixel_lock
        if params.get("n") is not None:
            try:
                n = int(params["n"])
            except (TypeError, ValueError):
                raise HTTPException(400, "出图数量 n 非法")
            if not (1 <= n <= settings.max_image_n):
                raise HTTPException(400, f"出图数量需在 1..{settings.max_image_n} 之间")
            params["n"] = n
        size = params.get("size")
        if size is not None:
            m = _SIZE_RE.match(str(size))
            if not m:
                raise HTTPException(400, f"尺寸非法(最大 {settings.max_image_dim}px)")
            width = int(m.group(1))
            height = int(m.group(2))
            ratio = width / height if height else 0
            if not (
                0 < width <= settings.max_image_dim
                and 0 < height <= settings.max_image_dim
                and width % 16 == 0
                and height % 16 == 0
                and width * height <= image_max_pixels()
                and (1 / 3) <= ratio <= 3
            ):
                raise HTTPException(
                    400,
                    f"尺寸非法:最大边 {settings.max_image_dim}px，总像素不超过 {image_max_pixels()}，宽高需为 16 的倍数",
                )
    else:
        max_video_generation_seconds = settings.effective_max_video_generation_seconds
        if params.get("duration") is not None:
            try:
                d = int(params["duration"])
            except (TypeError, ValueError):
                raise HTTPException(400, "时长非法")
            if not (1 <= d <= max_video_generation_seconds):
                raise HTTPException(400, f"时长需在 1..{max_video_generation_seconds} 秒之间")
            params["duration"] = d
        for key in ("resolution", "target_resolution"):
            if params.get(key) is not None and params[key] not in _VIDEO_RESOLUTIONS:
                raise HTTPException(400, f"{key} 仅支持 480p/720p/1080p")
        if params.get("target_duration") is not None:
            try:
                td = int(params["target_duration"])
            except (TypeError, ValueError):
                raise HTTPException(400, "target_duration 非法")
            if not (1 <= td <= max_video_generation_seconds):
                raise HTTPException(400, f"target_duration 需在 1..{max_video_generation_seconds} 秒之间")
            params["target_duration"] = td
        if params.get("ratio") is not None and params["ratio"] not in _VIDEO_RATIOS:
            raise HTTPException(400, "ratio 不支持")
        if params.get("product_lock_mode") not in (None, ""):
            product_lock_mode = str(params["product_lock_mode"]).strip().lower()
            if product_lock_mode not in _PRODUCT_LOCK_MODES:
                raise HTTPException(400, "product_lock_mode 不支持")
            params["product_lock_mode"] = product_lock_mode
    return params


def estimate_generation_cost(
    model,
    category: str,
    stage: str,
    n: int = 1,
    *,
    params: dict | None = None,
    source_type: str | None = None,
) -> int:
    return generation_cost(
        category=category,
        stage=stage,
        params=params or {},
        n=n,
        source_type=source_type,
        model_extra=getattr(model, "extra", None),
    )


def estimate_generation_cost_from_snapshot(
    snapshot: dict,
    category: str,
    stage: str,
    n: int = 1,
    *,
    params: dict | None = None,
    source_type: str | None = None,
) -> int:
    return generation_cost_from_snapshot(
        snapshot,
        category=category,
        stage=stage,
        params=params or {},
        n=n,
        source_type=source_type,
    )


def assert_reference_access(db: Session, user_id: int, *urls: str | None) -> None:
    for url in urls:
        if not url:
            continue
        key = local_storage_key_from_user_asset_url(url)
        if not key:
            continue
        row = db.get(UploadedAsset, key)
        if row:
            if row.user_id != user_id:
                raise HTTPException(404, "上传素材不存在")
            continue
        if key.startswith(("upload/", "upload_preview/", "upload_video/", "upload_video_preview/")):
            raise HTTPException(404, "上传素材不存在")
        try:
            asset_refs.generated_asset_reference_path(db, user_id, key)
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e)) from e


def default_image_n(db: Session) -> int:
    try:
        n = int(get_setting(db, "image_n", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "默认出图数量配置非法,请联系管理员")
    if not (1 <= n <= settings.max_image_n):
        raise HTTPException(400, f"默认出图数量需在 1..{settings.max_image_n} 之间,请联系管理员")
    return n


def _fingerprint_clean(value):
    if isinstance(value, dict):
        return {
            str(k): _fingerprint_clean(v)
            for k, v in value.items()
            if not str(k).startswith("_")
        }
    if isinstance(value, list):
        return [_fingerprint_clean(v) for v in value]
    return value


def request_fingerprint(
    *,
    category: str,
    stage: str,
    source_asset_url: str | None,
    source_type: str | None,
    prompt: dict,
    params: dict,
    parent_task_id: int | None,
) -> str:
    payload = {
        "category": category,
        "stage": stage,
        "source_asset_url": source_asset_url,
        "source_type": source_type,
        "prompt": _fingerprint_clean(prompt or {}),
        "params": _fingerprint_clean(params or {}),
        "parent_task_id": parent_task_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def assert_client_request_replay(task: GenTask, fingerprint: str) -> None:
    existing = ((task.params or {}).get("_client_request_fingerprint") or "").strip()
    if existing and existing != fingerprint:
        raise HTTPException(409, "client_request_id 已用于不同请求,请更换后重试")
