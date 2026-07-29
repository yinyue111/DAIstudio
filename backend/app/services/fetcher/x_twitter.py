"""X / Twitter extractor: post-media whitelist + render fallback for status cards."""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from app.services.fetcher import _fetch

from ._fetch import (
    _GENERIC_MAX_BODY_BYTES,
    _GENERIC_MAX_READ_SECONDS,
    _timeout_hint,
)
from ._urls import _normalise_asset_url
from .generic import extract_assets

log = logging.getLogger("fetcher")


def _is_x_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com")


def _is_x_media_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == "twimg.com" or host.endswith(".twimg.com")


# X 帖子媒体的真实路径形态:图片位于 /media/,视频与其封面位于
# ext_tw_video / amplify_video / tweet_video(GIF 转视频)系列路径。
# 白名单按前缀放行,继续排除头像(/profile_images/)、横幅(/profile_banners/)
# 与表情(/emoji/)等非帖子内容。
_X_POST_MEDIA_PATH_PREFIXES = (
    "/media/",
    "/ext_tw_video/",
    "/ext_tw_video_thumb/",
    "/amplify_video/",
    "/amplify_video_thumb/",
    "/tweet_video/",
    "/tweet_video_thumb/",
)


def _is_x_post_media_url(url: str | None) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").rstrip(".").lower()
    return _is_x_media_host(host) and parsed.path.startswith(_X_POST_MEDIA_PATH_PREFIXES)


def _normalise_x_media_url(url: str | None) -> str | None:
    url = _normalise_asset_url(url)
    if not url or not _is_x_post_media_url(url):
        return url
    parsed = urlparse(url)
    path = parsed.path
    suffix_format = None
    if re.search(r"\.(?:jpe?g|png|webp):(?:orig|large|small|medium|thumb)$", path, re.I):
        path, size_name = path.rsplit(":", 1)
        ext = path.rsplit(".", 1)[-1].lower()
        suffix_format = "jpg" if ext == "jpeg" else ext
        query = urlencode({"format": suffix_format, "name": "orig" if size_name == "orig" else "large"})
    else:
        qs = parse_qs(parsed.query)
        fmt = (qs.get("format") or [""])[0]
        if not fmt and "." in path.rsplit("/", 1)[-1]:
            ext = path.rsplit(".", 1)[-1].lower()
            if ext in {"jpg", "jpeg", "png", "webp"}:
                fmt = "jpg" if ext == "jpeg" else ext
        name = (qs.get("name") or [""])[0]
        if fmt:
            query = urlencode({"format": fmt, "name": "orig" if name == "orig" else "large"})
        else:
            query = parsed.query
    return urlunparse(parsed._replace(path=path, query=query, fragment=""))


def _x_media_dedupe_key(url: str | None) -> str:
    normalised = _normalise_x_media_url(url) or (url or "")
    parsed = urlparse(normalised)
    return f"{(parsed.hostname or '').lower()}{parsed.path}"


def _dedupe_x_media_assets(assets: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for asset in assets:
        url = _normalise_x_media_url(asset.get("url"))
        if not url:
            continue
        key = _x_media_dedupe_key(url)
        if key in seen:
            continue
        seen.add(key)
        thumb = _normalise_x_media_url(asset.get("thumb")) if asset.get("thumb") else url
        out.append({**asset, "url": url, "thumb": thumb})
    return out


def _run_x(url: str) -> list[dict]:
    def post_media_only(assets: list[dict]) -> list[dict]:
        return _dedupe_x_media_assets([
            asset for asset in assets if _is_x_post_media_url(asset.get("url"))
        ])

    try:
        html = _fetch._render_with_httpx(url, max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                                         max_body_bytes=_GENERIC_MAX_BODY_BYTES)
        assets = post_media_only(extract_assets(html, url))
        if assets:
            return assets
    except httpx.HTTPError as e:
        log.warning("x/twitter html fetch failed, falling back to render: %s", e)
    html = _fetch._render_with_playwright(url)
    if html is None:
        raise ValueError(_timeout_hint("X"))
    return post_media_only(extract_assets(html, url))
