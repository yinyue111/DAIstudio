"""Mock gateway + watermark pipeline (no network)."""
import base64
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


def test_discovery_marks_only_explicit_media_models_as_importable():
    models = annotate_discovered_models(
        [
            {"id": "gemini-3.1-flash-image"},
            {"id": "grok-imagine-video-1.5"},
            {"id": "grok-imagine-edit"},
            {"id": "grok-4.5"},
        ],
        provider="grok",
        gateway_format="openai",
    )

    assert models[0]["recommended_uses"] == ["image"]
    assert models[0]["default_extra"]["image_transport"] == "grok_images"
    assert models[1]["recommended_uses"] == ["video"]
    assert models[2]["recommended_uses"] == []
    assert models[3]["recommended_uses"] == ["prompt"]


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
                {"index": 1, "timestamp_seconds": 0.0},
                {"index": 2, "timestamp_seconds": 10.004},
            ],
        },
        gateway_config=cfg,
    )

    text_items = [item["text"] for item in seen["content"] if item["type"] == "text"]
    assert any("720x960" in text and "3:4" in text and "10.054" in text for text in text_items)
    assert "第 1 帧，时间戳 0.000 秒" in text_items
    assert "第 2 帧，时间戳 10.004 秒" in text_items
    assert result["shots"][0]["visual"] == "液滴入水"
    assert result["shots"][-1]["end_seconds"] == 2.2
    assert result["analysis_gaps"] == [{"start_seconds": 2.2, "end_seconds": 10.054}]
    assert "shots" not in result["structured"]
    assert result["structured"]["时序分镜"].startswith("0.000-2.200s 液滴入水")
    assert "0.000-2.200s 液滴入水" not in result["final_text"]


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
    # when the model omits final_text, we synthesize from all dimensions
    r = gateway._parse_structured('{"主体":"a cat","光线":"soft","负向":"text"}')
    assert "a cat" in r["final_text"] and "soft" in r["final_text"]
    assert "text" in r["final_text"]  # negative appended


def test_video_reverse_template_keeps_generation_prompt_compact():
    template = gateway._reverse_template("video", n_frames=4)

    assert "220-360 个中文字符" in template
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


def test_video_reverse_removes_conflicting_provider_duration_from_final_text(monkeypatch):
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

    assert "10.000秒" in result["final_text"]
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


def test_compose_final_uses_reverse_dimension_order():
    r = gateway._parse_structured(
        '{"光线":"soft left key light","主体":"red product bottle",'
        '"图像类型":"产品图","反推重点":"产品优先",'
        '"场景背景":"white studio","风格":"premium product photo"}'
    )

    text = r["final_text"]
    assert text.index("图像类型") < text.index("反推重点") < text.index("主体")
    assert text.index("主体") < text.index("场景背景") < text.index("风格") < text.index("光线")


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
    assert seen["retries"] == settings.gateway_max_retries
    assert seen["payload"]["model"] == "vision-model"


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
