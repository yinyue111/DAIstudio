"""微信公众号 (mp.weixin.qq.com) extractor: fully server-rendered articles."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.services.fetcher import _fetch

from ._fetch import _GENERIC_MAX_BODY_BYTES, _GENERIC_MAX_READ_SECONDS
from ._urls import (
    _abs,
    _classify,
    _filter_safe_assets,
    _looks_like_audio_url,
    _safe_asset_url,
)


# --- WeChat 公众号 (mp.weixin.qq.com) ---------------------------------------
def _is_weixin_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == "mp.weixin.qq.com" or host.endswith(".mp.weixin.qq.com")


def _weixin_full_res(url: str) -> str:
    """mmbiz serves a resized variant as the trailing path segment (e.g. /640);
    /0 is the full-resolution original. Upgrade it so style reference isn't fed
    a downscaled image."""
    if "mmbiz.qpic.cn" not in url:
        return url
    return re.sub(r"/\d+(?=\?|$)", "/0", url, count=1)


def _extract_weixin_assets(html: str, base_url: str) -> list[dict]:
    """公众号 articles are fully server-rendered: body images carry the real URL
    in ``data-src`` (``src`` is usually a blank placeholder). Scope to the
    article body so header/QR/avatar chrome doesn't leak in."""
    soup = BeautifulSoup(html, "lxml")
    content = soup.find(id="js_content") or soup
    seen: set[str] = set()
    out: list[dict] = []

    def push(raw: str | None, type_: str, thumb: str | None = None):
        abs_url = _abs(base_url, raw)
        if not abs_url:
            return
        url = _safe_asset_url(_weixin_full_res(abs_url))
        if not url or url in seen or _looks_like_audio_url(url):
            return
        seen.add(url)
        out.append({"type": _classify(url) or type_, "url": url,
                    "thumb": _safe_asset_url(thumb) if thumb else None,
                    "width": None, "height": None})

    for img in content.find_all("img"):
        dtype = (img.get("data-type") or "").lower()
        push(img.get("data-src") or img.get("src"),
             "video" if dtype in ("mp4", "video") else "image")
    # cover / share image as a fallback when the body has no inline media
    for m in soup.find_all("meta"):
        prop = (m.get("property") or m.get("name") or "").lower()
        if prop in ("og:image", "twitter:image"):
            push(m.get("content"), "image")
    return _filter_safe_assets(out)


def _run_weixin(url: str) -> list[dict]:
    html = _fetch._render_with_httpx(url, max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                                     max_body_bytes=_GENERIC_MAX_BODY_BYTES)
    return _extract_weixin_assets(html, url)
