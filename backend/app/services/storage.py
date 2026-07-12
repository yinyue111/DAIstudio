"""Storage for media (previews + HD results).

Default is local filesystem. The S3/MinIO adapter code is retained as a future
adapter, but runtime preflight currently rejects ``STORAGE_BACKEND=s3`` because
media downloads, ffmpeg frame extraction, upload reuse, and HD authorization
still depend on local filesystem paths.
"""
from __future__ import annotations

import os
import stat
import tempfile
import uuid
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse

from ..config import settings

ROOT = Path(settings.storage.dir)
LOCAL_MEDIA_SUBDIRS = {
    "preview",
    "video_preview",
    "hd",
    "video_hd",
    "upload",
    "upload_preview",
    "upload_video",
    "upload_video_preview",
    "upload_model_ref",
    "model_ref",
}


class StorageUnavailable(RuntimeError):
    pass


def _backend() -> str:
    return str(settings.storage.backend or "local").strip().lower()


def _s3_enabled() -> bool:
    return _backend() == "s3"


def is_object_storage_enabled() -> bool:
    """Whether media objects are stored through the configured object store."""
    return _s3_enabled()


@lru_cache(maxsize=1)
def _s3_client():
    try:
        import boto3  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise StorageUnavailable("S3/MinIO 存储需要安装 boto3") from e
    storage_settings = settings.storage
    if not storage_settings.s3_bucket:
        raise StorageUnavailable("S3/MinIO 存储未配置 STORAGE_S3_BUCKET")
    return boto3.client(
        "s3",
        endpoint_url=storage_settings.s3_endpoint_url or None,
        region_name=storage_settings.s3_region or None,
        aws_access_key_id=storage_settings.s3_access_key_id or None,
        aws_secret_access_key=storage_settings.s3_secret_access_key or None,
    )


def _ensure(subdir: str) -> Path:
    d = ROOT / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_key(key: str) -> str:
    if not key:
        raise ValueError("非法存储 key")
    parts = key.split("/")
    if len(parts) < 2 or parts[0] not in LOCAL_MEDIA_SUBDIRS:
        raise ValueError("非法存储 key")
    if key.startswith("/") or ".." in parts:
        raise ValueError("非法存储 key")
    if _s3_enabled():
        return key
    try:
        resolved = (ROOT / key).resolve()
        resolved.relative_to(ROOT.resolve())
    except (ValueError, OSError) as e:
        raise ValueError("非法存储 key") from e
    return key


def _atomic_publish(target: Path, write_content: Callable[[BinaryIO], object]) -> None:
    temp_path: Path | None = None
    try:
        for _ in range(tempfile.TMP_MAX):
            candidate = target.parent / f".storage-{uuid.uuid4().hex}.tmp"
            try:
                temp_file = open(candidate, "xb")
            except FileExistsError:
                continue
            temp_path = candidate
            break
        else:
            raise FileExistsError("无法创建临时存储文件")

        with temp_file:
            write_content(temp_file)
            try:
                target_mode = stat.S_IMODE(target.stat().st_mode)
            except FileNotFoundError:
                pass
            else:
                temp_file.flush()
                os.fchmod(temp_file.fileno(), target_mode)
        os.replace(temp_path, target)
        temp_path = None
    except BaseException as publish_error:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception as cleanup_error:
                raise publish_error from cleanup_error
        raise


def save_bytes(data: bytes, subdir: str, ext: str) -> str:
    """Save bytes and return the relative key (subdir/filename)."""
    name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    key = f"{subdir}/{name}"
    if _s3_enabled():
        _s3_client().put_object(Bucket=settings.storage.s3_bucket, Key=key, Body=data)
        return key
    _ensure(subdir)
    _atomic_publish(ROOT / key, lambda file_obj: file_obj.write(data))
    return key


def save_bytes_named(data: bytes, subdir: str, filename: str) -> str:
    """Save bytes under a caller-chosen safe filename in ``subdir``."""
    safe = Path(filename).name
    if not safe or safe in {".", ".."} or "/" in safe:
        raise ValueError("非法文件名")
    key = f"{subdir}/{safe}"
    if _s3_enabled():
        _s3_client().put_object(Bucket=settings.storage.s3_bucket, Key=key, Body=data)
        return key
    _ensure(subdir)
    _atomic_publish(ROOT / key, lambda file_obj: file_obj.write(data))
    return key


def save_file(path: Path, subdir: str, ext: str) -> str:
    """Persist an existing local file and return the storage key."""
    name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    key = f"{subdir}/{name}"
    if _s3_enabled():
        _s3_client().upload_file(str(path), settings.storage.s3_bucket, key)
        return key
    _ensure(subdir)
    target = ROOT / key

    def copy_content(dst: BinaryIO) -> None:
        with open(path, "rb") as src:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)

    _atomic_publish(target, copy_content)
    return key


def reserve_key(subdir: str, ext: str) -> tuple[str, Path]:
    """Reserve a new storage key and return (key, absolute path)."""
    if _s3_enabled():
        raise StorageUnavailable("S3/MinIO 存储不支持 reserve_key/local_path 写入流程")
    _ensure(subdir)
    name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    key = f"{subdir}/{name}"
    return key, ROOT / key


def save_stream(stream: BinaryIO, subdir: str, ext: str, *, max_bytes: int) -> str:
    """Stream bytes to disk and return the key, deleting partial files on error."""
    if _s3_enabled():
        data = bytearray()
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError("文件超出大小上限")
        return save_bytes(bytes(data), subdir, ext)
    key, path = reserve_key(subdir, ext)
    total = 0
    try:
        with open(path, "wb") as f:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("文件超出大小上限")
                f.write(chunk)
        return key
    except Exception:
        path.unlink(missing_ok=True)
        raise


def public_url(key: str | None) -> str | None:
    if not key:
        return None
    storage_settings = settings.storage
    if _s3_enabled() and storage_settings.s3_public_base_url:
        return f"{storage_settings.s3_public_base_url.rstrip('/')}/{key}"
    return f"{storage_settings.public_base_url.rstrip('/')}/media/{key}"


def upload_api_url(key: str | None) -> str | None:
    if not key:
        return None
    storage_settings = settings.storage
    if _s3_enabled() and storage_settings.s3_public_base_url:
        return f"{storage_settings.s3_public_base_url.rstrip('/')}/{key}"
    if not key.startswith(("upload/", "upload_preview/", "upload_video/", "upload_video_preview/")):
        return public_url(key)
    return f"{storage_settings.public_base_url.rstrip('/')}/api/uploads/{key}"


def local_path(key: str) -> Path:
    if _s3_enabled():
        raise StorageUnavailable("S3/MinIO 存储对象没有本地文件路径")
    return (ROOT / _validate_key(key)).resolve()


def download_to_local_temp(key: str) -> Path:
    """Return a local file path for a storage object.

    Local storage returns the existing safe path. S3/MinIO downloads to a
    caller-owned temp file; callers that use this in the future must unlink it
    after processing.
    """
    safe_key = _validate_key(key)
    if not _s3_enabled():
        return local_path(safe_key)
    suffix = Path(safe_key).suffix
    fd, tmp_name = tempfile.mkstemp(prefix="storage-object-", suffix=suffix)
    os.close(fd)
    try:
        _s3_client().download_file(settings.storage.s3_bucket, safe_key, tmp_name)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name)


def presigned_download_url(key: str, expires: int = 300) -> str | None:
    """Return a short-lived download URL when the active backend can create one."""
    safe_key = _validate_key(key)
    if not _s3_enabled():
        return public_url(safe_key)
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.storage.s3_bucket, "Key": safe_key},
        ExpiresIn=max(1, int(expires or 300)),
    )


def key_from_url(url: str) -> str | None:
    """Return the storage key for one of OUR media URLs, else None (external).

    Hardened against traversal: the key is stripped of any query/fragment, must
    come from ``settings.public_base_url``, must live in a known media subdir,
    must not contain a ``..`` segment, and its resolved path must stay inside the
    storage root — so a malformed/hostile ``hd_url`` like ``.../media/../.env``
    can never become an arbitrary local-file read (it's treated as external)."""
    if not url:
        return None
    parsed = urlparse(url)
    storage_settings = settings.storage
    base = urlparse(storage_settings.public_base_url.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        return None
    if (
        parsed.scheme.lower() != base.scheme.lower()
        or parsed.netloc.lower() != base.netloc.lower()
    ):
        if not (_s3_enabled() and storage_settings.s3_public_base_url):
            return None
        s3_base = urlparse(storage_settings.s3_public_base_url.rstrip("/"))
        if (
            parsed.scheme.lower() != s3_base.scheme.lower()
            or parsed.netloc.lower() != s3_base.netloc.lower()
        ):
            return None
        base_path = s3_base.path.rstrip("/")
        if base_path and not parsed.path.startswith(f"{base_path}/"):
            return None
        raw_path = parsed.path[len(base_path):] if base_path else parsed.path
        key = unquote(raw_path.lstrip("/"))
        if not key or key.startswith("/") or ".." in key.split("/"):
            return None
        return key if key.split("/", 1)[0] in LOCAL_MEDIA_SUBDIRS else None
    marker = "/media/"
    upload_marker = "/api/uploads/"
    if parsed.path.startswith(marker):
        key = parsed.path[len(marker):]
    elif parsed.path.startswith(upload_marker):
        key = parsed.path[len(upload_marker):]
    else:
        return None
    key = unquote(key)
    if not key or key.startswith("/") or ".." in key.split("/"):
        return None
    if key.split("/", 1)[0] not in LOCAL_MEDIA_SUBDIRS:
        return None
    try:
        resolved = (ROOT / key).resolve()
        resolved.relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    return key
