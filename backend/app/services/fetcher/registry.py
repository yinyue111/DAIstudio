"""Platform registry and the public ``parse_url`` entry point."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..ssrf import SsrfError, assert_safe_url
from ._fetch import _timeout_hint
from .douyin import _is_douyin_host, _run_douyin
from .generic import _run_generic
from .jd import _is_jd_host, _run_jd
from .taobao import _is_taobao_host, _run_taobao
from .weixin import _is_weixin_host, _run_weixin
from .x_twitter import _is_x_host, _run_x
from .xiaohongshu import _is_xiaohongshu_host, _run_xiaohongshu

# --- Platform registry ------------------------------------------------------
# Each platform is one self-contained unit: a host matcher, a ``run`` that
# fetches + extracts (encapsulating any httpx→render fallback), a timeout hint,
# and an empty-result message. Adding a platform = append one entry here; the
# generic web fallback is the catch-all last entry. parse_url stays trivial.


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
        "未获取到京东商品素材，可能被安全校验、登录态或商品可见性限制拦截"),
    PlatformExtractor(
        "淘宝", _is_taobao_host, _run_taobao, _timeout_hint("淘宝"),
        "未获取到淘宝商品素材，可能被安全校验、登录态或商品可见性限制拦截"),
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
