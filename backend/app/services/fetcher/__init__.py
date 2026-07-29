"""Link scraper: fetch a page and extract image/video reference assets.

Dispatch is table-driven via ``_REGISTRY`` (see ``registry``). Each platform
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

Package layout (this file is a pure re-export compatibility layer — every name
that used to live in ``fetcher.py`` is still reachable as ``fetcher.<name>``):

* ``_urls``       — URL normalisation / classification / SSRF-safe filtering
* ``_json_state`` — SSR JSON / JSONP / JS-string state parsers
* ``_fetch``      — httpx + Playwright fetch infrastructure
* ``generic``     — generic web fallback extractor
* ``xiaohongshu`` / ``douyin`` / ``weixin`` / ``x_twitter`` — social platforms
* ``_product`` / ``jd`` / ``taobao`` — e-commerce product pages
* ``registry``    — platform table + ``parse_url``

Monkeypatching note: platform modules reach the fetch layer through the module
object (``_fetch._render_with_httpx(...)``), so tests patch
``fetcher._fetch.<name>`` — patching the re-export on this package would not be
seen by the callers.
"""
from __future__ import annotations

import logging

from ...config import settings
from ..safe_logging import redact_url_for_log
from ..ssrf import (
    MAX_REDIRECTS,
    SsrfError,
    assert_safe_url,
    pinned_client,
)
from . import (
    _fetch,
    _json_state,
    _product,
    _urls,
    douyin,
    generic,
    jd,
    registry,
    taobao,
    weixin,
    x_twitter,
    xiaohongshu,
)
from ._fetch import (
    _GENERIC_MAX_BODY_BYTES,
    _GENERIC_MAX_READ_SECONDS,
    _PARSE_BUSY_HINT,
    _PW_SEM,
    UA,
    RenderedPage,
    _charset_from_content_type,
    _inflate_capped,
    _install_ssrf_route,
    _read_body_capped,
    _render_page_with_httpx,
    _render_with_httpx,
    _render_with_playwright,
    _timeout_hint,
)
from ._json_state import (
    _JS_STRING_RE,
    _extract_js_string_assignment,
    _js_unquote,
    _json_from_jsonp,
    _json_like_state,
    _plain_json,
    _walk_strings,
)
from ._product import (
    _ecommerce_security_message,
    _is_ecommerce_host,
    _normalise_product_image_url,
    _push_product_asset,
    _push_product_video_asset,
    _strip_ecommerce_img_variant,
)
from ._urls import (
    _AUDIO_EXT,
    _IMG_EXT,
    _URL_IN_TEXT_RE,
    _VID_EXT,
    _abs,
    _canonical_url,
    _classify,
    _filter_safe_assets,
    _first_safe_url,
    _html_media_size,
    _int_or_none,
    _looks_like_audio_url,
    _normalise_asset_url,
    _safe_asset_url,
    _same_site_or_platform,
    extract_first_url,
)
from .douyin import (
    _DOUYIN_ID_RE,
    _DOUYIN_RENDER_RE,
    _douyin_aweme_id_from_url,
    _douyin_aweme_key,
    _douyin_cover_url,
    _douyin_detail_url,
    _douyin_image_url,
    _douyin_url_list,
    _douyin_video_url,
    _extract_douyin_assets,
    _extract_douyin_aweme_assets,
    _fetch_douyin_detail_assets,
    _is_douyin_host,
    _run_douyin,
    _walk_douyin_awemes,
)
from .generic import (
    _BG_RE,
    _LAZY_IMG_ATTRS,
    _LD_MEDIA_KEYS,
    _MIN_KEEP_DIM,
    _NOISE_RE,
    _SSR_SCAN_MAX,
    _URL_IN_JSON_RE,
    _best_srcset_url,
    _is_noise_url,
    _jsonld_media_urls,
    _run_generic,
    extract_assets,
)
from .jd import (
    _JD_SKU_RE,
    _extract_jd_assets,
    _fetch_jd_video_asset,
    _is_jd_host,
    _jd_return_url,
    _jd_sku_from_url,
    _jd_video_api_url,
    _jd_video_ids_from_html,
    _run_jd,
)
from .registry import _REGISTRY, PlatformExtractor, _select, parse_url
from .taobao import (
    _TAOBAO_ITEM_ID_RE,
    _extract_taobao_assets,
    _extract_taobao_assets_from_json,
    _fetch_taobao_h5_detail_assets,
    _is_taobao_host,
    _run_taobao,
    _taobao_item_id_from_url,
    _taobao_mtop_detail_url,
    _taobao_target_url,
)
from .weixin import (
    _extract_weixin_assets,
    _is_weixin_host,
    _run_weixin,
    _weixin_full_res,
)
from .x_twitter import (
    _X_POST_MEDIA_PATH_PREFIXES,
    _dedupe_x_media_assets,
    _is_x_host,
    _is_x_media_host,
    _is_x_post_media_url,
    _normalise_x_media_url,
    _run_x,
    _x_media_dedupe_key,
)
from .xiaohongshu import (
    _XHS_STATE_RE,
    _extract_xiaohongshu_assets,
    _is_xiaohongshu_host,
    _run_xiaohongshu,
    _xhs_block_message,
    _xhs_image_size,
    _xhs_image_urls,
    _xhs_note_thumb,
    _xhs_scene_url,
    _xhs_security_hint,
    _xhs_stream_url,
    _xhs_streams,
    _xhs_video_source,
)

log = logging.getLogger("fetcher")

# Preserve the old single-module patch surface while implementations live in
# child modules. Assigning fetcher.<name> updates every matching child binding.
from ..compat_facade import (  # noqa: E402
    install_assignment_forwarding as _install_assignment_forwarding,
)

_install_assignment_forwarding(
    __name__,
    (_fetch, _json_state, _product, _urls, douyin, generic, jd, registry, taobao,
     weixin, x_twitter, xiaohongshu),
)

__all__ = [
    # submodules
    "_fetch", "_json_state", "_product", "_urls", "douyin", "generic", "jd",
    "registry", "taobao", "weixin", "x_twitter", "xiaohongshu",
    # public API
    "extract_assets", "extract_first_url", "parse_url", "PlatformExtractor",
    "RenderedPage", "UA",
    # shared context re-exported for backwards compatibility
    "log", "settings", "redact_url_for_log",
    "MAX_REDIRECTS", "SsrfError", "assert_safe_url", "pinned_client",
    # _fetch
    "_GENERIC_MAX_BODY_BYTES", "_GENERIC_MAX_READ_SECONDS", "_PARSE_BUSY_HINT",
    "_PW_SEM", "_charset_from_content_type", "_inflate_capped",
    "_install_ssrf_route", "_read_body_capped", "_render_page_with_httpx",
    "_render_with_httpx", "_render_with_playwright", "_timeout_hint",
    # _urls
    "_AUDIO_EXT", "_IMG_EXT", "_URL_IN_TEXT_RE", "_VID_EXT", "_abs",
    "_canonical_url", "_classify", "_filter_safe_assets", "_first_safe_url",
    "_html_media_size", "_int_or_none", "_looks_like_audio_url",
    "_normalise_asset_url", "_safe_asset_url", "_same_site_or_platform",
    # _json_state
    "_JS_STRING_RE", "_extract_js_string_assignment", "_js_unquote",
    "_json_from_jsonp", "_json_like_state", "_plain_json", "_walk_strings",
    # generic
    "_BG_RE", "_LAZY_IMG_ATTRS", "_LD_MEDIA_KEYS", "_MIN_KEEP_DIM",
    "_NOISE_RE", "_SSR_SCAN_MAX", "_URL_IN_JSON_RE", "_best_srcset_url",
    "_is_noise_url", "_jsonld_media_urls", "_run_generic",
    # xiaohongshu
    "_XHS_STATE_RE", "_extract_xiaohongshu_assets", "_is_xiaohongshu_host",
    "_run_xiaohongshu", "_xhs_block_message", "_xhs_image_size",
    "_xhs_image_urls", "_xhs_note_thumb", "_xhs_scene_url",
    "_xhs_security_hint", "_xhs_stream_url", "_xhs_streams",
    "_xhs_video_source",
    # douyin
    "_DOUYIN_ID_RE", "_DOUYIN_RENDER_RE", "_douyin_aweme_id_from_url",
    "_douyin_aweme_key", "_douyin_cover_url", "_douyin_detail_url",
    "_douyin_image_url", "_douyin_url_list", "_douyin_video_url",
    "_extract_douyin_assets", "_extract_douyin_aweme_assets",
    "_fetch_douyin_detail_assets", "_is_douyin_host", "_run_douyin",
    "_walk_douyin_awemes",
    # weixin
    "_extract_weixin_assets", "_is_weixin_host", "_run_weixin",
    "_weixin_full_res",
    # x / twitter
    "_X_POST_MEDIA_PATH_PREFIXES", "_dedupe_x_media_assets", "_is_x_host",
    "_is_x_media_host", "_is_x_post_media_url", "_normalise_x_media_url",
    "_run_x", "_x_media_dedupe_key",
    # e-commerce
    "_ecommerce_security_message", "_is_ecommerce_host",
    "_normalise_product_image_url", "_push_product_asset",
    "_push_product_video_asset", "_strip_ecommerce_img_variant",
    "_JD_SKU_RE", "_extract_jd_assets", "_fetch_jd_video_asset", "_is_jd_host",
    "_jd_return_url", "_jd_sku_from_url", "_jd_video_api_url",
    "_jd_video_ids_from_html", "_run_jd",
    "_TAOBAO_ITEM_ID_RE", "_extract_taobao_assets",
    "_extract_taobao_assets_from_json", "_fetch_taobao_h5_detail_assets",
    "_is_taobao_host", "_run_taobao", "_taobao_item_id_from_url",
    "_taobao_mtop_detail_url", "_taobao_target_url",
    # registry
    "_REGISTRY", "_select",
]
