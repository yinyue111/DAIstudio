"""User media uploads used as generation references."""
from __future__ import annotations

import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import UploadedAsset, User
from ..schemas import Asset
from ..services import audit, storage, video_frames
from ..services.rate_limit import incr_window
from ..services.request_limits import enforce_content_length
from ..services.upload_quota import ensure_user_media_quota, preflight_user_media_quota
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
_SUPPORTED_VIDEO_CONTENT_TYPES = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}
_SUPPORTED_VIDEO_SUFFIXES = {
    ".mp4": "mp4",
    ".mov": "mov",
    ".webm": "webm",
}
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _content_length(request: Request) -> int | None:
    header = request.headers.get("content-length")
    if not header:
        return None
    try:
        return int(header)
    except ValueError:
        raise HTTPException(400, "Content-Length 非法") from None


def _normalize_image_upload(data: bytes) -> tuple[bytes, int, int]:
    """Validate image bytes and return a metadata-stripped PNG.

    PIL decoding is the format/magic-number authority here; we do not trust the
    supplied content-type or filename extension. Re-encoding strips EXIF/GPS and
    other ancillary metadata before the image is stored or sent to models.
    """
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
    try:
        img.load()
        if fmt == "gif" and getattr(img, "is_animated", False):
            img.seek(0)
        normalized = img.convert("RGB")
        buf = io.BytesIO()
        normalized.save(buf, format="PNG")
    except (OSError, ValueError):
        raise HTTPException(400, "图片内容无法解析,请更换文件")
    return buf.getvalue(), width, height


def _cleanup_storage_keys(*keys: str | None) -> None:
    for key in keys:
        if not key:
            continue
        try:
            storage.local_path(key).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _rate_limit_upload(user_id: int) -> None:
    key = f"upload:media:{user_id}"
    n = incr_window(key, _UPLOAD_RATE_WINDOW)
    if n > _UPLOAD_RATE_LIMIT:
        raise HTTPException(429, "上传过于频繁,请稍后再试")


def _video_ext(file: UploadFile) -> str:
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type in _SUPPORTED_VIDEO_CONTENT_TYPES:
        return _SUPPORTED_VIDEO_CONTENT_TYPES[content_type]
    suffix = Path(file.filename or "").suffix.lower()
    if suffix in _SUPPORTED_VIDEO_SUFFIXES:
        return _SUPPORTED_VIDEO_SUFFIXES[suffix]
    raise HTTPException(400, "仅支持 MP4/MOV/WebM 视频")


def _inspect_video(path: str) -> tuple[int | None, int | None, float | None]:
    if not video_frames.FFPROBE:
        raise HTTPException(400, "视频校验组件不可用,请联系管理员安装 ffprobe")
    meta = video_frames.probe_media(path)
    width = meta.get("width")
    height = meta.get("height")
    duration = meta.get("duration")
    if not width or not height:
        raise HTTPException(400, "上传文件不是有效视频")
    if duration and duration > int(settings.max_video_seconds):
        raise HTTPException(400, f"视频时长不能超过 {int(settings.max_video_seconds)} 秒")
    return (
        int(width) if width else None,
        int(height) if height else None,
        float(duration) if duration else None,
    )


def _inspect_video_and_poster(path: str) -> tuple[int | None, int | None, float | None, bytes | None]:
    if not video_frames.acquire_video_slot():
        raise HTTPException(429, "视频校验繁忙,请稍后再试")
    try:
        width, height, duration = _inspect_video(path)
        poster = video_frames.extract_poster(path)
        return width, height, duration, poster
    finally:
        video_frames.release_video_slot()


async def _save_upload_stream(file: UploadFile, subdir: str, ext: str, *, limit: int) -> tuple[str, int]:
    key, path = storage.reserve_key(subdir, ext)
    total = 0
    try:
        with open(path, "wb") as f:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, f"视频不能超过 {limit // 1024 // 1024}MB")
                f.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if total <= 0:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "请选择要上传的视频")
    return key, total


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

    normalized_png, width, height = _normalize_image_upload(data)
    try:
        preview_png, _, _ = make_image_preview(normalized_png, max_pixels=settings.max_upload_image_pixels)
        model_ref_jpeg, _, _ = make_model_reference(
            normalized_png,
            max_side=1024,
            max_pixels=settings.max_upload_image_pixels,
            quality=92,
            subsampling=0,
        )
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "图片内容无法解析,请更换文件")

    upload_key = None
    preview_key = None
    model_ref_key = None
    try:
        ensure_user_media_quota(db, user.id, len(normalized_png) + len(preview_png) + len(model_ref_jpeg))
        stem = uuid.uuid4().hex
        upload_key = storage.save_bytes_named(normalized_png, "upload", f"{stem}.png")
        preview_key = storage.save_bytes_named(preview_png, "upload_preview", f"{stem}.png")
        model_ref_key = storage.save_bytes_named(model_ref_jpeg, "upload_model_ref", f"{stem}.jpg")
        original_filename = file.filename or upload_key.rsplit("/", 1)[-1]
        db.add(
            UploadedAsset(
                key=upload_key,
                user_id=user.id,
                mime="image/png",
                width=width,
                height=height,
                bytes=len(normalized_png),
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
                mime="image/jpeg",
                width=width,
                height=height,
                bytes=len(model_ref_jpeg),
                original_filename=f"model-ref:{original_filename}.jpg",
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
            "bytes": len(normalized_png),
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


@router.post("/video", response_model=Asset)
async def upload_video(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _rate_limit_upload(user.id)
    ext = _video_ext(file)
    limit = int(settings.max_upload_video_bytes)
    enforce_content_length(request, limit + 1024 * 1024, f"视频不能超过 {limit // 1024 // 1024}MB")
    declared_size = _content_length(request)
    if declared_size is not None:
        estimated_file_bytes = max(0, min(declared_size, limit + 1024 * 1024) - 1024 * 1024)
        preflight_user_media_quota(db, user.id, estimated_file_bytes)

    upload_key = None
    preview_key = None
    bytes_written = 0
    try:
        upload_key, bytes_written = await _save_upload_stream(file, "upload_video", ext, limit=limit)
        stem = Path(upload_key).stem
        upload_path = storage.local_path(upload_key)
        width, height, duration, poster = _inspect_video_and_poster(str(upload_path))
        ensure_user_media_quota(db, user.id, bytes_written + (len(poster) if poster else 0))
        if poster:
            preview_key = storage.save_bytes_named(poster, "upload_video_preview", f"{stem}.jpg")
        original_filename = file.filename or upload_key.rsplit("/", 1)[-1]
        db.add(
            UploadedAsset(
                key=upload_key,
                user_id=user.id,
                mime=(file.content_type or f"video/{ext}").split(";", 1)[0],
                width=width,
                height=height,
                bytes=bytes_written,
                original_filename=original_filename,
            )
        )
        if preview_key:
            db.add(
                UploadedAsset(
                    key=preview_key,
                    user_id=user.id,
                    mime="image/jpeg",
                    width=width,
                    height=height,
                    bytes=len(poster),
                    original_filename=f"poster:{original_filename}",
                )
            )
        db.commit()
    except HTTPException:
        db.rollback()
        _cleanup_storage_keys(upload_key, preview_key)
        raise
    except Exception:  # noqa: BLE001
        db.rollback()
        _cleanup_storage_keys(upload_key, preview_key)
        raise
    audit.log(
        db,
        user_id=user.id,
        action="upload_video",
        biz_type="upload",
        ip=get_client_ip(request),
        detail={
            "filename": file.filename,
            "content_type": file.content_type,
            "bytes": bytes_written,
            "width": width,
            "height": height,
            "duration": duration,
        },
    )
    return Asset(
        type="video",
        url=storage.upload_api_url(upload_key),
        thumb=storage.upload_api_url(preview_key) if preview_key else None,
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
    if kind not in {"upload", "upload_preview", "upload_video", "upload_video_preview"}:
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
    return FileResponse(str(path), filename=path.name, media_type=row.mime or None)
