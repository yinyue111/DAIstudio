"""Shared helpers for e-commerce product pages (JD / Taobao / Tmall)."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ._urls import (
    _VID_EXT,
    _looks_like_audio_url,
    _normalise_asset_url,
    _safe_asset_url,
)
from .generic import _is_noise_url


def _is_ecommerce_host(host: str | None) -> bool:
    # Imported inside the function: jd/taobao import this module.
    from .jd import _is_jd_host
    from .taobao import _is_taobao_host

    return _is_jd_host(host) or _is_taobao_host(host)


def _strip_ecommerce_img_variant(url: str) -> str:
    """Upgrade common e-commerce CDN thumbnails to larger product images."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path
    if "360buyimg.com" in host:
        # n1/s720x720_jfs/... -> n1/jfs/...; n7/jfs... is already fine.
        path = re.sub(r"/s\d+x\d+_(jfs/)", r"/\1", path)
        return parsed._replace(path=path).geturl()
    if "alicdn.com" in host or "taobaocdn.com" in host:
        # //img.alicdn.com/imgextra/...jpg_430x430q90.jpg -> original jpg
        path = re.sub(r"_(?:\d+x\d+|[\dx]+q\d+|q\d+|sum|webp)[^/]*$", "", path, flags=re.I)
        return parsed._replace(path=path).geturl()
    return url


def _normalise_product_image_url(url: str | None, *, platform: str) -> str | None:
    url = _normalise_asset_url(url)
    if not url:
        return None
    if platform == "jd" and url.startswith("jfs/"):
        return "https://img13.360buyimg.com/n1/" + url
    if platform == "taobao" and url.startswith(("imgextra/", "bao/uploaded/")):
        return "https://img.alicdn.com/" + url
    parsed = urlparse(url)
    if platform == "jd" and parsed.netloc == "" and parsed.path.startswith("/jfs/"):
        url = "https://img13.360buyimg.com/n1" + parsed.path
    elif platform == "taobao" and parsed.netloc == "" and parsed.path.startswith(("/imgextra/", "/bao/uploaded/")):
        url = "https://img.alicdn.com" + parsed.path
    return _strip_ecommerce_img_variant(url)


# --- E-commerce product pages (JD / Taobao / Tmall) -------------------------
def _ecommerce_security_message(platform: str, html: str, final_url: str = "") -> str | None:
    text = (html or "")[:20_000]
    joined = f"{final_url}\n{text}".lower()
    if platform == "jd" and ("京东验证" in text or "risk_handler" in joined or "jdr_shields" in joined):
        return "京东返回安全校验，当前环境无法直接浏览该商品页；请换 PC 端公开商品详情页链接，或保存商品主图后使用「上传图片参考」"
    if platform == "taobao" and (
        "x5secdata" in joined
        or "punish" in joined
        or "bixi.alicdn.com" in joined
        or "login.taobao.com" in joined
        or "login.m.taobao.com" in joined
    ):
        return "淘宝返回安全校验，当前环境无法直接浏览该商品页；请换 PC 端公开商品详情页链接，或保存商品主图后使用「上传图片参考」"
    return None


def _push_product_asset(
    out: list[dict],
    seen: set[str],
    raw_url: str | None,
    *,
    platform: str,
    width: int | None = None,
    height: int | None = None,
) -> None:
    normalised = _normalise_product_image_url(raw_url, platform=platform)
    safe = _safe_asset_url(normalised)
    if not safe or safe in seen:
        return
    host = (urlparse(safe).hostname or "").lower()
    if platform == "jd" and "360buyimg.com" not in host:
        return
    if platform == "taobao" and not ("alicdn.com" in host or "taobaocdn.com" in host):
        return
    path = urlparse(safe).path.lower()
    if platform == "taobao" and (
        "/tfs/" in path
        or "apple-touch-icon" in path
        or path.endswith(".ico")
        or re.search(r"-\d{2,3}-\d{2,3}\.(?:png|jpg|jpeg|webp)$", path)
    ):
        return
    if _is_noise_url(safe):
        return
    seen.add(safe)
    out.append({
        "type": "image",
        "url": safe,
        "thumb": safe,
        "width": width,
        "height": height,
    })


def _push_product_video_asset(
    out: list[dict],
    seen: set[str],
    raw_url: str | None,
    *,
    thumb: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    safe = _safe_asset_url(raw_url)
    if not safe or safe in seen:
        return
    if _looks_like_audio_url(safe):
        return
    path = urlparse(safe).path.lower()
    if not path.endswith(_VID_EXT):
        return
    seen.add(safe)
    out.append({
        "type": "video",
        "url": safe,
        "thumb": _safe_asset_url(thumb) if thumb else None,
        "width": width,
        "height": height,
    })
