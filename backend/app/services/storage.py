"""Local filesystem storage for media (previews + HD results).

Abstraction is deliberately tiny so MinIO/OSS can replace it later: everything
goes through ``save_bytes`` / ``public_url`` / ``local_path``.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse

from ..config import settings

ROOT = Path(settings.storage_dir)
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


def _ensure(subdir: str) -> Path:
    d = ROOT / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_bytes(data: bytes, subdir: str, ext: str) -> str:
    """Save bytes and return the relative key (subdir/filename)."""
    _ensure(subdir)
    name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    key = f"{subdir}/{name}"
    with open(ROOT / key, "wb") as f:
        f.write(data)
    return key


def save_bytes_named(data: bytes, subdir: str, filename: str) -> str:
    """Save bytes under a caller-chosen safe filename in ``subdir``."""
    _ensure(subdir)
    safe = Path(filename).name
    if not safe or safe in {".", ".."} or "/" in safe:
        raise ValueError("非法文件名")
    key = f"{subdir}/{safe}"
    with open(ROOT / key, "wb") as f:
        f.write(data)
    return key


def reserve_key(subdir: str, ext: str) -> tuple[str, Path]:
    """Reserve a new storage key and return (key, absolute path)."""
    _ensure(subdir)
    name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    key = f"{subdir}/{name}"
    return key, ROOT / key


def save_stream(stream: BinaryIO, subdir: str, ext: str, *, max_bytes: int) -> str:
    """Stream bytes to disk and return the key, deleting partial files on error."""
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
    return f"{settings.public_base_url.rstrip('/')}/media/{key}"


def upload_api_url(key: str | None) -> str | None:
    if not key:
        return None
    if not key.startswith(("upload/", "upload_preview/", "upload_video/", "upload_video_preview/")):
        return public_url(key)
    return f"{settings.public_base_url.rstrip('/')}/api/uploads/{key}"


def local_path(key: str) -> Path:
    if not key:
        raise ValueError("非法存储 key")
    parts = key.split("/")
    if len(parts) < 2 or parts[0] not in LOCAL_MEDIA_SUBDIRS:
        raise ValueError("非法存储 key")
    if key.startswith("/") or ".." in parts:
        raise ValueError("非法存储 key")
    try:
        resolved = (ROOT / key).resolve()
        resolved.relative_to(ROOT.resolve())
    except (ValueError, OSError) as e:
        raise ValueError("非法存储 key") from e
    return resolved


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
    base = urlparse(settings.public_base_url.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        return None
    if (
        parsed.scheme.lower() != base.scheme.lower()
        or parsed.netloc.lower() != base.netloc.lower()
    ):
        return None
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
