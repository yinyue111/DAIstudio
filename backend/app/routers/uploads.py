"""User media uploads used as generation references."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import subprocess
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
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
log = logging.getLogger("uploads")
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
_MAX_ORIGINAL_FILENAME = 180
_FILENAME_SAFE_RE = re.compile(r"[\x00-\x1f\x7f/\\:]+")
_UPLOAD_PROCESSING_SEMAPHORE = threading.BoundedSemaphore(
    int(settings.upload_processing_parallelism)
)


async def _finish_thread_task_after_cancel(
    task: asyncio.Task,
    cancelled: asyncio.CancelledError,
    on_cancel_result=None,
) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:  # noqa: BLE001
            break
    if task.done() and not task.cancelled():
        try:
            result = task.result()
        except Exception:  # noqa: BLE001
            log.exception("upload background operation failed while request cancellation was pending")
        else:
            if on_cancel_result is not None:
                try:
                    on_cancel_result(result)
                except Exception:  # noqa: BLE001
                    log.exception("upload cancellation cleanup failed")
    raise cancelled


async def _run_upload_thread(func, /, *args, on_cancel_result=None, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        await _finish_thread_task_after_cancel(task, cancelled, on_cancel_result)


@asynccontextmanager
async def _upload_processing_slot():
    acquire_task = asyncio.create_task(
        asyncio.to_thread(
            _UPLOAD_PROCESSING_SEMAPHORE.acquire,
            True,
            float(settings.upload_processing_acquire_timeout_seconds),
        )
    )
    try:
        acquired = await asyncio.shield(acquire_task)
    except asyncio.CancelledError as cancelled:
        while not acquire_task.done():
            try:
                await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                continue
        if acquire_task.result():
            _UPLOAD_PROCESSING_SEMAPHORE.release()
        raise cancelled
    if not acquired:
        raise HTTPException(503, "媒体处理繁忙,请稍后重试")
    try:
        yield
    finally:
        _UPLOAD_PROCESSING_SEMAPHORE.release()


def _content_length(request: Request) -> int | None:
    header = request.headers.get("content-length")
    if not header:
        return None
    try:
        return int(header)
    except ValueError:
        raise HTTPException(400, "Content-Length 非法") from None


def _safe_original_filename(filename: str | None, fallback: str) -> str:
    name = Path(str(filename or "").replace("\\", "/")).name.strip()
    if not name:
        name = Path(fallback).name
    name = _FILENAME_SAFE_RE.sub("_", name).strip(" .")
    if not name:
        name = Path(fallback).name
    return name[:_MAX_ORIGINAL_FILENAME]


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
        has_alpha = "A" in img.getbands() or "transparency" in img.info
        normalized = img.convert("RGBA" if has_alpha else "RGB")
        if has_alpha and normalized.getchannel("A").getextrema()[0] >= 255:
            normalized = normalized.convert("RGB")
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


def _video_transcode_timeout(duration: float | None) -> int:
    if not duration:
        return 180
    return max(60, min(900, int(duration * 8) + 60))


def _transcode_sanitized_video(src: Path, *, duration: float | None = None) -> Path:
    """Re-encode uploaded video into a metadata-stripped MP4.

    We do not persist user-supplied video bytes directly. Re-encoding through
    ffmpeg normalizes the container/codecs and drops metadata/data/subtitle
    streams before the file can be sent to model gateways or downloaded later.
    """
    if not video_frames.FFMPEG:
        raise HTTPException(400, "视频净化组件不可用,请联系管理员安装 ffmpeg")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    dst = Path(tmp.name)
    tmp.close()
    cmd = [
        video_frames.FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(src),
        "-fs",
        str(int(settings.max_upload_video_bytes)),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-dn",
        "-sn",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_video_transcode_timeout(duration),
        )
    except subprocess.TimeoutExpired:
        dst.unlink(missing_ok=True)
        raise HTTPException(400, "视频净化超时,请压缩后重新上传") from None
    except (OSError, subprocess.SubprocessError):
        dst.unlink(missing_ok=True)
        raise HTTPException(400, "视频净化失败,请更换标准 MP4/MOV/WebM 文件") from None
    if result.returncode != 0 or not dst.exists() or dst.stat().st_size <= 0:
        dst.unlink(missing_ok=True)
        log_msg = (result.stderr or result.stdout or "").strip()[:300]
        if log_msg:
            log.info("video transcode rejected upload: %s", log_msg)
        raise HTTPException(400, "视频净化失败,请更换标准 MP4/MOV/WebM 文件")
    if dst.stat().st_size > int(settings.max_upload_video_bytes):
        dst.unlink(missing_ok=True)
        raise HTTPException(413, f"视频净化后仍超过 {int(settings.max_upload_video_bytes) // 1024 // 1024}MB")
    return dst


def _sanitize_video_and_poster(path: Path) -> tuple[Path, int | None, int | None, float | None, bytes | None]:
    if not video_frames.acquire_video_slot():
        raise HTTPException(429, "视频校验繁忙,请稍后再试")
    sanitized_path: Path | None = None
    try:
        _width, _height, source_duration = _inspect_video(str(path))
        sanitized_path = _transcode_sanitized_video(path, duration=source_duration)
        width, height, duration = _inspect_video(str(sanitized_path))
        poster = video_frames.extract_poster(str(sanitized_path))
        return sanitized_path, width, height, duration, poster
    except Exception:
        if sanitized_path is not None:
            sanitized_path.unlink(missing_ok=True)
        raise
    finally:
        video_frames.release_video_slot()


async def _save_upload_stream_to_temp(file: UploadFile, ext: str, *, limit: int) -> tuple[Path, int]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext.lstrip('.')}")
    path = Path(tmp.name)
    tmp.close()
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
                await _run_upload_thread(f.write, chunk)
    except (Exception, asyncio.CancelledError):
        path.unlink(missing_ok=True)
        raise
    if total <= 0:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "请选择要上传的视频")
    return path, total


@router.post("/image", response_model=Asset)
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    async with _upload_processing_slot():
        _rate_limit_upload(user.id)
        limit = int(settings.max_upload_image_bytes)
        enforce_content_length(request, limit + 1024 * 1024, f"图片不能超过 {limit // 1024 // 1024}MB")
        data = await file.read(limit + 1)
        if not data:
            raise HTTPException(400, "请选择要上传的图片")
        if len(data) > limit:
            raise HTTPException(413, f"图片不能超过 {limit // 1024 // 1024}MB")

        normalized_png, width, height = await _run_upload_thread(_normalize_image_upload, data)
        try:
            preview_png, _, _ = await _run_upload_thread(
                make_image_preview,
                normalized_png,
                max_pixels=settings.max_upload_image_pixels,
            )
            model_ref_jpeg, _, _ = await _run_upload_thread(
                make_model_reference,
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
            upload_key = await _run_upload_thread(
                storage.save_bytes_named,
                normalized_png,
                "upload",
                f"{stem}.png",
                on_cancel_result=lambda key: _cleanup_storage_keys(key),
            )
            preview_key = await _run_upload_thread(
                storage.save_bytes_named,
                preview_png,
                "upload_preview",
                f"{stem}.png",
                on_cancel_result=lambda key: _cleanup_storage_keys(key),
            )
            model_ref_key = await _run_upload_thread(
                storage.save_bytes_named,
                model_ref_jpeg,
                "upload_model_ref",
                f"{stem}.jpg",
                on_cancel_result=lambda key: _cleanup_storage_keys(key),
            )
            original_filename = _safe_original_filename(file.filename, upload_key.rsplit("/", 1)[-1])
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
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
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
    raw_path = None
    sanitized_path = None
    raw_bytes = 0
    stored_bytes = 0
    width = height = duration = None
    async with _upload_processing_slot():
        try:
            raw_path, raw_bytes = await _save_upload_stream_to_temp(file, ext, limit=limit)
            preflight_user_media_quota(db, user.id, raw_bytes)
            sanitized_path, width, height, duration, poster = await _run_upload_thread(
                _sanitize_video_and_poster,
                raw_path,
                on_cancel_result=lambda result: result[0].unlink(missing_ok=True),
            )
            stored_bytes = sanitized_path.stat().st_size
            ensure_user_media_quota(db, user.id, stored_bytes + (len(poster) if poster else 0))
            upload_key = await _run_upload_thread(
                storage.save_file,
                sanitized_path,
                "upload_video",
                "mp4",
                on_cancel_result=lambda key: _cleanup_storage_keys(key),
            )
            stem = Path(upload_key).stem
            if poster:
                preview_key = await _run_upload_thread(
                    storage.save_bytes_named,
                    poster,
                    "upload_video_preview",
                    f"{stem}.jpg",
                    on_cancel_result=lambda key: _cleanup_storage_keys(key),
                )
            original_filename = _safe_original_filename(file.filename, upload_key.rsplit("/", 1)[-1])
            db.add(
                UploadedAsset(
                    key=upload_key,
                    user_id=user.id,
                    mime="video/mp4",
                    width=width,
                    height=height,
                    bytes=stored_bytes,
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
        except asyncio.CancelledError:
            db.rollback()
            _cleanup_storage_keys(upload_key, preview_key)
            raise
        except Exception:  # noqa: BLE001
            db.rollback()
            _cleanup_storage_keys(upload_key, preview_key)
            raise
        finally:
            if raw_path is not None:
                raw_path.unlink(missing_ok=True)
            if sanitized_path is not None:
                sanitized_path.unlink(missing_ok=True)
    audit.log(
        db,
        user_id=user.id,
        action="upload_video",
        biz_type="upload",
        ip=get_client_ip(request),
        detail={
            "filename": file.filename,
            "content_type": file.content_type,
            "raw_bytes": raw_bytes,
            "bytes": stored_bytes,
            "width": width,
            "height": height,
            "duration": duration,
            "sanitized": True,
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
