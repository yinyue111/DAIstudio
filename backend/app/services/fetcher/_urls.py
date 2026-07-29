"""URL helpers: normalisation, classification and SSRF-safe asset filtering.

Pure URL/host utilities shared by every platform extractor. This module must
stay free of module-level imports of the platform modules (see
``_same_site_or_platform``) so the package has no import cycles.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from ...config import settings
from ..safe_logging import redact_url_for_log
from ..ssrf import SsrfError, assert_safe_url

log = logging.getLogger("fetcher")


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
_VID_EXT = (".mp4", ".webm", ".mov", ".m3u8")
_AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".flac")

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>，。；、]+", re.I)


def _abs(base: str, src: str | None) -> str | None:
    if not src:
        return None
    src = src.strip()
    if not src or src.startswith("data:"):
        return None
    return urljoin(base, src)


def _classify(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if path.endswith(_VID_EXT):
        return "video"
    if path.endswith(_IMG_EXT):
        return "image"
    return None  # unknown extension; caller decides


def _canonical_url(url: str) -> str:
    return urlparse(url)._replace(query="", fragment="").geturl()


def _normalise_asset_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        return "https:" + url
    # XHS image hosts serve the same content over https; prefer it so browser
    # pages do not run into mixed-content or stricter referrer policies.
    if url.startswith("http://sns-"):
        return "https://" + url[len("http://"):]
    return url


def _safe_asset_url(url: str | None) -> str | None:
    url = _normalise_asset_url(url)
    if not url:
        return None
    try:
        return assert_safe_url(url)
    except SsrfError:
        log.warning("dropped unsafe fetched asset url: %s", redact_url_for_log(url))
        return None


def _same_site_or_platform(page_url: str, request_url: str) -> bool:
    # Imported inside the function: the platform modules import this module,
    # so a module-level import here would be circular.
    from .douyin import _is_douyin_host
    from .jd import _is_jd_host
    from .taobao import _is_taobao_host
    from .x_twitter import _is_x_host, _is_x_media_host
    from .xiaohongshu import _is_xiaohongshu_host

    page_host = (urlparse(page_url).hostname or "").rstrip(".").lower()
    req_host = (urlparse(request_url).hostname or "").rstrip(".").lower()
    if not page_host or not req_host:
        return False
    if req_host == page_host or req_host.endswith("." + page_host):
        return True
    if _is_xiaohongshu_host(page_host):
        return _is_xiaohongshu_host(req_host) or req_host.endswith(".xhscdn.com")
    if _is_douyin_host(page_host):
        return _is_douyin_host(req_host) or req_host.endswith(".douyinpic.com") or req_host.endswith(".snssdk.com")
    if _is_x_host(page_host):
        return _is_x_host(req_host) or _is_x_media_host(req_host)
    if _is_jd_host(page_host):
        return _is_jd_host(req_host) or req_host.endswith(".360buyimg.com")
    if _is_taobao_host(page_host):
        return _is_taobao_host(req_host) or req_host.endswith(".alicdn.com") or req_host.endswith(".taobaocdn.com")
    return False


def _filter_safe_assets(assets: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    cap = max(1, int(settings.parse_max_assets or 1))
    for asset in assets:
        url = _safe_asset_url(asset.get("url"))
        if not url or url in seen:
            continue
        thumb = _safe_asset_url(asset.get("thumb")) if asset.get("thumb") else None
        cleaned = {**asset, "url": url, "thumb": thumb}
        out.append(cleaned)
        seen.add(url)
        if len(out) >= cap:
            break
    return out


def extract_first_url(text: str) -> str | None:
    """Return the first http(s) URL from copied share text."""
    for match in _URL_IN_TEXT_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(")）]】.,，。;；")
        try:
            return assert_safe_url(candidate)
        except SsrfError:
            continue
    return None


def _int_or_none(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _html_media_size(tag) -> tuple[int | None, int | None]:
    width = _int_or_none(tag.get("width") or tag.get("data-width"))
    height = _int_or_none(tag.get("height") or tag.get("data-height"))
    return width, height


def _first_safe_url(urls: list[str]) -> str | None:
    for url in urls:
        safe = _safe_asset_url(url)
        if safe:
            return safe
    return None


def _looks_like_audio_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(_AUDIO_EXT) or "/ies-music/" in path or "music" in parsed.netloc.lower()
