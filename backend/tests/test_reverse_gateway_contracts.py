from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError

from app.config import settings
from app.schemas import ModelConfigIn, ReverseOperationCreate, ReverseOperationOut
from app.services import gateway, video_frames
from app.services.gateway_prompting import (
    ReverseResultValidationError,
    reverse_template,
    validate_reverse_result,
)
from app.services.model_gateway_config import RuntimeGatewayConfig


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        ("image", {"主体": "产品", "final_text": "商业产品图"}),
        ("video", {"主体": "产品", "shots": [], "final_text": "产品视频"}),
        ("product_profile", {"产品品类": "护肤品", "final_text": "产品身份锁定"}),
        ("portrait_profile", {"脸型五官": "椭圆脸", "final_text": "人物身份锁定"}),
        (
            "image_to_video",
            {
                "静态观察": "产品居中",
                "主体运动设计": "新设计:缓慢旋转",
                "镜头运动设计": "新设计:缓慢推近",
                "final_text": "单图转视频运动设计",
            },
        ),
    ],
)
def test_target_contracts_require_typed_core_fields(target, payload):
    result = validate_reverse_result(payload, target)
    assert result["final_text"]

    core_key = next(key for key in payload if key not in {"shots", "final_text"})
    invalid = {**payload, core_key: {"invalid": True}}
    with pytest.raises(ReverseResultValidationError):
        validate_reverse_result(invalid, target)


def test_profile_visual_prompts_keep_identity_prefix_and_negative_constraints():
    product = validate_reverse_result(
        {
            "产品品类": "精华瓶",
            "包装结构": "长方形瓶身",
            "负向": "Logo变形,多余产品",
            "final_text": "模型自由文本",
        },
        "product_profile",
    )
    portrait = validate_reverse_result(
        {
            "脸型五官": "椭圆脸,深棕色眼睛",
            "负向": "换脸,年龄突变",
            "final_text": "模型自由文本",
        },
        "portrait_profile",
    )

    assert product["final_text"].startswith("上传产品是唯一商品主角")
    assert "避免：Logo变形,多余产品" in product["final_text"]
    assert portrait["final_text"].startswith("保持上传人物身份稳定")
    assert "避免：换脸,年龄突变" in portrait["final_text"]


def test_reverse_json_decoder_accepts_one_bare_object_and_rejects_extra_content():
    valid = validate_reverse_result('\ufeff  {"主体":"猫","final_text":"猫的肖像"}  ', "image")
    assert valid["structured"]["主体"] == "猫"

    with pytest.raises(ReverseResultValidationError, match="无法解析反推 JSON"):
        validate_reverse_result('```json\n{"主体":"猫","final_text":"猫的肖像"}\n```', "image")
    with pytest.raises(ReverseResultValidationError, match="额外非空内容"):
        validate_reverse_result('{"主体":"猫","final_text":"ok"} 这是解释', "image")
    with pytest.raises(ReverseResultValidationError, match="final_text"):
        validate_reverse_result({"主体": "猫", "final_text": {"text": "wrong"}}, "image")


def test_reverse_operation_out_always_groups_lifecycle_timestamps():
    created_at = datetime(2026, 7, 16, 1, 2, 3, tzinfo=timezone.utc)
    finished_at = datetime(2026, 7, 16, 1, 3, 4, tzinfo=timezone.utc)
    payload = ReverseOperationOut(
        id=42,
        target="image",
        source_type="image",
        status="succeeded",
        created_at=created_at,
        updated_at=finished_at,
        started_at=created_at,
        finished_at=finished_at,
    ).model_dump(mode="json")

    assert payload["timestamps"] == {
        "created_at": created_at.isoformat(),
        "updated_at": finished_at.isoformat(),
        "started_at": created_at.isoformat(),
        "finished_at": finished_at.isoformat(),
    }
    assert payload["created_at"] == payload["timestamps"]["created_at"]
    assert payload["finished_at"] == payload["timestamps"]["finished_at"]


def _vision_config(**overrides) -> RuntimeGatewayConfig:
    values = {
        "use": "vision",
        "provider": "custom_openai",
        "base_url": "https://vision.example.com/v1",
        "api_key": "vision-key",
        "gateway_format": "openai",
    }
    values.update(overrides)
    return RuntimeGatewayConfig(**values)


def test_reverse_result_gets_at_most_one_text_only_repair(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    responses = iter([
        {
            "choices": [{"message": {"content": '{"final_text":"缺少核心主体字段"}'}}],
            "usage": {"total_tokens": 7},
        },
        {
            "choices": [{"message": {"content": '{"主体":"猫","final_text":"猫肖像"}'}}],
            "usage": {"total_tokens": 3},
        },
    ])

    def fake_post(_path, payload, **_kwargs):
        calls.append(payload)
        return next(responses)

    monkeypatch.setattr(gateway, "_post", fake_post)
    result = gateway.reverse_prompt("data:image/jpeg;base64,eA==", "vision", gateway_config=_vision_config())

    assert len(calls) == 2
    assert isinstance(calls[0]["messages"][0]["content"], list)
    assert isinstance(calls[1]["messages"][0]["content"], str)
    assert "image_url" not in calls[1]["messages"][0]["content"]
    assert result["repair_attempted"] is True
    assert result["repair_succeeded"] is True
    assert result["usage"]["total_tokens"] == 10


def test_reverse_repair_callback_runs_before_text_only_request(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    events = []
    responses = iter([
        {"choices": [{"message": {"content": '{"final_text":"missing core field"}'}}]},
        {"choices": [{"message": {"content": '{"主体":"猫","final_text":"猫肖像"}'}}]},
    ])

    def fake_post(*_args, **_kwargs):
        events.append("post")
        return next(responses)

    def before_repair():
        events.append("before_repair")
        return True

    monkeypatch.setattr(gateway, "_post", fake_post)
    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "vision",
        gateway_config=_vision_config(),
        before_repair=before_repair,
    )

    assert events == ["post", "before_repair", "post"]
    assert result["repair_succeeded"] is True


def test_reverse_cancel_before_repair_skips_second_gateway_request(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: (
            calls.append("post")
            or {"choices": [{"message": {"content": '{"final_text":"invalid"}'}}]}
        ),
    )
    caplog.set_level(logging.INFO, logger="gateway")

    with pytest.raises(gateway.GatewayError, match="修复前取消") as caught:
        gateway.reverse_prompt(
            "data:image/jpeg;base64,eA==",
            "vision",
            gateway_config=_vision_config(),
            before_repair=lambda: False,
            audit_context={"operation_id": 41, "preset": "standard", "cost_credits": 18},
        )

    assert calls == ["post"]
    assert caught.value.error_code == "CANCELED"
    assert caught.value.phase == "repairing"
    audit_events = [
        json.loads(record.getMessage().split("=", 1)[1])
        for record in caplog.records
        if record.getMessage().startswith("reverse_gateway_event=")
    ]
    failure = next(event for event in audit_events if event["status"] == "failed")
    assert failure == {
        "event": "reverse_gateway_call",
        "status": "failed",
        "operation_id": 41,
        "phase": "repairing",
        "error_code": "CANCELED",
        "target": "image",
        "preset": "standard",
        "cost_credits": 18,
        "latency_ms": failure["latency_ms"],
    }


def test_reverse_result_fails_after_one_unsuccessful_repair(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    calls = []

    def fake_post(_path, payload, **_kwargs):
        calls.append(payload)
        return {"choices": [{"message": {"content": "not-json"}}]}

    monkeypatch.setattr(gateway, "_post", fake_post)
    with pytest.raises(gateway.GatewayError, match="一次自动修复"):
        gateway.reverse_prompt("data:image/jpeg;base64,eA==", "vision", gateway_config=_vision_config())
    assert len(calls) == 2


def test_audio_evidence_gate_overrides_model_voice_sfx_and_shot_audio(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    payload = {
        "主体": "产品",
        "旁白": "模型虚构的旁白",
        "音效": "模型虚构的音效",
        "shots": [{
            "start_seconds": 0,
            "end_seconds": 1,
            "visual": "产品居中",
            "audio_cue": "模型虚构的音频",
            "evidence_frame_indices": [1],
            "confidence": 0.9,
        }],
        "final_text": (
            "产品居中。OCR:限时优惠。观察事实:第1帧可见产品。证据帧:1。"
            "旁白‘模型虚构的旁白’。SFX‘模型虚构的音效’"
        ),
    }
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        },
    )
    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "vision",
        target="video",
        gateway_config=_vision_config(),
        video_analysis={
            "source": {"duration_seconds": 1, "audio_analyzed": False},
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0}],
        },
    )

    assert result["structured"]["旁白"] == "未分析"
    assert result["structured"]["音效"] == "未分析"
    assert result["shots"][0]["audio_cue"] == "未分析"
    assert "模型虚构的旁白" not in result["final_text"]
    assert "模型虚构的音效" not in result["final_text"]
    assert "OCR" not in result["final_text"]
    assert "观察事实" not in result["final_text"]
    assert "证据帧" not in result["final_text"]


@pytest.mark.parametrize(
    "nonvisual_clause",
    [
        "观察事实为第1帧可见产品",
        "OCR识别文字为限时优惠",
        "背景配乐随镜头节奏增强",
        "伴随水滴声和鼓点卡点",
        "对白说今天真好",
    ],
)
def test_nonvisual_clause_variants_never_enter_video_generation_text(
    monkeypatch,
    nonvisual_clause,
):
    monkeypatch.setattr(settings, "mock_mode", False)
    payload = {
        "主体": "产品",
        "光线": "柔和侧光",
        "shots": [{
            "start_seconds": 0,
            "end_seconds": 1,
            "visual": "产品居中",
            "evidence_frame_indices": [1],
            "confidence": 0.9,
        }],
        "final_text": f"产品居中。{nonvisual_clause}。柔和侧光",
    }
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        },
    )

    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "vision",
        target="video",
        gateway_config=_vision_config(),
        video_analysis={
            "source": {"duration_seconds": 1, "audio_analyzed": False},
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0}],
        },
    )

    assert nonvisual_clause not in result["final_text"]
    assert "产品居中" in result["final_text"]
    assert "柔和侧光" in result["final_text"]
    assert result["structured"]["旁白"] == "未分析"
    assert result["structured"]["音效"] == "未分析"


def test_provider_prose_and_unknown_evidence_never_enter_visual_prompt(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    provider_text = (
        "女声轻声说欢迎回来；屏幕中央浮出限时优惠；"
        "OCR识别BUY NOW；证据帧显示红色产品"
    )
    payload = {
        "主体": "红色产品",
        "光线": "柔和侧光",
        "未知审计字段": "证据帧中有BUY NOW",
        "shots": [{
            "start_seconds": 0,
            "end_seconds": 1,
            "visual": "产品居中",
            "ocr": "BUY NOW",
            "audio_cue": "女声轻声说欢迎回来",
            "evidence_frame_indices": [1],
            "confidence": 0.9,
        }],
        "final_text": provider_text,
    }
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        },
    )

    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "vision",
        target="video",
        gateway_config=_vision_config(),
        video_analysis={
            "source": {"duration_seconds": 1, "audio_analyzed": False},
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0}],
        },
    )

    assert result["provider_final_text"] == provider_text
    assert result["structured"]["未知审计字段"] == "证据帧中有BUY NOW"
    assert result["shots"][0]["ocr"] == "BUY NOW"
    assert result["shots"][0]["audio_cue"] == "未分析"
    assert result["final_text"] == "红色产品；柔和侧光；1.000秒；镜头1: 产品居中"
    for forbidden in ("欢迎回来", "限时优惠", "BUY NOW", "OCR", "证据帧"):
        assert forbidden not in result["final_text"]


def test_single_frame_evidence_cannot_authorize_motion_camera_or_transition(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    payload = {
        "主体": "红色产品",
        "主体动作": "产品快速旋转",
        "镜头运动": "镜头环绕一周",
        "转场": "闪白转场",
        "shots": [{
            "start_seconds": 0,
            "end_seconds": 1,
            "visual": "产品居中",
            "action": "产品快速旋转",
            "camera": "镜头环绕一周",
            "lighting": "柔和侧光",
            "transition": "闪白转场",
            "evidence_frame_indices": [1, 2],
            "confidence": 0.95,
        }],
        "final_text": "产品快速旋转,镜头环绕一周,闪白转场",
    }
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        },
    )

    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "vision",
        target="video",
        gateway_config=_vision_config(),
        video_analysis={
            "analysis_mode": "keyframes",
            "source": {"duration_seconds": 1, "audio_analyzed": False},
            "sampled_frames": [
                {"index": 1, "timestamp_seconds": 0.5},
                {"index": 2, "timestamp_seconds": 0.5},
            ],
        },
    )

    assert len(result["shots"]) == 1
    assert result["shots"][0]["visual"] == "产品居中"
    assert result["shots"][0]["lighting"] == "柔和侧光"
    assert result["shots"][0]["action"] == ""
    assert result["shots"][0]["camera"] == ""
    assert result["shots"][0]["transition"] == ""
    assert set(result["unverified_temporal_fields"]) == {
        "主体动作", "镜头运动", "转场",
    }
    for forbidden in ("快速旋转", "环绕一周", "闪白转场"):
        assert forbidden not in result["final_text"]
    assert "产品居中" in result["final_text"]
    assert "柔和侧光" in result["final_text"]


def test_video_without_verified_shots_drops_temporal_claims_and_rebuilds_static_text(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    payload = {
        "主体": "白色产品",
        "场景背景": "灰色影棚",
        "主体动作": "产品突然旋转并飞出画面",
        "镜头运动": "镜头快速环绕",
        "剪辑节奏": "三次跳切",
        "时序分镜": "0-1秒旋转,1-2秒飞出",
        "shots": [],
        "final_text": "产品突然旋转并飞出画面,镜头快速环绕并三次跳切",
    }
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        },
    )

    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "vision",
        target="video",
        gateway_config=_vision_config(),
        video_analysis={
            "analysis_mode": "keyframes",
            "source": {"duration_seconds": 2, "audio_analyzed": False},
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0}],
        },
    )

    assert result["shots"] == []
    assert result["analysis_gaps"] == [{"start_seconds": 0.0, "end_seconds": 2.0}]
    assert set(result["unverified_temporal_fields"]) == {
        "主体动作",
        "镜头运动",
        "剪辑节奏",
        "时序分镜",
    }
    assert all(
        key not in result["structured"]
        for key in ("主体动作", "镜头运动", "剪辑节奏", "时序分镜")
    )
    assert "白色产品" in result["final_text"]
    assert "灰色影棚" in result["final_text"]
    assert "旋转" not in result["final_text"]
    assert "环绕" not in result["final_text"]
    assert "跳切" not in result["final_text"]


def test_mock_video_uses_the_same_evidence_and_audio_gates(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)

    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "mock-vision",
        target="video",
        video_analysis={
            "analysis_mode": "keyframes",
            "source": {"duration_seconds": 6, "audio_analyzed": False},
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0}],
        },
    )

    assert result["shots"] == []
    assert result["analysis_gaps"] == [{"start_seconds": 0.0, "end_seconds": 6.0}]
    assert result["structured"]["旁白"] == "未分析"
    assert result["structured"]["音效"] == "未分析"
    assert set(result["unverified_temporal_fields"]) >= {
        "主体动作",
        "可迁移主体动作",
        "镜头运动",
        "运动节奏",
        "时序分镜",
    }
    assert all(
        key not in result["structured"]
        for key in ("主体动作", "镜头运动", "运动节奏", "时序分镜")
    )
    assert "turns" not in result["final_text"]
    assert "dolly" not in result["final_text"]
    assert "6s" not in result["final_text"]


def test_image_to_video_has_explicit_single_image_evidence_and_audit_log(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(settings, "mock_mode", False)
    payload = {
        "静态观察": "产品居中",
        "主体运动设计": "新设计:产品缓慢旋转",
        "镜头运动设计": "新设计:镜头缓慢推近",
        "final_text": "保持产品外观稳定,设计缓慢旋转和推近运动",
    }
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        },
    )
    caplog.set_level(logging.INFO, logger="gateway")

    result = gateway.reverse_prompt(
        "data:image/jpeg;base64,eA==",
        "vision",
        target="image_to_video",
        gateway_config=_vision_config(),
        audit_context={"operation_id": 42, "preset": "quality", "cost_credits": 2},
    )

    analysis = result["video_analysis"]
    assert analysis["analysis_mode"] == "image_motion"
    assert analysis["source"] == {"source_type": "image", "audio_analyzed": False}
    assert analysis["sampled_frames"] == [{"index": 1, "timestamp_seconds": 0}]
    assert analysis["analysis_gaps"] == [
        {"message": "单图输入没有可观察的视频时间线"}
    ]
    success_events = [
        json.loads(record.getMessage().split("=", 1)[1])
        for record in caplog.records
        if record.getMessage().startswith("reverse_gateway_event=")
        and '"status":"ok"' in record.getMessage()
    ]
    assert success_events[0]["operation_id"] == 42
    assert success_events[0]["target"] == "image_to_video"
    assert success_events[0]["preset"] == "quality"
    assert success_events[0]["cost_credits"] == 2
    assert success_events[0]["error_code"] is None


def test_product_and_portrait_templates_are_isolated():
    product = reverse_template("product_profile")
    portrait = reverse_template("portrait_profile")
    assert "产品品类" in product and "脸型五官" not in product
    assert "脸型五官" in portrait and "包装结构" not in portrait


def test_vision_anthropic_is_rejected_by_schema_and_gateway(monkeypatch):
    with pytest.raises(ValidationError, match="不支持 Anthropic"):
        ModelConfigIn(
            use="vision",
            model_id="claude-vision",
            provider="anthropic",
            gateway_format="anthropic",
            cost_credits=2,
        )
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(gateway, "_post", lambda *_a, **_k: pytest.fail("不应调用网关"))
    with pytest.raises(gateway.GatewayError, match="不支持 Anthropic"):
        gateway.reverse_prompt(
            "data:image/jpeg;base64,eA==",
            "claude-vision",
            gateway_config=_vision_config(provider="anthropic", gateway_format="anthropic"),
        )


def test_workspace_snapshot_rejects_credentials_and_embedded_media():
    valid = ReverseOperationCreate(
        client_request_id="snapshot-valid-01",
        asset_url="https://cdn.example.com/ref.jpg",
        workspace_snapshot_v2={
            "version": 2,
            "creation_mode": "image",
            "assets": [{"url": "https://cdn.example.com/ref.jpg", "width": 100}],
        },
    )
    assert valid.workspace_snapshot_v2["version"] == 2

    for snapshot in ({"api_key": "secret"}, {"asset": {"preview": "data:image/png;base64,eA=="}}):
        with pytest.raises(ValidationError):
            ReverseOperationCreate(
                client_request_id="snapshot-invalid-01",
                asset_url="https://cdn.example.com/ref.jpg",
                workspace_snapshot_v2=snapshot,
            )
    with pytest.raises(ValidationError, match="64KB"):
        ReverseOperationCreate(
            client_request_id="snapshot-too-large-01",
            asset_url="https://cdn.example.com/ref.jpg",
            workspace_snapshot_v2={"final_text": "x" * (64 * 1024)},
        )


def test_frame_payload_budget_keeps_endpoint_and_scene_priority():
    mib = 1024 * 1024
    frames = [
        video_frames.SampledVideoFrame(b"x" * (4 * mib), 0, priority=3),
        video_frames.SampledVideoFrame(b"x" * (4 * mib), 1, priority=1),
        video_frames.SampledVideoFrame(b"x" * (4 * mib), 2, priority=2),
        video_frames.SampledVideoFrame(b"x" * (4 * mib), 3, priority=3),
    ]
    selected = video_frames.fit_frame_payload_budget(frames, max_bytes=12 * mib)
    assert [frame.timestamp_seconds for frame in selected] == [0, 2, 3]
    assert sum(len(frame.jpeg) for frame in selected) <= 12 * mib


def test_frame_payload_budget_reencodes_valid_jpegs_before_pruning():
    frames = []
    for index in range(4):
        image = Image.effect_noise((768, 768), 96 + index).convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=100, subsampling=0)
        frames.append(
            video_frames.SampledVideoFrame(
                output.getvalue(),
                float(index),
                priority=3 if index in {0, 3} else 1,
            )
        )
    initial_bytes = sum(len(frame.jpeg) for frame in frames)
    budget = initial_bytes // 2

    selected = video_frames.fit_frame_payload_budget(frames, max_bytes=budget)

    assert len(selected) == len(frames)
    assert sum(len(frame.jpeg) for frame in selected) <= budget
    assert all(
        len(after.jpeg) < len(before.jpeg)
        for before, after in zip(frames, selected, strict=True)
    )
    for frame in selected:
        with Image.open(BytesIO(frame.jpeg)) as image:
            image.verify()


@pytest.mark.skipif(not video_frames.FFMPEG, reason="ffmpeg not installed")
def test_grab_frame_caps_portrait_longest_edge_at_1024(tmp_path):
    source = tmp_path / "portrait.jpg"
    video = tmp_path / "portrait.mp4"
    output = tmp_path / "frame.jpg"
    Image.new("RGB", (600, 1200), "white").save(source, format="JPEG")
    encoded = subprocess.run(
        [
            video_frames.FFMPEG,
            "-y",
            "-loop",
            "1",
            "-i",
            str(source),
            "-t",
            "0.2",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        timeout=20,
    )
    assert encoded.returncode == 0

    assert video_frames._grab_frame(str(video), 0, str(output)) is True
    with Image.open(output) as frame:
        assert max(frame.size) == 1024
        assert frame.height == 1024
        assert frame.width < frame.height
