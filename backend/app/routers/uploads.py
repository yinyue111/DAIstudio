"""User media uploads used as generation references."""
from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import UploadedAsset, User
from ..redis_client import redis_client
from ..schemas import Asset
from ..services import audit, storage
from ..services.request_limits import enforce_content_length
from ..services.watermark import make_image_preview, make_model_reference

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
_UPLOAD_RATE_LIMIT = 30
_UPLOAD_RATE_WINDOW = 3600

_SUPPORTED_FORMATS = {
    "jpeg": "jpg",
    "jpg": "jpg",
    "png": "png",
    "webp": "webp",
    "gif": "gif",
}


def _inspect_image(data: bytes) -> tuple[str, int, int]:
    try:
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").lower()
        width, height = img.size
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(400, "上传文件不是有效图片")
    ext = _SUPPORTED_FORMATS.get(fmt)
    if not ext:
        raise HTTPException(400, "仅支持 JPEG/PNG/GIF/WebP 图片")
    if width <= 0 or height <= 0:
        raise HTTPException(400, "图片尺寸非法")
    if width * height > settings.max_upload_image_pixels:
        raise HTTPException(400, "图片像素过大,请压缩后再上传")
    return ext, width, height


def _cleanup_storage_keys(*keys: str | None) -> None:
    for key in keys:
        if not key:
            continue
        try:
            storage.local_path(key).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _rate_limit_upload(user_id: int) -> None:
    key = f"upload:image:{user_id}"
    n = redis_client.incr(key)
    if n == 1:
        redis_client.expire(key, _UPLOAD_RATE_WINDOW)
    if n > _UPLOAD_RATE_LIMIT:
        raise HTTPException(429, "上传过于频繁,请稍后再试")


@router.post("/image", response_model=Asset)
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _rate_limit_upload(user.id)
    limit = int(settings.max_upload_image_bytes)
    enforce_content_length(request, limit + 1024 * 1024, f"图片不能超过 {limit // 1024 // 1024}MB")
    data = await file.read(limit + 1)
    if not data:
        raise HTTPException(400, "请选择要上传的图片")
    if len(data) > limit:
        raise HTTPException(413, f"图片不能超过 {limit // 1024 // 1024}MB")

    ext, width, height = _inspect_image(data)
    try:
        preview_png, _, _ = make_image_preview(data, max_pixels=settings.max_upload_image_pixels)
        model_ref_png, _, _ = make_model_reference(data, max_pixels=settings.max_upload_image_pixels)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "图片内容无法解析,请更换文件")

    upload_key = None
    preview_key = None
    model_ref_key = None
    try:
        stem = uuid.uuid4().hex
        upload_key = storage.save_bytes_named(data, "upload", f"{stem}.{ext}")
        preview_key = storage.save_bytes_named(preview_png, "upload_preview", f"{stem}.png")
        model_ref_key = storage.save_bytes_named(model_ref_png, "upload_model_ref", f"{stem}.png")
        original_filename = file.filename or upload_key.rsplit("/", 1)[-1]
        db.add(
            UploadedAsset(
                key=upload_key,
                user_id=user.id,
                mime=f"image/{'jpeg' if ext == 'jpg' else ext}",
                width=width,
                height=height,
                bytes=len(data),
                original_filename=original_filename,
            )
        )
        db.add(
            UploadedAsset(
                key=preview_key,
                user_id=user.id,
                mime="image/png",
                width=width,
                height=height,
                bytes=len(preview_png),
                original_filename=f"preview:{original_filename}",
            )
        )
        db.add(
            UploadedAsset(
                key=model_ref_key,
                user_id=user.id,
                mime="image/png",
                width=width,
                height=height,
                bytes=len(model_ref_png),
                original_filename=f"model-ref:{original_filename}",
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        _cleanup_storage_keys(upload_key, preview_key, model_ref_key)
        raise
    audit.log(
        db,
        user_id=user.id,
        action="upload_image",
        biz_type="upload",
        ip=get_client_ip(request),
        detail={
            "filename": file.filename,
            "content_type": file.content_type,
            "bytes": len(data),
            "width": width,
            "height": height,
        },
    )
    return Asset(
        type="image",
        url=storage.upload_api_url(upload_key),
        thumb=storage.upload_api_url(preview_key),
        width=width,
        height=height,
    )


@router.get("/{kind}/{filename}")
def get_uploaded_image(
    kind: str,
    filename: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if kind not in {"upload", "upload_preview"}:
        raise HTTPException(404, "文件不存在")
    key = f"{kind}/{filename}"
    path = storage.local_path(key)
    try:
        path.resolve().relative_to((storage.ROOT / kind).resolve())
    except (ValueError, OSError):
        raise HTTPException(404, "文件不存在")
    row = db.get(UploadedAsset, key)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "文件不存在")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(path), filename=path.name)
