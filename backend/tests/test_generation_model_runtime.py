from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import gateway
from app.services.generation_model_runtime import (
    FrozenAdapterConfigError,
    frozen_image_adapter,
    frozen_video_adapter,
    gen_image_with_model_config,
    poll_video_with_model_config,
    submit_video_with_model_config,
)
from app.services.model_gateway_config import RuntimeGatewayConfig


def _runtime_model(*, use: str, gateway_format: str, extra: dict) -> SimpleNamespace:
    config = RuntimeGatewayConfig(
        use=use,
        provider="test-provider",
        base_url="https://frozen.example.com/v1",
        api_key="frozen-secret",
        gateway_format=gateway_format,
        source="model",
    )
    return SimpleNamespace(
        model_id="frozen-model",
        extra=extra,
        use=use,
        gateway_key_fingerprint=None,
        _runtime_gateway_config=config,
    )


def test_frozen_image_adapter_captures_final_path_payload_and_profile(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = {}

    def fake_post(path, payload, n, config=None):
        seen.update(path=path, payload=payload, n=n, config=config)
        return gateway.ImageBatchResult([b"image"])

    monkeypatch.setattr(gateway, "_post_single_image_repeated", fake_post)
    model = _runtime_model(
        use="image",
        gateway_format="openai",
        extra={
            "generation_path": "/v1/frozen/images/generations",
            "edit_path": "/v1/frozen/images/edits",
            "image_payload": {
                "field_map": {"model": "engine", "prompt": "input", "size": "canvas"},
                "seed": 42,
            },
            "prompt_profile": {
                "name": "frozen-profile",
                "prefix": "PROFILE-PREFIX",
                "suffix": "PROFILE-SUFFIX",
            },
        },
    )

    result = gen_image_with_model_config(
        model,
        "draw a lighthouse",
        n=1,
        size="1024x1024",
        reference_image_url=None,
        edit_path=None,
        extra_payload={"negative_prompt": "fog"},
    )

    assert result == [b"image"]
    assert seen["path"] == "/v1/frozen/images/generations"
    assert seen["payload"]["engine"] == "frozen-model"
    assert seen["payload"]["input"] == (
        "PROFILE-PREFIX\ndraw a lighthouse\nPROFILE-SUFFIX"
    )
    assert seen["payload"]["canvas"] == "1024x1024"
    assert seen["payload"]["seed"] == 42
    assert seen["payload"]["negative_prompt"] == "fog"
    assert "model" not in seen["payload"]
    assert "prompt" not in seen["payload"]
    assert "field_map" not in seen["payload"]


def test_frozen_image_edit_maps_json_image_input_and_keeps_route_distinct(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = {}
    monkeypatch.setattr(
        gateway,
        "_post_single_image_repeated",
        lambda path, payload, n, config=None: (
            seen.update(path=path, payload=payload) or gateway.ImageBatchResult([b"edited"])
        ),
    )
    model = _runtime_model(
        use="image",
        gateway_format="openai",
        extra={
            "generation_path": "/v1/frozen/generate",
            "edit_path": "/v1/frozen/edit",
            "image_payload": {
                "edit_payload_format": "json",
                "field_map": {"images": "source_images", "prompt": "instruction"},
            },
        },
    )

    result = gen_image_with_model_config(
        model,
        "change the sky",
        n=1,
        size="1024x1024",
        reference_image_url="data:image/png;base64,aW1hZ2U=",
        edit_path="/ignored/current/catalog/edit",
        extra_payload=None,
    )

    assert result == [b"edited"]
    assert seen["path"] == "/v1/frozen/edit"
    assert seen["payload"]["source_images"] == [
        {"image_url": "data:image/png;base64,aW1hZ2U="}
    ]
    assert seen["payload"]["instruction"] == "change the sky"
    assert "images" not in seen["payload"]


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example.com/v1/images",
        "//evil.example.com/v1/images",
        "/v1/../admin",
        "/v1/%2e%2e/admin",
        "/v1/images?token=secret",
        "/v1/images#fragment",
        "/v1/images\nother",
    ],
)
def test_invalid_frozen_image_path_is_rejected_before_outbound(path):
    with pytest.raises(FrozenAdapterConfigError):
        frozen_image_adapter({"generation_path": path})


@pytest.mark.parametrize(
    "field_map",
    [
        {"prompt": ""},
        {"prompt": 123},
        {"prompt": "field_map"},
        {"prompt": "value", "model": "value"},
    ],
)
def test_invalid_frozen_field_map_is_rejected_before_outbound(field_map):
    with pytest.raises(FrozenAdapterConfigError):
        frozen_image_adapter({"field_map": field_map})


def test_runtime_field_map_collision_is_rejected_before_http(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    called = []
    monkeypatch.setattr(
        gateway,
        "_post_single_image_repeated",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    with pytest.raises(gateway.GatewayError, match="冲突字段 seed"):
        gateway.gen_image(
            "prompt",
            "model",
            n=1,
            extra_payload={"seed": 1},
            field_map={"prompt": "seed"},
            gateway_config=_runtime_model(
                use="image",
                gateway_format="openai",
                extra={},
            )._runtime_gateway_config,
        )

    assert called == []


@pytest.mark.parametrize(
    "extra",
    [
        {"image_payload": {"seed": 7, "field_map": {"prompt": "seed"}}},
        {"field_map": {"model": "prompt"}},
        {
            "edit_path": "/v1/custom/edit",
            "image_payload": {
                "edit_payload_format": "json",
                "field_map": {"model": "images"},
            },
        },
        {
            "edit_path": "/v1/images/edits",
            "image_payload": {"field_map": {"prompt": "image"}},
        },
    ],
)
def test_frozen_adapter_rejects_collisions_for_each_actual_image_shape(extra):
    with pytest.raises(FrozenAdapterConfigError, match="冲突字段"):
        frozen_image_adapter(extra)


@pytest.mark.parametrize(
    ("edit_path", "field_map", "extra_payload"),
    [
        (None, {"model": "prompt"}, {}),
        ("/v1/custom/edit", {"model": "images"}, {"edit_payload_format": "json"}),
        ("/v1/images/edits", {"prompt": "image"}, {}),
    ],
)
def test_gateway_rejects_shape_collision_before_http(
    monkeypatch,
    edit_path,
    field_map,
    extra_payload,
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
    reference = "data:image/png;base64,aW1hZ2U=" if edit_path else None

    with pytest.raises(gateway.GatewayError, match="冲突字段"):
        gateway.gen_image(
            "prompt",
            "model",
            n=1,
            reference_image_url=reference,
            edit_path=edit_path,
            extra_payload=extra_payload,
            field_map=field_map,
            gateway_config=_runtime_model(
                use="image",
                gateway_format="openai",
                extra={},
            )._runtime_gateway_config,
        )

    assert calls == []


def test_legacy_image_adapter_defaults_preserve_openai_payload(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = {}
    monkeypatch.setattr(
        gateway,
        "_post_single_image_repeated",
        lambda path, payload, n, config=None: (
            seen.update(path=path, payload=payload) or gateway.ImageBatchResult([b"image"])
        ),
    )

    gateway.gen_image(
        "legacy prompt",
        "legacy-model",
        n=1,
        size="512x512",
        gateway_config=_runtime_model(
            use="image",
            gateway_format="openai",
            extra={},
        )._runtime_gateway_config,
    )

    assert seen["path"] == "/images/generations"
    assert seen["payload"]["model"] == "legacy-model"
    assert seen["payload"]["prompt"] == "legacy prompt"
    assert seen["payload"]["size"] == "512x512"


def test_generic_video_runtime_uses_frozen_submit_and_poll_paths(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", False)
    seen = []
    monkeypatch.setattr(
        gateway,
        "_video_post",
        lambda path, payload, config=None: (
            seen.append(("post", path, payload, config.base_url)) or {"id": "task-123"}
        ),
    )
    monkeypatch.setattr(
        gateway,
        "_video_get",
        lambda path, timeout=30, config=None: (
            seen.append(("get", path, None, config.base_url)) or {"status": "running"}
        ),
    )
    model = _runtime_model(
        use="video",
        gateway_format="openai",
        extra={
            "submit_path": "/v1/frozen/videos/submit",
            "poll_path": "/v1/frozen/videos/{id}",
        },
    )

    task_id = submit_video_with_model_config(model, "animate", {"duration": 5})
    status = poll_video_with_model_config(model, task_id)

    assert task_id == "task-123"
    assert status["status"] == "running"
    assert seen[0][1] == "/v1/frozen/videos/submit"
    assert seen[1][1] == "/v1/frozen/videos/task-123"
    assert all(call[3] == "https://frozen.example.com/v1" for call in seen)


def test_ark_route_override_is_explicitly_unsupported_before_outbound():
    with pytest.raises(FrozenAdapterConfigError, match="Ark.*不支持"):
        frozen_video_adapter(
            {"submit_path": "/custom/ark/submit"},
            gateway_format="ark",
        )


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example.com/tasks/{request_id}",
        "//evil.example.com/tasks/{request_id}",
        "/v1/videos/../tasks/{request_id}",
        "/v1/videos/%2e%2e/tasks/{request_id}",
        "/v1/videos/by-request/{request_id}?model=x",
        "/v1/videos/by-request/{request_id}#fragment",
        "/v1/videos/by-request/{request_id}\nother",
    ],
)
def test_frozen_video_request_query_path_rejects_unsafe_routes(path):
    with pytest.raises(FrozenAdapterConfigError, match="request_query_path"):
        frozen_video_adapter(
            {"request_query_path": path},
            gateway_format="openai",
        )


@pytest.mark.parametrize(
    "path",
    [
        "/v1/videos/by-request/{request_id}",
        "/v1/videos/by-request/{request_id}/models/{model}",
    ],
)
def test_frozen_video_request_query_path_preserves_legitimate_routes(path):
    adapter = frozen_video_adapter(
        {"request_query_path": path},
        gateway_format="openai",
    )

    assert adapter.request_query_path == path


def test_anthropic_image_route_mapping_override_is_explicitly_unsupported():
    with pytest.raises(FrozenAdapterConfigError, match="Anthropic Messages.*不支持"):
        frozen_image_adapter(
            {
                "image_transport": "anthropic_messages",
                "generation_path": "/custom/images/generate",
                "field_map": {"prompt": "input"},
            }
        )
