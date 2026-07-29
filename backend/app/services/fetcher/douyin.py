"""抖音 (douyin) extractor: web detail API with a RENDER_DATA render fallback."""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx

from app.services.fetcher import _fetch

from ._fetch import _timeout_hint
from ._json_state import _plain_json
from ._urls import (
    _filter_safe_assets,
    _first_safe_url,
    _int_or_none,
    _looks_like_audio_url,
    _safe_asset_url,
)

log = logging.getLogger("fetcher")


_DOUYIN_RENDER_RE = re.compile(
    r"<script[^>]+id=[\"']RENDER_DATA[\"'][^>]*>(.*?)</script>", re.S
)
_DOUYIN_ID_RE = re.compile(r"(?:video|note|modal_id|aweme_id)[=/](\d{16,25})")


def _is_douyin_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def _douyin_aweme_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "item_id"):
        value = (qs.get(key) or [""])[0]
        if value.isdigit():
            return value
    for part in parsed.path.split("/"):
        if part.isdigit() and 16 <= len(part) <= 25:
            return part
    m = _DOUYIN_ID_RE.search(url)
    return m.group(1) if m else None


def _douyin_url_list(media: dict | None) -> list[str]:
    if not isinstance(media, dict):
        return []
    out: list[str] = []
    for key in ("url_list", "urlList", "download_url_list", "downloadUrlList"):
        urls = media.get(key)
        if isinstance(urls, str):
            out.append(urls)
        elif isinstance(urls, list):
            out.extend(u for u in urls if isinstance(u, str))
    return out


def _douyin_image_url(image: dict) -> str | None:
    # display urls are normally clean; download_url_list often contains
    # watermark variants, so only use it as a last resort.
    return _first_safe_url(_douyin_url_list({
        "url_list": image.get("url_list") or image.get("urlList") or [],
    })) or _first_safe_url(_douyin_url_list(image))


def _douyin_cover_url(video: dict | None) -> str | None:
    if not isinstance(video, dict):
        return None
    for key in ("origin_cover", "cover", "dynamic_cover"):
        url = _first_safe_url(_douyin_url_list(video.get(key)))
        if url:
            return url
    return None


def _douyin_video_url(video: dict | None) -> str | None:
    if not isinstance(video, dict):
        return None
    candidates: list[str] = []
    candidates.extend(_douyin_url_list(video.get("play_addr_no_watermark")))
    candidates.extend(_douyin_url_list(video.get("download_addr")))
    candidates.extend(_douyin_url_list(video.get("play_addr")))
    candidates.extend(_douyin_url_list(video.get("play_addr_h264")))
    candidates.extend(_douyin_url_list(video.get("play_addr_265")))
    for item in video.get("bit_rate") or []:
        if isinstance(item, dict):
            candidates.extend(_douyin_url_list(item.get("play_addr")))
    for url in candidates:
        if _looks_like_audio_url(url):
            continue
        safe = _safe_asset_url(url)
        if safe:
            return safe
    return None


def _extract_douyin_aweme_assets(aweme: dict | None) -> list[dict]:
    if not isinstance(aweme, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    video = aweme.get("video") or {}
    cover = _douyin_cover_url(video)

    images = aweme.get("images") or aweme.get("image_list") or aweme.get("imageList") or []
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            url = _douyin_image_url(image)
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({
                "type": "image",
                "url": url,
                "thumb": url,
                "width": _int_or_none(image.get("width")),
                "height": _int_or_none(image.get("height")),
            })

    video_url = _douyin_video_url(video)
    if video_url and video_url not in seen:
        # 图文作品的 video.play_addr 可能是背景音乐 mp3，_douyin_video_url 已过滤。
        out.insert(0, {
            "type": "video",
            "url": video_url,
            "thumb": cover,
            "width": _int_or_none(video.get("width")),
            "height": _int_or_none(video.get("height")),
        })
    return _filter_safe_assets(out)


def _douyin_aweme_key(obj: dict) -> str:
    return str(obj.get("aweme_id") or obj.get("awemeId") or obj.get("group_id") or "")


def _walk_douyin_awemes(obj):
    if isinstance(obj, dict):
        if _douyin_aweme_key(obj) and (
            obj.get("video") or obj.get("images")
            or obj.get("image_list") or obj.get("imageList")
        ):
            # Yield this post at its outermost match and do NOT descend into it,
            # so nested wrappers (mix_info, related items) can't re-yield the
            # same logical post and inflate / disorder the candidate list.
            yield obj
            return
        for value in obj.values():
            yield from _walk_douyin_awemes(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_douyin_awemes(value)


def _extract_douyin_assets(html: str, page_url: str) -> list[dict]:
    target_id = _douyin_aweme_id_from_url(page_url)
    m = _DOUYIN_RENDER_RE.search(html)
    if not m:
        return []
    raw = unquote(m.group(1).strip())
    state = _plain_json(raw, "douyin render")
    if not state:
        return []
    candidates = list(_walk_douyin_awemes(state))
    if target_id:
        # use the SAME identity set the matcher accepts (incl. group_id) so a
        # target keyed only by group_id still sorts to the front.
        candidates.sort(key=lambda item: _douyin_aweme_key(item) != target_id)
    for aweme in candidates:
        assets = _extract_douyin_aweme_assets(aweme)
        if assets:
            return assets
    return []


def _douyin_detail_url(aweme_id: str) -> str:
    params = {
        "aweme_id": aweme_id,
        "aid": "6383",
        "device_platform": "webapp",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "1280",
        "screen_height": "720",
        "browser_language": "en-US",
        "browser_platform": "MacIntel",
        "browser_name": "Chrome",
        "browser_version": "124.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "124.0",
        "os_name": "Windows",
        "os_version": "10",
        "platform": "PC",
    }
    return "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + urlencode(params)


def _fetch_douyin_detail_assets(url: str) -> list[dict]:
    aweme_id = _douyin_aweme_id_from_url(url)
    if not aweme_id:
        return []
    detail_url = _douyin_detail_url(aweme_id)
    raw = _fetch._render_with_httpx(
        detail_url,
        timeout=12.0,
        max_read_seconds=8.0,
        max_body_bytes=2_000_000,
    )
    data = _plain_json(raw, "douyin detail")
    if not data:
        return []
    if data.get("status_code") not in (0, None):
        msg = data.get("status_msg") or "抖音作品详情接口返回异常"
        raise ValueError(str(msg))
    return _extract_douyin_aweme_assets(data.get("aweme_detail") or data.get("aweme"))


def _run_douyin(url: str) -> list[dict]:
    direct_error: str | None = None
    try:
        assets = _fetch_douyin_detail_assets(url)
        if assets:
            return assets
    except ValueError as e:
        direct_error = str(e)
        log.warning("douyin detail returned no usable assets, falling back to render: %s", e)
    except httpx.HTTPError as e:
        direct_error = str(e)
        log.warning("douyin detail fetch failed, falling back to render: %s", e)
    html = _fetch._render_with_playwright(url)
    if html is None:
        raise ValueError(_timeout_hint("抖音"))
    assets = _extract_douyin_assets(html, url)
    if not assets and direct_error:
        raise ValueError(f"未获取到抖音作品素材，作品详情返回:{direct_error}")
    return assets
