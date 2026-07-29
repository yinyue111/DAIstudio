import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.config import settings
from app.db import SessionLocal
from app.models import CreditTransaction, ModelConfig, ReverseOperation, User, UserPrompt
from app.routers import prompt
from app.schemas import ReverseIn
from app.services import (
    config_store,
    gateway,
    gateway_prompting,
    retention,
    reverse_operations,
    reverse_source_resolution,
    video_frames,
)


def _data_image_ref(payload: bytes, media_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _data_image_ref_with_hash(
    payload: bytes,
    media_type: str = "image/jpeg",
) -> tuple[str, str]:
    return _data_image_ref(payload, media_type), hashlib.sha256(payload).hexdigest()


def test_reverse_image_template_is_compact_and_generation_focused():
    template = gateway_prompting.reverse_template("image")

    assert "商业图片复刻分析器" in template
    assert '"图像类型"' in template
    assert "产品图" in template
    assert "人物图" in template
    assert "人物+产品混合图" in template
    assert '"商品服装"' in template
    assert '"人像意图"' in template
    assert '"人物比例"' in template
    assert '"身材体态"' in template
    assert '"体态线条"' in template
    assert '"服装结构"' in template
    assert '"服装覆盖"' in template
    assert '"妆发五官"' in template
    assert '"文字版式"' in template
    assert '"一致性约束"' in template
    assert "商业人像" in template
    assert "不写三围" in template
    assert "控制在 80-140 个中文字符" in template
    assert "可选字段直接省略" in template
    assert "最多 8 条" in template
    assert "通用质量修饰词" in template
    assert '"反推重点"' not in template
    assert '"广告目标"' not in template
    assert '"平台质感"' not in template
    assert '"标签"' not in template
    assert "直接可见事实" not in template
    assert "视觉估计" not in template
    assert "高置信推断" not in template
    assert '"身材曲线"' not in template
    assert '"尺码三围"' not in template
    assert '"露肤度"' not in template


def test_reverse_product_profile_template_extracts_subject_identity():
    template = gateway_prompting.reverse_template("product_profile")

    assert "产品身份档案" in template
    assert '"品牌Logo"' in template
    assert '"包装文字"' in template
    assert '"不可改项"' in template
    assert "上传产品是唯一商品主角" in template
    assert "替换参考素材原主体" in template


def test_video_reverse_cover_fallback_preserves_probed_source_analysis(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(video_frames, "available", lambda: True)
    monkeypatch.setattr(
        video_frames,
        "sample_video",
        lambda *_args, **_kwargs: video_frames.VideoSample(
            frames=(),
            source=video_frames.VideoMetadata(
                width=720,
                height=1280,
                duration_seconds=12.5,
                fps=24.0,
                has_audio=True,
            ),
        ),
    )
    monkeypatch.setattr(prompt, "_gateway_ref", lambda *_args, **_kwargs: "resolved:cover")

    refs, analysis = prompt._collect_refs(
        ReverseIn(
            asset_url="https://cdn.example.com/reference.mp4",
            fallback_image="https://cdn.example.com/cover.jpg",
            source_type="video",
            target="video",
        ),
        db=object(),
        user=User(id=1, phone="13800000000", status="active"),
        frame_budget=8,
        video_preset="standard",
        gateway_mock=False,
    )

    assert refs == ["resolved:cover"]
    assert analysis["source"] == {
        "width": 720,
        "height": 1280,
        "ratio": "9:16",
        "duration_seconds": 12.5,
        "fps": 24.0,
        "has_audio": True,
        "audio_analyzed": False,
    }
    assert analysis["sampled_frames"] == []
    assert analysis["analysis_mode"] == "cover_fallback"


def test_video_reverse_cover_fallback_marks_degraded_when_sampling_returns_none(monkeypatch):
    monkeypatch.setattr(video_frames, "available", lambda: True)
    monkeypatch.setattr(video_frames, "sample_video", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_gateway_ref", lambda *_args, **_kwargs: "resolved:cover")

    refs, analysis = prompt._collect_refs(
        ReverseIn(
            asset_url="https://cdn.example.com/reference.mp4",
            fallback_image="https://cdn.example.com/cover.jpg",
            source_type="video",
            target="video",
        ),
        db=object(),
        user=User(id=1, phone="13800000000", status="active"),
        gateway_mock=False,
    )

    assert refs == ["resolved:cover"]
    assert analysis["analysis_mode"] == "cover_fallback"
    assert analysis["sampled_frames"] == []
    assert analysis["degraded_reason"]


def test_video_reverse_cover_fallback_handles_sampler_exception(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(video_frames, "available", lambda: True)
    monkeypatch.setattr(
        video_frames,
        "sample_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("decode failed")),
    )
    monkeypatch.setattr(prompt, "_gateway_ref", lambda *_args, **_kwargs: "resolved:cover")

    refs, analysis = prompt._collect_refs(
        ReverseIn(
            asset_url="https://cdn.example.com/reference.mp4",
            fallback_image="https://cdn.example.com/cover.jpg",
            source_type="video",
            target="video",
        ),
        db=object(),
        user=User(id=1, phone="13800000000", status="active"),
        gateway_mock=False,
    )

    assert refs == ["resolved:cover"]
    assert analysis["analysis_mode"] == "cover_fallback"
    assert analysis["sampled_frames"] == []


def test_video_reverse_selection_error_never_falls_back_to_cover(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(video_frames, "available", lambda: True)
    monkeypatch.setattr(
        video_frames,
        "sample_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            video_frames.VideoSelectionError("视频分析片段结束时间超过素材时长")
        ),
    )
    monkeypatch.setattr(prompt, "_gateway_ref", lambda *_args, **_kwargs: "resolved:cover")

    with pytest.raises(HTTPException) as caught:
        prompt._collect_refs(
            ReverseIn(
                asset_url="https://cdn.example.com/reference.mp4",
                fallback_image="https://cdn.example.com/cover.jpg",
                source_type="video",
                target="video",
            ),
            db=object(),
            user=User(id=1, phone="13800000000", status="active"),
            gateway_mock=False,
        )

    assert caught.value.status_code == 422
    assert "超过素材时长" in str(caught.value.detail)


def test_video_reverse_local_cover_fallback_probes_source_when_sampler_returns_none(
    monkeypatch,
):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(video_frames, "available", lambda: True)
    monkeypatch.setattr(prompt.storage, "key_from_url", lambda _url: "generated/video.mp4")
    monkeypatch.setattr(
        prompt.asset_refs,
        "generated_video_reference_path",
        lambda *_args, **_kwargs: "/tmp/generated-video.mp4",
    )
    monkeypatch.setattr(video_frames, "sample_video_from_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        video_frames,
        "probe_media",
        lambda _path: {
            "width": 1080,
            "height": 1920,
            "duration_seconds": 15.0,
            "fps": 30.0,
            "has_audio": True,
        },
    )
    monkeypatch.setattr(prompt, "_gateway_ref", lambda *_args, **_kwargs: "resolved:cover")

    refs, analysis = prompt._collect_refs(
        ReverseIn(
            asset_url="http://testserver/api/uploads/generated/video.mp4",
            fallback_image="http://testserver/api/uploads/upload/cover.jpg",
            source_type="video",
            target="video",
        ),
        db=object(),
        user=User(id=1, phone="13800000000", status="active"),
        frame_budget=8,
        video_preset="standard",
        gateway_mock=False,
    )

    assert refs == ["resolved:cover"]
    assert analysis["source"] == {
        "width": 1080,
        "height": 1920,
        "ratio": "9:16",
        "duration_seconds": 15.0,
        "fps": 30.0,
        "has_audio": True,
        "audio_analyzed": False,
    }
    assert analysis["sampled_frames"] == []
    assert analysis["analysis_mode"] == "cover_fallback"


def test_video_reverse_local_upload_samples_frames_in_mock_mode(monkeypatch):
    monkeypatch.setattr(video_frames, "available", lambda: True)
    monkeypatch.setattr(prompt.storage, "key_from_url", lambda _url: "upload_video/local.mp4")
    monkeypatch.setattr(
        prompt.asset_refs,
        "generated_video_reference_path",
        lambda *_args, **_kwargs: "/tmp/local-video.mp4",
    )
    sampled = []

    def _sample(path, *, n, preset):
        sampled.append((path, n, preset))
        return video_frames.VideoSample(
            frames=(
                video_frames.SampledVideoFrame(
                    jpeg=b"local-frame",
                    timestamp_seconds=0.0,
                    priority=3,
                ),
            ),
            source=video_frames.VideoMetadata(
                width=640,
                height=640,
                duration_seconds=3.0,
                fps=24.0,
                has_audio=False,
            ),
        )

    monkeypatch.setattr(video_frames, "sample_video_from_path", _sample)

    refs, analysis = prompt._collect_refs(
        ReverseIn(
            asset_url="http://testserver/api/uploads/upload_video/local.mp4",
            fallback_image="http://testserver/api/uploads/upload_video_preview/local.jpg",
            source_type="video",
            target="video",
        ),
        db=object(),
        user=User(id=1, phone="13800000000", status="active"),
        frame_budget=8,
        video_preset="standard",
        gateway_mock=True,
    )

    assert sampled == [("/tmp/local-video.mp4", 8, "standard")]
    assert refs == ["data:image/jpeg;base64,bG9jYWwtZnJhbWU="]
    assert analysis["analysis_mode"] == "keyframes"
    assert analysis["source"]["audio_analyzed"] is False
    assert analysis["sampled_frames"] == [{
        "index": 1,
        "timestamp_seconds": 0.0,
        "absolute_timestamp_seconds": 0.0,
    }]


@pytest.mark.parametrize("sampler_available", [False, True])
def test_video_reverse_cover_fallback_always_reports_degraded_analysis(
    monkeypatch,
    sampler_available,
):
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(video_frames, "available", lambda: sampler_available)
    monkeypatch.setattr(video_frames, "sample_video", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_gateway_ref", lambda *_args, **_kwargs: "resolved:cover")

    refs, analysis = prompt._collect_refs(
        ReverseIn(
            asset_url="https://cdn.example.com/reference.mp4",
            fallback_image="https://cdn.example.com/cover.jpg",
            source_type="video",
            target="video",
        ),
        db=object(),
        user=User(id=1, phone="13800000000", status="active"),
        frame_budget=8,
        video_preset="standard",
    )

    assert refs == ["resolved:cover"]
    assert analysis["analysis_mode"] == "cover_fallback"
    assert analysis["sampled_frames"] == []
    assert analysis["source"]["duration_seconds"] is None
    assert "封面" in analysis["degraded_reason"]


def test_reverse_rejects_video_url_before_gateway(monkeypatch):
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(gateway.GatewayError("called gateway")),
    )

    with pytest.raises(HTTPException) as exc:
        prompt.reverse(
            ReverseIn(asset_url="https://sns-video-zl.xhscdn.com/stream/test.mp4?sign=abc"),
            db=object(),
            user=User(id=1, phone="13800000000", status="active"),
        )

    assert exc.value.status_code == 400
    assert "视频素材仅支持视频反推" in exc.value.detail


def test_reverse_source_type_video_uses_video_template_without_suffix(client, make_user, monkeypatch):
    seen = {}
    uid = make_user("13800000011", balance=100)

    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: (
            [
                _data_image_ref(b"frame-1", "image/png"),
                _data_image_ref(b"frame-2", "image/png"),
            ],
            {
                "analysis_mode": "multi_frame",
                "source": {"audio_analyzed": False},
                "sampled_frames": [
                    {"index": 0, "timestamp_seconds": 0.0},
                    {"index": 1, "timestamp_seconds": 1.0},
                ],
            },
        ),
    )
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)

    def fake_reverse(refs, model_id, target="image"):
        seen.update({"refs": refs, "model_id": model_id, "target": target})
        return {"structured": {"主体": "x"}, "final_text": "x"}

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(
                asset_url="https://cdn.example.com/video-stream?id=1",
                target="video",
                source_type="video",
                fallback_image="https://cdn.example.com/cover.png",
            ),
            db=db,
            user=db.get(User, uid),
        )
    finally:
        db.close()

    assert out.final_text == "x"
    assert seen["target"] == "video"
    assert seen["refs"] == [
        _data_image_ref(b"frame-1", "image/png"),
        _data_image_ref(b"frame-2", "image/png"),
    ]


def test_reverse_product_profile_uses_single_image_ref(client, make_user, monkeypatch):
    seen = {}
    uid = make_user("13800000012", balance=100)

    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "gateway_ref_with_content_hash",
        lambda db, user, url: _data_image_ref_with_hash(b"product", "image/png"),
    )
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_operations, "_remember_history", lambda *_args, **_kwargs: None)

    def fake_reverse(refs, model_id, target="image"):
        seen.update({"refs": refs, "model_id": model_id, "target": target})
        return {
            "structured": {"产品品类": "棉柔巾", "包装文字": "黑魔法"},
            "final_text": "上传产品是唯一商品主角: 黑魔法棉柔巾包装文字完整保留",
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(
                asset_url="https://cdn.example.com/product.jpg",
                target="product_profile",
                source_type="image",
            ),
            db=db,
            user=db.get(User, uid),
        )
    finally:
        db.close()

    assert out.charged_credits == 5
    assert out.reference_count == 1
    assert out.final_text.startswith("上传产品是唯一商品主角")
    assert seen["target"] == "product_profile"
    assert seen["refs"] == [_data_image_ref(b"product", "image/png")]


def test_reverse_video_usage_cost_is_not_multiplied_by_frame_count(client, make_user, monkeypatch):
    uid = make_user("13800000002", balance=100)
    db = SessionLocal()
    try:
        model = db.query(ModelConfig).filter(ModelConfig.use == "vision").one()
        model.model_id = "gpt-5.5"
        model.cost_credits = 1
        model.enabled = True
        model.extra = {
            "official_pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "total_per_1m": 100_000,
                "credit_value_cny": 1,
            }
        }
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_source_resolution, "collect_refs", lambda *_args, **_kwargs: [
        _data_image_ref(b"frame-a"),
        _data_image_ref(b"frame-b"),
        _data_image_ref(b"frame-c"),
        _data_image_ref(b"frame-d"),
    ])
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(
                asset_url="https://cdn.example.com/video.mp4",
                target="video",
                source_type="video",
            ),
            db=db,
            user=db.get(User, uid),
        )
        user = db.get(User, uid)
        assert out.reference_count == 4
        assert out.charged_credits == 5
        assert user.balance_credits == 95
    finally:
        db.close()


def test_reverse_blocks_unsafe_gateway_output_before_history(client, make_user, monkeypatch):
    uid = make_user("13800000004", balance=100)
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"unsafe-output-source")],
    )
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "forbidden-output product"},
            "final_text": "forbidden-output product poster",
            "usage": {"total_tokens": 10},
        },
    )

    db = SessionLocal()
    try:
        config_store.set_settings(
            db,
            {
                "reverse_prompt_enabled": True,
                "content_safety_enabled": True,
                "content_safety_banned_terms": "forbidden-output",
            },
        )

        with pytest.raises(HTTPException) as exc:
            prompt.reverse(
                ReverseIn(asset_url="https://cdn.example.com/a.jpg", target="image"),
                db=db,
                user=db.get(User, uid),
            )

        assert exc.value.status_code == 400
        user = db.get(User, uid)
        assert user.balance_credits == 100
        assert (
            db.query(UserPrompt)
            .filter(UserPrompt.user_id == uid, UserPrompt.source == "reverse")
            .count()
            == 0
        )
    finally:
        config_store.set_settings(
            db,
            {"content_safety_enabled": False, "content_safety_banned_terms": ""},
        )
        db.close()


def test_reverse_usage_metadata_does_not_override_fixed_image_price(client, make_user, monkeypatch):
    uid = make_user("13800000003", balance=5)
    db = SessionLocal()
    try:
        model = db.query(ModelConfig).filter(ModelConfig.use == "vision").one()
        model.model_id = "gpt-5.5"
        model.cost_credits = 1
        model.enabled = True
        model.extra = {
            "official_pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "total_per_1m": 1_000_000,
                "credit_value_cny": 1,
            }
        }
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"fixed-price-source")],
    )
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(asset_url="https://cdn.example.com/a.jpg", target="image"),
            db=db,
            user=db.get(User, uid),
        )
        user = db.get(User, uid)
        assert out.charged_credits == 5
        assert user.balance_credits == 0
    finally:
        db.close()


def test_reverse_client_request_id_replays_without_double_charge(client, make_user, auth, monkeypatch):
    uid = make_user("13800000005", balance=100)
    headers = auth("13800000005")
    calls = {"gateway": 0, "rate": 0}

    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"replay-source")],
    )

    def fake_rate(*_args, **_kwargs):
        calls["rate"] += 1
        return calls["rate"]

    def fake_reverse(*_args, **_kwargs):
        calls["gateway"] += 1
        return {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(prompt, "incr_window", fake_rate)
    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)
    body = {
        "client_request_id": "reverse-retry-001",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }

    first = client.post("/api/prompt/reverse", json=body, headers=headers)
    assert first.status_code == 200, first.text
    second = client.post("/api/prompt/reverse", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert calls == {"gateway": 1, "rate": 1}

    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 95
        assert db.query(ReverseOperation).filter_by(client_request_id="reverse-retry-001").count() == 1
    finally:
        db.close()


def test_legacy_reverse_http_error_includes_deprecation_headers_and_logs_call(
    client,
    make_user,
    auth,
    monkeypatch,
    caplog,
):
    uid = make_user("13800000016", balance=100)
    headers = auth("13800000016")
    caplog.set_level("WARNING", logger="prompt")

    def reject_by_current_asset_policy(*_args, **_kwargs):
        raise HTTPException(400, "素材不符合当前安全策略")

    monkeypatch.setattr(
        prompt,
        "_validate_reverse_asset_request",
        reject_by_current_asset_policy,
    )
    response = client.post(
        "/api/prompt/reverse",
        json={
            "client_request_id": "legacy-reverse-deprecation-error-001",
            "asset_url": "https://cdn.example.com/rejected.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "素材不符合当前安全策略"
    assert response.headers["deprecation"] == "true"
    assert response.headers["sunset"] == "Wed, 16 Sep 2026 00:00:00 GMT"
    assert response.headers["link"] == (
        '</api/prompt/reverse-operations>; rel="successor-version"'
    )
    assert any(
        record.name == "prompt"
        and "deprecated reverse endpoint called" in record.getMessage()
        and f"user_id={uid}" in record.getMessage()
        and "client_request_id=legacy-reverse-deprecation-error-001" in record.getMessage()
        and "target=image" in record.getMessage()
        for record in caplog.records
    )


def test_reverse_empty_structured_result_fails_and_refunds_without_settlement(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13800000015", balance=100)
    headers = auth("13800000015")
    calls = {"gateway": 0}

    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"empty-result-source")],
    )

    def fake_reverse(*_args, **_kwargs):
        calls["gateway"] += 1
        return {
            "structured": {},
            "final_text": "上游返回的纯文本降级提示词",
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)
    body = {
        "client_request_id": "reverse-text-fallback-001",
        "asset_url": "https://cdn.example.com/fallback.jpg",
        "target": "image",
    }

    first = client.post("/api/prompt/reverse", json=body, headers=headers)
    second = client.post("/api/prompt/reverse", json=body, headers=headers)

    assert first.status_code == 502, first.text
    assert second.status_code == 409, second.text
    assert calls["gateway"] == 1
    db = SessionLocal()
    try:
        operation = db.query(ReverseOperation).filter_by(
            client_request_id="reverse-text-fallback-001"
        ).one()
        assert operation.status == "failed"
        assert operation.error_code == "INVALID_REVERSE_RESULT"
        assert operation.cost_frozen == 0
        assert operation.cost_settled == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse_operation",
            biz_ref=operation.id,
            type="refund",
        ).count() == 1
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse_operation",
            biz_ref=operation.id,
            type="settle",
        ).count() == 0
    finally:
        db.close()


def test_reverse_without_client_request_id_persists_operation_and_uses_it_for_billing(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13800000008", balance=100)
    headers = auth("13800000008")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"no-request-id-source")],
    )
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)

    response = client.post(
        "/api/prompt/reverse",
        json={"asset_url": "https://cdn.example.com/no-id.jpg", "target": "image"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    db = SessionLocal()
    try:
        operation = db.query(ReverseOperation).filter_by(user_id=uid).one()
        assert operation.client_request_id is None
        assert operation.status == "succeeded"
        assert operation.charged_credits == 5
        frozen = db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse_operation",
            type="freeze",
        ).one()
        settled = db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse_operation",
            type="settle",
        ).one()
        assert frozen.biz_ref == operation.id
        assert settled.biz_ref == operation.id
        assert settled.real_cost == 5
        assert db.get(User, uid).balance_credits == 95
    finally:
        db.close()


def test_reverse_client_request_id_rejects_different_payload(client, make_user, auth, monkeypatch):
    make_user("13800000006", balance=100)
    headers = auth("13800000006")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"conflict-source")],
    )
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)

    body = {
        "client_request_id": "reverse-retry-002",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }
    assert client.post("/api/prompt/reverse", json=body, headers=headers).status_code == 200
    conflict = client.post(
        "/api/prompt/reverse",
        json={**body, "asset_url": "https://cdn.example.com/b.jpg"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert "client_request_id 已用于不同反推请求" in conflict.text


def test_reverse_http_exception_refunds_and_marks_operation_failed(client, make_user, auth, monkeypatch):
    uid = make_user("13800000007", balance=100)
    headers = auth("13800000007")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"provider-error-source")],
    )
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(502, "provider rejected")),
    )
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)

    body = {
        "client_request_id": "reverse-retry-003",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }
    failed = client.post("/api/prompt/reverse", json=body, headers=headers)
    assert failed.status_code == 502

    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100
        op = db.query(ReverseOperation).filter_by(client_request_id="reverse-retry-003").one()
        assert op.status == "failed"
        assert op.charged_credits == 0
        assert "provider rejected" in (op.error or "")
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse_operation",
            biz_ref=op.id,
            type="refund",
        ).count() == 1
    finally:
        db.close()


def test_reverse_completion_cannot_overwrite_stale_refund(client, make_user, auth, monkeypatch):
    uid = make_user("13800000009", balance=100)
    headers = auth("13800000009")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_source_resolution,
        "collect_refs",
        lambda *_args, **_kwargs: [_data_image_ref(b"race-refund-source")],
    )
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)

    def reverse_after_reaper(*_args, **_kwargs):
        reaper_db = SessionLocal()
        try:
            operation = reaper_db.query(ReverseOperation).filter_by(
                client_request_id="reverse-race-refund"
            ).one()
            operation.updated_at = datetime.now(timezone.utc) - timedelta(minutes=31)
            reaper_db.commit()
            assert reverse_operations.reap_operations()["failed"] == 1
        finally:
            reaper_db.close()
        return {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(gateway, "reverse_prompt", reverse_after_reaper)

    response = client.post(
        "/api/prompt/reverse",
        json={
            "client_request_id": "reverse-race-refund",
            "asset_url": "https://cdn.example.com/race-refund.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 502, response.text
    db = SessionLocal()
    try:
        operation = db.query(ReverseOperation).filter_by(
            client_request_id="reverse-race-refund"
        ).one()
        assert operation.status == "failed"
        assert operation.error_code == "OPERATION_TIMEOUT"
        assert operation.charged_credits == 0
        assert operation.cost_frozen == 0
        assert operation.cost_settled == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.query(UserPrompt).filter_by(user_id=uid, source="reverse").count() == 0
    finally:
        db.close()
