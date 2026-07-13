"""Volcengine Ark (Seedance) video adapter — payload shaping (no network)."""
from app.config import settings
from app.services import gateway
from app.services.model_gateway_config import RuntimeGatewayConfig


def test_ark_text_flags():
    t = gateway._ark_text("a cat walking",
                          {"resolution": "1080p", "duration": 5, "ratio": "16:9", "seed": 42})
    assert "a cat walking" in t
    assert "--resolution 1080p" in t
    assert "--duration 5" in t
    assert "--ratio 16:9" in t
    assert "--seed 42" in t
    assert "--watermark false" in t


def test_ark_text_formats_duration_flag():
    t = gateway._ark_text("long ad sequence", {"duration": 15, "resolution": "1080p"})
    assert "--duration 15" in t


def test_ark_payload_preserves_negative_prompt():
    payload = gateway._ark_payload(
        "premium product video",
        "doubao-seedance-1-5-pro-251215",
        {
            "duration": 5,
            "resolution": "1080p",
            "negative_prompt": "包装文字乱码，Logo扭曲",
        },
    )

    assert payload["negative_prompt"] == "包装文字乱码，Logo扭曲"
    assert "负向约束：包装文字乱码，Logo扭曲" in payload["content"][0]["text"]


def test_ark_content_image_to_video():
    c = gateway._ark_content("animate", {"first_frame_image": "http://x/y.png", "resolution": "720p"})
    assert c[0]["type"] == "text"
    assert c[1]["type"] == "image_url"
    assert c[1]["image_url"]["url"] == "http://x/y.png"
    assert c[1]["role"] == "first_frame"


def test_ark_content_image_to_video_with_last_frame():
    c = gateway._ark_content(
        "animate",
        {
            "first_frame_image": "http://x/first.png",
            "last_frame_image": "http://x/last.png",
            "resolution": "720p",
        },
    )
    assert c[1]["image_url"]["url"] == "http://x/first.png"
    assert c[2]["image_url"]["url"] == "http://x/last.png"
    assert c[1]["role"] == "first_frame"
    assert c[2]["role"] == "last_frame"


def test_ark_content_keeps_same_image_for_distinct_first_and_last_frame_roles():
    c = gateway._ark_content(
        "locked product",
        {
            "first_frame_image": "http://x/product.png",
            "last_frame_image": "http://x/product.png",
        },
    )

    assert [item.get("role") for item in c[1:]] == ["first_frame", "last_frame"]


def test_ark_content_includes_distinct_character_reference():
    c = gateway._ark_content(
        "portrait rebuild",
        {
            "first_frame_image": "http://x/first.png",
            "last_frame_image": "http://x/first.png",
            "character_reference_image": "http://x/person.png",
            "resolution": "720p",
        },
    )
    assert [item["type"] for item in c] == ["text", "image_url", "image_url", "image_url"]
    assert c[1]["image_url"]["url"] == "http://x/first.png"
    assert c[1]["role"] == "first_frame"
    assert c[2]["image_url"]["url"] == "http://x/first.png"
    assert c[2]["role"] == "last_frame"
    assert c[3]["image_url"]["url"] == "http://x/person.png"
    assert c[3]["role"] == "reference_image"
    assert "人物身份参考" in c[0]["text"]


def test_ark_content_includes_distinct_style_reference():
    c = gateway._ark_content(
        "product restyle",
        {
            "first_frame_image": "http://x/product.png",
            "style_reference_image": "http://x/style.png",
            "resolution": "720p",
        },
    )

    assert [item["type"] for item in c] == ["text", "image_url", "image_url"]
    assert c[1]["image_url"]["url"] == "http://x/product.png"
    assert c[1]["role"] == "first_frame"
    assert c[2]["image_url"]["url"] == "http://x/style.png"
    assert c[2]["role"] == "reference_image"
    assert "风格参考" in c[0]["text"]


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


def test_video_rejects_incomplete_runtime_gateway_config(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    cfg = RuntimeGatewayConfig(
        use="video",
        provider="custom_openai",
        base_url="",
        api_key="",
        gateway_format="openai",
    )

    try:
        gateway.submit_video("animate", "video-model", {}, gateway_config=cfg)
    except gateway.GatewayError as e:
        assert "配置不完整" in str(e)
    else:
        raise AssertionError("incomplete video gateway config must not create a mock task")

    try:
        gateway.poll_video("external-task", "video-model", gateway_config=cfg)
    except gateway.GatewayError as e:
        assert "配置不完整" in str(e)
    else:
        raise AssertionError("incomplete video gateway config must not mock poll results")


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


def test_ark_unknown_status_with_error_is_failed(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://ark.example.com/api/v3")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "ark")
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda *_args, **_kwargs: {
            "status": "quota_exceeded",
            "error": {"code": "insufficient_quota", "message": "video quota exhausted"},
        },
    )

    res = gateway.poll_video("task-1", "doubao-seedance-1-5-pro-251215")

    assert res["status"] == "failed"
    assert res["error"] == "video quota exhausted"
    assert res["error_code"] == "insufficient_quota"
    assert res["raw_status"] == "quota_exceeded"


def test_ark_poll_extracts_nested_download_url(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://ark.example.com/api/v3")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "ark")
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "content": {
                "result": {
                    "download_url": "https://cdn.example.com/ark-download.mp4",
                },
            },
        },
    )

    res = gateway.poll_video("task-1", "doubao-seedance-1-5-pro-251215")

    assert res["status"] == "succeeded"
    assert res["url"] == "https://cdn.example.com/ark-download.mp4"


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


def test_generic_video_submit_accepts_grok_request_id(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_post",
        lambda *_args, **_kwargs: {"request_id": "grok-request-1"},
    )

    task_id = gateway.submit_video("animate", "grok-imagine-video-1.5", {})

    assert task_id == "grok-request-1"


def test_generic_video_submit_preserves_last_frame(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["payload"] = payload
        return {"id": "task-1"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "animate",
        "video-model",
        {
            "duration": 5,
            "first_frame_image": "https://example.com/cover.jpg",
            "last_frame_image": "https://example.com/end.jpg",
        },
        extra={"first_frame_field": "image_url", "last_frame_field": "end_image_url"},
    )

    assert task_id == "task-1"
    assert seen["payload"]["image_url"] == "https://example.com/cover.jpg"
    assert seen["payload"]["end_image_url"] == "https://example.com/end.jpg"
    assert "first_frame_image" not in seen["payload"]
    assert "last_frame_image" not in seen["payload"]


def test_generic_video_submit_preserves_character_reference(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["payload"] = payload
        return {"id": "task-1"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "portrait rebuild",
        "video-model",
        {
            "duration": 5,
            "character_reference_image": "https://example.com/person.jpg",
        },
        extra={"character_image_field": "person_image_url"},
    )

    assert task_id == "task-1"
    assert seen["payload"]["person_image_url"] == "https://example.com/person.jpg"
    assert "character_reference_image" not in seen["payload"]


def test_generic_video_submit_preserves_style_reference(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["payload"] = payload
        return {"id": "task-1"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "product restyle",
        "video-model",
        {
            "duration": 5,
            "style_reference_image": "https://example.com/style.jpg",
        },
        extra={"style_image_field": "style_image_url"},
    )

    assert task_id == "task-1"
    assert seen["payload"]["style_image_url"] == "https://example.com/style.jpg"
    assert "style_reference_image" not in seen["payload"]


def test_generic_video_submit_filters_internal_params(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    def fake_post(path, payload, timeout=120):
        seen["payload"] = payload
        return {"id": "task-1"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "animate",
        "video-model",
        {
            "duration": 5,
            "resolution": "720p",
            "ratio": "9:16",
            "request_id": "video-1",
            "target_resolution": "1080p",
            "target_duration": 8,
            "preview_resolution": "480p",
            "reference_image_url": "http://localhost/upload-preview.png",
            "_model_snapshot": {"model_id": "x"},
            "_video_request_id": "internal",
            "first_frame_image": "https://example.com/cover.jpg",
        },
        extra={"first_frame_field": "image_url"},
    )

    assert task_id == "task-1"
    assert seen["payload"] == {
        "model": "video-model",
        "prompt": "animate",
        "duration": 5,
        "resolution": "720p",
        "ratio": "9:16",
        "request_id": "video-1",
        "image_url": "https://example.com/cover.jpg",
    }


def test_video_ark_uses_per_model_gateway_config(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    cfg = RuntimeGatewayConfig(
        use="video",
        provider="volcengine_ark",
        base_url="https://ark.model.example.com/api/v3",
        api_key="ark-key",
        gateway_format="ark",
    )

    def fake_request(method, url, *, headers, json=None, timeout, retries):
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        class Resp:
            status_code = 200
            is_redirect = False
            def json(self):
                return {"id": "ark-task-1"}
        return Resp()

    monkeypatch.setattr(gateway, "_request", fake_request)
    task_id = gateway.submit_video(
        "animate",
        "doubao-seedance-x",
        {"duration": 5, "resolution": "1080p", "ratio": "9:16", "seed": 42},
        gateway_config=cfg,
    )

    assert task_id == "ark-task-1"
    assert seen["url"] == "https://ark.model.example.com/api/v3/contents/generations/tasks"
    assert seen["headers"]["Authorization"] == "Bearer ark-key"
    assert seen["json"]["model"] == "doubao-seedance-x"
    assert seen["json"]["resolution"] == "1080p"
    assert seen["json"]["duration"] == 5
    assert seen["json"]["ratio"] == "9:16"
    assert seen["json"]["seed"] == 42
    assert seen["json"]["watermark"] is False
    assert "--resolution 1080p" in seen["json"]["content"][0]["text"]


def test_generic_video_poll_accepts_data_dict(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "data": {"url": "https://cdn.example.com/video.mp4"},
        },
    )

    res = gateway.poll_video("task-1", "video-model")

    assert res["status"] == "succeeded"
    assert res["url"] == "https://cdn.example.com/video.mp4"


def test_generic_video_poll_preserves_provider_failure(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda *_args, **_kwargs: {
            "status": "rate_limited",
            "error": {"code": "rate_limited", "message": "provider rate limit"},
        },
    )

    res = gateway.poll_video("task-1", "video-model")

    assert res["status"] == "failed"
    assert res["error"] == "provider rate limit"
    assert res["error_code"] == "rate_limited"
    assert res["raw_status"] == "rate_limited"


def test_generic_video_poll_accepts_top_level_video_urls(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "video_url": "https://cdn.example.com/video-url.mp4",
            "download_url": "https://cdn.example.com/download-url.mp4",
        },
    )

    res = gateway.poll_video("task-1", "video-model")

    assert res["status"] == "succeeded"
    assert res["url"] == "https://cdn.example.com/video-url.mp4"


def test_generic_video_poll_url_encodes_task_id(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    seen = {}

    def fake_get(path, timeout=30, config=None):
        seen["path"] = path
        return {"status": "running"}

    monkeypatch.setattr(gateway, "_video_get", fake_get)

    res = gateway.poll_video("task/a?b#c", "video-model")

    assert seen["path"] == "/v1/videos/task%2Fa%3Fb%23c"
    assert res["status"] == "running"


def test_generic_video_request_id_lookup(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    seen = {}

    def fake_get(path, timeout=30, config=None):
        seen["path"] = path
        seen["timeout"] = timeout
        return {
            "data": {
                "task": {
                    "id": "task-from-request",
                    "state": "completed",
                    "result": {"video_url": "https://cdn.example.com/result.mp4"},
                }
            }
        }

    monkeypatch.setattr(gateway, "_video_get", fake_get)

    found = gateway.find_video_by_request_id(
        "req/a?b#c",
        "video/model",
        extra={
            "request_query_path": "/v1/videos/by-request/{request_id}?model={model}",
            "request_query_result_path": "data.task",
            "request_query_id_field": "id",
            "request_query_status_field": "state",
            "request_query_timeout_seconds": 12,
        },
    )

    assert seen == {"path": "/v1/videos/by-request/req%2Fa%3Fb%23c?model=video%2Fmodel", "timeout": 12}
    assert found["external_task_id"] == "task-from-request"
    assert found["status"] == "succeeded"
    assert found["url"] == "https://cdn.example.com/result.mp4"


def test_video_request_id_lookup_is_opt_in():
    assert gateway.find_video_by_request_id("req-1", "video-model", extra={}) is None
