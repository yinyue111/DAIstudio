"""Mock gateway + watermark pipeline (no network)."""
import base64
import json
import time
from pathlib import Path

import pytest

from app.config import settings
from app.services import gateway, video_frames
from app.services.model_discovery import annotate_discovered_models
from app.services.model_gateway_config import RuntimeGatewayConfig
from app.services.safe_logging import redact_url_for_log
from app.services.watermark import image_ext, make_image_preview


def test_mock_image_and_preview():
    settings.mock_mode = True
    imgs = gateway.gen_image("a cat", "gpt-image-1", n=2, size="256x256")
    assert len(imgs) == 2
    assert imgs[0][:4] == b"\x89PNG"  # PNG magic

    preview, w, h = make_image_preview(imgs[0])
    assert preview[:4] == b"\x89PNG"
    assert (w, h) == (256, 256)


def test_antigravity_messages_image_transport_decodes_markdown_data_uri(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    raw = b"\xff\xd8\xff\xe0antigravity-image"
    encoded = base64.b64encode(raw).decode()
    seen = {}

    def fake_post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, kwargs=kwargs)
        return {
            "content": [
                {"type": "text", "text": f"![image](data:image/jpeg;base64,{encoded})"}
            ]
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="antigravity",
        base_url="https://gateway.example.com/antigravity",
        api_key="secret",
        gateway_format="anthropic",
    )

    images = gateway.gen_image(
        "a product photo",
        "gemini-3.1-flash-image",
        n=1,
        size="1024x1024",
        extra_payload={"image_transport": "anthropic_messages"},
        gateway_config=cfg,
    )

    assert images == [raw]
    assert seen["path"] == "/messages"
    assert seen["payload"]["model"] == "gemini-3.1-flash-image"


def test_antigravity_messages_image_transport_sends_edit_references(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    raw = b"\xff\xd8\xff\xe0edited-image"
    encoded = base64.b64encode(raw).decode()
    seen = {}

    def fake_post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, kwargs=kwargs)
        return {
            "content": [
                {"type": "text", "text": f"data:image/jpeg;base64,{encoded}"}
            ]
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="antigravity",
        base_url="https://gateway.example.com/antigravity",
        api_key="secret",
        gateway_format="anthropic",
    )
    refs = [
        "data:image/png;base64,eA==",
        "https://cdn.example.com/style.webp",
    ]

    images = gateway.gen_image(
        "keep the product and transfer the style",
        "gemini-3.1-flash-image",
        n=1,
        size="1024x1024",
        reference_image_urls=refs,
        edit_path="/messages",
        extra_payload={"image_transport": "anthropic_messages"},
        gateway_config=cfg,
    )

    assert images == [raw]
    assert seen["path"] == "/messages"
    blocks = seen["payload"]["messages"][0]["content"]
    assert [block["type"] for block in blocks] == ["image", "image", "text"]
    assert blocks[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "eA==",
    }
    assert blocks[1]["source"] == {
        "type": "url",
        "url": "https://cdn.example.com/style.webp",
    }


def test_antigravity_messages_image_transport_rejects_mask(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="antigravity",
        base_url="https://gateway.example.com/antigravity",
        api_key="secret",
        gateway_format="anthropic",
    )

    with pytest.raises(gateway.GatewayError, match="不支持蒙版编辑"):
        gateway.gen_image(
            "edit the source image",
            "gemini-3.1-flash-image",
            n=1,
            reference_image_url="data:image/png;base64,eA==",
            edit_path="/messages",
            extra_payload={
                "image_transport": "anthropic_messages",
                "mask": "data:image/png;base64,bWFzaw==",
            },
            gateway_config=cfg,
        )

    assert calls == []


def test_grok_image_transport_maps_canvas_to_native_fields(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = {}

    def fake_submit(path, payload, n, config=None):
        seen.update(path=path, payload=payload, n=n, config=config)
        return gateway.ImageBatchResult([b"image"])

    monkeypatch.setattr(gateway, "_post_single_image_repeated", fake_submit)
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="grok",
        base_url="https://gateway.example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    images = gateway.gen_image(
        "a product photo",
        "grok-imagine-image",
        n=1,
        size="1792x1024",
        extra_payload={"image_transport": "grok_images"},
        gateway_config=cfg,
    )

    assert images == [b"image"]
    assert seen["path"] == "/images/generations"
    assert seen["payload"]["aspect_ratio"] == "16:9"
    assert seen["payload"]["resolution"] == "2k"
    assert seen["payload"]["response_format"] == "b64_json"
    assert "size" not in seen["payload"]


@pytest.mark.parametrize(
    ("refs", "source_field"),
    [
        (["data:image/png;base64,eA=="], "image"),
        (
            [
                "data:image/png;base64,eA==",
                "https://cdn.example.com/style.png",
            ],
            "images",
        ),
    ],
)
def test_grok_image_transport_maps_native_edit_payload(
    monkeypatch,
    refs,
    source_field,
):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = {}

    def fake_submit(path, payload, n, config=None):
        seen.update(path=path, payload=payload, n=n, config=config)
        return gateway.ImageBatchResult([b"edited"])

    monkeypatch.setattr(gateway, "_post_single_image_repeated", fake_submit)
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="grok",
        base_url="https://gateway.example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    images = gateway.gen_image(
        "edit the source image",
        "grok-imagine-image",
        n=1,
        size="1024x1024",
        reference_image_urls=refs,
        edit_path="/images/edits",
        extra_payload={
            "image_transport": "grok_images",
            "edit_payload_format": "json",
        },
        gateway_config=cfg,
    )

    assert images == [b"edited"]
    assert seen["path"] == "/images/edits"
    assert source_field in seen["payload"]
    assert "edit_payload_format" not in seen["payload"]
    sources = (
        [seen["payload"][source_field]]
        if source_field == "image"
        else seen["payload"][source_field]
    )
    assert [source["url"] for source in sources] == refs


def test_grok_image_transport_rejects_mask(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    monkeypatch.setattr(
        gateway,
        "_post_single_image_repeated",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="grok",
        base_url="https://gateway.example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    with pytest.raises(gateway.GatewayError, match="Grok .*不支持蒙版编辑"):
        gateway.gen_image(
            "edit the source image",
            "grok-imagine-image",
            n=1,
            reference_image_url="data:image/png;base64,eA==",
            edit_path="/images/edits",
            extra_payload={
                "image_transport": "grok_images",
                "mask": "data:image/png;base64,bWFzaw==",
            },
            gateway_config=cfg,
        )

    assert calls == []


def test_discovery_marks_only_explicit_media_models_as_importable():
    models = annotate_discovered_models(
        [
            {"id": "gemini-3.1-flash-image"},
            {"id": "grok-imagine-video-1.5"},
            {"id": "generic-video-model"},
            {"id": "grok-imagine-edit"},
            {"id": "grok-4.5"},
        ],
        provider="grok",
        gateway_format="openai",
    )

    assert models[0]["recommended_uses"] == []
    assert models[0]["default_extra"] is None
    assert models[1]["recommended_uses"] == ["video"]
    assert models[1]["default_extra"]["capabilities"]["image_to_video"] is True
    assert models[2]["recommended_uses"] == []
    assert models[2]["default_extra"] is None
    assert models[3]["recommended_uses"] == []
    assert models[4]["recommended_uses"] == ["prompt"]
    assert models[4]["default_extra"] == {
        "capabilities": {"prompt_optimization": True}
    }


def test_discovery_marks_verified_grok_and_gemini_image_edit_models():
    grok_models = annotate_discovered_models(
        [
            {"id": "grok-imagine-image"},
            {"id": "grok-imagine-image-quality"},
            {"id": "grok-imagine-image-2.0"},
        ],
        provider="custom_openai",
        gateway_format="openai",
    )
    grok = grok_models[0]
    gemini = annotate_discovered_models(
        [{"id": "gemini-3.1-flash-image"}],
        provider="antigravity",
        gateway_format="anthropic",
    )[0]

    assert grok["default_extra"]["edit_path"] == "/images/edits"
    assert grok["default_extra"]["capabilities"]["image_to_image"] is True
    assert grok["default_extra"]["capabilities"]["max_reference_images"] == 2
    assert grok["default_extra"]["capabilities"]["mask_edit"] is False
    assert all(model["recommended_uses"] == ["image"] for model in grok_models)
    assert all(
        model["default_extra"] == grok["default_extra"] for model in grok_models
    )
    assert gemini["default_extra"]["edit_path"] == "/messages"
    assert gemini["default_extra"]["capabilities"]["image_to_image"] is True
    assert gemini["default_extra"]["capabilities"]["max_reference_images"] == 2
    assert gemini["default_extra"]["capabilities"]["mask_edit"] is False
    assert "image_edit" not in grok["default_extra"]["capabilities"]
    assert "image_edit" not in gemini["default_extra"]["capabilities"]


def test_discovery_marks_gpt_image_2_with_verified_openai_edit_adapter():
    image = annotate_discovered_models(
        [{"id": "gpt-image-2"}],
        provider="yinyue",
        gateway_format="openai",
    )[0]

    assert image["recommended_uses"] == ["image"]
    assert image["default_extra"]["edit_path"] == "/v1/images/edits"
    assert image["default_extra"]["multi_image_edit_enabled"] is True
    assert image["default_extra"]["capabilities"] == {
        "text_to_image": True,
        "image_to_image": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 2,
        "mask_edit": True,
    }


def test_discovery_routes_verified_visual_models_to_vision_imports():
    gpt = annotate_discovered_models(
        [{"id": "gpt-5.6-sol"}],
        provider="yinyue",
        gateway_format="openai",
    )[0]
    gemini = annotate_discovered_models(
        [{"id": "gemini-3.1-pro-high"}],
        provider="antigravity",
        gateway_format="anthropic",
    )[0]

    assert gpt["recommended_uses"] == ["vision"]
    assert gpt["default_extra_by_use"]["vision"]["capabilities"] == {
        "image_analysis": True,
        "video_analysis": True,
        "product_profile": True,
        "portrait_profile": True,
    }
    assert gemini["recommended_uses"] == ["vision", "prompt"]
    assert gemini["default_extra_by_use"]["vision"]["capabilities"][
        "video_analysis"
    ] is True
    assert gemini["default_extra_by_use"]["prompt"]["capabilities"] == {
        "prompt_optimization": True
    }


def test_discovery_marks_seedance_15_as_first_last_frame_only():
    seedance = annotate_discovered_models(
        [{"id": "doubao-seedance-1-5-pro-251215"}],
        provider="volcengine_ark",
        gateway_format="ark",
    )[0]

    capabilities = seedance["default_extra"]["capabilities"]
    assert capabilities["image_to_video"] is True
    assert capabilities["first_last_frame"] is True
    assert capabilities["reference_image"] is False
    assert capabilities["multi_reference"] is False
    assert "max_reference_images" not in capabilities


def test_discovery_splits_grok_reference_video_from_15_image_to_video():
    grok_reference, grok_15 = annotate_discovered_models(
        [
            {"id": "grok-imagine-video"},
            {"id": "grok-imagine-video-1.5"},
        ],
        provider="grok",
        gateway_format="openai",
    )

    assert grok_reference["recommended_uses"] == ["video"]
    assert grok_reference["default_extra"]["product_images_field"] == "reference_images"
    assert grok_reference["default_extra"]["product_images_item_field"] == "url"
    assert grok_reference["default_extra"]["negative_prompt_mode"] == "append_to_prompt"
    assert grok_reference["default_extra"]["capabilities"] == {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 7,
        "max_reference_duration_seconds": 10,
        "durations": list(range(1, 16)),
        "min_duration_seconds": 1,
        "max_duration_seconds": 15,
        "reference_image_mode_exclusive": True,
        "first_last_frame": False,
        "video_to_video": True,
        "video_reference": False,
        "video_edit": True,
        "audio_reference": False,
        "resolutions": ["480p", "720p"],
        "max_reference_videos": 1,
        "max_reference_audio": 0,
    }

    assert grok_15["recommended_uses"] == ["video"]
    assert grok_15["default_extra"]["first_frame_field"] == "image"
    assert grok_15["default_extra"]["first_frame_item_field"] == "url"
    assert "product_images_field" not in grok_15["default_extra"]
    assert grok_15["default_extra"]["capabilities"] == {
        "text_to_video": False,
        "image_to_video": True,
        "reference_image": False,
        "multi_reference": False,
        "first_last_frame": False,
        "video_to_video": False,
        "video_reference": False,
        "video_edit": False,
        "audio_reference": False,
        "durations": list(range(1, 16)),
        "min_duration_seconds": 1,
        "max_duration_seconds": 15,
        "resolutions": ["480p", "720p", "1080p"],
        "max_reference_videos": 0,
        "max_reference_audio": 0,
    }


@pytest.mark.parametrize(
    "model_id",
    ["grok-imagine-video", "grok-imagine-video-1.5"],
)
def test_discovery_does_not_enable_grok_video_for_non_openai_gateway(model_id):
    model = annotate_discovered_models(
        [{"id": model_id}],
        provider="grok",
        gateway_format="anthropic",
    )[0]

    assert model["recommended_uses"] == []
    assert model["default_extra"] is None


@pytest.mark.parametrize(
    "model_id",
    [
        "doubao-seedance-2-0-pro-260128",
        "doubao-seedance-2-0-fast-260615",
        "doubao-seedance-2-0-mini-260615",
    ],
)
def test_discovery_marks_verified_ark_seedance_20_capabilities(model_id):
    seedance = annotate_discovered_models(
        [{"id": model_id}],
        provider="volcengine_ark",
        gateway_format="ark",
    )[0]

    assert seedance["recommended_uses"] == ["video"]
    capabilities = seedance["default_extra"]["capabilities"]
    assert capabilities["text_to_video"] is True
    assert capabilities["image_to_video"] is True
    assert capabilities["reference_image"] is True
    assert capabilities["first_last_frame"] is True
    assert capabilities["multi_reference"] is True
    assert capabilities["max_reference_images"] == 9
    assert capabilities["video_to_video"] is True
    assert capabilities["video_reference"] is True
    assert capabilities["video_edit"] is False
    assert capabilities["audio_reference"] is False
    assert capabilities["max_reference_videos"] == 1
    assert capabilities["max_reference_audio"] == 0


@pytest.mark.parametrize(
    "model_id",
    [
        "doubao-seedance-2-1-pro-270101",
        "doubao-seedance-3-0-pro-280101",
    ],
)
def test_discovery_does_not_inherit_seedance_20_capabilities_for_future_versions(model_id):
    seedance = annotate_discovered_models(
        [{"id": model_id}],
        provider="volcengine_ark",
        gateway_format="ark",
    )[0]

    assert seedance["recommended_uses"] == []
    assert seedance["capability_label"] == "视频模型（需手动配置）"
    assert seedance["default_extra"] is None


@pytest.mark.parametrize(
    ("provider", "gateway_format"),
    [
        ("custom_openai", "openai"),
        ("custom_openai", "ark"),
        ("volcengine_ark", "openai"),
    ],
)
def test_discovery_does_not_enable_seedance_for_mismatched_provider_route(
    provider,
    gateway_format,
):
    seedance = annotate_discovered_models(
        [{"id": "doubao-seedance-2-0-mini-260615"}],
        provider=provider,
        gateway_format=gateway_format,
    )[0]

    assert seedance["recommended_uses"] == []
    assert seedance["default_extra"] is None


@pytest.mark.parametrize(
    "model_id",
    [
        "text-embedding-3-large",
        "whisper-1",
        "omni-moderation-latest",
        "bge-reranker-v2-m3",
        "tts-1",
    ],
)
def test_discovery_does_not_recommend_non_chat_models_for_prompt_optimization(
    model_id,
):
    model = annotate_discovered_models(
        [{"id": model_id}],
        provider="openai",
        gateway_format="openai",
    )[0]

    assert model["recommended_uses"] == []
    assert model["default_extra"] is None
    assert model["capability_label"] == "未识别模型（需手动配置）"


@pytest.mark.parametrize(
    ("provider", "gateway_format", "model_id"),
    [
        ("openai", "openai", "gpt-5.2"),
        ("openrouter", "openai", "anthropic/claude-sonnet-4"),
        ("siliconflow", "openai", "deepseek-ai/DeepSeek-V3"),
        ("deepseek", "openai", "deepseek-chat"),
        ("moonshot", "openai", "moonshot-v1-128k"),
        ("zhipu", "openai", "glm-4.5"),
        ("dashscope", "openai", "qwen-max"),
        ("baidu_qianfan", "openai", "ernie-4.5-turbo"),
        ("tencent_hunyuan", "openai", "hunyuan-turbos-latest"),
        ("anthropic", "anthropic", "claude-sonnet-4-5"),
        ("gemini", "openai", "gemini-2.5-pro"),
        ("custom_openai", "openai", "llama-3.3-70b-instruct"),
    ],
)
def test_discovery_recommends_supported_chat_model_families_for_prompt_use(
    provider,
    gateway_format,
    model_id,
):
    model = annotate_discovered_models(
        [{"id": model_id}],
        provider=provider,
        gateway_format=gateway_format,
    )[0]

    assert model["recommended_uses"] == ["prompt"]
    assert model["default_extra"] == {
        "capabilities": {"prompt_optimization": True}
    }


def test_mock_video_is_a_playable_mp4(tmp_path):
    raw = gateway.mock_video()
    assert raw[4:8] == b"ftyp"

    path = tmp_path / "mock-video.mp4"
    path.write_bytes(raw)
    if video_frames.FFPROBE:
        meta = video_frames.probe_media(str(path))
        assert meta["width"] == 640
        assert meta["height"] == 360
        assert meta["duration"] == 1.0


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
    # Mock output is subject to the same evidence gate as a real provider.
    assert all(key not in s for key in ("镜头运动", "主体动作", "时序分镜"))
    assert r["shots"] == []
    assert r["analysis_gaps"] == [{"message": "缺少视频帧分析证据"}]
    assert r["structured"]["旁白"] == "未分析"
    assert r["structured"]["音效"] == "未分析"
    assert r["final_text"]


def test_video_reverse_labels_frames_with_authoritative_timestamps(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = {}
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )

    def fake_post(_path, payload, **_kwargs):
        seen["content"] = payload["messages"][0]["content"]
        return {
            "choices": [{"message": {"content": (
                '{"主体":"水感精华广告","shots":['
                '{"start_seconds":0,"end_seconds":2.2,"visual":"液滴入水",'
                '"action":"液滴落下","camera":"微距固定",'
                '"lighting":"冷白柔光","transition":"动作匹配",'
                '"ocr":"无","audio_cue":"未分析",'
                '"evidence_frame_indices":[1,2],"confidence":0.95}],'
                '"final_text":"3:4竖版，10秒水感精华广告"}'
            )}}],
            "usage": {"total_tokens": 9},
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    result = gateway.reverse_prompt(
        ["data:image/jpeg;base64,Zmlyc3Q=", "data:image/jpeg;base64,bGFzdA=="],
        "vision-model",
        target="video",
        video_analysis={
            "source": {
                "width": 720,
                "height": 960,
                "ratio": "3:4",
                "duration_seconds": 10.054,
                "fps": 23.0,
                "has_audio": True,
                "audio_analyzed": False,
            },
            "sampled_frames": [
                {
                    "index": 1,
                    "timestamp_seconds": 0.0,
                    "detected_shot_index": 1,
                    "detected_shot_id": "detected-shot-1-0-2200",
                    "detected_shot_start_seconds": 0.0,
                    "detected_shot_end_seconds": 2.2,
                },
                {
                    "index": 2,
                    "timestamp_seconds": 10.004,
                    "detected_shot_index": 2,
                    "detected_shot_id": "detected-shot-1-2200-10054",
                    "detected_shot_start_seconds": 2.2,
                    "detected_shot_end_seconds": 10.054,
                },
            ],
        },
        reference_context=[
            {
                "role": "frame",
                "source_type": "video",
                "timestamp_seconds": 0.0,
                "detected_shot_index": 1,
                "detected_shot_id": "detected-shot-1-0-2200",
                "detected_shot_start_seconds": 0.0,
                "detected_shot_end_seconds": 2.2,
            },
            {
                "role": "frame",
                "source_type": "video",
                "timestamp_seconds": 10.004,
                "detected_shot_index": 2,
                "detected_shot_id": "detected-shot-1-2200-10054",
                "detected_shot_start_seconds": 2.2,
                "detected_shot_end_seconds": 10.054,
            },
        ],
        gateway_config=cfg,
    )

    text_items = [item["text"] for item in seen["content"] if item["type"] == "text"]
    assert any("720x960" in text and "3:4" in text and "10.054" in text for text in text_items)
    assert any(
        "第 1 帧，源视频时间戳 0.000 秒" in text
        and "服务端检测镜头 1" in text
        and "0.000-2.200 秒" in text
        for text in text_items
    )
    assert any(
        "第 2 帧，源视频时间戳 10.004 秒" in text
        and "服务端检测镜头 2" in text
        and "2.200-10.054 秒" in text
        for text in text_items
    )
    assert result["shots"][0]["visual"] == "液滴入水"
    assert result["shots"][-1]["end_seconds"] == 2.2
    assert result["analysis_gaps"] == [{"start_seconds": 2.2, "end_seconds": 10.054}]
    assert "shots" not in result["structured"]
    assert result["structured"]["时序分镜"].startswith("0.000-2.200s 液滴入水")
    assert "0.000-2.200s 液滴入水" not in result["final_text"]


def test_generation_video_reverse_repairs_missing_sampled_frame_coverage(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    responses = iter([{
            "choices": [{"message": {"content": (
                '{"主体":"精华瓶","shots":['
                '{"start_seconds":0,"end_seconds":1,"visual":"瓶盖特写",'
                '"evidence_frame_indices":[1],"confidence":0.9}],'
                '"final_text":"瓶盖特写后，完整精华瓶立于水面"}'
            )}}],
            "usage": {"total_tokens": 7},
        }, {
            "choices": [{"message": {"content": (
                '{"frames":[{"frame_index":2,'
                '"visual":"完整精华瓶立于水面","lighting":"冷白轮廓光",'
                '"confidence":0.9}]}'
            )}}],
            "usage": {"total_tokens": 3},
        }])

    def fake_post(_path, payload, **_kwargs):
        calls.append(payload)
        return next(responses)

    monkeypatch.setattr(gateway, "_post", fake_post)
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )
    result = gateway.reverse_prompt(
        ["data:image/jpeg;base64,Zmlyc3Q=", "data:image/jpeg;base64,c2Vjb25k"],
        "vision-model",
        target="video",
        video_analysis={
            "source": {
                "duration_seconds": 4.0,
                "audio_analyzed": False,
            },
            "sampled_frames": [
                {"index": 1, "timestamp_seconds": 0.0},
                {"index": 2, "timestamp_seconds": 3.9},
            ],
        },
        require_video_frame_coverage=True,
        gateway_config=cfg,
    )

    assert len(calls) == 2
    repair_content = calls[1]["messages"][0]["content"]
    assert isinstance(repair_content, list)
    assert [item["type"] for item in repair_content].count("image_url") == 1
    assert result["repair_attempted"] is True
    assert result["usage"]["total_tokens"] == 10
    assert {
        index
        for shot in result["shots"]
        for index in shot["evidence_frame_indices"]
    } == {1, 2}


def test_video_reverse_multiframe_repair_preserves_shot_temporal_semantics(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    temporal = {
        "subject_tracking": "同一双手始终在画面中央操作洗脸巾",
        "pose": "双手由分开持巾变为向内扭紧",
        "action": "双手缓慢扭干吸满水的洗脸巾，水流持续滴落",
        "camera": "轻微向前推近",
        "transition": "硬切进入下一镜头",
    }
    responses = iter([
        {
            "choices": [{"message": {"content": json.dumps({
                "主体": "白色洗脸巾",
                "shots": [{
                    "start_seconds": 0.0,
                    "end_seconds": 3.0,
                    "visual": "双手持白色洗脸巾",
                    "camera": "固定机位",
                    "evidence_frame_indices": [1],
                    "confidence": 0.9,
                }],
                "final_text": "双手扭干白色洗脸巾",
            }, ensure_ascii=False)}}],
            "usage": {"total_tokens": 7},
        },
        {
            "choices": [{"message": {"content": json.dumps({
                "frames": [
                    {
                        "frame_index": 2,
                        "visual": "双手向内扭压洗脸巾",
                        "lighting": "柔和冷白侧光",
                        "ocr": "厚实吸水",
                        **temporal,
                        "confidence": 0.9,
                    },
                    {
                        "frame_index": 3,
                        "visual": "水流从扭紧的洗脸巾中滴落",
                        "lighting": "柔和冷白侧光",
                        "ocr": "厚实吸水",
                        **temporal,
                        "confidence": 0.92,
                    },
                ],
            }, ensure_ascii=False)}}],
            "usage": {"total_tokens": 5},
        },
    ])

    def fake_post(_path, payload, **_kwargs):
        calls.append(payload)
        return next(responses)

    monkeypatch.setattr(gateway, "_post", fake_post)
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )
    sampled_frames = [
        {
            "index": index,
            "timestamp_seconds": timestamp,
            "relative_timestamp_seconds": timestamp,
            "detected_shot_id": "shot-1",
            "detected_shot_index": 1,
            "detected_shot_start_seconds": 0.0,
            "detected_shot_end_seconds": 3.0,
        }
        for index, timestamp in ((1, 0.0), (2, 1.5), (3, 2.9))
    ]

    result = gateway.reverse_prompt(
        ["data:image/jpeg;base64,eA=="] * 3,
        "vision-model",
        target="video",
        video_analysis={
            "source": {"duration_seconds": 3.0, "audio_analyzed": False},
            "sampled_frames": sampled_frames,
        },
        require_video_frame_coverage=True,
        gateway_config=cfg,
    )

    assert len(calls) == 2
    repair_text = "\n".join(
        item["text"]
        for item in calls[1]["messages"][0]["content"]
        if item["type"] == "text"
    )
    assert "至少提供 2 张缺失采样帧" in repair_text
    assert repair_text.count("可与同镜头其他帧做时序对比") == 2
    assert len(result["shots"]) == 1
    shot = result["shots"][0]
    assert shot["evidence_frame_indices"] == [1, 2, 3]
    assert shot["subject_tracking"] == temporal["subject_tracking"]
    assert shot["pose"] == temporal["pose"]
    assert shot["action"] == temporal["action"]
    assert shot["camera"] == "固定机位"
    assert shot["transition"] == temporal["transition"]
    assert "双手缓慢扭干" in result["final_text"]


def test_generation_video_reverse_reports_placeholder_visuals_as_gaps(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    responses = iter([{
            "choices": [{"message": {"content": (
                '{"主体":"精华瓶","shots":['
                '{"start_seconds":0,"end_seconds":4,"visual":"未见",'
                '"evidence_frame_indices":[1,2],"confidence":0.9}],'
                '"final_text":"精华瓶产品视频"}'
            )}}],
            "usage": {"total_tokens": 7},
        }, {
            "choices": [{"message": {"content": (
                '{"frames":['
                '{"frame_index":1,"visual":"精华瓶置于水面中央","lighting":"柔和侧光","confidence":0.9},'
                '{"frame_index":2,"visual":"精华瓶与水波同框收尾","lighting":"柔和侧光","confidence":0.9}'
                ']}'
            )}}],
            "usage": {"total_tokens": 11},
        }])

    def fake_post(_path, payload, **_kwargs):
        calls.append(payload)
        return next(responses)

    monkeypatch.setattr(gateway, "_post", fake_post)
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )
    result = gateway.reverse_prompt(
        ["data:image/jpeg;base64,Zmlyc3Q=", "data:image/jpeg;base64,c2Vjb25k"],
        "vision-model",
        target="video",
        video_analysis={
            "source": {"duration_seconds": 4.0, "audio_analyzed": False},
            "sampled_frames": [
                {"index": 1, "timestamp_seconds": 0.0},
                {"index": 2, "timestamp_seconds": 3.9},
            ],
        },
        require_video_frame_coverage=True,
        gateway_config=cfg,
    )

    assert len(calls) == 2
    assert result["repair_attempted"] is True
    assert len(result["shots"]) == 2
    assert {
        index
        for shot in result["shots"]
        for index in shot["evidence_frame_indices"]
    } == {1, 2}


def test_video_shots_are_sorted_before_normalization():
    shots = gateway._normalize_video_shots(
        [
            {
                "start_seconds": 4,
                "end_seconds": 6,
                "visual": "后镜头",
                "evidence_frame_indices": [2],
                "confidence": 0.8,
            },
            {
                "start_seconds": 0,
                "end_seconds": 2,
                "visual": "前镜头",
                "evidence_frame_indices": [1],
                "confidence": 0.8,
            },
        ],
        duration_seconds=6,
        frame_count=3,
    )

    assert [shot["visual"] for shot in shots if shot["visual"] in {"前镜头", "后镜头"}] == [
        "前镜头",
        "后镜头",
    ]
    assert [(shot["start_seconds"], shot["end_seconds"]) for shot in shots] == [
        (0.0, 2.0),
        (4.0, 6.0),
    ]


def test_video_shots_report_unobserved_ranges_without_inventing_actions():
    shots = gateway._normalize_video_shots(
        [
            {"start_seconds": 1, "end_seconds": 2, "visual": "第一个已识别镜头", "evidence_frame_indices": [1], "confidence": 0.8},
            {"start_seconds": 4, "end_seconds": 6, "visual": "第二个已识别镜头", "evidence_frame_indices": [2], "confidence": 0.8},
        ],
        duration_seconds=10,
        frame_count=5,
    )
    assert [(shot["start_seconds"], shot["end_seconds"]) for shot in shots] == [
        (1.0, 2.0),
        (4.0, 6.0),
    ]
    assert gateway._video_analysis_gaps(shots, duration_seconds=10) == [
        {"start_seconds": 0.0, "end_seconds": 1.0},
        {"start_seconds": 2.0, "end_seconds": 4.0},
        {"start_seconds": 6.0, "end_seconds": 10.0},
    ]

    fallback = gateway._normalize_video_shots(
        [{"start_seconds": "NaN", "end_seconds": 5}],
        duration_seconds=10,
        frame_count=5,
    )
    assert fallback == []
    assert gateway._video_analysis_gaps(fallback, duration_seconds=10) == [
        {"start_seconds": 0.0, "end_seconds": 10.0}
    ]


def test_video_analysis_gaps_ignore_shots_without_frame_evidence():
    unsupported = gateway._normalize_video_shots(
        [{
            "start_seconds": 0,
            "end_seconds": 10,
            "visual": "模型声称完整覆盖",
            "evidence_frame_indices": [],
            "confidence": 0,
        }],
        duration_seconds=10,
        frame_count=4,
    )

    assert unsupported == []
    assert gateway._video_analysis_gaps(unsupported, duration_seconds=10) == [
        {"start_seconds": 0.0, "end_seconds": 10.0}
    ]
    assert gateway._video_analysis_gaps(
        [{
            "start_seconds": 0,
            "end_seconds": 10,
            "evidence_frame_indices": [1],
            "confidence": 1,
        }],
        duration_seconds=10,
        analysis_mode="cover_fallback",
    ) == [{"start_seconds": 0.0, "end_seconds": 10.0}]


def test_unverified_shot_does_not_shift_verified_shot_coverage():
    shots = gateway._normalize_video_shots(
        [
            {
                "start_seconds": 0,
                "end_seconds": 9,
                "visual": "unsupported model guess",
                "evidence_frame_indices": [],
                "confidence": 0,
            },
            {
                "start_seconds": 8,
                "end_seconds": 10,
                "visual": "observed closing frame",
                "evidence_frame_indices": [2],
                "confidence": 0.9,
            },
        ],
        duration_seconds=10,
        frame_count=2,
    )

    assert len(shots) == 1
    assert shots[0]["start_seconds"] == 8.0
    assert gateway._video_analysis_gaps(
        shots,
        duration_seconds=10,
    ) == [{"start_seconds": 0.0, "end_seconds": 8.0}]


def test_shot_evidence_must_match_its_time_range():
    sampled_frames = [
        {"index": 1, "timestamp_seconds": 0.0},
        {"index": 2, "timestamp_seconds": 8.0},
    ]

    unsupported = gateway._normalize_video_shots(
        [{
            "start_seconds": 0,
            "end_seconds": 2,
            "visual": "错误引用末尾帧",
            "evidence_frame_indices": [2],
            "confidence": 0.9,
        }],
        duration_seconds=10,
        frame_count=2,
        sampled_frames=sampled_frames,
    )
    bracketed = gateway._normalize_video_shots(
        [{
            "start_seconds": 2,
            "end_seconds": 7,
            "visual": "两帧夹持的高置信区间",
            "evidence_frame_indices": [1, 2],
            "confidence": 0.9,
        }],
        duration_seconds=10,
        frame_count=2,
        sampled_frames=sampled_frames,
    )

    assert unsupported == []
    assert bracketed[0]["evidence_frame_indices"] == [1, 2]


def test_single_frame_cannot_claim_a_long_video_interval():
    shots = gateway._normalize_video_shots(
        [{
            "start_seconds": 0,
            "end_seconds": 10,
            "visual": "单帧被错误扩写为全片",
            "evidence_frame_indices": [2],
            "confidence": 0.9,
        }],
        duration_seconds=10,
        frame_count=3,
        sampled_frames=[
            {"index": 1, "timestamp_seconds": 0.0},
            {"index": 2, "timestamp_seconds": 5.0},
            {"index": 3, "timestamp_seconds": 9.95},
        ],
    )

    assert shots == []
    assert gateway._video_analysis_gaps(shots, duration_seconds=10) == [
        {"start_seconds": 0.0, "end_seconds": 10.0}
    ]


def test_single_frame_can_support_a_short_local_interval():
    shots = gateway._normalize_video_shots(
        [{
            "start_seconds": 4.5,
            "end_seconds": 5.5,
            "visual": "中段产品特写",
            "evidence_frame_indices": [2],
            "confidence": 0.9,
        }],
        duration_seconds=10,
        frame_count=3,
        sampled_frames=[
            {"index": 1, "timestamp_seconds": 0.0},
            {"index": 2, "timestamp_seconds": 5.0},
            {"index": 3, "timestamp_seconds": 9.95},
        ],
    )

    assert [(shot["start_seconds"], shot["end_seconds"]) for shot in shots] == [
        (4.5, 5.5)
    ]


def test_multi_frame_evidence_clamps_unobserved_shot_edges():
    shots = gateway._normalize_video_shots(
        [{
            "start_seconds": 0,
            "end_seconds": 10,
            "visual": "中段有证据的长镜头",
            "evidence_frame_indices": [2, 3],
            "confidence": 0.9,
        }],
        duration_seconds=10,
        frame_count=4,
        sampled_frames=[
            {"index": 1, "timestamp_seconds": 0.0},
            {"index": 2, "timestamp_seconds": 3.0},
            {"index": 3, "timestamp_seconds": 7.0},
            {"index": 4, "timestamp_seconds": 9.95},
        ],
    )

    assert [(shot["start_seconds"], shot["end_seconds"]) for shot in shots] == [
        (2.25, 7.75)
    ]
    assert gateway._video_analysis_gaps(shots, duration_seconds=10) == [
        {"start_seconds": 0.0, "end_seconds": 2.25},
        {"start_seconds": 7.75, "end_seconds": 10.0},
    ]


def test_reversed_invalid_shot_does_not_create_synthetic_split():
    shots = gateway._normalize_video_shots(
        [{"start_seconds": 4, "end_seconds": 2, "visual": "非法倒置镜头"}],
        duration_seconds=10,
        frame_count=3,
    )

    assert shots == []
    assert gateway._video_analysis_gaps(shots, duration_seconds=10) == [
        {"start_seconds": 0.0, "end_seconds": 10.0}
    ]


def test_compose_final_fallback():
    # Legacy fallback still emits only positive, usable visual dimensions.
    r = gateway._parse_structured('{"主体":"a cat","光线":"soft","负向":"text"}')
    assert "a cat" in r["final_text"] and "soft" in r["final_text"]
    assert "text" not in r["final_text"]
    assert r["structured"]["负向"] == "text"


def test_video_reverse_template_keeps_generation_prompt_compact():
    template = gateway._reverse_template("video", n_frames=4)

    assert "120-220 个中文字符" in template
    assert "末尾追加英文视频关键词" not in template


@pytest.mark.parametrize("content", ["[]", '"hello"', "null"])
def test_parse_structured_non_object_json_uses_text_fallback(content):
    result = gateway._parse_structured(content)

    assert result["structured"] == {}
    assert result["final_text"] == content
    assert result["shots"] == []


def test_video_reverse_missing_final_text_does_not_serialize_raw_shot_objects():
    result = gateway._parse_structured(
        '{"主体":"产品","shots":[{"start_seconds":0,"end_seconds":2,"visual":"产品入镜"}]}'
    )

    assert "shots" not in result["final_text"]
    assert "start_seconds" not in result["final_text"]
    assert result["shots"][0]["visual"] == "产品入镜"


def test_video_reverse_keeps_source_spec_structured_and_out_of_visual_prompt(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )

    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": (
                '{"主体":"精华广告","shots":[],"final_text":"3:4竖版，8秒水感精华广告"}'
            )}}],
            "usage": {"total_tokens": 9},
        },
    )

    result = gateway.reverse_prompt(
        ["data:image/jpeg;base64,Zmlyc3Q="],
        "vision-model",
        target="video",
        video_analysis={
            "source": {
                "width": 720,
                "height": 960,
                "ratio": "3:4",
                "duration_seconds": 10.0,
                "fps": 24.0,
                "has_audio": False,
                "audio_analyzed": False,
            },
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0.0}],
        },
        gateway_config=cfg,
    )

    assert "10.000秒" in result["structured"]["源视频规格"]
    assert "720x960" in result["structured"]["源视频规格"]
    assert "10.000秒" not in result["final_text"]
    assert "720x960" not in result["final_text"]
    assert "24" not in result["final_text"]
    assert "8秒" not in result["final_text"]


def test_video_duration_cleanup_preserves_shot_timing_language():
    cleaned = gateway._without_conflicting_video_durations(
        "前2秒产品静置，3秒后切到特写，每个镜头约4秒，8秒水感精华广告",
        10,
    )

    assert "前2秒" in cleaned
    assert "3秒后" in cleaned
    assert "每个镜头约4秒" in cleaned
    assert "8秒" not in cleaned


def test_video_duration_cleanup_preserves_shot_pacing_and_relative_timing():
    cleaned = gateway._without_conflicting_video_durations(
        "总时长8秒，每个镜头约2秒，0-2秒产品静置，3秒后切到特写，10秒广告",
        10.0,
    )

    assert "总时长8秒" not in cleaned
    assert "每个镜头约2秒" in cleaned
    assert "0-2秒产品静置" in cleaned
    assert "3秒后切到特写" in cleaned
    assert "10秒广告" in cleaned


@pytest.mark.parametrize(
    ("text", "removed"),
    [
        ("total duration 8 seconds, first 2 seconds hold on product", "total duration 8 seconds"),
        ("8 sec video, each shot lasts 2 sec", "8 sec video"),
    ],
)
def test_video_duration_cleanup_supports_english_second_units(text, removed):
    cleaned = gateway._without_conflicting_video_durations(text, 10.0)

    assert removed not in cleaned
    assert "2 sec" in cleaned or "2 seconds" in cleaned

    english = gateway._without_conflicting_video_durations(
        "8s video, first 2s hold, 10s ad",
        10.0,
    )
    assert "8s video" not in english
    assert "first 2s hold" in english
    assert "10s ad" in english


def test_video_reverse_separates_post_production_from_final_text(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": (
                '{"主体":"洗脸巾广告","细节特征":"微距展开如意云纹","shots":[],"final_text":'
                '"微距展开如意云纹。字幕‘干湿两用’。旁白‘温柔开启新一天’。SFX‘水滴声’"}'
            )}}],
            "usage": {"total_tokens": 9},
        },
    )

    result = gateway.reverse_prompt(
        ["data:image/jpeg;base64,Zmlyc3Q="],
        "vision-model",
        target="video",
        video_analysis={
            "source": {"duration_seconds": 10.0, "audio_analyzed": False},
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0.0}],
        },
        gateway_config=cfg,
    )

    assert "微距展开如意云纹" in result["final_text"]
    assert "洗脸巾广告" in result["final_text"]
    assert result["shots"] == []
    assert result["analysis_gaps"] == [{"start_seconds": 0.0, "end_seconds": 10.0}]
    assert "干湿两用" not in result["final_text"]
    assert "温柔开启新一天" not in result["final_text"]
    assert "水滴声" not in result["final_text"]
    assert result["structured"]["字幕卖点"] == "干湿两用"
    assert result["structured"]["旁白"] == "未分析"
    assert result["structured"]["音效"] == "未分析"


def test_video_reverse_separates_post_production_without_video_metadata(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": (
                '{"主体":"洗脸巾广告","细节特征":"微距展开云纹","final_text":'
                '"微距展开云纹。字幕‘干湿两用’。旁白‘温柔开始’。SFX‘水滴声’"}'
            )}}],
            "usage": {"total_tokens": 9},
        },
    )

    result = gateway.reverse_prompt(
        ["data:image/jpeg;base64,Zmlyc3Q="],
        "vision-model",
        target="video",
        gateway_config=cfg,
    )

    assert "洗脸巾广告" in result["final_text"]
    assert "微距展开云纹" in result["final_text"]
    assert result["analysis_gaps"] == [{"message": "缺少视频帧分析证据"}]
    assert result["structured"]["字幕卖点"] == "干湿两用"
    assert result["structured"]["旁白"] == "未分析"
    assert result["structured"]["音效"] == "未分析"


def test_compose_final_uses_generation_dimension_order_and_drops_analysis_fields():
    r = gateway._parse_structured(
        '{"光线":"soft left key light","主体":"red product bottle",'
        '"图像类型":"产品图","反推重点":"产品优先",'
        '"场景背景":"white studio","风格":"premium product photo"}'
    )

    text = r["final_text"]
    assert text.index("主体") < text.index("场景背景") < text.index("风格") < text.index("光线")
    assert "图像类型" not in text
    assert "反推重点" not in text


def test_parse_structured_rejects_fenced_json():
    r = gateway._parse_structured('```json\n{"主体":"x","final_text":"hello"}\n```')
    assert r["structured"] == {}
    assert r["final_text"].startswith("```json")


def test_decode_image_response_downloads_each_url_once(monkeypatch):
    calls = []

    def fake_download(url, **kwargs):
        calls.append((url, kwargs))
        return b"image-bytes"

    monkeypatch.setattr(gateway, "_download", fake_download)
    out = gateway._decode_image_response({"data": [{"url": "https://example.com/a.png"}]})

    assert out == [b"image-bytes"]
    assert calls == [(
        "https://example.com/a.png",
        {
            "max_bytes": settings.generated_image_max_bytes,
            "allowed_content_types": ("image/",),
            "timeout_seconds": settings.image_download_timeout_seconds,
        },
    )]


def test_decode_image_response_prefers_high_resolution_download_url(monkeypatch):
    calls = []

    def fake_download(url, **kwargs):
        calls.append((url, kwargs))
        return b"image-bytes"

    monkeypatch.setattr(gateway, "_download", fake_download)
    out = gateway._decode_image_response({
        "data": [{
            "url": "https://example.com/preview.png",
            "download_url": "https://example.com/4k.png",
        }]
    })

    assert out == [b"image-bytes"]
    assert calls[0][0] == "https://example.com/4k.png"


def test_decode_image_response_prefers_hd_url_over_inline_preview(monkeypatch):
    calls = []

    def fake_download(url, **kwargs):
        calls.append((url, kwargs))
        return b"full-resolution-image"

    monkeypatch.setattr(gateway, "_download", fake_download)
    out = gateway._decode_image_response({
        "data": [{
            "b64_json": base64.b64encode(b"small-inline-preview").decode(),
            "url": "https://example.com/preview.png",
            "hd_url": "https://example.com/full-4k.png",
        }]
    })

    assert out == [b"full-resolution-image"]
    assert calls[0][0] == "https://example.com/full-4k.png"


def test_decode_image_response_captures_gateway_echo_diagnostics(monkeypatch):
    raw = base64.b64encode(b"image-bytes").decode()

    out, diagnostics = gateway._decode_image_response_with_diagnostics({
        "data": [{"b64_json": raw}],
        "model": "gpt-image-2-codex",
        "size": "auto",
        "quality": "auto",
        "output_format": "jpeg",
    })

    assert out == [b"image-bytes"]
    assert len(diagnostics) == 1
    assert diagnostics[0].model == "gpt-image-2-codex"
    assert diagnostics[0].size == "auto"
    assert diagnostics[0].quality == "auto"
    assert diagnostics[0].output_format == "jpeg"
    assert diagnostics[0].selected_source == "b64_json"


def test_decode_image_response_rejects_oversized_base64(monkeypatch):
    monkeypatch.setattr(settings, "generated_image_max_bytes", 2)

    try:
        gateway._decode_image_response({"data": [{"b64_json": base64.b64encode(b"abcd").decode()}]})
    except gateway.GatewayError as e:
        assert "大小上限" in str(e)
    else:
        raise AssertionError("oversized image payload should fail")


def test_download_error_does_not_leak_signed_url(monkeypatch):
    class FakeStream:
        is_redirect = False
        status_code = 403
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FakeStream()

    monkeypatch.setattr(gateway, "pinned_client", lambda *_args, **_kwargs: FakeClient())
    signed = "https://cdn.example.com/video.mp4?token=secret-token"
    try:
        gateway.download_bytes(signed)
    except gateway.GatewayError as e:
        assert "secret-token" not in str(e)
        assert "cdn.example.com" not in str(e)
        assert "403" in str(e)
    else:
        raise AssertionError("download should fail")


def test_download_to_path_rejects_explicit_non_video_type(monkeypatch, tmp_path):
    class FakeStream:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield b"<html>not video</html>"

    class FakeClient:
        def __init__(self, *_args, **kwargs):
            self.timeout = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FakeStream()

    seen = {}

    def fake_client(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return FakeClient(*args, **kwargs)

    monkeypatch.setattr(gateway.httpx, "Client", fake_client)
    out = Path(tmp_path) / "bad.mp4"

    try:
        gateway.download_to_path(
            "https://cdn.example.com/rendered",
            out,
            timeout_seconds=123,
            allowed_content_types=("video/", "application/octet-stream"),
        )
    except gateway.GatewayError as e:
        assert "类型" in str(e)
    else:
        raise AssertionError("download should reject text/html")

    assert 0 < seen["timeout"].connect <= 123
    assert 0 < seen["timeout"].read <= 123
    assert not out.exists()


def test_download_to_path_rejects_missing_content_type_when_required(monkeypatch, tmp_path):
    class FakeStream:
        is_redirect = False
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_raw(self):
            yield b"not typed"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FakeStream()

    monkeypatch.setattr(gateway.httpx, "Client", lambda *_args, **_kwargs: FakeClient())
    out = Path(tmp_path) / "missing-type.mp4"

    try:
        gateway.download_to_path(
            "https://cdn.example.com/rendered",
            out,
            allowed_content_types=("video/", "application/octet-stream"),
        )
    except gateway.GatewayError as e:
        assert "类型" in str(e)
    else:
        raise AssertionError("download should reject missing content-type")

    assert not out.exists()


def test_download_to_path_rejects_low_speed_stream(monkeypatch, tmp_path):
    now = {"value": 1000.0}

    class FakeStream:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_raw(self):
            now["value"] += 61.0
            yield b"x" * 1024

    @gateway.contextmanager
    def fake_guarded_stream(_client, _method, _url, **_kwargs):
        yield FakeStream()

    monkeypatch.setattr(gateway.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(gateway, "_guarded_stream", fake_guarded_stream)
    out = Path(tmp_path) / "slow.mp4"

    try:
        gateway.download_to_path(
            "https://cdn.example.com/rendered.mp4",
            out,
            timeout_seconds=300,
            allowed_content_types=("video/",),
            low_speed_timeout_seconds=60,
            low_speed_min_bytes_per_second=16 * 1024,
        )
    except gateway.GatewayError as e:
        assert "速度过慢" in str(e)
    else:
        raise AssertionError("slow result downloads must be interrupted")

    assert not out.exists()


def test_download_to_path_drops_auth_on_cross_origin_redirect(monkeypatch, tmp_path):
    responses = [
        type(
            "Redirect",
            (),
            {
                "is_redirect": True,
                "status_code": 302,
                "headers": {"location": "https://cdn.example.com/final.mp4"},
            },
        )(),
        type(
            "Video",
            (),
            {
                "is_redirect": False,
                "status_code": 200,
                "headers": {"content-type": "video/mp4"},
                "iter_raw": lambda self: iter((b"video",)),
            },
        )(),
    ]
    seen_headers = []

    @gateway.contextmanager
    def fake_guarded_stream(_client, _method, _url, **kwargs):
        seen_headers.append(dict(kwargs.get("headers") or {}))
        yield responses.pop(0)

    monkeypatch.setattr(gateway, "assert_safe_url", lambda _url: None)
    monkeypatch.setattr(gateway, "_guarded_stream", fake_guarded_stream)
    out = Path(tmp_path) / "redirected.mp4"

    gateway.download_to_path(
        "https://provider.example.com/content",
        out,
        allowed_content_types=("video/",),
        request_headers={"Authorization": "Bearer secret"},
    )

    assert out.read_bytes() == b"video"
    assert seen_headers[0]["Authorization"] == "Bearer secret"
    assert "Authorization" not in seen_headers[1]
    assert all(headers["Accept-Encoding"] == "identity" for headers in seen_headers)


def test_download_to_path_allows_exact_trusted_result_host(monkeypatch, tmp_path):
    host = "ark-content-generation-cn-beijing.tos-cn-beijing.volces.com"
    seen = {}

    class Video:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "video/mp4"}

        def iter_raw(self):
            yield b"video"

    @gateway.contextmanager
    def fake_guarded_stream(_client, _method, url, **kwargs):
        seen["url"] = url
        seen["trusted_hosts"] = kwargs.get("trusted_hosts")
        yield Video()

    monkeypatch.setattr(
        gateway,
        "assert_safe_url",
        lambda _url: (_ for _ in ()).throw(AssertionError("trusted host used DNS guard")),
    )
    monkeypatch.setattr(gateway, "_guarded_stream", fake_guarded_stream)
    out = Path(tmp_path) / "trusted.mp4"

    gateway.download_to_path(
        f"https://{host}/rendered.mp4",
        out,
        allowed_content_types=("video/",),
        trusted_hosts=(host,),
    )

    assert out.read_bytes() == b"video"
    assert seen["trusted_hosts"] == (host,)


def test_download_to_path_rechecks_redirect_from_trusted_host(monkeypatch, tmp_path):
    host = "ark-content-generation-cn-beijing.tos-cn-beijing.volces.com"

    class Redirect:
        is_redirect = True
        status_code = 302
        headers = {"location": "http://127.0.0.1/private.mp4"}

    @gateway.contextmanager
    def fake_guarded_stream(_client, _method, _url, **_kwargs):
        yield Redirect()

    def fake_assert_safe_url(url):
        if "127.0.0.1" in url:
            raise gateway.SsrfError("禁止访问内网/保留地址")
        return url

    monkeypatch.setattr(gateway, "assert_safe_url", fake_assert_safe_url)
    monkeypatch.setattr(gateway, "_guarded_stream", fake_guarded_stream)
    out = Path(tmp_path) / "redirected-private.mp4"

    with pytest.raises(gateway.GatewayError, match="安全策略拦截"):
        gateway.download_to_path(
            f"https://{host}/rendered.mp4",
            out,
            allowed_content_types=("video/",),
            trusted_hosts=(host,),
        )

    assert not out.exists()


def test_download_uses_remaining_deadline_for_each_redirect(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)

    class FakeStream:
        def __init__(self, *, redirect=False):
            self.is_redirect = redirect
            self.status_code = 302 if redirect else 200
            self.headers = {"location": "https://cdn.example.com/final"} if redirect else {
                "content-type": "image/png",
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_raw(self):
            yield b"png"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    calls = []
    streams = [FakeStream(redirect=True), FakeStream()]
    now = {"value": 1000.0}

    def fake_monotonic():
        now["value"] += 3.0
        return now["value"]

    def fake_guarded_stream(_client, _method, _url, **kwargs):
        calls.append(kwargs["timeout"])
        return streams.pop(0)

    monkeypatch.setattr(gateway.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gateway.httpx, "Client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr(gateway, "_guarded_stream", fake_guarded_stream)

    assert gateway._download(
        "https://cdn.example.com/start",
        max_bytes=128,
        allowed_content_types=("image/",),
        timeout_seconds=30,
    ) == b"png"
    assert [call.connect for call in calls] == [27.0, 24.0]
    assert [call.read for call in calls] == [27.0, 24.0]


def test_download_rejects_compressed_result_and_uses_identity_header(monkeypatch):
    seen = {}

    class FakeStream:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "image/png", "content-encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_raw(self):
            yield b"compressed"

    @gateway.contextmanager
    def fake_guarded_stream(_client, method, url, **kwargs):
        seen.update({"method": method, "url": url, "headers": kwargs.get("headers")})
        yield FakeStream()

    monkeypatch.setattr(gateway, "_guarded_stream", fake_guarded_stream)

    try:
        gateway._download(
            "https://cdn.example.com/a.png",
            max_bytes=1024,
            allowed_content_types=("image/",),
        )
    except gateway.GatewayError as e:
        assert "压缩编码" in str(e)
    else:
        raise AssertionError("compressed media downloads must be rejected")

    assert seen["headers"]["Accept-Encoding"] == "identity"


def test_download_rejects_invalid_content_length(monkeypatch):
    class FakeStream:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "image/png", "content-length": "not-a-number"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_raw(self):
            yield b"png"

    @gateway.contextmanager
    def fake_guarded_stream(_client, _method, _url, **_kwargs):
        yield FakeStream()

    monkeypatch.setattr(gateway, "_guarded_stream", fake_guarded_stream)

    try:
        gateway._download(
            "https://cdn.example.com/a.png",
            max_bytes=1024,
            allowed_content_types=("image/",),
        )
    except gateway.GatewayError as e:
        assert "Content-Length 非法" in str(e)
    else:
        raise AssertionError("invalid Content-Length should be normalized")


def test_redact_url_for_log_strips_userinfo_query_and_fragment():
    redacted = redact_url_for_log("https://user:pass@example.com:8443/a.png?sig=secret#token")

    assert redacted == "https://example.com:8443/a.png?<redacted>#<redacted>"
    assert "user" not in redacted
    assert "pass" not in redacted
    assert "secret" not in redacted
    assert "token" not in redacted


def test_image_edit_repeats_without_n(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    image_raw = gateway._mock_image("x", "256x256", 0)
    raw = base64.b64encode(image_raw).decode()
    ref = f"data:image/png;base64,{base64.b64encode(image_raw).decode()}"
    calls = []

    def fake_request_multipart_json(method, url, *, headers, data, files, timeout, retries):
        calls.append((method, url, dict(data), list(files), timeout, retries, dict(headers)))
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_multipart_json", fake_request_multipart_json)
    imgs = gateway.gen_image(
        "same style",
        "gpt-image-2",
        n=3,
        size="256x256",
        reference_image_url=ref,
        edit_path="/v1/images/edits",
        extra_payload={"mask": ref},
    )

    assert len(imgs) == 3
    assert len(calls) == 3
    assert all(url.endswith("/v1/images/edits") for _method, url, _data, _files, _timeout, _retries, _headers in calls)
    assert all("n" not in data for _method, _url, data, _files, _timeout, _retries, _headers in calls)
    assert all(data["model"] == "gpt-image-2" for _method, _url, data, _files, _timeout, _retries, _headers in calls)
    assert all(data["prompt"] == "same style" for _method, _url, data, _files, _timeout, _retries, _headers in calls)
    assert all([name for name, _file in files] == ["image", "mask"]
               for _method, _url, _data, files, _timeout, _retries, _headers in calls)
    assert all(headers == {"Authorization": "Bearer test-key"}
               for _method, _url, _data, _files, _timeout, _retries, headers in calls)
    assert all(timeout == settings.image_gateway_timeout_seconds
               for _method, _url, _data, _files, timeout, _retries, _headers in calls)
    assert all(retries == 0 for _method, _url, _data, _files, _timeout, retries, _headers in calls)


@pytest.mark.parametrize(
    ("reference_image_url", "edit_path", "extra_payload", "error"),
    [
        (
            "data:image/png;base64,aW1hZ2U=",
            None,
            {},
            "未配置参考图编辑端点",
        ),
        (
            None,
            "/v1/images/edits",
            {"mask": "data:image/png;base64,bWFzaw=="},
            "蒙版编辑必须提供参考图",
        ),
        (
            "https://example.com/source.png",
            "/custom/images/edits",
            {
                "edit_payload_format": "json",
                "mask": "data:image/png;base64,bWFzaw==",
            },
            "蒙版编辑仅支持 multipart",
        ),
    ],
)
def test_image_edit_invalid_input_fails_before_http(
    monkeypatch,
    reference_image_url,
    edit_path,
    extra_payload,
    error,
):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    monkeypatch.setattr(
        gateway,
        "_post_single_image_repeated",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        gateway,
        "_post_single_image_multipart_repeated",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="openai",
        base_url="https://gateway.example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    with pytest.raises(gateway.GatewayError, match=error):
        gateway.gen_image(
            "edit the source image",
            "gpt-image-2",
            n=1,
            reference_image_url=reference_image_url,
            edit_path=edit_path,
            extra_payload=extra_payload,
            gateway_config=cfg,
        )

    assert calls == []


def test_image_edit_can_use_json_payload_format(monkeypatch):
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
        n=2,
        size="256x256",
        reference_image_url="http://example.com/ref.png",
        edit_path="/custom/images/edits",
        extra_payload={"edit_payload_format": "json"},
    )

    assert len(imgs) == 2
    assert len(calls) == 2
    assert all(payload["images"] == [{"image_url": "http://example.com/ref.png"}]
               for _method, _url, payload, _timeout, _retries in calls)


def test_image_edit_multipart_sends_all_reference_images(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    image_raw = gateway._mock_image("x", "256x256", 0)
    encoded = base64.b64encode(image_raw).decode()
    refs = [f"data:image/png;base64,{encoded}", f"data:image/png;base64,{encoded}"]
    calls = []

    def fake_request_multipart_json(method, url, *, headers, data, files, timeout, retries):
        calls.append((dict(data), list(files)))
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setattr(gateway, "_request_multipart_json", fake_request_multipart_json)
    images = gateway.gen_image(
        "transfer the second image style",
        "gpt-image-2",
        n=1,
        size="768x1024",
        reference_image_url=refs[0],
        reference_image_urls=refs,
        edit_path="/v1/images/edits",
    )

    assert len(images) == 1
    assert len(calls) == 1
    data, files = calls[0]
    assert data["size"] == "768x1024"
    assert [name for name, _file in files] == ["image", "image"]
    assert [file[0] for _name, file in files] == ["image-1.png", "image-2.png"]


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


def test_text_to_image_sends_quality_for_official_4k(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = []

    def fake_request_json(method, url, *, headers, payload, timeout, retries):
        calls.append(dict(payload))
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", fake_request_json)
    imgs = gateway.gen_image("a cat", "gpt-image-2", n=1, size="2160x3840")

    assert len(imgs) == 1
    assert calls[0]["size"] == "2160x3840"
    assert calls[0]["quality"] == "high"


def test_text_to_image_uses_per_model_gateway_config(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = []
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="custom_openai",
        base_url="https://model-gateway.example.com/v1",
        api_key="model-key",
        gateway_format="openai",
    )

    def fake_request_json(method, url, *, headers, payload, timeout, retries):
        calls.append((method, url, headers, dict(payload)))
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", fake_request_json)
    imgs = gateway.gen_image("a cat", "gpt-image-x", n=1, size="256x256", gateway_config=cfg)

    assert len(imgs) == 1
    assert calls[0][1] == "https://model-gateway.example.com/v1/images/generations"
    assert calls[0][2]["Authorization"] == "Bearer model-key"
    assert calls[0][3]["model"] == "gpt-image-x"


def test_text_to_image_rejects_incomplete_runtime_gateway_config(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="custom_openai",
        base_url="",
        api_key="",
        gateway_format="openai",
    )

    try:
        gateway.gen_image("a cat", "gpt-image-x", n=1, size="256x256", gateway_config=cfg)
    except gateway.GatewayError as e:
        assert "配置不完整" in str(e)
    else:
        raise AssertionError("incomplete per-model gateway config must not fall back to mock output")


def test_explicit_mock_mode_allows_incomplete_runtime_gateway_config(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    cfg = RuntimeGatewayConfig(
        use="image",
        provider="custom_openai",
        base_url="",
        api_key="",
        gateway_format="openai",
    )

    imgs = gateway.gen_image("a cat", "gpt-image-x", n=1, size="256x256", gateway_config=cfg)

    assert len(imgs) == 1
    assert imgs[0][:4] == b"\x89PNG"


def test_reverse_uses_per_model_gateway_config(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = {}
    cfg = RuntimeGatewayConfig(
        use="vision",
        provider="custom_openai",
        base_url="https://vision-gateway.example.com/v1",
        api_key="vision-key",
        gateway_format="openai",
    )

    def fake_post(path, payload, timeout=None, config=None, retries=None):
        seen["path"] = path
        seen["payload"] = payload
        seen["timeout"] = timeout
        seen["config"] = config
        seen["retries"] = retries
        return {
            "choices": [{
                "message": {"content": '{"final_text":"same style","主体":"cat"}'},
            }],
            "usage": {"total_tokens": 9},
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    res = gateway.reverse_prompt("https://example.com/ref.png", "vision-model", gateway_config=cfg)

    assert res["final_text"] == "cat"
    assert res["provider_final_text"] == "same style"
    assert seen["path"] == "/chat/completions"
    assert seen["timeout"] == settings.reverse_gateway_timeout_seconds
    assert seen["config"] is cfg
    assert seen["retries"] == settings.reverse_gateway_max_retries
    assert seen["payload"]["model"] == "vision-model"
    assert seen["payload"]["max_tokens"] == settings.reverse_gateway_max_tokens
    assert "reasoning_effort" not in seen["payload"]


def test_reverse_gpt5_completion_controls_bound_output_and_reasoning(monkeypatch):
    monkeypatch.setattr(settings, "reverse_gateway_max_tokens", 4096)
    monkeypatch.setattr(settings, "reverse_gateway_reasoning_effort", "medium")

    assert gateway._reverse_completion_controls("gpt-5.6-sol") == {
        "max_tokens": 4096,
        "reasoning_effort": "medium",
    }
    assert gateway._reverse_completion_controls("openai/gpt-5.5") == {
        "max_tokens": 4096,
        "reasoning_effort": "medium",
    }
    assert gateway._reverse_completion_controls("vision-model") == {
        "max_tokens": 4096,
    }


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
    monkeypatch.setattr(settings, "image_gateway_max_retries", 0)
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
    assert [failure.message for failure in imgs.failures] == ["temporary account unavailable"]
    assert calls["n"] == 4


def test_text_to_image_refills_empty_unsubmitted_slots(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "image_gateway_parallelism", 4)
    monkeypatch.setattr(settings, "image_gateway_refill_attempts", 2)
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = {"n": 0}

    def sometimes_empty(method, url, *, headers, payload, timeout, retries):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"data": []}
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", sometimes_empty)
    imgs = gateway.gen_image("a cat", "gpt-image-2", n=4, size="256x256")

    assert len(imgs) == 4
    assert calls["n"] == 5
    assert [failure.message for failure in imgs.failures] == ["图像子请求未返回结果"]


def test_text_to_image_batch_rejects_total_size_over_budget(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "image_gateway_parallelism", 1)
    monkeypatch.setattr(settings, "image_gateway_max_retries", 0)
    monkeypatch.setattr(settings, "generated_image_max_bytes", 1)
    monkeypatch.setattr(settings, "generated_image_batch_max_bytes", 8)
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()

    def fake_request_json(method, url, *, headers, payload, timeout, retries):
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", fake_request_json)
    try:
        gateway.gen_image("a cat", "gpt-image-2", n=2, size="256x256")
    except gateway.GatewayError as e:
        assert "未返回任何结果" in str(e)
    else:
        raise AssertionError("oversized batch should fail")


def test_text_to_image_retries_explicit_not_accepted_gateway_status(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "image_gateway_parallelism", 4)
    monkeypatch.setattr(settings, "image_gateway_max_retries", 1)
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = {"n": 0}

    def flaky_request_json(method, url, *, headers, payload, timeout, retries):
        calls["n"] += 1
        if calls["n"] == 2:
            raise gateway.GatewayError(
                "upstream 502",
                status_code=502,
                transient=True,
                submit_state_unknown=False,
            )
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", flaky_request_json)
    imgs = gateway.gen_image("a cat", "gpt-image-2", n=4, size="256x256")

    assert len(imgs) == 4
    assert calls["n"] == 5


def test_text_to_image_does_not_retry_unknown_submit_state(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "image_gateway_parallelism", 1)
    monkeypatch.setattr(settings, "image_gateway_max_retries", 1)
    raw = base64.b64encode(gateway._mock_image("x", "256x256", 0)).decode()
    calls = {"n": 0}

    def flaky_request_json(method, url, *, headers, payload, timeout, retries):
        calls["n"] += 1
        if calls["n"] == 1:
            raise gateway.GatewayError("read timed out", transient=True)
        return {"data": [{"b64_json": raw}]}

    monkeypatch.setattr(gateway, "_request_json", flaky_request_json)
    try:
        gateway.gen_image("a cat", "gpt-image-2", n=1, size="256x256")
    except gateway.GatewayError as e:
        assert "未返回任何结果" in str(e)
        assert "read timed out" in str(e)
    else:
        raise AssertionError("unknown submit state should not be retried")

    assert calls["n"] == 1


def test_text_to_image_does_not_retry_default_5xx_unknown_submit_state(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "image_gateway_parallelism", 1)
    monkeypatch.setattr(settings, "image_gateway_max_retries", 1)
    calls = {"n": 0}

    def flaky_request_json(method, url, *, headers, payload, timeout, retries):
        calls["n"] += 1
        raise gateway.GatewayError("upstream 502", status_code=502, transient=True)

    monkeypatch.setattr(gateway, "_request_json", flaky_request_json)
    try:
        gateway.gen_image("a cat", "gpt-image-2", n=1, size="256x256")
    except gateway.GatewayError as e:
        assert "upstream 502" in str(e)
    else:
        raise AssertionError("default 5xx submit state should not be retried")

    assert calls["n"] == 1


def test_download_to_storage_streams_even_when_effective_mock_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "")
    monkeypatch.setattr(settings, "gateway_api_key", "")
    calls = []

    def fail_bytes(*_args, **_kwargs):
        raise AssertionError("download_bytes_limited should not be used for storage downloads")

    def fake_download_to_path(url, path, **kwargs):
        calls.append((url, path, kwargs))
        path.write_bytes(b"video")
        return 5

    monkeypatch.setattr(gateway, "download_bytes_limited", fail_bytes)
    monkeypatch.setattr(gateway, "download_to_path", fake_download_to_path)

    key = gateway.download_to_storage("https://cdn.example.com/out.mp4", "video_preview", "mp4")

    assert key.startswith("video_preview/")
    assert calls and calls[0][0] == "https://cdn.example.com/out.mp4"


def test_download_to_storage_forwards_progress_callback(monkeypatch):
    calls = []
    progress = []

    def fake_download_to_path(url, path, **kwargs):
        calls.append(kwargs)
        kwargs["progress_callback"]()
        path.write_bytes(b"video")
        return 5

    monkeypatch.setattr(gateway, "download_to_path", fake_download_to_path)

    key = gateway.download_to_storage(
        "https://cdn.example.com/out.mp4",
        "video_preview",
        "mp4",
        progress_callback=lambda: progress.append("tick"),
    )

    assert key.startswith("video_preview/")
    assert calls and callable(calls[0]["progress_callback"])
    assert progress == ["tick"]
