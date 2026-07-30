"""video_to_video 真通道：源视频 URL 必须进入上游 payload，失败必须显式报错。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import ModelConfig, UploadedAsset
from app.services import gateway, storage
from app.services import generation_video_submit as submit_mod
from app.services.gateway_video_payloads import (
    ark_content,
    ark_payload,
    generic_video_payload_params,
)
from app.services.model_gateway_config import encrypt_api_key
from app.services.model_routes import ensure_legacy_route
from app.services.model_versions import sync_model_versions

# ---------------------------------------------------------------------------
# generic（OpenAI 风格）payload 构建
# ---------------------------------------------------------------------------

def test_generic_payload_maps_source_video_to_default_field():
    payload = generic_video_payload_params(
        {"duration": 5, "source_video_url": "https://cdn.example.com/source.mp4"},
        {},
    )
    assert payload["video_url"] == "https://cdn.example.com/source.mp4"
    assert "source_video_url" not in payload


def test_generic_payload_maps_source_video_to_configured_field_with_item_field():
    payload = generic_video_payload_params(
        {"source_video_url": "https://cdn.example.com/source.mp4"},
        {"video_url_field": "video", "video_url_item_field": "url"},
    )
    assert payload["video"] == {"url": "https://cdn.example.com/source.mp4"}
    assert "video_url" not in payload


def test_generic_payload_rejects_explicitly_disabled_video_field():
    with pytest.raises(ValueError, match="未配置源视频上游字段"):
        generic_video_payload_params(
            {"source_video_url": "https://cdn.example.com/source.mp4"},
            {"video_url_field": ""},
        )


def test_generic_payload_rejects_video_field_collision():
    with pytest.raises(ValueError, match="同一上游字段"):
        generic_video_payload_params(
            {
                "source_video_url": "https://cdn.example.com/source.mp4",
                "first_frame_image": "https://cdn.example.com/first.png",
            },
            {"first_frame_field": "video_url"},
        )


def test_generic_payload_does_not_pass_source_video_through_allowlist():
    # 即使管理员把 source_video_url 加进 allowed_param_fields，也只能走显式通道。
    payload = generic_video_payload_params(
        {"source_video_url": "https://cdn.example.com/source.mp4"},
        {"allowed_param_fields": ["source_video_url"]},
    )
    assert payload == {"video_url": "https://cdn.example.com/source.mp4"}


def test_generic_submit_video_sends_source_video_field(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["payload"] = payload
        return {"id": "task-v2v"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "remake this video",
        "video-model",
        {
            "duration": 5,
            "source_video_url": "https://cdn.example.com/source.mp4",
            "first_frame_image": "https://cdn.example.com/first.png",
        },
    )

    assert task_id == "task-v2v"
    assert seen["payload"]["video_url"] == "https://cdn.example.com/source.mp4"
    assert seen["payload"]["first_frame_image"] == "https://cdn.example.com/first.png"
    assert "source_video_url" not in seen["payload"]


def test_grok_video_edit_uses_edit_endpoint_and_official_payload(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://api.x.ai")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["path"] = path
        seen["payload"] = payload
        return {"id": "task-grok-edit"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    task_id = gateway.submit_video(
        "replace the background",
        "grok-imagine-video",
        {
            "source_video_url": "https://cdn.example.com/source.mp4",
            "duration": 8,
            "ratio": "9:16",
            "resolution": "720p",
            "seed": 42,
            "request_id": "must-not-be-sent",
        },
        extra={"submit_path": "/v1/videos/generations"},
    )

    assert task_id == "task-grok-edit"
    assert seen["path"] == "/v1/videos/edits"
    assert seen["payload"] == {
        "model": "grok-imagine-video",
        "prompt": "replace the background",
        "video": {"url": "https://cdn.example.com/source.mp4"},
    }


def test_grok_model_id_does_not_override_explicit_non_grok_provider_route(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    config = gateway.RuntimeGatewayConfig(
        use="video",
        provider="custom_openai",
        base_url="https://video.example.com/v1",
        api_key="test-key",
        gateway_format="openai",
    )

    def fake_post(path, payload, timeout=120, config=None):
        seen["path"] = path
        seen["payload"] = payload
        return {"id": "task-custom-edit"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    gateway.submit_video(
        "restyle this clip",
        "grok-imagine-video",
        {"source_video_url": "https://cdn.example.com/source.mp4"},
        extra={
            "submit_path": "/v1/custom/video/jobs",
            "video_url_field": "source_video",
        },
        gateway_config=config,
    )

    assert seen["path"] == "/v1/custom/video/jobs"
    assert seen["payload"]["source_video"] == "https://cdn.example.com/source.mp4"
    assert "video" not in seen["payload"]


def test_grok_video_generation_without_source_keeps_generation_endpoint(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://api.x.ai")
    monkeypatch.setattr(settings, "video_gateway_api_key", "test-key")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")

    def fake_post(path, payload, timeout=120):
        seen["path"] = path
        seen["payload"] = payload
        return {"id": "task-grok-generate"}

    monkeypatch.setattr(gateway, "_video_post", fake_post)

    gateway.submit_video(
        "a cinematic ocean",
        "grok-imagine-video",
        {"duration": 8, "ratio": "9:16", "resolution": "720p"},
    )

    assert seen["path"] == "/v1/videos/generations"
    assert seen["payload"]["duration"] == 8
    assert seen["payload"]["aspect_ratio"] == "9:16"
    assert seen["payload"]["resolution"] == "720p"


def test_quote_rejects_grok_edit_with_independent_image_reference(
    client,
    make_user,
    auth,
):
    phone = "13900003991"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    video_key = "upload_video/grok-edit-source.mp4"
    image_key = "upload/grok-edit-style.png"
    source_url = storage.upload_api_url(video_key)
    style_url = storage.upload_api_url(image_key)

    with SessionLocal() as db:
        model = db.query(ModelConfig).filter(ModelConfig.use == "video").one()
        model_id = int(model.id)
        original = {
            "model_id": model.model_id,
            "provider": model.provider,
            "base_url": model.base_url,
            "api_key_encrypted": model.api_key_encrypted,
            "gateway_format": model.gateway_format,
            "extra": deepcopy(model.extra),
        }
        model.model_id = "grok-imagine-video"
        model.provider = "grok"
        model.base_url = "https://api.x.ai/v1"
        model.api_key_encrypted = encrypt_api_key("grok-edit-test-key")
        model.gateway_format = "openai"
        model.extra = {
            **dict(model.extra or {}),
            "capabilities": {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 7,
                "video_to_video": True,
                "video_reference": False,
                "video_edit": True,
                "audio_reference": False,
                "max_reference_videos": 1,
                "max_reference_audio": 0,
            },
        }
        db.add_all(
            [
                UploadedAsset(
                    key=video_key,
                    user_id=user_id,
                    mime="video/mp4",
                    bytes=10,
                    original_filename="source.mp4",
                ),
                UploadedAsset(
                    key=image_key,
                    user_id=user_id,
                    mime="image/png",
                    bytes=10,
                    original_filename="style.png",
                ),
            ]
        )
        sync_model_versions(db, model)
        db.commit()

    try:
        response = client.post(
            "/api/quotes",
            headers=headers,
            json={
                "model_config_id": model_id,
                "source_asset_url": source_url,
                "source_type": "video",
                "category": "video",
                "stage": "preview",
                "prompt": {"final_text": "replace the background"},
                "params": {
                    "duration": 5,
                    "resolution": "720p",
                    "ratio": "9:16",
                    "style_reference_image": style_url,
                },
            },
        )

        assert response.status_code == 400, response.text
        assert "不能同时提交独立参考图" in response.text
    finally:
        with SessionLocal() as db:
            model = db.get(ModelConfig, model_id)
            if model is not None:
                model.model_id = original["model_id"]
                model.provider = original["provider"]
                model.base_url = original["base_url"]
                model.api_key_encrypted = original["api_key_encrypted"]
                model.gateway_format = original["gateway_format"]
                model.extra = original["extra"]
                sync_model_versions(db, model)
                ensure_legacy_route(db, model)
            db.query(UploadedAsset).filter(
                UploadedAsset.key.in_([video_key, image_key])
            ).delete(synchronize_session=False)
            db.commit()


# ---------------------------------------------------------------------------
# Ark（Seedance）payload 构建
# ---------------------------------------------------------------------------

def test_ark_content_appends_reference_video_item():
    content = ark_content(
        "remake",
        {
            "first_frame_image": "http://x/first.png",
            "source_video_url": "https://cdn.example.com/source.mp4",
        },
    )
    assert [item["type"] for item in content] == ["text", "image_url", "video_url"]
    assert content[2]["video_url"] == {"url": "https://cdn.example.com/source.mp4"}
    assert content[2]["role"] == "reference_video"
    assert "参考视频" in content[0]["text"]


def test_ark_payload_includes_reference_video():
    payload = ark_payload(
        "doubao-seedance-2-0-260128",
        "remake",
        {"duration": 5, "source_video_url": "https://cdn.example.com/source.mp4"},
    )
    videos = [item for item in payload["content"] if item.get("type") == "video_url"]
    assert videos == [
        {
            "type": "video_url",
            "video_url": {"url": "https://cdn.example.com/source.mp4"},
            "role": "reference_video",
        }
    ]


def test_ark_content_without_source_video_is_unchanged():
    content = ark_content("plain", {"first_frame_image": "http://x/first.png"})
    assert all(item.get("type") != "video_url" for item in content)
    assert "参考视频" not in content[0]["text"]


# ---------------------------------------------------------------------------
# 提交侧：源视频 URL 解析与显式失败
# ---------------------------------------------------------------------------

class _FakeDb:
    def __init__(self, upload_row=None):
        self._upload_row = upload_row

    def get(self, _model, _key):
        return self._upload_row


def _task(**overrides):
    defaults = {
        "id": 7,
        "user_id": 42,
        "stage": "final",
        "source_type": "video",
        "source_asset_url": "http://localhost:8000/media/upload_video/a.mp4",
        "params": {},
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_gateway_source_video_url_returns_presigned_url(monkeypatch):
    monkeypatch.setattr(
        submit_mod.storage, "key_from_url", lambda _url: "upload_video/a.mp4"
    )
    monkeypatch.setattr(
        submit_mod.storage, "is_object_storage_enabled", lambda: True
    )
    monkeypatch.setattr(
        submit_mod.storage,
        "presigned_download_url",
        lambda key, expires=300: f"https://cdn.example.com/{key}?sig=1&ttl={expires}",
    )
    db = _FakeDb(upload_row=SimpleNamespace(user_id=42))

    url = submit_mod.gateway_source_video_url(db, _task())

    assert url.startswith("https://cdn.example.com/upload_video/a.mp4")
    assert f"ttl={submit_mod.VIDEO_SOURCE_URL_TTL_SECONDS}" in url


def test_gateway_source_video_url_signals_unavailable_on_local_storage(monkeypatch):
    """本地存储 + 上传原片：抛专用异常，让调用方能可见降级而不是整体失败。

    这一类是"部署能力不足"而非"请求非法"，所以用 SourceVideoUrlUnavailable
    区分开——调用方据此退回首帧并标注降级（见 video_submit_params）。
    """
    monkeypatch.setattr(
        submit_mod.storage, "key_from_url", lambda _url: "upload_video/a.mp4"
    )
    monkeypatch.setattr(
        submit_mod.storage, "is_object_storage_enabled", lambda: False
    )
    db = _FakeDb(upload_row=SimpleNamespace(user_id=42))
    with pytest.raises(submit_mod.SourceVideoUrlUnavailable, match="未启用对象存储"):
        submit_mod.gateway_source_video_url(db, _task())


def test_gateway_source_video_url_rejects_external_video(monkeypatch):
    monkeypatch.setattr(submit_mod.storage, "key_from_url", lambda _url: None)
    with pytest.raises(RuntimeError, match="必须是本站素材"):
        submit_mod.gateway_source_video_url(
            _FakeDb(), _task(source_asset_url="https://evil.example.com/x.mp4")
        )


def test_gateway_source_video_url_rejects_foreign_upload(monkeypatch):
    monkeypatch.setattr(
        submit_mod.storage, "key_from_url", lambda _url: "upload_video/a.mp4"
    )
    db = _FakeDb(upload_row=SimpleNamespace(user_id=999))
    with pytest.raises(RuntimeError, match="上传视频不存在"):
        submit_mod.gateway_source_video_url(db, _task())


def test_gateway_source_video_url_fails_loudly_without_presigned_url(monkeypatch):
    monkeypatch.setattr(
        submit_mod.storage, "key_from_url", lambda _url: "upload_video/a.mp4"
    )
    monkeypatch.setattr(
        submit_mod.storage, "is_object_storage_enabled", lambda: True
    )
    monkeypatch.setattr(
        submit_mod.storage, "presigned_download_url", lambda key, expires=300: None
    )
    db = _FakeDb(upload_row=SimpleNamespace(user_id=42))
    with pytest.raises(RuntimeError, match="无法为源视频生成上游可访问地址"):
        submit_mod.gateway_source_video_url(db, _task())


def test_video_submit_params_carries_source_video_url(monkeypatch):
    monkeypatch.setattr(
        submit_mod,
        "gateway_source_video_url",
        lambda _db, _task: "https://cdn.example.com/presigned-source.mp4",
    )
    monkeypatch.setattr(
        submit_mod, "gateway_video_first_frame", lambda _db, _task: None
    )
    task = _task(params={"target_resolution": "720p", "target_duration": 5})

    params = submit_mod.video_submit_params(_FakeDb(), task)

    assert params["source_video_url"] == "https://cdn.example.com/presigned-source.mp4"


def test_video_submit_params_skips_analysis_only_source_video(monkeypatch):
    called = {"resolve": False}

    def _resolve(_db, _task):
        called["resolve"] = True
        return "https://cdn.example.com/should-not-happen.mp4"

    monkeypatch.setattr(submit_mod, "gateway_source_video_url", _resolve)
    # 反推链路（analysis-only）保持原样：不透传源视频，也不抽首帧。
    task = _task(
        params={
            "_video_reference_roles": [
                {
                    "role": "motion_analysis",
                    "source": "source_asset_url",
                    "mode": "analysis_only",
                }
            ],
        }
    )

    params = submit_mod.video_submit_params(_FakeDb(), task)

    assert called["resolve"] is False
    assert "source_video_url" not in params


def test_video_submit_params_resolution_failure_is_loud(monkeypatch):
    """请求非法类失败（非部署能力不足）仍必须整体中止，不得降级。"""
    def _resolve(_db, _task):
        raise RuntimeError("视频参考生成的源视频必须是本站素材，无法提交外部视频链接")

    monkeypatch.setattr(submit_mod, "gateway_source_video_url", _resolve)
    with pytest.raises(RuntimeError, match="必须是本站素材"):
        submit_mod.video_submit_params(_FakeDb(), _task())


def test_video_submit_params_degrades_visibly_when_deployment_cannot_serve_video(
    monkeypatch,
):
    """本地存储部署：退回首帧但显式标注降级——既不整体失败，也不静默丢运动信息。"""
    def _resolve(_db, _task):
        raise submit_mod.SourceVideoUrlUnavailable(
            "当前部署未启用对象存储，无法为上传原片生成上游可访问地址"
        )

    monkeypatch.setattr(submit_mod, "gateway_source_video_url", _resolve)
    monkeypatch.setattr(
        submit_mod,
        "gateway_video_first_frame",
        lambda _db, _task: "data:image/jpeg;base64,AAAA",
    )

    params = submit_mod.video_submit_params(_FakeDb(), _task())

    # 提交没有被打断，首帧照常产出
    assert params["first_frame_image"].startswith("data:image/jpeg;base64,")
    # 但源视频字段绝不能带上去（否则 payload 构建方会当成真视频通道）
    assert "source_video_url" not in params
    # 降级必须可见，且原因里说清怎么恢复完整能力
    assert params["source_video_degraded"] == "first_frame_only"
    assert "未传递运动信息" in params["source_video_degraded_reason"]
    assert "对象存储" in params["source_video_degraded_reason"]
