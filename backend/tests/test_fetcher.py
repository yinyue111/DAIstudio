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
    assert fetcher._select("x.com").name == "X"
    assert fetcher._select("mobile.twitter.com").name == "X"
    assert fetcher._select("mp.weixin.qq.com").name == "公众号"
    assert fetcher._select("3.cn").name == "京东"
    assert fetcher._select("item.jd.com").name == "京东"
    assert fetcher._select("e.tb.cn").name == "淘宝"
    assert fetcher._select("item.taobao.com").name == "淘宝"
    assert fetcher._select("detail.tmall.com").name == "淘宝"
    # suffix spoof and arbitrary hosts fall through to the generic catch-all
    assert fetcher._select("evilxiaohongshu.com").name == "网页"
    assert fetcher._select("eviljd.com").name == "网页"
    assert fetcher._select("notx.com").name == "网页"
    assert fetcher._select("example.com").name == "网页"


def test_extract_first_url_from_copied_ecommerce_share_text():
    text = (
        "【淘宝】7天无理由退货 https://e.tb.cn/h.RsKuM6YesPztUCt?tk=ZTsRgiCip4j "
        "CA381 「DAMAH黑魔法一次性洗脸巾」"
    )

    assert fetcher.extract_first_url(text) == "https://e.tb.cn/h.RsKuM6YesPztUCt?tk=ZTsRgiCip4j"


def test_jd_extractor_reads_product_gallery_and_upgrades_size():
    html = """
    <html><body>
      <script>
        var pageConfig = {
          src: 'jfs/t20280614/454557/27/9867/153532/main.jpg',
          imageList: [
            "jfs/t20280614/454557/27/9867/153532/main.jpg",
            "jfs/t1/424575/2/13940/68021/detail.jpg"
          ]
        };
      </script>
      <img id="spec-img"
           data-origin="//img13.360buyimg.com/n1/s720x720_jfs/t20280614/454557/27/9867/153532/main.jpg"
           data-url="jfs/t20280614/454557/27/9867/153532/main.jpg">
    </body></html>
    """

    assets = fetcher._extract_jd_assets(html, "https://item.jd.com/10210432712118.html")

    assert [a["url"] for a in assets][:2] == [
        "https://img13.360buyimg.com/n1/jfs/t20280614/454557/27/9867/153532/main.jpg",
        "https://img13.360buyimg.com/n1/jfs/t1/424575/2/13940/68021/detail.jpg",
    ]
    assert all("s720x720" not in a["url"] for a in assets)


def test_jd_extractor_reads_main_video(monkeypatch):
    def fake_video(video_id):
        assert video_id == "3573240983"
        return {
            "url": "https://vod.300hu.com/path/product-demo.mp4?source=1",
            "thumb": "https://img.300hu.com/path/poster.jpg",
            "width": 1280,
            "height": 720,
        }

    monkeypatch.setattr(fetcher, "_fetch_jd_video_asset", fake_video)
    html = """
    <html><body>
      <script>var pageConfig = {
        imageAndVideoJson: {"mainVideoId":"3573240983"},
        imageList: ["jfs/t1/product.jpg"]
      };</script>
      <div class="video" id="v-video" data-vu="3573240983"></div>
    </body></html>
    """

    assets = fetcher._extract_jd_assets(html, "https://item.jd.com/100108209840.html")

    assert assets[0] == {
        "type": "video",
        "url": "https://vod.300hu.com/path/product-demo.mp4?source=1",
        "thumb": "https://img.300hu.com/path/poster.jpg",
        "width": 1280,
        "height": 720,
    }
    assert assets[1]["url"] == "https://img13.360buyimg.com/n1/jfs/t1/product.jpg"


def test_jd_video_api_jsonp_parsed(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_render_with_httpx",
        lambda *_args, **_kwargs: (
            'jdVideo({"duration":10,"code":0,'
            '"imageUrl":"https://img.300hu.com/poster.jpg",'
            '"playUrl":"https://vod.300hu.com/video.mp4?source=1",'
            '"extInfo":{"vwidth":1280,"vheight":720}})'
        ),
    )

    asset = fetcher._fetch_jd_video_asset("3573240983")

    assert asset == {
        "url": "https://vod.300hu.com/video.mp4?source=1",
        "thumb": "https://img.300hu.com/poster.jpg",
        "width": 1280,
        "height": 720,
    }


def test_jd_fallback_keeps_sku_from_original_url(monkeypatch):
    calls = []

    def fake_page(url, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            return fetcher.RenderedPage(
                html="<html><title>京东验证</title></html>",
                final_url="https://cfe.m.jd.com/privatedomain/risk_handler/03101900/",
            )
        return fetcher.RenderedPage(
            html='<script>var pageConfig = { imageList: ["jfs/t1/product.jpg"] };</script>',
            final_url=url,
        )

    monkeypatch.setattr(fetcher, "_render_page_with_httpx", fake_page)

    assets = fetcher.parse_url("https://item.jd.com/100108209840.html?from=card")

    assert calls[1] == "https://item.jd.com/100108209840.html"
    assert assets[0]["url"] == "https://img13.360buyimg.com/n1/jfs/t1/product.jpg"


def test_jd_short_link_falls_back_to_pc_product_page(monkeypatch):
    calls = []

    def fake_page(url, **_kwargs):
        calls.append(url)
        if url.startswith("https://3.cn/"):
            return fetcher.RenderedPage(
                html="<title>京东验证</title>",
                final_url=(
                    "https://cfe.m.jd.com/privatedomain/risk_handler/03101900/"
                    "?returnurl=https%3A%2F%2Fitem.m.jd.com%2Fproduct%2F10210432712118.html"
                ),
            )
        return fetcher.RenderedPage(
            html='<script>var pageConfig={imageList:["jfs/t1/product.jpg"]};</script>',
            final_url="https://item.jd.com/10210432712118.html",
        )

    monkeypatch.setattr(fetcher, "_render_page_with_httpx", fake_page)

    assets = fetcher.parse_url("https://3.cn/2Tswk-cG?jkl=@S6Pp90buMlXo@")

    assert calls == [
        "https://3.cn/2Tswk-cG?jkl=@S6Pp90buMlXo@",
        "https://item.jd.com/10210432712118.html",
    ]
    assert assets[0]["url"] == "https://img13.360buyimg.com/n1/jfs/t1/product.jpg"


def test_taobao_short_link_uses_embedded_target_url(monkeypatch):
    calls = []

    def fake_page(url, **_kwargs):
        calls.append(url)
        if url.startswith("https://e.tb.cn/"):
            return fetcher.RenderedPage(
                html=(
                    "<script>var url = 'https://item.taobao.com/item.htm?id=954144213943"
                    "&shareurl=true';</script>"
                ),
                final_url=url,
            )
        return fetcher.RenderedPage(
            html=(
                "<script>window.__DATA__={auctionImages:["
                "'//img.alicdn.com/imgextra/i1/abc/O1CN01main.jpg_430x430q90.jpg',"
                "'//img.alicdn.com/imgextra/i2/abc/O1CN01detail.jpg'"
                "]};</script>"
            ),
            final_url="https://item.taobao.com/item.htm?id=954144213943",
        )

    monkeypatch.setattr(fetcher, "_render_page_with_httpx", fake_page)

    assets = fetcher.parse_url("https://e.tb.cn/h.RsKuM6YesPztUCt?tk=ZTsRgiCip4j")

    assert calls[:2] == [
        "https://e.tb.cn/h.RsKuM6YesPztUCt?tk=ZTsRgiCip4j",
        "https://item.taobao.com/item.htm?id=954144213943&shareurl=true",
    ]
    assert assets[0]["url"] == "https://img.alicdn.com/imgextra/i1/abc/O1CN01main.jpg"


def test_taobao_h5_detail_fallback_extracts_product_images(monkeypatch):
    def fake_page(url, **_kwargs):
        return fetcher.RenderedPage(
            html='<a href="https://bixi.alicdn.com/punish/foo"></a><script>x5secdata=""</script>',
            final_url=url,
        )

    monkeypatch.setattr(fetcher, "_render_page_with_httpx", fake_page)
    monkeypatch.setattr(
        fetcher,
        "_render_with_httpx",
        lambda *_args, **_kwargs: (
            'mtopjsonp1({"api":"mtop.taobao.detail.data.get","v":"1.0",'
            '"ret":["SUCCESS::调用成功"],"data":{"item":{"images":['
            '"//img.alicdn.com/imgextra/i1/abc/O1CN01main.jpg_430x430q90.jpg",'
            '"//img.alicdn.com/imgextra/i2/abc/O1CN01detail.jpg"'
            ']}}})'
        ),
    )

    assets = fetcher.parse_url("https://detail.tmall.com/item.htm?id=991767518632")

    assert [a["url"] for a in assets[:2]] == [
        "https://img.alicdn.com/imgextra/i1/abc/O1CN01main.jpg",
        "https://img.alicdn.com/imgextra/i2/abc/O1CN01detail.jpg",
    ]


def test_taobao_h5_fallback_keeps_item_id_from_original_url(monkeypatch):
    def fake_page(_url, **_kwargs):
        return fetcher.RenderedPage(
            html='<a href="https://bixi.alicdn.com/punish/foo"></a><script>x5secdata=""</script>',
            final_url="https://login.taobao.com/member/login.jhtml?from=sm",
        )

    def fake_h5(item_id):
        assert item_id == "991767518632"
        return [{
            "type": "image",
            "url": "https://img.alicdn.com/imgextra/i1/abc/O1CN01main.jpg",
            "thumb": "https://img.alicdn.com/imgextra/i1/abc/O1CN01main.jpg",
            "width": None,
            "height": None,
        }]

    monkeypatch.setattr(fetcher, "_render_page_with_httpx", fake_page)
    monkeypatch.setattr(fetcher, "_fetch_taobao_h5_detail_assets", fake_h5)

    assets = fetcher.parse_url("https://detail.tmall.com/item.htm?id=991767518632")

    assert assets[0]["url"] == "https://img.alicdn.com/imgextra/i1/abc/O1CN01main.jpg"


def test_taobao_security_page_returns_clear_error(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_render_page_with_httpx",
        lambda url, **_kwargs: fetcher.RenderedPage(
            html='<a href="https://bixi.alicdn.com/punish/foo"></a><script>x5secdata=""</script>',
            final_url=url,
        ),
    )
    monkeypatch.setattr(
        fetcher,
        "_render_with_httpx",
        lambda *_args, **_kwargs: 'mtopjsonp1({"ret":["RGV587_ERROR::SM::login"],"data":{}})',
    )

    try:
        fetcher.parse_url("https://item.taobao.com/item.htm?id=954144213943")
    except ValueError as e:
        assert "淘宝返回安全校验" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_x_status_falls_back_to_rendered_twitter_image_meta(monkeypatch):
    calls = {"httpx": 0, "playwright": 0}
    rendered = """
    <html><head>
      <meta name="twitter:image"
            content="https://pbs.twimg.com/media/GrJQ-demo?format=jpg&name=large">
    </head><body></body></html>
    """

    monkeypatch.setattr(fetcher, "assert_safe_url", lambda url: url)

    def fake_httpx(_url, **_kwargs):
        calls["httpx"] += 1
        return "<html><head></head><body></body></html>"

    def fake_playwright(_url):
        calls["playwright"] += 1
        return rendered

    monkeypatch.setattr(fetcher, "_render_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_render_with_playwright", fake_playwright)

    assets = fetcher.parse_url("https://x.com/i/status/2069410454028296307")

    assert calls == {"httpx": 1, "playwright": 1}
    assert assets[0]["type"] == "image"
    assert assets[0]["url"] == "https://pbs.twimg.com/media/GrJQ-demo?format=jpg&name=large"


def test_x_status_filters_profile_images_and_prioritizes_post_media(monkeypatch):
    html = """
    <html><head>
      <meta name="twitter:image"
            content="https://pbs.twimg.com/media/post-image?format=jpg&name=large">
    </head><body>
      <img src="https://pbs.twimg.com/profile_images/123/avatar_400x400.jpg">
      <img src="https://pbs.twimg.com/profile_banners/123/banner.jpg">
    </body></html>
    """

    monkeypatch.setattr(fetcher, "assert_safe_url", lambda url: url)
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: html)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: None)

    assets = fetcher.parse_url("https://twitter.com/example/status/2069410454028296307")

    assert [asset["url"] for asset in assets] == [
        "https://pbs.twimg.com/media/post-image?format=jpg&name=large"
    ]


def test_x_status_dedupes_colon_large_media_variant(monkeypatch):
    html = """
    <html><head>
      <meta name="twitter:image" content="https://pbs.twimg.com/media/HLuuR4Ba4AAgVii.jpg">
    </head><body>
      <img src="https://pbs.twimg.com/media/HLuuR4Ba4AAgVii.jpg:large">
      <img src="https://pbs.twimg.com/media/HLuuSYZbkAA10Kp.jpg:orig">
    </body></html>
    """

    monkeypatch.setattr(fetcher, "assert_safe_url", lambda url: url)
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: html)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: None)

    assets = fetcher.parse_url("https://x.com/i/status/2070441131263893801")

    assert [asset["url"] for asset in assets] == [
        "https://pbs.twimg.com/media/HLuuR4Ba4AAgVii.jpg?format=jpg&name=large",
        "https://pbs.twimg.com/media/HLuuSYZbkAA10Kp.jpg?format=jpg&name=orig",
    ]


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


def test_x_post_media_url_filter_allows_video_paths_and_blocks_profile_chrome():
    # 真实形状的 X 视频/封面 URL:视频在 video.twimg.com 的 ext_tw_video /
    # amplify_video / tweet_video 路径,封面在 pbs.twimg.com 的 *_thumb 路径。
    allowed = [
        "https://video.twimg.com/ext_tw_video/1815696123456789012/pu/vid/avc1/720x1280/AbCdEf12345.mp4?tag=12",
        "https://video.twimg.com/amplify_video/1815696123456789012/vid/avc1/1280x720/XyZw9876.mp4?tag=14",
        "https://video.twimg.com/tweet_video/GhIjKlMnOpQ.mp4",
        "https://pbs.twimg.com/ext_tw_video_thumb/1815696123456789012/pu/img/AbCdEf12345.jpg",
        "https://pbs.twimg.com/amplify_video_thumb/1815696123456789012/img/XyZw9876.jpg",
        "https://pbs.twimg.com/tweet_video_thumb/GhIjKlMnOpQ.jpg",
        "https://pbs.twimg.com/media/GrJQ-demo?format=jpg&name=large",
    ]
    blocked = [
        "https://pbs.twimg.com/profile_images/123/avatar_400x400.jpg",
        "https://pbs.twimg.com/profile_banners/123/1700000000/1500x500",
        "https://abs.twimg.com/emoji/v2/svg/1f600.svg",
        "https://evil.example.com/ext_tw_video/1/pu/vid/avc1/720x1280/a.mp4",
        "https://eviltwimg.com/media/spoof.jpg",
    ]
    for url in allowed:
        assert fetcher._is_x_post_media_url(url), url
    for url in blocked:
        assert not fetcher._is_x_post_media_url(url), url


def test_x_status_extracts_video_post_media(monkeypatch):
    # 视频帖:og:video 指向 ext_tw_video mp4,封面是 ext_tw_video_thumb。
    # 修复前视频资源被 /media/ 前缀过滤为空,并触发误导性的登录态文案。
    html = """
    <html><head>
      <meta property="og:video"
            content="https://video.twimg.com/ext_tw_video/1815696123456789012/pu/vid/avc1/720x1280/AbCdEf12345.mp4?tag=12">
      <meta name="twitter:image"
            content="https://pbs.twimg.com/ext_tw_video_thumb/1815696123456789012/pu/img/AbCdEf12345.jpg">
    </head><body>
      <img src="https://pbs.twimg.com/profile_images/123/avatar_400x400.jpg">
    </body></html>
    """

    monkeypatch.setattr(fetcher, "assert_safe_url", lambda url: url)
    monkeypatch.setattr(fetcher, "_render_with_httpx", lambda _url, **_kwargs: html)
    monkeypatch.setattr(fetcher, "_render_with_playwright", lambda _url: None)

    assets = fetcher.parse_url("https://x.com/i/status/2069410454028296307")

    urls = {asset["url"] for asset in assets}
    assert (
        "https://video.twimg.com/ext_tw_video/1815696123456789012/pu/vid/avc1/720x1280/AbCdEf12345.mp4?tag=12"
        in urls
    )
    video_assets = [asset for asset in assets if asset["type"] == "video"]
    assert video_assets, assets
    assert all("profile_images" not in asset["url"] for asset in assets)
