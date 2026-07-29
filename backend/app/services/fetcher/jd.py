"""京东 (JD) product-page extractor."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.fetcher import _fetch

from ..ssrf import SsrfError, assert_safe_url
from ._fetch import (
    _GENERIC_MAX_BODY_BYTES,
    _GENERIC_MAX_READ_SECONDS,
    RenderedPage,
)
from ._json_state import _JS_STRING_RE, _json_from_jsonp
from ._product import (
    _ecommerce_security_message,
    _push_product_asset,
    _push_product_video_asset,
)
from ._urls import _filter_safe_assets, _html_media_size, _int_or_none

log = logging.getLogger("fetcher")


_JD_SKU_RE = re.compile(
    r"(?:item\.jd\.com/|item\.m\.jd\.com/product/)(\d{6,20})\.html|[?&]skuId=(\d{6,20})",
    re.I,
)


def _is_jd_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return (
        host in {"3.cn", "jd.com", "jd.hk", "360buy.com"}
        or host.endswith(".jd.com")
        or host.endswith(".jd.hk")
        or host.endswith(".360buy.com")
    )


def _jd_sku_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _JD_SKU_RE.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _jd_return_url(page: RenderedPage) -> str | None:
    parsed = urlparse(page.final_url)
    qs = parse_qs(parsed.query)
    ret = (qs.get("returnurl") or qs.get("returnUrl") or [""])[0]
    if ret:
        return assert_safe_url(unquote(ret))
    return None


def _jd_video_ids_from_html(html: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    patterns = (
        r"\bmainVideoId\b\s*[:=]\s*[\"']?(\d{5,20})",
        r"\bvideoId\b\s*[:=]\s*[\"']?(\d{5,20})",
        r"\bdata-vu\s*=\s*[\"'](\d{5,20})[\"']",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.I):
            video_id = match.group(1)
            if video_id in seen:
                continue
            seen.add(video_id)
            out.append(video_id)
    return out


def _jd_video_api_url(video_id: str) -> str:
    params = {
        "callback": "jdVideo",
        "vid": video_id,
        "type": "1",
        "from": "1",
        "appid": "item-v3",
        "functionId": "pc_tencent_video_v3",
        "_": str(int(time.time() * 1000)),
    }
    return "https://api.m.jd.com/tencent/video_v3?" + urlencode(params)


def _fetch_jd_video_asset(video_id: str) -> dict | None:
    raw = _fetch._render_with_httpx(
        _jd_video_api_url(video_id),
        timeout=8.0,
        max_read_seconds=5.0,
        max_body_bytes=512_000,
    )
    data = _json_from_jsonp(raw, "jd video")
    if not data or data.get("code") not in (0, "0", None):
        return None
    ext = data.get("extInfo") if isinstance(data.get("extInfo"), dict) else {}
    play_url = data.get("playUrl") or data.get("url")
    if not isinstance(play_url, str):
        return None
    return {
        "url": play_url,
        "thumb": data.get("imageUrl") if isinstance(data.get("imageUrl"), str) else None,
        "width": _int_or_none(ext.get("vwidth") or ext.get("width")),
        "height": _int_or_none(ext.get("vheight") or ext.get("height")),
    }


def _extract_jd_assets(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen: set[str] = set()

    for video_id in _jd_video_ids_from_html(html):
        try:
            video = _fetch_jd_video_asset(video_id)
        except (httpx.HTTPError, SsrfError, ValueError) as e:
            log.warning("jd video fetch failed id=%s error=%s", video_id, e)
            continue
        if video:
            _push_product_video_asset(
                out,
                seen,
                video.get("url"),
                thumb=video.get("thumb"),
                width=video.get("width"),
                height=video.get("height"),
            )

    # Preferred: product gallery emitted as imageList: ["jfs/...", ...].
    m = re.search(r"\bimageList\s*:\s*\[(.*?)\]", html, flags=re.S)
    if m:
        for item in re.finditer(_JS_STRING_RE, m.group(1)):
            _push_product_asset(out, seen, item.group(1) or item.group(2), platform="jd")

    # Main product image in the PC detail DOM.
    for img in soup.find_all("img"):
        width, height = _html_media_size(img)
        for attr in ("data-origin", "data-url", "src"):
            _push_product_asset(out, seen, img.get(attr), platform="jd", width=width, height=height)

    # Last-resort structured URLs from scripts, but only on JD image CDN.
    for match in re.finditer(r"(?:(?:https?:)?//img\d{2}\.360buyimg\.com/[^\s\"'<>]+|jfs/[A-Za-z0-9_./-]+)", html):
        _push_product_asset(out, seen, match.group(0), platform="jd")

    return _filter_safe_assets(out)


def _run_jd(url: str) -> list[dict]:
    first = _fetch._render_page_with_httpx(
        url,
        timeout=12.0,
        max_read_seconds=_GENERIC_MAX_READ_SECONDS,
        max_body_bytes=_GENERIC_MAX_BODY_BYTES,
    )
    pages: list[RenderedPage] = [first]

    candidate_url = _jd_return_url(first)
    sku = (
        _jd_sku_from_url(candidate_url)
        or _jd_sku_from_url(first.final_url)
        or _jd_sku_from_url(url)
    )
    if sku:
        pc_url = f"https://item.jd.com/{sku}.html"
        if pc_url not in {p.final_url for p in pages}:
            try:
                pages.append(_fetch._render_page_with_httpx(
                    pc_url,
                    timeout=12.0,
                    max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                    max_body_bytes=_GENERIC_MAX_BODY_BYTES,
                ))
            except httpx.HTTPError as e:
                log.warning("jd pc fallback failed: %s", e)

    blocked: str | None = None
    for page in pages:
        assets = _extract_jd_assets(page.html, page.final_url)
        if assets:
            return assets
        if msg := _ecommerce_security_message("jd", page.html, page.final_url):
            blocked = msg
    if blocked:
        raise ValueError(blocked)
    return []
