"""Link scraper: fetch a page and extract image/video reference assets.

Dispatch is table-driven via ``_REGISTRY`` (see bottom of file). Each platform
is one self-contained ``PlatformExtractor`` — host matcher + ``run`` (fetch +
extract, owning any httpx→Playwright fallback) + messages. ``parse_url`` just
selects the matching platform and runs it. **Adding a platform = append one
registry entry**; nothing else changes.

Current platforms: 小红书 (window.__INITIAL_STATE__ SSR), 抖音 (web detail API +
RENDER_DATA render fallback), 微信公众号 (SSR data-src on mmbiz.qpic.cn), X/Twitter
(render fallback for status cards), 京东/淘宝商品页, and a generic web fallback
for everything else.

The generic fallback (``extract_assets``) mines: <img> (src + lazy data-* attrs
+ best-resolution srcset), <picture>/<source>, <video>/<source>, JSON-LD
ImageObject/VideoObject, og/twitter/itemprop meta (image AND video), <link>
image hints, SSR state blobs (__NEXT_DATA__/__NUXT__/JSON), and CSS
background-image. It filters obvious chrome (sprites/icons/tracking pixels) and
returns content first / decorative backgrounds last.

Safety: every user URL and every redirect hop is SSRF-checked and DNS-pinned;
httpx reads cap the *compressed* bytes (gzip-bomb guard) and Playwright is bound
by a process-wide concurrency cap + an HTML-size ceiling.

Note on referer/anti-hotlink: we send a browser-like UA and set Referer to the
page origin so most hotlink protections pass. Assets that still 403 are left in
the list (the frontend shows a broken-image hint) rather than failing the whole
parse.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape as html_unescape
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from .safe_logging import redact_url_for_log
from .ssrf import (
    MAX_REDIRECTS,
    SsrfError,
    assert_safe_url,
    pinned_client,
)

log = logging.getLogger("fetcher")

# Cap concurrent headless-browser renders process-wide. A Playwright render
# spawns a real multi-process Chromium (~100-300MB RSS); without this an
# attacker (or a burst of slow pages) could launch dozens at once and OOM the
# box / starve the request thread pool. Excess renders fail fast (see below).
_PW_SEM = threading.BoundedSemaphore(max(1, int(settings.parse_playwright_parallelism)))
_PARSE_BUSY_HINT = "解析服务繁忙，请稍后重试"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_BG_RE = re.compile(r"url\((['\"]?)(.*?)\1\)", re.I)
_XHS_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>", re.S)
_DOUYIN_RENDER_RE = re.compile(
    r"<script[^>]+id=[\"']RENDER_DATA[\"'][^>]*>(.*?)</script>", re.S
)
_DOUYIN_ID_RE = re.compile(r"(?:video|note|modal_id|aweme_id)[=/](\d{16,25})")
_JD_SKU_RE = re.compile(
    r"(?:item\.jd\.com/|item\.m\.jd\.com/product/)(\d{6,20})\.html|[?&]skuId=(\d{6,20})",
    re.I,
)
_TAOBAO_ITEM_ID_RE = re.compile(r"[?&](?:id|itemId)=(\d{6,20})", re.I)
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
_VID_EXT = (".mp4", ".webm", ".mov", ".m3u8")
_AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".flac")
_GENERIC_MAX_BODY_BYTES = 2_000_000
_GENERIC_MAX_READ_SECONDS = 8.0

# Generic-fallback tuning. Drop obvious chrome (sprites/icons/tracking pixels)
# and sub-thumbnail images, and only scan the head of huge SSR JSON blobs.
_MIN_KEEP_DIM = 50
_SSR_SCAN_MAX = 2_000_000
_NOISE_RE = re.compile(
    r"(?:sprite|favicon|/icons?/|/logos?/|[-_/]logo[-_.]|1x1|spacer|"
    r"/avatars?/|tracking|/beacon|/pixel[-_.]|/emoji/|/badges?/)",
    re.I,
)
# http(s) URL inside a JSON/JS blob; tolerates escaped slashes (https:\/\/a\/b).
_URL_IN_JSON_RE = re.compile(r"https?:(?:\\?/){2}(?:[^\s\"'<>\\]|\\/)+", re.I)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>，。；、]+", re.I)
_JS_STRING_RE = r"""(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)')"""
# <img> lazy-load attribute variants that carry the real URL.
_LAZY_IMG_ATTRS = (
    "src", "data-src", "data-original", "data-lazy-src", "data-actualsrc",
    "data-echo", "data-img", "data-original-src",
)
# JSON-LD keys whose (string / list / nested-object) values are media URLs.
_LD_MEDIA_KEYS = ("image", "thumbnailurl", "contenturl", "poster")


@dataclass(frozen=True)
class RenderedPage:
    html: str
    final_url: str


def _is_xiaohongshu_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")


def _is_douyin_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def _is_x_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com")


def _is_jd_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return (
        host in {"3.cn", "jd.com", "jd.hk", "360buy.com"}
        or host.endswith(".jd.com")
        or host.endswith(".jd.hk")
        or host.endswith(".360buy.com")
    )


def _is_taobao_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return (
        host in {"tb.cn", "e.tb.cn", "m.tb.cn", "taobao.com", "tmall.com", "tmall.hk"}
        or host.endswith(".tb.cn")
        or host.endswith(".taobao.com")
        or host.endswith(".tmall.com")
        or host.endswith(".tmall.hk")
    )


def _is_ecommerce_host(host: str | None) -> bool:
    return _is_jd_host(host) or _is_taobao_host(host)


def _is_x_media_host(host: str | None) -> bool:
    host = (host or "").rstrip(".").lower()
    return host == "twimg.com" or host.endswith(".twimg.com")


def _is_x_post_media_url(url: str | None) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").rstrip(".").lower()
    return _is_x_media_host(host) and parsed.path.startswith("/media/")


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


def _canonical_url(url: str) -> str:
    return urlparse(url)._replace(query="", fragment="").geturl()


def _normalise_asset_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    # XHS image hosts serve the same content over https; prefer it so browser
    # pages do not run into mixed-content or stricter referrer policies.
    if url.startswith("http://sns-"):
        return "https://" + url[len("http://"):]
    return url


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


def _install_ssrf_route(page, page_url: str) -> None:
    """Proxy browser requests through our pinned httpx fetcher where possible.

    Chromium cannot pin DNS to the vetted IP from ``resolve_safe``. For render
    fallback we only need HTML/JS/JSON state, so abort heavy media subresources
    and fulfill navigations/scripts/XHRs via the SSRF-guarded httpx path.
    """

    def handler(route):
        try:
            req = route.request
            req_url = req.url
            parsed = urlparse(req_url)
            if parsed.scheme not in ("http", "https"):
                route.abort()
                return
            if not _same_site_or_platform(page_url, req_url):
                route.abort()
                return
            resource_type = req.resource_type
            if resource_type in {"image", "media", "font", "websocket", "manifest"}:
                route.abort()
                return
            if resource_type in {"document", "script", "xhr", "fetch"}:
                try:
                    body = _render_with_httpx(
                        req_url,
                        timeout=8.0,
                        max_read_seconds=4.0,
                        max_body_bytes=settings.parse_max_render_html_bytes,
                    )
                    route.fulfill(
                        status=200,
                        headers={"content-type": "text/html; charset=utf-8"},
                        body=body,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "blocked playwright proxied request %s: %s",
                        redact_url_for_log(req_url),
                        e,
                    )
                    route.abort()
                return
            route.abort()
        except Exception:  # pragma: no cover - fail closed
            try:
                route.abort()
            except Exception:
                pass

    page.route("**/*", handler)


def _render_with_playwright(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    # Bound concurrent browsers process-wide; fail fast instead of camping a
    # request thread (and a future browser) when we're already saturated.
    if not _PW_SEM.acquire(timeout=settings.parse_playwright_acquire_timeout_seconds):
        raise ValueError(_PARSE_BUSY_HINT)
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=UA)
            _install_ssrf_route(page, url)
            nav_error = None
            try:
                # Network-idle is too strict for sites with long polling /
                # anti-bot beacons. DOM readiness plus a short settle window is
                # enough for SSR state extraction and avoids false timeouts.
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:  # noqa: BLE001
                nav_error = e
                log.warning("playwright navigation did not reach domcontentloaded: %s", e)
            # the final URL may differ after redirects — re-validate it
            final_url = page.url
            if urlparse(final_url).scheme not in ("http", "https"):
                return None
            assert_safe_url(final_url)
            # nudge lazy-loaded images
            page.evaluate("document.body && window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            html = page.content()
            # Bound the serialized DOM we pull into this process; a hostile page
            # can grow the DOM without limit and page.content() copies it whole.
            if len(html) > settings.parse_max_render_html_bytes:
                log.warning("playwright content too large (%d bytes); dropping", len(html))
                return None
            if nav_error and len(html) < 200:
                return None
            return html
    except SsrfError:
        raise
    except Exception as e:  # pragma: no cover
        log.warning("playwright render failed: %s", e)
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        _PW_SEM.release()


def _charset_from_content_type(content_type: str | None) -> str:
    if content_type and "charset=" in content_type.lower():
        cs = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
        if cs:
            return cs
    return "utf-8"


def _inflate_capped(raw: bytes, encoding: str | None, max_out: int) -> bytes:
    """Bounded content-decoding. The caller has already capped ``raw`` (the
    *compressed* bytes), and we cap the decoded output to ``max_out`` so a gzip
    bomb cannot inflate one chunk into hundreds of MB before any size check.

    We only advertise gzip/deflate (see Accept-Encoding below), so those are the
    encodings we expect; anything else (or a server that mislabels plain text)
    falls back to using the raw bytes as-is, truncated to ``max_out``."""
    enc = (encoding or "").lower().strip()
    if enc in ("", "identity"):
        return raw[:max_out]
    if enc in ("gzip", "x-gzip", "deflate"):
        for wbits in (zlib.MAX_WBITS | 32, -zlib.MAX_WBITS):
            try:
                return zlib.decompressobj(wbits).decompress(raw, max_out)
            except zlib.error:
                continue
        return raw[:max_out]  # labeled compressed but isn't — treat as plain
    # Unknown/unsupported encoding (we never request br/zstd); be lenient.
    return raw[:max_out]


def _read_body_capped(r: httpx.Response, max_body_bytes: int | None,
                      max_read_seconds: float | None,
                      max_raw_bytes: int | None) -> str:
    """Read a streamed response body with a cap on the COMPRESSED bytes pulled
    off the wire, then bounded-decode it. Using ``iter_raw`` (pre-decompression)
    means httpx never auto-inflates a whole chunk past our budget."""
    raw_cap = max_raw_bytes or settings.parse_max_raw_body_bytes
    out_cap = max_body_bytes or settings.parse_max_raw_body_bytes
    started = time.monotonic()
    raw = bytearray()
    for chunk in r.iter_raw():
        if not chunk:
            continue
        raw.extend(chunk)
        if len(raw) >= raw_cap:
            del raw[raw_cap:]
            break
        if max_read_seconds and time.monotonic() - started >= max_read_seconds:
            break
    body = _inflate_capped(bytes(raw), r.headers.get("content-encoding"), out_cap)
    charset = _charset_from_content_type(r.headers.get("content-type"))
    try:
        return body.decode(charset, "ignore")
    except LookupError:
        return body.decode("utf-8", "ignore")


def _render_with_httpx(url: str, timeout: float = 20.0,
                       max_read_seconds: float | None = None,
                       max_body_bytes: int | None = None,
                       max_raw_bytes: int | None = None) -> str:
    """Fetch HTML with redirects followed manually so every hop is SSRF-checked.

    httpx's own ``follow_redirects`` re-resolves DNS and would happily follow a
    302 into the internal network, bypassing the guard — so we disable it and
    validate each Location ourselves, capped at MAX_REDIRECTS. We only advertise
    gzip/deflate so the bounded decoder in ``_read_body_capped`` covers every
    response (a malicious br/zstd body would just be read as opaque bytes)."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    headers = {"User-Agent": UA, "Referer": origin, "Accept": "text/html,*/*",
               "Accept-Encoding": "gzip, deflate"}
    assert_safe_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        with pinned_client(url, follow_redirects=False, timeout=timeout, headers=headers) as c:
            with c.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    blocked = _xhs_block_message(url)
                    if blocked:
                        raise ValueError(blocked)
                    assert_safe_url(url)  # red line: re-check before following
                    continue
                r.raise_for_status()
                return _read_body_capped(r, max_body_bytes, max_read_seconds, max_raw_bytes)
    raise ValueError("重定向次数过多")


def _render_page_with_httpx(url: str, timeout: float = 20.0,
                            max_read_seconds: float | None = None,
                            max_body_bytes: int | None = None,
                            max_raw_bytes: int | None = None) -> RenderedPage:
    """Like _render_with_httpx, but returns the last validated URL as well."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    headers = {
        "User-Agent": UA,
        "Referer": origin,
        "Accept": "text/html,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip, deflate",
    }
    assert_safe_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        with pinned_client(url, follow_redirects=False, timeout=timeout, headers=headers) as c:
            with c.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    blocked = _xhs_block_message(url)
                    if blocked:
                        raise ValueError(blocked)
                    assert_safe_url(url)
                    continue
                r.raise_for_status()
                return RenderedPage(
                    html=_read_body_capped(r, max_body_bytes, max_read_seconds, max_raw_bytes),
                    final_url=str(r.url),
                )
    raise ValueError("重定向次数过多")


def _json_like_state(raw: str) -> dict | None:
    """Parse JSON-like JS state emitted by SSR pages.

    Xiaohongshu's ``window.__INITIAL_STATE__`` is mostly JSON but may contain
    bare ``undefined`` values, so strict json.loads fails without a small,
    value-position-only cleanup.
    """
    raw = raw.strip().rstrip(";")
    cleaned = re.sub(r"([:\[,])\s*undefined\s*(?=[,\]}])", r"\1null", raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("failed to parse xiaohongshu initial state", exc_info=True)
        return None


def _plain_json(raw: str, label: str) -> dict | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("failed to parse %s json state", label, exc_info=True)
        return None


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


def _int_or_none(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _xhs_image_size(image: dict) -> tuple[int | None, int | None]:
    return _int_or_none(image.get("width")), _int_or_none(image.get("height"))


def _html_media_size(tag) -> tuple[int | None, int | None]:
    width = _int_or_none(tag.get("width") or tag.get("data-width"))
    height = _int_or_none(tag.get("height") or tag.get("data-height"))
    return width, height


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


def _first_safe_url(urls: list[str]) -> str | None:
    for url in urls:
        safe = _safe_asset_url(url)
        if safe:
            return safe
    return None


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


def _looks_like_audio_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(_AUDIO_EXT) or "/ies-music/" in path or "music" in parsed.netloc.lower()


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
    raw = _render_with_httpx(
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


def _best_srcset_url(srcset: str | None) -> str | None:
    """Pick the highest-resolution candidate from a srcset.

    srcset has NO ordering requirement, so the old "take the last comma item"
    heuristic frequently grabbed the SMALLEST image. Parse each candidate's
    width ('1024w') or density ('2x') descriptor and keep the max; split on any
    whitespace run so tab/newline-separated descriptors don't leak into the URL.
    """
    if not srcset:
        return None
    best: str | None = None
    best_score = -1.0
    for cand in srcset.split(","):
        toks = cand.split()
        if not toks:
            continue
        url = toks[0]
        score = 1.0  # no descriptor → treat as 1x
        for d in toks[1:]:
            if d.endswith("w"):
                try:
                    score = float(d[:-1])
                except ValueError:
                    continue
                break
            if d.endswith("x"):
                try:
                    score = float(d[:-1])
                except ValueError:
                    continue
        if score > best_score:
            best_score = score
            best = url
    return best


def _is_noise_url(url: str) -> bool:
    """True for obvious site chrome (sprites, icons, tracking pixels, logos,
    avatars) that pollute a reference-asset list. Strong signals only."""
    return bool(_NOISE_RE.search(urlparse(url).path))


def _jsonld_media_urls(node, media_ctx: bool = False) -> list[str]:
    """Collect media URLs from a parsed JSON-LD graph. Values under media keys
    (image/thumbnailUrl/contentUrl/poster), incl. nested ImageObject/VideoObject
    ``url`` fields, are harvested; bare top-level ``url`` (the page link) is not."""
    out: list[str] = []
    if isinstance(node, str):
        if media_ctx and node.startswith(("http://", "https://", "//")):
            out.append(node)
    elif isinstance(node, list):
        for v in node:
            out += _jsonld_media_urls(v, media_ctx)
    elif isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if kl in _LD_MEDIA_KEYS:
                out += _jsonld_media_urls(v, True)
            elif kl == "url" and media_ctx:
                out += _jsonld_media_urls(v, True)
            else:
                out += _jsonld_media_urls(v, False)
    return out


def extract_assets(html: str, base_url: str) -> list[dict]:
    """Generic page extractor used for any host without a dedicated platform
    extractor. Mines DOM media (<img>/srcset, <picture>/<source>, <video>),
    structured data (JSON-LD, og/twitter/itemprop meta, <link> hints) and SSR
    state blobs (__NEXT_DATA__/__NUXT__/JSON), filters obvious chrome, and
    returns content first / decorative backgrounds last."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    img_assets: list[dict] = []
    video_assets: list[dict] = []
    meta_assets: list[dict] = []
    ssr_assets: list[dict] = []
    bg_assets: list[dict] = []

    def add(bucket: list[dict], url: str | None, type_: str,
            thumb: str | None = None, width: int | None = None,
            height: int | None = None):
        url = _safe_asset_url(url)
        if not url or url in seen:
            return
        if _looks_like_audio_url(url):
            return
        # Let a recognizable extension correct the tag-derived type (e.g. a
        # <source>/lazy-attr pointing at .mp4 typed as image, or vice versa).
        type_ = _classify(url) or type_
        if _is_noise_url(url):
            return
        if width and height and width < _MIN_KEEP_DIM and height < _MIN_KEEP_DIM:
            return
        thumb = _safe_asset_url(thumb) if thumb else None
        seen.add(url)
        bucket.append({"type": type_, "url": url, "thumb": thumb,
                       "width": width, "height": height})

    # <img> with common lazy-load attributes + best-resolution srcset
    for img in soup.find_all("img"):
        width, height = _html_media_size(img)
        for attr in _LAZY_IMG_ATTRS:
            add(img_assets, _abs(base_url, img.get(attr)), "image",
                width=width, height=height)
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            add(img_assets, _abs(base_url, _best_srcset_url(srcset)), "image",
                width=width, height=height)

    # <video> + its <source> children (poster as thumb)
    for vid in soup.find_all("video"):
        poster = _abs(base_url, vid.get("poster"))
        width, height = _html_media_size(vid)
        add(video_assets, _abs(base_url, vid.get("src")), "video", poster,
            width=width, height=height)
        for src in vid.find_all("source"):
            add(video_assets, _abs(base_url, src.get("src")), "video", poster,
                width=width, height=height)

    # <picture>/<source srcset> and any other top-level <source> (skip video
    # sources, already handled above; dedup makes the overlap a no-op anyway)
    for src in soup.find_all("source"):
        stype = (src.get("type") or "").lower()
        if stype.startswith("video"):
            continue
        ss = src.get("srcset") or src.get("data-srcset")
        if ss:
            add(img_assets, _abs(base_url, _best_srcset_url(ss)), "image")
        elif src.get("src"):
            add(img_assets, _abs(base_url, src.get("src")), "image")

    # Structured data: JSON-LD ImageObject/VideoObject etc.
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for u in _jsonld_media_urls(data):
            add(meta_assets, _abs(base_url, u.replace("\\/", "/")),
                _classify(u) or "image")

    # og:image / twitter:image / itemprop=image  +  og:video / twitter:player
    for m in soup.find_all("meta"):
        prop = (m.get("property") or m.get("name") or m.get("itemprop") or "").lower()
        content = m.get("content")
        if prop in ("og:image", "og:image:url", "og:image:secure_url",
                    "twitter:image", "twitter:image:src", "image"):
            add(meta_assets, _abs(base_url, content), "image")
        elif prop in ("og:video", "og:video:url", "og:video:secure_url",
                      "twitter:player:stream"):
            add(meta_assets, _abs(base_url, content), "video")

    # <link rel=image_src> share image and <link rel=preload as=image> LCP hint
    for ln in soup.find_all("link"):
        rel = ln.get("rel")
        rel = " ".join(rel).lower() if isinstance(rel, list) else str(rel or "").lower()
        if "image_src" in rel:
            add(meta_assets, _abs(base_url, ln.get("href")), "image")
        if "preload" in rel and (ln.get("as") or "").lower() == "image":
            add(meta_assets, _abs(base_url, ln.get("href")), "image")
            if ln.get("imagesrcset"):
                add(meta_assets, _abs(base_url, _best_srcset_url(ln["imagesrcset"])),
                    "image")

    # SSR hydration blobs (__NEXT_DATA__/__NUXT__/JSON): harvest only URLs whose
    # extension we recognize, so page links / API paths don't pollute results.
    for script in soup.find_all("script"):
        sid = (script.get("id") or "").lower()
        stype = (script.get("type") or "").lower()
        txt = script.string or script.get_text() or ""
        if not txt:
            continue
        head = txt[:2000]
        is_state = (
            sid in ("__next_data__", "__nuxt__", "__initial_state__")
            or stype == "application/json"
            or "window.__" in head
        )
        if not is_state:
            continue
        for mm in _URL_IN_JSON_RE.finditer(txt[:_SSR_SCAN_MAX]):
            u = mm.group(0).replace("\\/", "/")
            t = _classify(u)
            if t:
                add(ssr_assets, u, t)

    # CSS background-image (inline style + <style> blocks) — decorative, last
    for el in soup.find_all(style=True):
        for mm in _BG_RE.finditer(el["style"]):
            add(bg_assets, _abs(base_url, mm.group(2)), "image")
    for style in soup.find_all("style"):
        for mm in _BG_RE.finditer(style.get_text() or ""):
            add(bg_assets, _abs(base_url, mm.group(2)), "image")

    # content first (gallery in reading order), structured/meta next, decorative
    # backgrounds last; sprites/pixels already filtered so nothing junk leads.
    assets = img_assets + video_assets + meta_assets + ssr_assets + bg_assets
    return assets[:max(1, int(settings.parse_max_assets or 1))]


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


def _js_unquote(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return bytes(raw, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return raw


def _extract_js_string_assignment(html: str, name: str) -> str | None:
    m = re.search(rf"\b{re.escape(name)}\s*=\s*{_JS_STRING_RE}", html)
    if not m:
        return None
    return _js_unquote(m.group(1) or m.group(2))


def _jd_sku_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _JD_SKU_RE.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _taobao_item_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _TAOBAO_ITEM_ID_RE.search(url)
    return m.group(1) if m else None


def _jd_return_url(page: RenderedPage) -> str | None:
    parsed = urlparse(page.final_url)
    qs = parse_qs(parsed.query)
    ret = (qs.get("returnurl") or qs.get("returnUrl") or [""])[0]
    if ret:
        return assert_safe_url(unquote(ret))
    return None


def _taobao_target_url(page: RenderedPage) -> str | None:
    target = _extract_js_string_assignment(page.html, "url")
    if target and _is_taobao_host(urlparse(target).hostname):
        return assert_safe_url(html_unescape(target))
    for match in _URL_IN_TEXT_RE.finditer(page.html[:20_000]):
        candidate = html_unescape(match.group(0).replace("\\/", "/"))
        if _is_taobao_host(urlparse(candidate).hostname):
            return assert_safe_url(candidate)
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


def _extract_jd_assets(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen: set[str] = set()

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


def _run_jd(url: str) -> list[dict]:
    first = _render_page_with_httpx(
        url,
        timeout=12.0,
        max_read_seconds=_GENERIC_MAX_READ_SECONDS,
        max_body_bytes=_GENERIC_MAX_BODY_BYTES,
    )
    pages: list[RenderedPage] = [first]

    candidate_url = _jd_return_url(first)
    sku = _jd_sku_from_url(candidate_url or first.final_url or url)
    if sku:
        pc_url = f"https://item.jd.com/{sku}.html"
        if pc_url not in {p.final_url for p in pages}:
            try:
                pages.append(_render_page_with_httpx(
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


def _run_taobao(url: str) -> list[dict]:
    first = _render_page_with_httpx(
        url,
        timeout=12.0,
        max_read_seconds=_GENERIC_MAX_READ_SECONDS,
        max_body_bytes=_GENERIC_MAX_BODY_BYTES,
    )
    pages: list[RenderedPage] = [first]

    target = _taobao_target_url(first)
    item_id = _taobao_item_id_from_url(target or first.final_url or url)
    for candidate in (
        target,
        f"https://item.taobao.com/item.htm?id={item_id}" if item_id else None,
        f"https://h5.m.taobao.com/awp/core/detail.htm?id={item_id}" if item_id else None,
    ):
        if not candidate or candidate in {p.final_url for p in pages}:
            continue
        try:
            pages.append(_render_page_with_httpx(
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
    if blocked:
        raise ValueError(blocked)
    return []


# --- Platform registry ------------------------------------------------------
# Each platform is one self-contained unit: a host matcher, a ``run`` that
# fetches + extracts (encapsulating any httpx→render fallback), a timeout hint,
# and an empty-result message. Adding a platform = append one entry here; the
# generic web fallback is the catch-all last entry. parse_url stays trivial.
def _timeout_hint(platform: str = "") -> str:
    prefix = f"{platform}页面" if platform else "页面"
    return (f"{prefix}加载超时，通常是目标站安全校验或网络风控导致，"
            "请稍后重试或更换可公开访问链接")


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
            html = _render_with_httpx(
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
        html = _render_with_playwright(candidate)
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
    html = _render_with_playwright(url)
    if html is None:
        raise ValueError(_timeout_hint("抖音"))
    assets = _extract_douyin_assets(html, url)
    if not assets and direct_error:
        raise ValueError(f"未获取到抖音作品素材，作品详情返回:{direct_error}")
    return assets


def _run_weixin(url: str) -> list[dict]:
    html = _render_with_httpx(url, max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                              max_body_bytes=_GENERIC_MAX_BODY_BYTES)
    return _extract_weixin_assets(html, url)


def _run_x(url: str) -> list[dict]:
    def post_media_only(assets: list[dict]) -> list[dict]:
        return [asset for asset in assets if _is_x_post_media_url(asset.get("url"))]

    try:
        html = _render_with_httpx(url, max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                                  max_body_bytes=_GENERIC_MAX_BODY_BYTES)
        assets = post_media_only(extract_assets(html, url))
        if assets:
            return assets
    except httpx.HTTPError as e:
        log.warning("x/twitter html fetch failed, falling back to render: %s", e)
    html = _render_with_playwright(url)
    if html is None:
        raise ValueError(_timeout_hint("X"))
    return post_media_only(extract_assets(html, url))


def _run_generic(url: str) -> list[dict]:
    html = _render_with_httpx(url, max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                              max_body_bytes=_GENERIC_MAX_BODY_BYTES)
    return extract_assets(html, url)


@dataclass(frozen=True)
class PlatformExtractor:
    name: str
    matches: Callable[[str], bool]
    run: Callable[[str], list[dict]]
    timeout_hint: str
    empty_msg: str


_REGISTRY: list[PlatformExtractor] = [
    PlatformExtractor(
        "小红书", _is_xiaohongshu_host, _run_xiaohongshu, _timeout_hint("小红书"),
        "未获取到小红书笔记素材，可能被安全校验、登录态或笔记可见性限制拦截"),
    PlatformExtractor(
        "抖音", _is_douyin_host, _run_douyin, _timeout_hint("抖音"),
        "未获取到抖音作品素材，可能被安全校验、登录态或作品可见性限制拦截"),
    PlatformExtractor(
        "公众号", _is_weixin_host, _run_weixin, _timeout_hint("公众号"),
        "未获取到公众号文章素材，可能是图片仅在客户端加载或文章可见性受限"),
    PlatformExtractor(
        "X", _is_x_host, _run_x, _timeout_hint("X"),
        "未获取到 X 帖子素材，可能被登录态、访问频率或帖子可见性限制拦截"),
    PlatformExtractor(
        "京东", _is_jd_host, _run_jd, _timeout_hint("京东"),
        "未获取到京东商品图片，可能被安全校验、登录态或商品可见性限制拦截"),
    PlatformExtractor(
        "淘宝", _is_taobao_host, _run_taobao, _timeout_hint("淘宝"),
        "未获取到淘宝商品图片，可能被安全校验、登录态或商品可见性限制拦截"),
    # catch-all generic web fallback — must stay last
    PlatformExtractor(
        "网页", lambda _host: True, _run_generic, _timeout_hint(),
        "未在该页面发现可用的图片/视频资源"),
]


def _select(host: str) -> PlatformExtractor:
    for platform in _REGISTRY:
        if platform.matches(host):
            return platform
    return _REGISTRY[-1]  # generic is always last; defensive fallback


def parse_url(url: str) -> list[dict]:
    """Public entry: returns a deduped asset list or raises on hard failure.

    Dispatch is table-driven (``_REGISTRY``); each platform's ``run`` owns its
    fetch strategy. Network failures are normalised to clear ValueError hints so
    the API never leaks a raw httpx exception string; SsrfError propagates."""
    assert_safe_url(url)
    host = urlparse(url).hostname or ""
    platform = _select(host)
    try:
        assets = platform.run(url)
    except SsrfError:
        raise
    except httpx.TimeoutException:
        raise ValueError(platform.timeout_hint)
    except httpx.HTTPStatusError as e:
        raise ValueError(f"目标页面无法访问(HTTP {e.response.status_code})")
    except httpx.HTTPError:
        raise ValueError("无法连接到该网站，请检查链接是否正确")
    if not assets:
        raise ValueError(platform.empty_msg)
    return assets
