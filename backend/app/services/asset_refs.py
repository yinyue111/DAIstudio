"""Helpers for safely forwarding user asset references to model gateways."""
from __future__ import annotations

import base64
import io
import mimetypes

from PIL import Image, UnidentifiedImageError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenAsset, UploadedAsset
from . import gateway, storage
from .ssrf import local_storage_key_from_user_asset_url


class AssetRefError(Exception):
    pass


def gateway_ref_for_user_asset(db: Session, user_id: int, url: str | None) -> str | None:
    """Return a gateway-readable ref for a user-controlled asset URL.

    Never forward user-controlled external URLs directly to a model gateway.
    Download them through our SSRF-pinned client, validate they are real images,
    and send a data URI. Authenticated upload URLs are owner-checked and also
    converted to data URIs because the gateway cannot fetch them.
    """
    if not url:
        return None
    key = local_storage_key_from_user_asset_url(url)
    if not key and settings.effective_mock_mode:
        return url
    if key:
        row_mime = None
        row = db.get(UploadedAsset, key)
        if row:
            if row.user_id != user_id:
                raise AssetRefError("上传素材不存在")
            row_mime = row.mime
            if key.startswith("upload/"):
                path = _gateway_image_path(key)
            else:
                path = storage.local_path(key)
        elif key.startswith(("upload/", "upload_preview/")):
            raise AssetRefError("上传素材不存在")
        else:
            path = generated_asset_reference_path(db, user_id, key)
        if not path.exists() or not path.is_file():
            raise AssetRefError("素材文件不存在")
        raw = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or row_mime
        return _image_data_uri(raw, fallback_mime=mime)

    try:
        raw = gateway.download_bytes_limited(
            url,
            max_bytes=int(settings.parse_localize_image_max_bytes),
            allowed_content_types=("image/",),
        )
    except gateway.GatewayError as e:
        raise AssetRefError(f"素材下载失败:{e}") from e
    return _image_data_uri(raw)


def _image_data_uri(raw: bytes, fallback_mime: str | None = None) -> str:
    try:
        img = Image.open(io.BytesIO(raw))
        width, height = img.size
        fmt = (img.format or "").upper()
        if width <= 0 or height <= 0:
            raise AssetRefError("素材图片尺寸非法")
        if width * height > int(settings.parse_localize_image_max_pixels):
            raise AssetRefError("素材图片像素过大")
        img.verify()
    except AssetRefError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise AssetRefError("素材不是有效图片") from e

    mime = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "GIF": "image/gif",
        "WEBP": "image/webp",
    }.get(fmt) or fallback_mime
    if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        raise AssetRefError("素材图片格式不支持")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _gateway_image_path(upload_key: str):
    """Use the compressed authenticated preview for gateway refs when present.

    The original upload may be up to tens of MB. Sending it as a data URI for
    every concurrent batch image request creates huge JSON payloads and gateway
    timeouts. The preview is already owner-gated and visually sufficient as a
    reference/edit input.
    """
    original = storage.local_path(upload_key)
    preview = storage.local_path(upload_key.replace("upload/", "upload_preview/", 1))
    return preview if preview.exists() else original


def generated_asset_reference_path(db: Session, user_id: int, key: str):
    """Return a safe local image path for a generated asset reference.

    Generated HD/final files are protected by owner + unlock checks on the
    download endpoint. Do the same when a local media URL is reused as a model
    reference, otherwise a leaked HD URL could bypass the API boundary.
    """
    asset = db.query(GenAsset).filter(
        or_(
            GenAsset.preview_url == storage.public_url(key),
            GenAsset.hd_url == storage.public_url(key),
        )
    ).first()
    is_hd = key.startswith(("hd/", "video_hd/"))
    if not asset:
        if is_hd:
            raise AssetRefError("生成素材不存在")
        return storage.local_path(key)
    if asset.user_id != user_id:
        raise AssetRefError("生成素材不存在")
    if is_hd and not asset.unlocked:
        raise AssetRefError("请先解锁该素材后再作为参考")
    if asset.preview_url:
        preview_key = storage.key_from_url(asset.preview_url)
        if preview_key and preview_key.startswith(("preview/", "video_preview/")):
            preview_path = storage.local_path(preview_key)
            if preview_path.exists() and preview_path.is_file():
                return preview_path
    return storage.local_path(key)
