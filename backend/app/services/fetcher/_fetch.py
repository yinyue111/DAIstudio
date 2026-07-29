"""Fetch infrastructure: SSRF-guarded httpx reads and Playwright rendering.

Platform modules must call into this module through the module object
(``_fetch._render_with_httpx(...)``) so tests can monkeypatch a single target.
"""
from __future__ import annotations

import logging
import threading
import time
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from ...config import settings
from ..safe_logging import redact_url_for_log
from ..ssrf import (
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

_GENERIC_MAX_BODY_BYTES = 2_000_000
_GENERIC_MAX_READ_SECONDS = 8.0


@dataclass(frozen=True)
class RenderedPage:
    html: str
    final_url: str


def _install_ssrf_route(page, page_url: str) -> None:
    """Proxy browser requests through our pinned httpx fetcher where possible.

    Chromium cannot pin DNS to the vetted IP from ``resolve_safe``. For render
    fallback we only need HTML/JS/JSON state, so abort heavy media subresources
    and fulfill navigations/scripts/XHRs via the SSRF-guarded httpx path.
    """
    # Imported inside the function to avoid a module-level import cycle
    # (``_urls`` is imported by every platform module, which import this one).
    from ._urls import _same_site_or_platform

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
            except Exception:  # noqa: BLE001
                log.debug("playwright browser close failed", exc_info=True)
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
    from .xiaohongshu import _xhs_block_message  # local import: cycle guard

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
    from .xiaohongshu import _xhs_block_message  # local import: cycle guard

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


def _timeout_hint(platform: str = "") -> str:
    prefix = f"{platform}页面" if platform else "页面"
    return (f"{prefix}加载超时，通常是目标站安全校验或网络风控导致，"
            "请稍后重试或更换可公开访问链接")
