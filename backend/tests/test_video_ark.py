"""Volcengine Ark (Seedance) video adapter — payload shaping (no network)."""
import pytest

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


def test_seedance_15_ark_payload_assigns_both_frame_roles():
    payload = gateway._ark_payload(
        "animate between two frames",
        "doubao-seedance-1-5-pro-251215",
        {
            "first_frame_image": "http://x/first.png",
            "last_frame_image": "http://x/last.png",
        },
    )

    assert [item.get("role") for item in payload["content"][1:]] == [
        "first_frame",
        "last_frame",
    ]


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


def test_ark_content_sends_product_identity_reference_without_frame_role():
    content = gateway._ark_content(
        "以上传产品图为唯一商品主体，产品从稍远景进入画面，随后手从包装底部抽出洗脸巾",
        {"product_reference_image": "http://x/product.png", "duration": 10},
    )

    assert [item["type"] for item in content] == ["text", "image_url"]
    assert content[1]["image_url"]["url"] == "http://x/product.png"
    assert content[1]["role"] == "reference_image"
    assert "产品身份参考" in content[0]["text"]
    assert "图片1中的产品" in content[0]["text"]
    assert "上传产品图" not in content[0]["text"]
    assert "第1张图片" not in content[0]["text"]


def test_ark_content_binds_product_to_its_actual_image_number():
    content = gateway._ark_content(
        "上传产品是唯一产品身份，保持包装文字清晰",
        {
            "first_frame_image": "http://x/first.png",
            "product_reference_image": "http://x/product.png",
        },
    )

    assert content[1]["role"] == "reference_image"
    assert content[2]["role"] == "first_frame"
    assert "图片2为首帧" in content[0]["text"]
    assert "图片1中的产品是唯一商品主体" in content[0]["text"]
    assert "图片1中的产品是唯一产品身份" in content[0]["text"]
    assert "上传产品" not in content[0]["text"]


def test_ark_content_orders_product_theme_then_details_before_other_references():
    content = gateway._ark_content(
        "保持上传产品的包装和细节",
        {
            "product_reference_image": "http://x/product.png",
            "product_detail_images": ["http://x/detail-a.png", "http://x/detail-b.png"],
            "first_frame_image": "http://x/first.png",
        },
    )

    assert [item["image_url"]["url"] for item in content[1:]] == [
        "http://x/product.png",
        "http://x/detail-a.png",
        "http://x/detail-b.png",
        "http://x/first.png",
    ]
    assert "图片2为产品细节参考1" in content[0]["text"]
    assert "图片3为产品细节参考2" in content[0]["text"]


def test_ark_content_keeps_seedance_20_theme_plus_nine_details():
    details = [f"http://x/detail-{index}.png" for index in range(1, 10)]
    content = gateway._ark_content(
        "保持产品主题和全部局部细节",
        {
            "product_reference_image": "http://x/product.png",
            "product_detail_images": details,
        },
    )

    assert [item["image_url"]["url"] for item in content[1:]] == [
        "http://x/product.png",
        *details,
    ]
    assert all(item["role"] == "reference_image" for item in content[1:])
    assert "图片10为产品细节参考9" in content[0]["text"]


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


def test_grok_video_submit_maps_first_frame_to_native_image_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["payload"] = payload
        return {"request_id": "grok-image-to-video"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "animate",
        "grok-imagine-video-1.5",
        {"first_frame_image": "https://example.com/first.png"},
    )

    assert task_id == "grok-image-to-video"
    assert seen["payload"]["image"] == {"url": "https://example.com/first.png"}
    assert "image_url" not in seen["payload"]
    assert "first_frame_image" not in seen["payload"]


def test_grok_video_submit_maps_ratio_to_native_aspect_ratio(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_post",
        lambda _path, payload, timeout=120: seen.update(payload=payload)
        or {"request_id": "grok-ratio-video"},
    )

    task_id = gateway.submit_video(
        "animate",
        "grok-imagine-video-1.5",
        {"duration": 5, "resolution": "480p", "ratio": "1:1"},
    )

    assert task_id == "grok-ratio-video"
    assert seen["payload"]["aspect_ratio"] == "1:1"
    assert "ratio" not in seen["payload"]


def test_grok_video_submit_maps_product_source_to_native_image_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["payload"] = payload
        return {"request_id": "grok-product-video"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "animate the uploaded product",
        "grok-imagine-video-1.5",
        {
            "duration": 10,
            "product_reference_image": "https://example.com/product.png",
        },
    )

    assert task_id == "grok-product-video"
    assert seen["payload"]["reference_images"] == [
        {"url": "https://example.com/product.png"},
    ]
    assert "image" not in seen["payload"]
    assert "image_url" not in seen["payload"]
    assert "product_reference_image" not in seen["payload"]
    assert "first_frame_image" not in seen["payload"]
    assert "last_frame_image" not in seen["payload"]


def test_grok_video_submit_maps_product_theme_and_detail_to_native_reference_schema(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_post",
        lambda _path, payload, timeout=120: seen.update(payload=payload)
        or {"request_id": "grok-product-details"},
    )

    task_id = gateway.submit_video(
        "keep the exact product identity",
        "grok-imagine-video-1.5",
        {
            "product_reference_image": "https://example.com/product.png",
            "product_detail_images": ["https://example.com/detail.png"],
            "negative_prompt": "warped logo, unreadable packaging text",
        },
        extra={
            # Tasks 154/155 froze this legacy mapping before migration 0076.
            "product_images_field": "images",
            "product_images_item_field": "url",
        },
    )

    assert task_id == "grok-product-details"
    assert seen["payload"]["reference_images"] == [
        {"url": "https://example.com/product.png"},
        {"url": "https://example.com/detail.png"},
    ]
    assert seen["payload"]["prompt"] == (
        "keep the exact product identity\n"
        "Negative constraints: warped logo, unreadable packaging text"
    )
    assert "negative_prompt" not in seen["payload"]
    assert "images" not in seen["payload"]
    assert "image" not in seen["payload"]
    assert "image_url" not in seen["payload"]
    assert "product_reference_image" not in seen["payload"]
    assert "product_detail_images" not in seen["payload"]


def test_generic_video_submit_maps_product_reference_to_configured_field(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_post",
        lambda _path, payload, timeout=120: seen.update(payload=payload) or {"id": "task-product"},
    )

    gateway.submit_video(
        "animate product",
        "custom-video",
        {"product_reference_image": "https://example.com/product.png"},
        extra={"product_image_field": "reference_image_url"},
    )

    assert seen["payload"]["reference_image_url"] == "https://example.com/product.png"
    assert "product_reference_image" not in seen["payload"]


def test_generic_video_submit_requires_explicit_product_detail_field(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    with pytest.raises(ValueError, match="未配置产品细节图上游字段"):
        gateway.submit_video(
            "animate product",
            "custom-video",
            {
                "product_reference_image": "https://example.com/product.png",
                "product_detail_images": ["https://example.com/detail.png"],
            },
            extra={"product_image_field": "product_image"},
        )


def test_generic_video_submit_maps_ordered_product_details_to_configured_field(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_post",
        lambda _path, payload, timeout=120: seen.update(payload=payload) or {"id": "task-details"},
    )

    gateway.submit_video(
        "animate product",
        "custom-video",
        {
            "product_reference_image": "https://example.com/product.png",
            "product_detail_images": [
                "https://example.com/detail-a.png",
                "https://example.com/detail-b.png",
            ],
        },
        extra={
            "product_image_field": "product_image",
            "product_detail_images_field": "detail_images",
        },
    )

    assert seen["payload"]["product_image"] == "https://example.com/product.png"
    assert seen["payload"]["detail_images"] == [
        "https://example.com/detail-a.png",
        "https://example.com/detail-b.png",
    ]
    assert "product_detail_images" not in seen["payload"]


def test_grok_video_submit_keeps_product_reference_separate_from_first_frame(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(
        gateway,
        "_video_post",
        lambda _path, payload, timeout=120: seen.update(payload=payload)
        or {"request_id": "grok-frame-and-product"},
    )

    task_id = gateway.submit_video(
        "animate product",
        "grok-imagine-video-1.5",
        {
            "product_reference_image": "https://example.com/product.png",
            "first_frame_image": "https://example.com/opening.png",
        },
    )

    assert task_id == "grok-frame-and-product"
    assert seen["payload"]["image"] == {"url": "https://example.com/opening.png"}
    assert seen["payload"]["reference_images"] == [
        {"url": "https://example.com/product.png"},
    ]


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


def test_generic_video_poll_resolves_relative_result_url_against_runtime_gateway(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    cfg = RuntimeGatewayConfig(
        use="video",
        provider="grok",
        base_url="https://video.example.com/v1",
        api_key="test-key",
        gateway_format="openai",
    )
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "url": "/v1/videos/task-1/content",
        },
    )

    res = gateway.poll_video("task-1", "video-model", gateway_config=cfg)

    assert res["status"] == "succeeded"
    assert res["url"] == "https://video.example.com/v1/videos/task-1/content"


def test_video_result_download_headers_only_include_auth_for_gateway_origin():
    cfg = RuntimeGatewayConfig(
        use="video",
        provider="grok",
        base_url="https://video.example.com/v1",
        api_key="test-key",
        gateway_format="openai",
    )

    assert gateway.video_result_download_headers(
        "https://video.example.com/v1/videos/task-1/content", cfg
    ) == {"Authorization": "Bearer test-key"}
    assert gateway.video_result_download_headers(
        "https://cdn.example.com/video.mp4", cfg
    ) == {}


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
