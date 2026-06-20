import gzip

import httpx

from app.services import fetcher
from app.services.fetcher import extract_assets


def test_inflate_capped_bounds_a_decompression_bomb():
    # a ~10MB payload that gzips to a few KB must NOT inflate past the cap
    bomb = gzip.compress(b"A" * 10_000_000)
    out = fetcher._inflate_capped(bomb, "gzip", 1_000)
    assert len(out) <= 1_000
    # identity is passed through (and still capped)
    assert fetcher._inflate_capped(b"hello world", "identity", 5) == b"hello"
    # content mislabeled as compressed degrades to raw bytes, not an exception
    assert fetcher._inflate_capped(b"<html>", "gzip", 100) == b"<html>"


def test_best_srcset_url_handles_descriptors_and_whitespace():
    assert fetcher._best_srcset_url("a.jpg 480w, b.jpg 1024w") == "b.jpg"
    assert fetcher._best_srcset_url("a.jpg 1024w, b.jpg 480w") == "a.jpg"
    assert fetcher._best_srcset_url("a.jpg 1x, b.jpg 3x") == "b.jpg"
    assert fetcher._best_srcset_url("only.jpg") == "only.jpg"
    assert fetcher._best_srcset_url("") is None


def test_xiaohongshu_initial_state_assets_preferred():
    html = """
    <html><head>
      <meta property="og:image" content="https://avatar.xhscdn.com/not-the-note.png">
    </head><body>
      <img src="https://fe-platform.xhscdn.com/platform/icon.png">
      <script>window.__INITIAL_STATE__={
        "note": {
          "noteDetailMap": {
            "abc": {
              "note": {
                "imageList": [
                  {
                    "url": "",
                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3",
                    "urlPre": "http://sns-webpic-qc.xhscdn.com/preview-a!nd_prv_wlteh_jpg_3",
                    "infoList": [
                      {"imageScene": "WB_PRV", "url": "http://sns-webpic-qc.xhscdn.com/preview-a"},
                      {"imageScene": "WB_DFT", "url": "http://sns-webpic-qc.xhscdn.com/full-a"}
                    ]
                  },
                  {
                    "url": "",
                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/full-b!nd_dft_wlteh_jpg_3",
                    "urlPre": "http://sns-webpic-qc.xhscdn.com/preview-b!nd_prv_wlteh_jpg_3"
                  }
                ]
              }
            }
          }
        },
        "global": {"pwaAddDesktopPrompt": undefined}
      }</script>
    </body></html>
    """

    assets = fetcher._extract_xiaohongshu_assets(html)

    assert assets == [
        {
            "type": "image",
            "url": "https://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3",
            "thumb": "https://sns-webpic-qc.xhscdn.com/preview-a!nd_prv_wlteh_jpg_3",
            "width": None,
            "height": None,
        },
        {
            "type": "image",
            "url": "https://sns-webpic-qc.xhscdn.com/full-b!nd_dft_wlteh_jpg_3",
            "thumb": "https://sns-webpic-qc.xhscdn.com/preview-b!nd_prv_wlteh_jpg_3",
            "width": None,
            "height": None,
        },
    ]


def test_xiaohongshu_video_initial_state_assets_preferred():
    html = """
    <html><body>
      <script>window.__INITIAL_STATE__={
        "note": {
          "noteDetailMap": {
            "abc": {
              "note": {
                "type": "video",
                "imageList": [
                  {
                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/poster-full!nd_dft_wlteh_jpg_3",
                    "urlPre": "http://sns-webpic-qc.xhscdn.com/poster-preview!nd_prv_wlteh_jpg_3"
                  }
                ],
                "video": {
                  "media": {
                    "stream": {
                      "h264": [
                        {
                          "masterUrl": "http://sns-video-zl.xhscdn.com/stream/test_258.mp4?sign=abc",
                          "backupUrls": ["http://sns-bak-v8.xhscdn.com/stream/test_258.mp4"]
                        }
                      ],
                      "h265": [],
                      "h266": [],
                      "av1": []
                    }
                  }
                }
              }
            }
          }
        }
      }</script>
    </body></html>
    """

    assets = fetcher._extract_xiaohongshu_assets(html)

    assert assets[0] == {
        "type": "video",
        "url": "https://sns-video-zl.xhscdn.com/stream/test_258.mp4?sign=abc",
        "thumb": "https://sns-webpic-qc.xhscdn.com/poster-preview!nd_prv_wlteh_jpg_3",
        "width": None,
        "height": None,
        "thumb_width": None,
        "thumb_height": None,
    }
    assert assets[1] == {
        "type": "image",
        "url": "https://sns-webpic-qc.xhscdn.com/poster-full!nd_dft_wlteh_jpg_3",
        "thumb": "https://sns-webpic-qc.xhscdn.com/poster-preview!nd_prv_wlteh_jpg_3",
        "width": None,
        "height": None,
    }


def test_xiaohongshu_asset_dimensions_are_extracted():
    html = """
    <html><body>
      <script>window.__INITIAL_STATE__={
        "note": {
          "noteDetailMap": {
            "abc": {
              "note": {
                "type": "video",
                "imageList": [
                  {
                    "width": 1320,
                    "height": 1760,
                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/poster-full!nd_dft_wlteh_jpg_3",
                    "urlPre": "http://sns-webpic-qc.xhscdn.com/poster-preview!nd_prv_wlteh_jpg_3"
                  }
                ],
                "video": {
                  "media": {
                    "stream": {
                      "h264": [
                        {
                          "width": 720,
                          "height": 1280,
                          "masterUrl": "http://sns-video-zl.xhscdn.com/stream/test_258.mp4?sign=abc"
                        }
                      ]
                    }
                  }
                }
              }
            }
          }
        }
      }</script>
    </body></html>
    """

    assets = fetcher._extract_xiaohongshu_assets(html)

    assert assets[0]["type"] == "video"
    assert assets[0]["width"] == 720
    assert assets[0]["height"] == 1280
    assert assets[0]["thumb_width"] == 1320
    assert assets[0]["thumb_height"] == 1760
    assert assets[1]["type"] == "image"
    assert assets[1]["width"] == 1320
    assert assets[1]["height"] == 1760


def test_xiaohongshu_initial_state_allows_spaced_assignment():
    html = """
    <html><body>
      <script>window.__INITIAL_STATE__ = {
        "note": {
          "noteDetailMap": {
            "abc": {
              "note": {
                "imageList": [
                  {
                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3",
                    "urlPre": "http://sns-webpic-qc.xhscdn.com/preview-a!nd_prv_wlteh_jpg_3"
                  }
                ]
              }
            }
          }
        }
      }</script>
    </body></html>
    """

    assets = fetcher._extract_xiaohongshu_assets(html)

    assert assets[0]["url"] == "https://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"


def test_generic_html_asset_dimensions_are_extracted():
    html = """
    <html><body>
      <img src="/vertical.jpg" width="720" height="1280">
      <video src="/clip.mp4" poster="/poster.jpg" width="1080" height="1920"></video>
    </body></html>
    """

    assets = extract_assets(html, "https://example.com/page")

    assert assets[0] == {
        "type": "image",
        "url": "https://example.com/vertical.jpg",
        "thumb": None,
        "width": 720,
        "height": 1280,
    }
    assert assets[1] == {
        "type": "video",
        "url": "https://example.com/clip.mp4",
        "thumb": "https://example.com/poster.jpg",
        "width": 1080,
        "height": 1920,
    }


def test_extract_assets_filters_unsafe_media_urls():
    html = """
    <html><body>
      <img src="http://127.0.0.1/private.jpg">
      <img src="https://example.com/public.jpg">
      <video src="https://example.com/clip.mp4" poster="http://169.254.169.254/meta.jpg"></video>
    </body></html>
    """

    assets = extract_assets(html, "https://example.com/page")

    assert [a["url"] for a in assets] == [
        "https://example.com/public.jpg",
        "https://example.com/clip.mp4",
    ]
    assert assets[1]["thumb"] is None


def test_extract_assets_caps_result_count(monkeypatch):
    monkeypatch.setattr(fetcher.settings, "parse_max_assets", 3)
    html = "<html><body>" + "".join(
        f'<img src="https://example.com/{i}.jpg" width="800" height="800">'
        for i in range(10)
    ) + "</body></html>"

    assets = extract_assets(html, "https://example.com/page")

    assert len(assets) == 3
    assert [a["url"] for a in assets] == [
        "https://example.com/0.jpg",
        "https://example.com/1.jpg",
        "https://example.com/2.jpg",
    ]


def test_platform_extractors_share_parse_max_assets_cap(monkeypatch):
    monkeypatch.setattr(fetcher.settings, "parse_max_assets", 3)
    images = [
        {
            "urlDefault": f"https://sns-webpic-qc.xhscdn.com/full-{i}.jpg",
            "urlPre": f"https://sns-webpic-qc.xhscdn.com/preview-{i}.jpg",
        }
        for i in range(8)
    ]
    html = (
        "<script>window.__INITIAL_STATE__="
        + str({"note": {"noteDetailMap": {"abc": {"note": {"imageList": images}}}}})
          .replace("'", '"')
        + "</script>"
    )

    assets = fetcher._extract_xiaohongshu_assets(html)

    assert len(assets) == 3
    assert [a["url"] for a in assets] == [
        "https://sns-webpic-qc.xhscdn.com/full-0.jpg",
        "https://sns-webpic-qc.xhscdn.com/full-1.jpg",
        "https://sns-webpic-qc.xhscdn.com/full-2.jpg",
    ]


def test_weixin_extractor_shares_parse_max_assets_cap(monkeypatch):
    monkeypatch.setattr(fetcher.settings, "parse_max_assets", 2)
    html = (
        '<div id="js_content">'
        + "".join(
            f'<img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/{i}/640" data-type="jpeg">'
            for i in range(6)
        )
        + "</div>"
    )

    assets = fetcher._extract_weixin_assets(html, "https://mp.weixin.qq.com/s/abc")

    assert len(assets) == 2
    assert [a["url"] for a in assets] == [
        "https://mmbiz.qpic.cn/mmbiz_jpg/0/0",
        "https://mmbiz.qpic.cn/mmbiz_jpg/1/0",
    ]


def test_generic_srcset_picks_largest_not_last():
    # srcset has no ordering requirement; descending order must still pick the
    # biggest, not the trailing (smallest) candidate.
    html = (
        '<html><body><img srcset="https://example.com/big.jpg 1024w, '
        'https://example.com/small.jpg 480w"></body></html>'
    )
    assets = extract_assets(html, "https://example.com/p")
    assert assets[0]["url"] == "https://example.com/big.jpg"


def test_generic_picture_source_srcset_extracted():
    html = (
        '<html><body><picture>'
        '<source srcset="https://example.com/hero.avif 1200w, '
        'https://example.com/hero-2x.avif 2400w" type="image/avif">'
        '<img src="https://example.com/hero-fallback.jpg">'
        '</picture></body></html>'
    )
    urls = [a["url"] for a in extract_assets(html, "https://example.com/p")]
    assert "https://example.com/hero-2x.avif" in urls  # biggest <source> candidate
    assert "https://example.com/hero-fallback.jpg" in urls


def test_generic_jsonld_image_and_video_extracted():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Article",'
        '"image":{"@type":"ImageObject","url":"https://example.com/ld-hero.jpg"},'
        '"video":{"@type":"VideoObject",'
        '"contentUrl":"https://example.com/ld-clip.mp4",'
        '"thumbnailUrl":"https://example.com/ld-thumb.jpg"}}'
        '</script></head><body></body></html>'
    )
    types = {a["url"]: a["type"] for a in extract_assets(html, "https://example.com/p")}
    assert types.get("https://example.com/ld-hero.jpg") == "image"
    assert types.get("https://example.com/ld-clip.mp4") == "video"
    assert "https://example.com/ld-thumb.jpg" in types


def test_generic_og_video_and_secure_url_extracted():
    html = (
        '<html><head>'
        '<meta property="og:image:secure_url" content="https://example.com/secure-hero.jpg">'
        '<meta property="og:video" content="https://example.com/og-clip.mp4">'
        '</head><body></body></html>'
    )
    types = {a["url"]: a["type"] for a in extract_assets(html, "https://example.com/p")}
    assert types.get("https://example.com/secure-hero.jpg") == "image"
    assert types.get("https://example.com/og-clip.mp4") == "video"


def test_generic_filters_chrome_and_surfaces_content_first():
    html = (
        '<html><head><meta property="og:image" content="https://example.com/hero.jpg"></head>'
        '<body>'
        '<img src="https://example.com/sprite.png">'
        '<img src="https://tracker.example.com/pixel.gif">'
        '<img src="https://example.com/photo1.jpg">'
        '</body></html>'
    )
    urls = [a["url"] for a in extract_assets(html, "https://example.com/p")]
    assert "https://example.com/photo1.jpg" in urls
    assert "https://example.com/hero.jpg" in urls
    assert all("sprite" not in u and "pixel" not in u for u in urls)
    # the real content image must not be buried after the og meta image
    assert urls.index("https://example.com/photo1.jpg") < urls.index("https://example.com/hero.jpg")


def test_generic_next_data_ssr_harvest_with_escaped_slashes():
    html = (
        '<html><body>'
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"cover":"https://cdn.example.com/ssr-cover.jpg",'
        '"clip":"https:\\/\\/cdn.example.com\\/ssr.mp4"}'
        '</script></body></html>'
    )
    types = {a["url"]: a["type"] for a in extract_assets(html, "https://example.com/p")}
    assert types.get("https://cdn.example.com/ssr-cover.jpg") == "image"
    assert types.get("https://cdn.example.com/ssr.mp4") == "video"


def test_non_xiaohongshu_parse_does_not_use_playwright(monkeypatch):
    calls = {"playwright": 0}
    html = '<html><body><img src="/public.jpg"></body></html>'

    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: html)

    def fake_playwright(_url):
        calls["playwright"] += 1
        return html

    monkeypatch.setattr(fetcher, "_render_with_playwright", fake_playwright)

    assets = fetcher.parse_url("https://example.com/page")

    assert calls["playwright"] == 0
    assert assets[0]["url"] == "https://example.com/public.jpg"


def test_xiaohongshu_host_matching_does_not_accept_suffix_spoof(monkeypatch):
    calls = {"playwright": 0, "httpx": []}
    html = '<html><body><img src="/public.jpg"></body></html>'

    def fake_httpx(url, **kwargs):
        calls["httpx"].append((url, kwargs))
        return html

    def fake_playwright(_url):
        calls["playwright"] += 1
        return html

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", fake_playwright)

    assets = fetcher.parse_url("https://evilxiaohongshu.com/page")

    assert calls["playwright"] == 0
    assert calls["httpx"][0][1]["max_body_bytes"] == fetcher._GENERIC_MAX_BODY_BYTES
    assert assets[0]["url"] == "https://evilxiaohongshu.com/public.jpg"


def test_xiaohongshu_httpx_timeout_falls_back_to_render(monkeypatch):
    html = """
    <html><body>
      <script>window.__INITIAL_STATE__={
        "note": {
          "noteDetailMap": {
            "abc": {
              "note": {
                "imageList": [
                  {
                    "urlDefault": "http://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3",
                    "urlPre": "http://sns-webpic-qc.xhscdn.com/preview-a!nd_prv_wlteh_jpg_3"
                  }
                ]
              }
            }
          }
        }
      }</script>
    </body></html>
    """

    monkeypatch.setattr(
        fetcher,
        "_render_with_httpx",
        lambda _url, **_kwargs: (_ for _ in ()).throw(httpx.TimeoutException("slow")),
    )
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: html)

    assets = fetcher.parse_url("https://www.xiaohongshu.com/explore/abc")

    assert assets[0]["url"] == "https://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"


def test_xiaohongshu_httpx_ignores_platform_images_and_uses_render(monkeypatch):
    partial = """
    <html><body>
      <img src="https://fe-platform.xhscdn.com/platform/icon.png">
    </body></html>
    """
    rendered = """
    <html><body>
      <script>window.__INITIAL_STATE__={
        "note": {"noteDetailMap": {"abc": {"note": {"imageList": [
          {"urlDefault": "http://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"}
        ]}}}}
      }</script>
    </body></html>
    """

    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: partial)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: rendered)

    assets = fetcher.parse_url("https://www.xiaohongshu.com/explore/abc")

    assert assets[0]["url"] == "https://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"
    assert all("fe-platform.xhscdn.com" not in asset["url"] for asset in assets)


def test_xiaohongshu_timeout_checks_canonical_security_redirect(monkeypatch):
    seen = []

    def fake_httpx(url, **_kwargs):
        seen.append(url)
        if len(seen) == 1:
            raise httpx.TimeoutException("slow")
        raise ValueError("小红书返回安全校验:当前笔记暂时无法浏览")

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: None)

    try:
        fetcher.parse_url("https://www.xiaohongshu.com/explore/abc?xsec_token=deadbeef")
    except ValueError as e:
        assert "安全校验" in str(e)
        assert "上传图片参考" in str(e)
    else:
        raise AssertionError("expected ValueError")

    assert seen == [
        "https://www.xiaohongshu.com/explore/abc?xsec_token=deadbeef",
        "https://www.xiaohongshu.com/explore/abc",
    ]


def test_xiaohongshu_security_check_falls_back_to_render(monkeypatch):
    rendered = """
    <html><body>
      <script>window.__INITIAL_STATE__={
        "note": {"noteDetailMap": {"abc": {"note": {"imageList": [
          {"urlDefault": "http://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"}
        ]}}}}
      }</script>
    </body></html>
    """
    calls = []

    def fake_httpx(url, **_kwargs):
        calls.append(url)
        raise ValueError("小红书返回安全校验:当前笔记暂时无法浏览(error_code=300031)")

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: rendered)

    assets = fetcher.parse_url("https://www.xiaohongshu.com/explore/abc?xsec_token=deadbeef")

    assert assets[0]["url"] == "https://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"
    assert calls == [
        "https://www.xiaohongshu.com/explore/abc?xsec_token=deadbeef",
        "https://www.xiaohongshu.com/explore/abc",
    ]


def test_xiaohongshu_timeout_canonical_http_error_still_uses_render(monkeypatch):
    html = """
    <html><body>
      <script>window.__INITIAL_STATE__={
        "note": {"noteDetailMap": {"abc": {"note": {"imageList": [
          {"urlDefault": "http://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"}
        ]}}}}
      }</script>
    </body></html>
    """
    seen = []

    def fake_httpx(url, **_kwargs):
        seen.append(url)
        if len(seen) == 1:
            raise httpx.TimeoutException("slow")
        raise httpx.RemoteProtocolError("server disconnected")

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: html)

    assets = fetcher.parse_url("https://www.xiaohongshu.com/explore/abc?xsec_token=deadbeef")

    assert assets[0]["url"] == "https://sns-webpic-qc.xhscdn.com/full-a!nd_dft_wlteh_jpg_3"


def test_xiaohongshu_timeout_render_failure_returns_xhs_hint(monkeypatch):
    def fake_httpx(_url, **_kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: None)

    try:
        fetcher.parse_url("https://www.xiaohongshu.com/explore/abc?xsec_token=deadbeef")
    except ValueError as e:
        assert "小红书页面加载超时" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_xiaohongshu_empty_render_returns_xhs_specific_hint(monkeypatch):
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: "<html></html>")
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: "<html></html>")

    try:
        fetcher.parse_url("https://www.xiaohongshu.com/explore/abc")
    except ValueError as e:
        assert "未获取到小红书笔记素材" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_xiaohongshu_playwright_none_does_not_do_slow_httpx_fallback(monkeypatch):
    calls = []

    def fake_httpx(_url, **_kwargs):
        calls.append(_url)
        return "<html></html>"

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: None)

    try:
        fetcher.parse_url("https://www.xiaohongshu.com/explore/abc")
    except ValueError as e:
        assert "小红书页面加载超时" in str(e)
    else:
        raise AssertionError("expected ValueError")

    assert len(calls) == 1


def test_douyin_modal_id_is_extracted_from_search_url():
    url = (
        "https://www.douyin.com/jingxuan/acg/search/%E6%91%84%E5%BD%B1"
        "?aid=b3b678fb-4dd4-4d0c-a6a1-81057f6c8f25"
        "&modal_id=7568790973988678833&type=general"
    )

    assert fetcher._douyin_aweme_id_from_url(url) == "7568790973988678833"


def test_douyin_detail_extracts_image_album(monkeypatch):
    raw = """
    {
      "status_code": 0,
      "aweme_detail": {
        "aweme_id": "7568790973988678833",
        "video": {"play_addr": {"url_list": ["https://example.com/music.mp3"]}},
        "images": [
          {
            "url_list": ["https://example.com/a.jpg"],
            "download_url_list": ["https://example.com/a-watermark.jpg"],
            "width": 2160,
            "height": 3240
          },
          {
            "urlList": ["https://example.com/b.webp"],
            "width": "1080",
            "height": "1920"
          }
        ]
      }
    }
    """
    calls = {"playwright": 0}
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: raw)
    monkeypatch.setattr(
        fetcher,
        "_render_with_playwright",
        lambda _url: calls.__setitem__("playwright", calls["playwright"] + 1),
    )

    assets = fetcher.parse_url(
        "https://www.douyin.com/jingxuan/acg/search/foo?modal_id=7568790973988678833"
    )

    assert calls["playwright"] == 0
    assert assets == [
        {
            "type": "image",
            "url": "https://example.com/a.jpg",
            "thumb": "https://example.com/a.jpg",
            "width": 2160,
            "height": 3240,
        },
        {
            "type": "image",
            "url": "https://example.com/b.webp",
            "thumb": "https://example.com/b.webp",
            "width": 1080,
            "height": 1920,
        },
    ]


def test_douyin_detail_extracts_video_and_prefers_no_watermark(monkeypatch):
    raw = """
    {
      "status_code": 0,
      "aweme_detail": {
        "aweme_id": "7636700980432347259",
        "video": {
          "width": 1440,
          "height": 2560,
          "origin_cover": {"url_list": ["https://example.com/poster.jpg"]},
          "play_addr_no_watermark": {"url_list": ["https://example.com/no-watermark.mp4"]},
          "download_addr": {"url_list": ["https://example.com/download.mp4"]},
          "play_addr": {"url_list": ["https://example.com/watermark.mp4"]},
          "bit_rate": [
            {"play_addr": {"url_list": ["https://example.com/backup.mp4"]}}
          ]
        }
      }
    }
    """
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: raw)

    assets = fetcher.parse_url(
        "https://www.douyin.com/jingxuan/acg/search/foo?modal_id=7636700980432347259"
    )

    assert assets == [
        {
            "type": "video",
            "url": "https://example.com/no-watermark.mp4",
            "thumb": "https://example.com/poster.jpg",
            "width": 1440,
            "height": 2560,
        }
    ]


def test_douyin_detail_business_error_falls_back_to_rendered_state(monkeypatch):
    target_id = "7636700980432347259"
    detail_error = '{"status_code": 8, "status_msg": "need verify"}'
    rendered = f"""
    <html><body>
      <script id="RENDER_DATA" type="application/json">
      {{
        "loaderData": {{
          "other": {{
            "aweme_id": "7568790973988678833",
            "images": [{{"url_list": ["https://example.com/wrong.jpg"]}}]
          }},
          "target": {{
            "aweme_id": "{target_id}",
            "video": {{
              "width": 720,
              "height": 1280,
              "cover": {{"url_list": ["https://example.com/render-poster.jpg"]}},
              "play_addr": {{"url_list": ["https://example.com/render-video.mp4"]}}
            }}
          }}
        }}
      }}
      </script>
    </body></html>
    """
    calls = {"httpx": 0, "playwright": 0}

    def fake_httpx(_url, **_kwargs):
        calls["httpx"] += 1
        return detail_error

    def fake_playwright(_url):
        calls["playwright"] += 1
        return rendered

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", fake_playwright)

    assets = fetcher.parse_url(
        f"https://www.douyin.com/jingxuan/acg/search/foo?modal_id={target_id}"
    )

    assert calls == {"httpx": 1, "playwright": 1}
    assert assets[0]["type"] == "video"
    assert assets[0]["url"] == "https://example.com/render-video.mp4"
    assert assets[0]["thumb"] == "https://example.com/render-poster.jpg"
    assert assets[0]["width"] == 720
    assert assets[0]["height"] == 1280


def test_registry_routes_hosts_to_expected_platforms():
    assert fetcher._select("www.xiaohongshu.com").name == "小红书"
    assert fetcher._select("v.douyin.com").name == "抖音"
    assert fetcher._select("mp.weixin.qq.com").name == "公众号"
    # suffix spoof and arbitrary hosts fall through to the generic catch-all
    assert fetcher._select("evilxiaohongshu.com").name == "网页"
    assert fetcher._select("example.com").name == "网页"


def test_weixin_extracts_data_src_and_upgrades_resolution(monkeypatch):
    html = """
    <html><body>
      <div id="js_content">
        <img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/ABC/640?wx_fmt=jpeg" data-type="jpeg">
        <img src="" data-src="https://mmbiz.qpic.cn/mmbiz_png/DEF/0?wx_fmt=png">
      </div>
      <img data-src="https://mmbiz.qpic.cn/avatar/should-be-ignored/640">
    </body></html>
    """
    calls = {"playwright": 0}
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: html)
    monkeypatch.setattr(
        fetcher, "_render_with_playwright",
        lambda _url: calls.__setitem__("playwright", calls["playwright"] + 1),
    )

    assets = fetcher.parse_url("https://mp.weixin.qq.com/s/abcdef")
    urls = [a["url"] for a in assets]

    assert calls["playwright"] == 0  # 公众号 is pure SSR, no browser needed
    # /640 resized variant upgraded to the /0 original
    assert "https://mmbiz.qpic.cn/mmbiz_jpg/ABC/0?wx_fmt=jpeg" in urls
    assert "https://mmbiz.qpic.cn/mmbiz_png/DEF/0?wx_fmt=png" in urls
    # avatar/chrome outside #js_content is not scraped
    assert all("avatar" not in u for u in urls)


def test_douyin_render_prefers_target_keyed_only_by_group_id(monkeypatch):
    # target post is identified by group_id (no aweme_id); it must still sort to
    # the front over a sibling that carries an aweme_id.
    target_id = "7600000000000000001"
    rendered = f"""
    <html><body>
      <script id="RENDER_DATA" type="application/json">
      {{
        "a": {{"aweme_id": "7600000000000000999",
               "images": [{{"url_list": ["https://example.com/sibling.jpg"]}}]}},
        "b": {{"group_id": "{target_id}",
               "images": [{{"url_list": ["https://example.com/target.jpg"]}}]}}
      }}
      </script>
    </body></html>
    """
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs:
                        (_ for _ in ()).throw(ValueError("need verify")))
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: rendered)

    assets = fetcher.parse_url(
        f"https://www.douyin.com/video/{target_id}"
    )
    assert assets[0]["url"] == "https://example.com/target.jpg"
