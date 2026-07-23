"""Storage for media (previews + HD results).

Local files and S3/MinIO use the same stable object keys. Object-backed media is
materialized into a bounded local cache when PIL, ffmpeg, or an authenticated
download route needs a filesystem path. ``STORAGE_MIRROR_LOCAL`` can retain a
second local copy during migration so switching back to ``local`` is immediate.
"""
from __future__ import annotations

import hashlib
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
OBJECT_CACHE_DIR = ".object-cache"
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
    safe_subdir = _validate_subdir(subdir)
    d = ROOT / safe_subdir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _local_object_path(key: str) -> Path:
    return (ROOT / key).resolve()


def _cache_path(key: str) -> Path:
    return (ROOT / OBJECT_CACHE_DIR / key).resolve()


def _is_missing_object_error(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    error_data = response.get("Error") if isinstance(response.get("Error"), dict) else {}
    metadata = response.get("ResponseMetadata") if isinstance(response.get("ResponseMetadata"), dict) else {}
    code = str(error_data.get("Code") or "")
    status = metadata.get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _publish_local_copy(data: bytes, key: str) -> None:
    target = _local_object_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_publish(target, lambda file_obj: file_obj.write(data))


def _publish_cache_bytes(data: bytes, key: str) -> None:
    target = _cache_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_publish(target, lambda file_obj: file_obj.write(data))


def _copy_path_atomically(path: Path, target: Path) -> None:
    def copy_content(dst: BinaryIO) -> None:
        with open(path, "rb") as src:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)

    _atomic_publish(target, copy_content)


def _cache_file(path: Path, key: str) -> None:
    target = _cache_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy_path_atomically(path, target)


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _validate_subdir(subdir: str) -> str:
    safe_subdir = str(subdir or "").strip()
    if safe_subdir not in LOCAL_MEDIA_SUBDIRS:
        raise ValueError("非法存储子目录")
    return safe_subdir


def _validate_extension(ext: str) -> str:
    safe_ext = str(ext or "").lstrip(".").lower()
    if (
        not safe_ext
        or len(safe_ext) > 16
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789" for char in safe_ext)
    ):
        raise ValueError("非法文件扩展名")
    return safe_ext


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
    subdir = _validate_subdir(subdir)
    ext = _validate_extension(ext)
    name = f"{uuid.uuid4().hex}.{ext}"
    key = f"{subdir}/{name}"
    if _s3_enabled():
        _s3_client().put_object(Bucket=settings.storage.s3_bucket, Key=key, Body=data)
        if settings.storage.mirror_local:
            _publish_local_copy(data, key)
        else:
            _publish_cache_bytes(data, key)
        return key
    _ensure(subdir)
    _atomic_publish(ROOT / key, lambda file_obj: file_obj.write(data))
    return key


def save_bytes_named(data: bytes, subdir: str, filename: str) -> str:
    """Save bytes under a caller-chosen safe filename in ``subdir``."""
    subdir = _validate_subdir(subdir)
    raw_filename = str(filename)
    safe = Path(raw_filename).name
    if (
        not safe
        or safe in {".", ".."}
        or safe != raw_filename
        or "/" in raw_filename
        or "\\" in raw_filename
    ):
        raise ValueError("非法文件名")
    key = f"{subdir}/{safe}"
    if _s3_enabled():
        _s3_client().put_object(Bucket=settings.storage.s3_bucket, Key=key, Body=data)
        if settings.storage.mirror_local:
            _publish_local_copy(data, key)
        else:
            _publish_cache_bytes(data, key)
        return key
    _ensure(subdir)
    _atomic_publish(ROOT / key, lambda file_obj: file_obj.write(data))
    return key


def save_file(path: Path, subdir: str, ext: str) -> str:
    """Persist an existing local file and return the storage key."""
    subdir = _validate_subdir(subdir)
    ext = _validate_extension(ext)
    name = f"{uuid.uuid4().hex}.{ext}"
    key = f"{subdir}/{name}"
    if _s3_enabled():
        _s3_client().upload_file(str(path), settings.storage.s3_bucket, key)
        if settings.storage.mirror_local:
            target = _local_object_path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_path_atomically(path, target)
        else:
            _cache_file(path, key)
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


def upload_existing(path: Path, key: str, *, overwrite: bool = False) -> bool:
    """Upload a local file under its existing stable key.

    Returns ``True`` when bytes were uploaded and ``False`` when an existing
    object with the same size was kept. This is intentionally separate from
    ``save_file`` because migration must not generate replacement keys.
    """
    safe_key = _validate_key(key)
    source = path.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not _s3_enabled():
        target = _local_object_path(safe_key)
        if source == target:
            return False
        if target.is_file() and not overwrite and target.stat().st_size == source.stat().st_size:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_path_atomically(source, target)
        return True

    remote_size = remote_object_size(safe_key)
    if remote_size is not None and not overwrite:
        if remote_size != source.stat().st_size:
            raise StorageUnavailable(f"存储对象已存在且大小不一致: {safe_key}")
        return False
    _s3_client().upload_file(str(source), settings.storage.s3_bucket, safe_key)
    if settings.storage.mirror_local and source != _local_object_path(safe_key):
        target = _local_object_path(safe_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_path_atomically(source, target)
    return True


def reserve_key(subdir: str, ext: str) -> tuple[str, Path]:
    """Reserve a new storage key and return (key, absolute path)."""
    subdir = _validate_subdir(subdir)
    ext = _validate_extension(ext)
    if _s3_enabled():
        raise StorageUnavailable("S3/MinIO 存储不支持 reserve_key/local_path 写入流程")
    _ensure(subdir)
    name = f"{uuid.uuid4().hex}.{ext}"
    key = f"{subdir}/{name}"
    return key, ROOT / key


def save_stream(stream: BinaryIO, subdir: str, ext: str, *, max_bytes: int) -> str:
    """Stream bytes to disk and return the key, deleting partial files on error."""
    ext = _validate_extension(ext)
    if _s3_enabled():
        suffix = f".{ext}"
        fd, temp_name = tempfile.mkstemp(prefix="storage-upload-", suffix=suffix)
        os.close(fd)
        temp_path = Path(temp_name)
        total = 0
        try:
            with open(temp_path, "wb") as file_obj:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("文件超出大小上限")
                    file_obj.write(chunk)
            return save_file(temp_path, subdir, ext)
        finally:
            temp_path.unlink(missing_ok=True)
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
    # Only low-risk previews belong on a public bucket/CDN. HD results,
    # originals, and model references keep an opaque backend URL and are read
    # through owner-gated API routes.
    if (
        _s3_enabled()
        and storage_settings.s3_public_base_url
        and key.startswith(("preview/", "video_preview/"))
    ):
        return f"{storage_settings.s3_public_base_url.rstrip('/')}/{key}"
    return f"{storage_settings.public_base_url.rstrip('/')}/media/{key}"


def upload_api_url(key: str | None) -> str | None:
    if not key:
        return None
    if not key.startswith(("upload/", "upload_preview/", "upload_video/", "upload_video_preview/")):
        return public_url(key)
    return f"{settings.storage.public_base_url.rstrip('/')}/api/uploads/{key}"


def local_path(key: str) -> Path:
    safe_key = _validate_key(key)
    local = _local_object_path(safe_key)
    if not _s3_enabled() or local.exists():
        return local

    cached = _cache_path(safe_key)
    if cached.exists():
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cached.parent / f".download-{uuid.uuid4().hex}.tmp"
    try:
        _s3_client().download_file(settings.storage.s3_bucket, safe_key, str(temp_path))
        os.replace(temp_path, cached)
    except Exception as error:
        temp_path.unlink(missing_ok=True)
        if _is_missing_object_error(error):
            return cached
        raise StorageUnavailable(f"存储对象读取失败: {safe_key}") from error
    return cached


def materialized_path_matches_key(path: Path, key: str) -> bool:
    """Verify that a materialized path is the local or cache path for ``key``."""
    try:
        safe_key = _validate_key(key)
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    return resolved in {_local_object_path(safe_key), _cache_path(safe_key)}


def download_to_local_temp(key: str) -> Path:
    """Return a local file path for a storage object.

    Local storage returns the existing safe path. S3/MinIO returns the shared
    materialized cache path so repeated frame extraction and reference reuse do
    not download the same object for every operation.
    """
    safe_key = _validate_key(key)
    return local_path(safe_key)


def exists(key: str) -> bool:
    safe_key = _validate_key(key)
    local = _local_object_path(safe_key)
    if local.exists():
        return local.is_file()
    if not _s3_enabled():
        return False
    try:
        _s3_client().head_object(Bucket=settings.storage.s3_bucket, Key=safe_key)
        return True
    except Exception as error:
        if _is_missing_object_error(error):
            return False
        raise StorageUnavailable(f"存储对象检查失败: {safe_key}") from error


def object_size(key: str) -> int | None:
    safe_key = _validate_key(key)
    local = _local_object_path(safe_key)
    if local.exists() and local.is_file():
        return int(local.stat().st_size)
    if not _s3_enabled():
        return None
    try:
        response = _s3_client().head_object(Bucket=settings.storage.s3_bucket, Key=safe_key)
        return int(response.get("ContentLength") or 0)
    except Exception as error:
        if _is_missing_object_error(error):
            return None
        raise StorageUnavailable(f"存储对象容量检查失败: {safe_key}") from error


def remote_object_size(key: str) -> int | None:
    """Return the S3 object size without treating a local mirror as remote."""
    safe_key = _validate_key(key)
    if not _s3_enabled():
        return object_size(safe_key)
    try:
        response = _s3_client().head_object(Bucket=settings.storage.s3_bucket, Key=safe_key)
        return int(response.get("ContentLength") or 0)
    except Exception as error:
        if _is_missing_object_error(error):
            return None
        raise StorageUnavailable(f"远端存储对象容量检查失败: {safe_key}") from error


def object_sha256(key: str) -> str | None:
    """Return the SHA-256 of an object without loading it fully into memory."""
    safe_key = _validate_key(key)
    local = _local_object_path(safe_key)
    if local.exists() and local.is_file() and not _s3_enabled():
        with local.open("rb") as stream:
            return _sha256_stream(stream)
    if not _s3_enabled():
        return None
    try:
        response = _s3_client().get_object(Bucket=settings.storage.s3_bucket, Key=safe_key)
        body = response["Body"]
        try:
            return _sha256_stream(body)
        finally:
            body.close()
    except Exception as error:
        if _is_missing_object_error(error):
            return None
        raise StorageUnavailable(f"存储对象校验失败: {safe_key}") from error


def delete(key: str) -> None:
    safe_key = _validate_key(key)
    if not _s3_enabled():
        local_path(safe_key).unlink(missing_ok=True)
        _cache_path(safe_key).unlink(missing_ok=True)
        return
    _s3_client().delete_object(Bucket=settings.storage.s3_bucket, Key=safe_key)
    _local_object_path(safe_key).unlink(missing_ok=True)
    _cache_path(safe_key).unlink(missing_ok=True)


def configure_lifecycle(*, abort_incomplete_days: int = 7) -> None:
    """Install only non-destructive housekeeping rules on the object bucket.

    Asset expiry remains application-controlled because retained/favorited media
    has no uniform object expiry. The bucket rule only aborts abandoned multipart
    uploads and removes expired version delete markers.
    """
    if not _s3_enabled():
        raise StorageUnavailable("生命周期规则仅适用于 S3/MinIO")
    _s3_client().put_bucket_lifecycle_configuration(
        Bucket=settings.storage.s3_bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "ai-studio-storage-housekeeping",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {
                        "DaysAfterInitiation": max(1, int(abort_incomplete_days)),
                    },
                    "Expiration": {"ExpiredObjectDeleteMarker": True},
                }
            ]
        },
    )


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
