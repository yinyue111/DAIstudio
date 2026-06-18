"""Mock gateway + watermark pipeline (no network)."""
import base64
import time

from app.config import settings
from app.services import gateway
from app.services.watermark import image_ext, make_image_preview


def test_mock_image_and_preview():
    settings.mock_mode = True
    imgs = gateway.gen_image("a cat", "gpt-image-1", n=2, size="256x256")
    assert len(imgs) == 2
    assert imgs[0][:4] == b"\x89PNG"  # PNG magic

    preview, w, h = make_image_preview(imgs[0])
    assert preview[:4] == b"\x89PNG"
    assert (w, h) == (256, 256)


def test_image_ext_detects_jpeg():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="JPEG")
    assert image_ext(buf.getvalue()) == "jpg"


def test_mock_reverse_image():
    settings.mock_mode = True
    r = gateway.reverse_prompt("http://x/y.png", "gpt-4o")  # default target=image
    assert "structured" in r and r["final_text"]
    # richer set of dimensions than before
    assert len(r["structured"]) >= 8
    assert "光线" in r["structured"]


def test_mock_reverse_video():
    settings.mock_mode = True
    r = gateway.reverse_prompt("http://x/cover.png", "gpt-4o", target="video")
    s = r["structured"]
    # video-specific motion/temporal dimensions present
    assert "镜头运动" in s and "主体动作" in s and "时序分镜" in s
    assert r["final_text"]


def test_compose_final_fallback():
    # when the model omits final_text, we synthesize from all dimensions
    r = gateway._parse_structured('{"主体":"a cat","光线":"soft","负向":"text"}')
    assert "a cat" in r["final_text"] and "soft" in r["final_text"]
    assert "text" in r["final_text"]  # negative appended


def test_parse_structured_fenced():
    r = gateway._parse_structured('```json\n{"主体":"x","final_text":"hello"}\n```')
    assert r["structured"]["主体"] == "x"
    assert r["final_text"] == "hello"


def test_decode_image_response_downloads_each_url_once(monkeypatch):
    calls = []

    def fake_download(url):
        calls.append(url)
        return b"image-bytes"

    monkeypatch.setattr(gateway, "_download", fake_download)
    out = gateway._decode_image_response({"data": [{"url": "https://example.com/a.png"}]})

    assert out == [b"image-bytes"]
    assert calls == ["https://example.com/a.png"]


def test_image_edit_repeats_without_n(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = []

    def fake_request_json(method, url, *, headers, payload, timeout, retries):
        calls.append((method, url, dict(payload), timeout, retries))
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", fake_request_json)
    imgs = gateway.gen_image(
        "same style",
        "gpt-image-2",
        n=3,
        size="256x256",
        reference_image_url="http://example.com/ref.png",
        edit_path="/v1/images/edits",
    )

    assert len(imgs) == 3
    assert len(calls) == 3
    assert all(url.endswith("/v1/images/edits") for _method, url, _payload, _timeout, _retries in calls)
    assert all("n" not in payload for _method, _url, payload, _timeout, _retries in calls)
    assert all(payload["image"] == "http://example.com/ref.png"
               for _method, _url, payload, _timeout, _retries in calls)
    assert all(timeout == settings.image_gateway_timeout_seconds
               for _method, _url, _payload, timeout, _retries in calls)
    assert all(retries == 0 for _method, _url, _payload, _timeout, retries in calls)


def test_text_to_image_repeats_without_n(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = []

    def fake_request_json(method, url, *, headers, payload, timeout, retries):
        calls.append((method, url, dict(payload), timeout, retries))
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", fake_request_json)
    imgs = gateway.gen_image("a cat", "gpt-image-2", n=2, size="256x256")

    assert len(imgs) == 2
    assert len(calls) == 2
    assert all(url.endswith("/v1/images/generations")
               for _method, url, _payload, _timeout, _retries in calls)
    assert all("n" not in payload for _method, _url, payload, _timeout, _retries in calls)
    assert all(timeout == settings.image_gateway_timeout_seconds
               for _method, _url, _payload, timeout, _retries in calls)
    assert all(retries == 0 for _method, _url, _payload, _timeout, retries in calls)


def test_text_to_image_repeated_requests_run_in_parallel(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "image_gateway_parallelism", 4)
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = []

    def fake_request_json(method, url, *, headers, payload, timeout, retries):
        calls.append(time.monotonic())
        time.sleep(0.08)
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", fake_request_json)
    start = time.monotonic()
    imgs = gateway.gen_image("a cat", "gpt-image-2", n=4, size="256x256")
    elapsed = time.monotonic() - start

    assert len(imgs) == 4
    assert len(calls) == 4
    assert elapsed < 0.24
    assert max(calls) - min(calls) < 0.06


def test_text_to_image_returns_partial_successes(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "image_gateway_parallelism", 4)
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = {"n": 0}

    def flaky_request_json(method, url, *, headers, payload, timeout, retries):
        calls["n"] += 1
        if calls["n"] == 2:
            raise gateway.GatewayError("temporary account unavailable", transient=True)
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", flaky_request_json)
    imgs = gateway.gen_image("a cat", "gpt-image-2", n=4, size="256x256")

    assert len(imgs) == 3
    assert calls["n"] == 4
