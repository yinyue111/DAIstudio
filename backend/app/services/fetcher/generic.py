"""Generic web fallback extractor used for hosts without a dedicated platform."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.services.fetcher import _fetch

from ...config import settings
from ._fetch import _GENERIC_MAX_BODY_BYTES, _GENERIC_MAX_READ_SECONDS
from ._urls import (
    _abs,
    _classify,
    _html_media_size,
    _looks_like_audio_url,
    _safe_asset_url,
)

_BG_RE = re.compile(r"url\((['\"]?)(.*?)\1\)", re.I)

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

# <img> lazy-load attribute variants that carry the real URL.
_LAZY_IMG_ATTRS = (
    "src", "data-src", "data-original", "data-lazy-src", "data-actualsrc",
    "data-echo", "data-img", "data-original-src",
)
# JSON-LD keys whose (string / list / nested-object) values are media URLs.
_LD_MEDIA_KEYS = ("image", "thumbnailurl", "contenturl", "poster")


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


def _run_generic(url: str) -> list[dict]:
    html = _fetch._render_with_httpx(url, max_read_seconds=_GENERIC_MAX_READ_SECONDS,
                                     max_body_bytes=_GENERIC_MAX_BODY_BYTES)
    return extract_assets(html, url)
