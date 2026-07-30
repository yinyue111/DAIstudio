"""Gateway result download with SSRF guard and streaming."""
from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Collection
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings
from . import storage
from .gateway_transport import GatewayError
from .safe_logging import redact_url_for_log
from .ssrf import MAX_REDIRECTS, SsrfError

log = logging.getLogger("gateway")
_DOWNLOAD_HEADERS = {"Accept-Encoding": "identity"}
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # generous cap for video results


def _trusted_download_host(url: str, trusted_hosts: Collection[str] | None) -> bool:
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    allowed = {str(item).rstrip(".").lower() for item in trusted_hosts or ()}
    return bool(host and host in allowed)


def _assert_download_url(
    url: str,
    trusted_hosts: Collection[str] | None,
    assert_safe_url,
) -> None:
    if _trusted_download_host(url, trusted_hosts):
        return
    assert_safe_url(url)


def _reject_compressed_download(response: httpx.Response) -> None:
    encoding = (response.headers.get("content-encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        raise GatewayError("下载结果不支持压缩编码")


def _content_length_exceeds_limit(content_length: str | None, max_bytes: int) -> bool:
    if not content_length:
        return False
    try:
        return int(content_length) > max_bytes
    except (TypeError, ValueError):
        raise GatewayError("下载结果 Content-Length 非法") from None


def _max_b64_len(max_bytes: int) -> int:
    return ((max_bytes + 2) // 3) * 4 + 8


def _remaining_download_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GatewayError("下载结果超时")
    return remaining


def _download_httpx_timeout(remaining: float) -> httpx.Timeout:
    """Bound both the whole request and a stalled read.

    Provider result URLs can return headers quickly and then trickle or stall.
    A large total timeout is useful for big videos, but a single blocked read
    should not occupy a worker for that whole window.
    """
    remaining = max(0.1, float(remaining))
    read_timeout = max(5.0, min(120.0, remaining))
    connect_timeout = max(2.0, min(30.0, remaining))
    return httpx.Timeout(
        remaining,
        connect=connect_timeout,
        read=read_timeout,
        write=max(2.0, min(30.0, remaining)),
        pool=max(2.0, min(30.0, remaining)),
    )


def _check_low_speed_download(
    *,
    started_at: float,
    total: int,
    low_speed_timeout_seconds: int | None,
    low_speed_min_bytes_per_second: int | None,
) -> None:
    if not low_speed_timeout_seconds or not low_speed_min_bytes_per_second or total <= 0:
        return
    elapsed = time.monotonic() - started_at
    if elapsed < float(low_speed_timeout_seconds):
        return
    if total / max(elapsed, 0.001) < float(low_speed_min_bytes_per_second):
        raise GatewayError("下载结果速度过慢")


def _download(
    url: str,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    allowed_content_types: tuple[str, ...] | None = None,
    timeout_seconds: int | None = None,
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
    trusted_hosts: Collection[str] | None = None,
) -> bytes:
    """Download a gateway-returned result URL with the SSRF guard applied.

    Even though these URLs come from the (trusted) gateway, a poisoned or
    compromised response must not make the worker fetch internal/metadata
    endpoints — so redirects are NOT auto-followed: every hop is re-validated and
    the socket is pinned to the vetted public IP. Failures normalise to
    GatewayError so callers handle them uniformly."""
    from . import gateway as _gw

    try:
        _assert_download_url(url, trusted_hosts, _gw.assert_safe_url)
        timeout = int(timeout_seconds or settings.image_download_timeout_seconds)
        deadline = time.monotonic() + timeout
        with httpx.Client(follow_redirects=False, timeout=_download_httpx_timeout(timeout)) as c:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = _remaining_download_timeout(deadline)
                with _gw._guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=_download_httpx_timeout(remaining),
                    headers=_DOWNLOAD_HEADERS,
                    trusted_hosts=trusted_hosts,
                ) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        _assert_download_url(url, trusted_hosts, _gw.assert_safe_url)
                        continue
                    if r.status_code >= 400:
                        log.warning(
                            "gateway result download failed %s: %s",
                            r.status_code,
                            redact_url_for_log(url),
                        )
                        raise GatewayError(f"下载结果失败 {r.status_code}")
                    _reject_compressed_download(r)
                    content_type = (r.headers.get("content-type") or "").lower()
                    if allowed_content_types and not any(
                        content_type.startswith(prefix.lower()) for prefix in allowed_content_types
                    ):
                        raise GatewayError("下载结果类型不支持")
                    if _content_length_exceeds_limit(r.headers.get("content-length"), max_bytes):
                        raise GatewayError("下载结果超出大小上限")
                    buf = bytearray()
                    body_started_at = time.monotonic()
                    for chunk in r.iter_raw():
                        if time.monotonic() > deadline:
                            raise GatewayError("下载结果超时")
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise GatewayError("下载结果超出大小上限")
                        _check_low_speed_download(
                            started_at=body_started_at,
                            total=len(buf),
                            low_speed_timeout_seconds=low_speed_timeout_seconds,
                            low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
                        )
                    return bytes(buf)
        raise GatewayError("下载结果重定向次数过多")
    except SsrfError as e:
        raise GatewayError(f"结果地址被安全策略拦截: {e}") from e


def download_bytes(url: str) -> bytes:
    return _download(url)


def download_bytes_limited(
    url: str,
    *,
    max_bytes: int,
    allowed_content_types: tuple[str, ...] | None = None,
    timeout_seconds: int | None = None,
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
    trusted_hosts: Collection[str] | None = None,
) -> bytes:
    return _download(
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        timeout_seconds=timeout_seconds,
        low_speed_timeout_seconds=low_speed_timeout_seconds,
        low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
        trusted_hosts=trusted_hosts,
    )


def download_to_path(
    url: str,
    path,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    timeout_seconds: int | None = None,
    allowed_content_types: tuple[str, ...] | None = None,
    progress_callback=None,
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
    request_headers: dict[str, str] | None = None,
    trusted_hosts: Collection[str] | None = None,
) -> int:
    """Download a gateway result directly to disk with SSRF/redirect checks."""
    from . import gateway as _gw

    try:
        _assert_download_url(url, trusted_hosts, _gw.assert_safe_url)
        credential_origin = _url_origin(url) if request_headers else None
        timeout = int(timeout_seconds or settings.image_download_timeout_seconds)
        deadline = time.monotonic() + timeout
        with httpx.Client(follow_redirects=False, timeout=_download_httpx_timeout(timeout)) as c:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = _remaining_download_timeout(deadline)
                headers = dict(_DOWNLOAD_HEADERS)
                if credential_origin is not None and _url_origin(url) == credential_origin:
                    headers.update(request_headers or {})
                with _gw._guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=_download_httpx_timeout(remaining),
                    headers=headers,
                    trusted_hosts=trusted_hosts,
                ) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        _assert_download_url(url, trusted_hosts, _gw.assert_safe_url)
                        continue
                    if r.status_code >= 400:
                        log.warning(
                            "gateway result download failed %s: %s",
                            r.status_code,
                            redact_url_for_log(url),
                        )
                        raise GatewayError(f"下载结果失败 {r.status_code}")
                    _reject_compressed_download(r)
                    content_type = (r.headers.get("content-type") or "").lower()
                    if allowed_content_types and not any(
                        content_type.startswith(prefix.lower()) for prefix in allowed_content_types
                    ):
                        raise GatewayError("下载结果类型不支持")
                    if _content_length_exceeds_limit(r.headers.get("content-length"), max_bytes):
                        raise GatewayError("下载结果超出大小上限")
                    total = 0
                    body_started_at = time.monotonic()
                    with open(path, "wb") as f:
                        for chunk in r.iter_raw():
                            if time.monotonic() > deadline:
                                raise GatewayError("下载结果超时")
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise GatewayError("下载结果超出大小上限")
                            f.write(chunk)
                            _check_low_speed_download(
                                started_at=body_started_at,
                                total=total,
                                low_speed_timeout_seconds=low_speed_timeout_seconds,
                                low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
                            )
                            if progress_callback:
                                progress_callback()
                    return total
        raise GatewayError("下载结果重定向次数过多")
    except SsrfError as e:
        raise GatewayError(f"结果地址被安全策略拦截: {e}") from e
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise


def download_to_storage(
    url: str,
    subdir: str,
    ext: str,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    timeout_seconds: int | None = None,
    allowed_content_types: tuple[str, ...] | None = None,
    progress_callback=None,
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
    request_headers: dict[str, str] | None = None,
    trusted_hosts: Collection[str] | None = None,
) -> str:
    from . import gateway as _gw

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext.lstrip('.')}")
    path = Path(tmp.name)
    tmp.close()
    try:
        _gw.download_to_path(
            url,
            path,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            allowed_content_types=allowed_content_types,
            progress_callback=progress_callback,
            low_speed_timeout_seconds=low_speed_timeout_seconds,
            low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
            request_headers=request_headers,
            trusted_hosts=trusted_hosts,
        )
        return storage.save_file(path, subdir, ext)
    finally:
        path.unlink(missing_ok=True)


def _url_origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        if not scheme or not host:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, host, port
