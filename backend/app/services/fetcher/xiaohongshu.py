"""小红书 (xiaohongshu) extractor: window.__INITIAL_STATE__ SSR mining."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.services.fetcher import _fetch

from ..safe_logging import redact_url_for_log
from ._fetch import _timeout_hint
from ._json_state import _json_like_state
from ._urls import (
    _canonical_url,
    _filter_safe_assets,
    _int_or_none,
    _normalise_asset_url,
)

log = logging.getLogger("fetcher")


_XHS_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>", re.S)


def _is_xiaohongshu_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")


def _xhs_block_message(url: str) -> str | None:
    parsed = urlparse(url)
    if not _is_xiaohongshu_host(parsed.hostname):
        return None
    if "/404/sec_" not in parsed.path:
        return None
    qs = parse_qs(parsed.query)
    msg = (qs.get("error_msg") or [""])[0]
    code = (qs.get("error_code") or [""])[0]
    msg = unquote(msg) if msg else "当前笔记暂时无法浏览"
    suffix = f"(error_code={code})" if code else ""
    return f"小红书返回安全校验:{msg}{suffix}"


def _xhs_scene_url(image: dict, scene: str) -> str | None:
    for info in image.get("infoList") or []:
        if not isinstance(info, dict):
            continue
        if info.get("imageScene") == scene and info.get("url"):
            return info["url"]
    return None


def _xhs_image_urls(image: dict) -> tuple[str | None, str | None]:
    url = _normalise_asset_url(
        image.get("urlDefault") or _xhs_scene_url(image, "WB_DFT") or image.get("url")
    )
    thumb = _normalise_asset_url(
        image.get("urlPre") or _xhs_scene_url(image, "WB_PRV") or url
    )
    return url, thumb


def _xhs_image_size(image: dict) -> tuple[int | None, int | None]:
    return _int_or_none(image.get("width")), _int_or_none(image.get("height"))


def _xhs_note_thumb(note: dict) -> str | None:
    for image in note.get("imageList") or []:
        if not isinstance(image, dict):
            continue
        _, thumb = _xhs_image_urls(image)
        if thumb:
            return thumb
    return None


def _xhs_streams(stream: dict | None):
    if not isinstance(stream, dict):
        return
    for codec in ("h264", "h265", "h266", "av1"):
        entries = stream.get(codec) or []
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            if isinstance(entry, dict):
                yield entry


def _xhs_stream_url(entry: dict) -> str | None:
    candidates = [entry.get("masterUrl"), entry.get("master_url")]
    for key in ("backupUrls", "backup_urls"):
        urls = entry.get(key)
        if isinstance(urls, list):
            candidates.extend(urls)
    for candidate in candidates:
        url = _normalise_asset_url(candidate)
        if url:
            return url
    return None


def _xhs_video_source(note: dict) -> dict | None:
    video = note.get("video") or {}
    if not isinstance(video, dict):
        return None

    media = video.get("media") or {}
    if isinstance(media, dict):
        for entry in _xhs_streams(media.get("stream")):
            url = _xhs_stream_url(entry)
            if url:
                return {
                    "url": url,
                    "width": _int_or_none(entry.get("width")),
                    "height": _int_or_none(entry.get("height")),
                }

    media_v2 = video.get("mediaV2") or note.get("mediaV2")
    if isinstance(media_v2, str):
        try:
            media_v2 = json.loads(media_v2)
        except json.JSONDecodeError:
            media_v2 = None
    if isinstance(media_v2, dict):
        for entry in _xhs_streams(media_v2.get("stream")):
            url = _xhs_stream_url(entry)
            if url:
                return {
                    "url": url,
                    "width": _int_or_none(entry.get("width")),
                    "height": _int_or_none(entry.get("height")),
                }

    return None


def _extract_xiaohongshu_assets(html: str) -> list[dict]:
    m = _XHS_STATE_RE.search(html)
    if not m:
        return []
    state = _json_like_state(m.group(1))
    if not state:
        return []

    note_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    if not isinstance(note_map, dict):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for detail in note_map.values():
        if not isinstance(detail, dict):
            continue
        note = detail.get("note") or {}
        if not isinstance(note, dict):
            continue
        video = _xhs_video_source(note)
        video_url = (video or {}).get("url")
        thumb = _xhs_note_thumb(note)
        thumb_w = thumb_h = None
        for image in note.get("imageList") or []:
            if isinstance(image, dict):
                thumb_w, thumb_h = _xhs_image_size(image)
                if thumb_w and thumb_h:
                    break
        if video_url and video_url not in seen:
            seen.add(video_url)
            out.append({
                "type": "video",
                "url": video_url,
                "thumb": thumb,
                "width": (video or {}).get("width"),
                "height": (video or {}).get("height"),
                "thumb_width": thumb_w,
                "thumb_height": thumb_h,
            })
        for image in note.get("imageList") or []:
            if not isinstance(image, dict):
                continue
            url, thumb = _xhs_image_urls(image)
            if not url or url in seen:
                continue
            seen.add(url)
            width, height = _xhs_image_size(image)
            out.append({"type": "image", "url": url, "thumb": thumb,
                        "width": width, "height": height})
    return _filter_safe_assets(out)


def _xhs_security_hint(reason: str | None = None) -> str:
    prefix = reason or "小红书返回安全校验:当前笔记暂时无法浏览"
    return (
        f"{prefix}。当前环境没有拿到公开可访问素材；请确认链接在未登录浏览器也能打开，"
        "或保存/截图后使用「上传图片参考」。"
    )


def _run_xiaohongshu(url: str) -> list[dict]:
    blocked_reason: str | None = None
    should_try_canonical = False
    rendered_empty = False

    def render_httpx(candidate: str, *, timeout: float, max_read_seconds: float,
                     max_body_bytes: int) -> list[dict]:
        nonlocal blocked_reason
        try:
            html = _fetch._render_with_httpx(
                candidate,
                timeout=timeout,
                max_read_seconds=max_read_seconds,
                max_body_bytes=max_body_bytes,
            )
        except ValueError as e:
            msg = str(e)
            if "小红书返回安全校验" in msg:
                blocked_reason = msg
                log.warning("xiaohongshu returned security check for %s: %s",
                            redact_url_for_log(candidate), msg)
                return []
            raise
        return _extract_xiaohongshu_assets(html)

    try:
        assets = render_httpx(
            url,
            timeout=12.0,
            max_read_seconds=8.0,
            max_body_bytes=2_000_000,
        )
        if assets:
            return assets
        should_try_canonical = blocked_reason is not None
    except httpx.TimeoutException:
        log.warning("xiaohongshu direct fetch timed out; checking canonical url")
        should_try_canonical = True
    except httpx.HTTPError as e:
        log.warning("xiaohongshu direct fetch failed, falling back to render: %s", e)

    canonical = _canonical_url(url)
    if should_try_canonical and canonical != url:
        try:
            assets = render_httpx(
                canonical,
                timeout=8.0,
                max_read_seconds=4.0,
                max_body_bytes=1_000_000,
            )
            if assets:
                return assets
        except httpx.TimeoutException as e:
            log.warning("xiaohongshu canonical fetch timed out: %s", e)
        except httpx.HTTPError as e:
            log.warning("xiaohongshu canonical fetch failed: %s", e)

    for candidate in (url, canonical) if canonical != url else (url,):
        html = _fetch._render_with_playwright(candidate)
        if html is None:
            continue
        rendered_empty = True
        assets = _extract_xiaohongshu_assets(html)
        if assets:
            return assets

    if blocked_reason:
        raise ValueError(_xhs_security_hint(blocked_reason))
    if rendered_empty:
        return []
    raise ValueError(_timeout_hint("小红书"))
