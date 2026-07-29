"""淘宝 / 天猫 (Taobao / Tmall) product-page extractor."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from html import unescape as html_unescape
from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.fetcher import _fetch

from ..ssrf import SsrfError, assert_safe_url
from ._fetch import (
    _GENERIC_MAX_BODY_BYTES,
    _GENERIC_MAX_READ_SECONDS,
    RenderedPage,
)
from ._json_state import (
    _JS_STRING_RE,
    _extract_js_string_assignment,
    _json_from_jsonp,
    _walk_strings,
)
from ._product import _ecommerce_security_message, _push_product_asset
from ._urls import _URL_IN_TEXT_RE, _filter_safe_assets, _html_media_size
from .generic import _LAZY_IMG_ATTRS, _URL_IN_JSON_RE, _best_srcset_url

log = logging.getLogger("fetcher")


_TAOBAO_ITEM_ID_RE = re.compile(r"[?&](?:id|itemId)=(\d{6,20})", re.I)


def _is_taobao_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return (
        host in {"tb.cn", "e.tb.cn", "m.tb.cn", "taobao.com", "tmall.com", "tmall.hk"}
        or host.endswith(".tb.cn")
        or host.endswith(".taobao.com")
        or host.endswith(".tmall.com")
        or host.endswith(".tmall.hk")
    )


def _taobao_item_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _TAOBAO_ITEM_ID_RE.search(url)
    return m.group(1) if m else None


def _taobao_target_url(page: RenderedPage) -> str | None:
    target = _extract_js_string_assignment(page.html, "url")
    if target and _is_taobao_host(urlparse(target).hostname):
        return assert_safe_url(html_unescape(target))
    for match in _URL_IN_TEXT_RE.finditer(page.html[:20_000]):
        candidate = html_unescape(match.group(0).replace("\\/", "/"))
        if _is_taobao_host(urlparse(candidate).hostname):
            return assert_safe_url(candidate)
    return None


def _extract_taobao_assets(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen: set[str] = set()

    # Common SSR fields: auctionImages:["//img.alicdn..."], images:[...],
    # picUrl/mainPic. Keep regex-based extraction broad but CDN-restricted.
    for marker in ("auctionImages", "images", "imageList", "picUrl", "mainPic", "itemPic"):
        for mm in re.finditer(rf"\b{marker}\b\s*[:=]\s*(\[[^\]]+\]|{_JS_STRING_RE})", html, flags=re.S):
            blob = mm.group(1)
            for item in re.finditer(_JS_STRING_RE, blob):
                _push_product_asset(out, seen, item.group(1) or item.group(2), platform="taobao")

    for img in soup.find_all("img"):
        width, height = _html_media_size(img)
        for attr in _LAZY_IMG_ATTRS + ("data-ks-lazyload", "data-imgurl"):
            _push_product_asset(out, seen, img.get(attr), platform="taobao", width=width, height=height)
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            _push_product_asset(out, seen, _best_srcset_url(srcset), platform="taobao", width=width, height=height)

    for match in re.finditer(r"(?:(?:https?:)?//)?(?:img|gw|g-search\d*)\.alicdn\.com/[^\s\"'<>]+", html):
        _push_product_asset(out, seen, match.group(0), platform="taobao")

    return _filter_safe_assets(out)


def _extract_taobao_assets_from_json(data: dict | None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(data, dict):
        return out

    for text in _walk_strings(data):
        if not text:
            continue
        cleaned = text.replace("\\/", "/")
        if (
            "alicdn.com" in cleaned
            or "taobaocdn.com" in cleaned
            or cleaned.startswith(("imgextra/", "bao/uploaded/", "/imgextra/", "/bao/uploaded/"))
        ):
            _push_product_asset(out, seen, cleaned, platform="taobao")
        for match in _URL_IN_JSON_RE.finditer(text):
            _push_product_asset(
                out,
                seen,
                match.group(0).replace("\\/", "/"),
                platform="taobao",
            )
    return _filter_safe_assets(out)


def _taobao_mtop_detail_url(item_id: str, api_name: str) -> str:
    app_key = "12574478"
    timestamp = str(int(time.time() * 1000))
    ex_params = {
        "id": item_id,
        "detail_v": "3.5.0",
        "appReqFrom": "detail",
        "container_type": "xdetail",
        "dinamic_v3": "true",
        "supportV7": "true",
        "ultron2": "true",
        "itemNumId": item_id,
        "pageCode": "miniAppDetail",
        "_from_": "miniapp",
        "openFrom": "pagedetail",
        "pageSource": "1",
        "requestSource": "detailH5",
    }
    payload = json.dumps(
        {
            "id": item_id,
            "detail_v": "3.5.0",
            "exParams": json.dumps(ex_params, separators=(",", ":"), ensure_ascii=False),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # Public H5 detail endpoints normally accept an empty-token signature. When
    # Taobao requires login/security verification it returns a structured error;
    # in that case we simply fall back to the clearer platform message.
    sign = hashlib.md5(f"&{timestamp}&{app_key}&{payload}".encode()).hexdigest()
    params = {
        "jsv": "2.7.4",
        "appKey": app_key,
        "t": timestamp,
        "sign": sign,
        "api": api_name,
        "v": "1.0",
        "ttid": "201200@taobao_h5_10.2.10",
        "requestSource": "detailH5",
        "isSec": "0",
        "ecode": "0",
        "AntiFlood": "true",
        "AntiCreep": "true",
        "H5Request": "true",
        "type": "jsonp",
        "dataType": "jsonp",
        "safariGoLogin": "true",
        "mainDomain": "taobao.com",
        "subDomain": "m",
        "prefix": "h5api",
        "syncCookieMode": "true",
        "getJSONP": "true",
        "callback": "mtopjsonp1",
        "data": payload,
    }
    return f"https://h5api.m.taobao.com/h5/{api_name}/1.0/?" + urlencode(params)


def _fetch_taobao_h5_detail_assets(item_id: str) -> list[dict]:
    for api_name in ("mtop.taobao.pcdetail.data.get", "mtop.taobao.detail.data.get"):
        raw = _fetch._render_with_httpx(
            _taobao_mtop_detail_url(item_id, api_name),
            timeout=8.0,
            max_read_seconds=5.0,
            max_body_bytes=2_000_000,
        )
        data = _json_from_jsonp(raw, api_name)
        assets = _extract_taobao_assets_from_json(data)
        if assets:
            return assets
        ret = data.get("ret") if isinstance(data, dict) else None
        if ret:
            log.info("taobao h5 detail returned no assets api=%s ret=%s", api_name, ret)
    return []


def _run_taobao(url: str) -> list[dict]:
    first = _fetch._render_page_with_httpx(
        url,
        timeout=12.0,
        max_read_seconds=_GENERIC_MAX_READ_SECONDS,
        max_body_bytes=_GENERIC_MAX_BODY_BYTES,
    )
    pages: list[RenderedPage] = [first]

    target = _taobao_target_url(first)
    item_id = (
        _taobao_item_id_from_url(target)
        or _taobao_item_id_from_url(first.final_url)
        or _taobao_item_id_from_url(url)
    )
    for candidate in (
        target,
        f"https://item.taobao.com/item.htm?id={item_id}" if item_id else None,
        f"https://h5.m.taobao.com/awp/core/detail.htm?id={item_id}" if item_id else None,
    ):
        if not candidate or candidate in {p.final_url for p in pages}:
            continue
        try:
            pages.append(_fetch._render_page_with_httpx(
                candidate,
                timeout=12.0,
                max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                max_body_bytes=_GENERIC_MAX_BODY_BYTES,
            ))
        except httpx.HTTPError as e:
            log.warning("taobao fallback failed: %s", e)

    blocked: str | None = None
    for page in pages:
        productish = (
            _taobao_item_id_from_url(page.final_url)
            or "auctionImages" in page.html
            or "picUrl" in page.html
            or "imgextra" in page.html
        )
        assets = _extract_taobao_assets(page.html, page.final_url)
        if productish and assets:
            return assets
        if msg := _ecommerce_security_message("taobao", page.html, page.final_url):
            blocked = msg
    if item_id:
        try:
            assets = _fetch_taobao_h5_detail_assets(item_id)
            if assets:
                return assets
        except (httpx.HTTPError, SsrfError, ValueError) as e:
            log.warning("taobao h5 detail fallback failed item=%s error=%s", item_id, e)
    if blocked:
        raise ValueError(blocked)
    return []
