"""Volcengine Ark (Seedance) video adapter — payload shaping (no network)."""
from app.config import settings
from app.services import gateway


def test_ark_text_flags():
    t = gateway._ark_text("a cat walking",
                          {"resolution": "1080p", "duration": 5, "ratio": "16:9", "seed": 42})
    assert "a cat walking" in t
    assert "--resolution 1080p" in t
    assert "--duration 5" in t
    assert "--ratio 16:9" in t
    assert "--seed 42" in t
    assert "--watermark false" in t


def test_ark_content_image_to_video():
    c = gateway._ark_content("animate", {"first_frame_image": "http://x/y.png", "resolution": "720p"})
    assert c[0]["type"] == "text"
    assert c[1]["type"] == "image_url"
    assert c[1]["image_url"]["url"] == "http://x/y.png"


def test_ark_content_text_to_video():
    c = gateway._ark_content("a scene", {"resolution": "720p"})
    assert len(c) == 1 and c[0]["type"] == "text"


def test_submit_video_mock_when_unconfigured():
    # conftest sets MOCK_MODE=true -> effective_video_mock, no network call
    settings.mock_mode = True
    tid = gateway.submit_video("x", "doubao-seedance-1-5-pro-251215", {})
    assert tid.startswith("mock-")
    res = gateway.poll_video(tid, "doubao-seedance-1-5-pro-251215")
    assert res["status"] == "succeeded" and res.get("mock")


def test_ark_expired_status_is_failed(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://ark.example.com/api/v3")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "ark")
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda *_args, **_kwargs: {
            "status": "expired",
            "error": {"message": "task expired"},
        },
    )

    res = gateway.poll_video("task-1", "doubao-seedance-1-5-pro-251215")

    assert res["status"] == "failed"
    assert res["error"] == "task expired"


def test_generic_video_submit_preserves_first_frame(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["path"] = path
        seen["payload"] = payload
        return {"id": "task-1"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "animate",
        "video-model",
        {"duration": 5, "first_frame_image": "https://example.com/cover.jpg"},
        extra={"first_frame_field": "image_url"},
    )

    assert task_id == "task-1"
    assert seen["payload"]["image_url"] == "https://example.com/cover.jpg"
    assert "first_frame_image" not in seen["payload"]
