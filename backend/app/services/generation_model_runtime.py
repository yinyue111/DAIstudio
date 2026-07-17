"""Model snapshot and runtime gateway helpers for generation tasks."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from ..models import GenTask
from . import gateway
from .config_store import get_model_config, resolve_model_config
from .generation_pricing import snapshot_credit_pricing
from .model_gateway_config import (
    RuntimeGatewayConfig,
    gateway_key_fingerprint,
    runtime_config_for_model,
)


class ModelSnapshotMismatchError(RuntimeError):
    """A task snapshot no longer matches the configured model gateway secret."""


def model_snapshot(model) -> dict:
    gateway_cfg = runtime_config_for_model(model, getattr(model, "use", None))
    extra = dict(model.extra or {})
    extra["credit_pricing"] = snapshot_credit_pricing(extra)
    return {
        "model_config_id": int(model.id) if getattr(model, "id", None) is not None else None,
        "model_name": str(getattr(model, "display_name", None) or model.model_id),
        "model_id": model.model_id,
        "cost_credits": int(model.cost_credits or 0),
        "unlock_cost": int(model.unlock_cost or 0),
        "extra": extra,
        "provider": gateway_cfg.provider,
        "base_url": gateway_cfg.base_url,
        "gateway_format": gateway_cfg.gateway_format,
        "gateway_source": gateway_cfg.source,
        "gateway_key_fingerprint": gateway_key_fingerprint(gateway_cfg),
    }


def model_config_for_task(db, task: GenTask, use: str, legacy_loader=None):
    """Load the exact catalog row selected at submission time.

    Test hooks written before multi-model support still receive the old two
    argument loader contract. Production tasks with a persisted catalog id are
    always resolved by id so changing the default cannot redirect queued work.
    """
    selected_id = getattr(task, "model_config_id", None)
    if selected_id is not None and (legacy_loader is None or legacy_loader is get_model_config):
        return resolve_model_config(
            db,
            use,
            int(selected_id),
            require_enabled=False,
        )
    model = legacy_loader(db, use) if legacy_loader is not None else get_model_config(db, use)
    if selected_id is not None and model is not None:
        loaded_id = getattr(model, "id", None)
        if loaded_id is not None and int(loaded_id) != int(selected_id):
            raise ModelSnapshotMismatchError("任务绑定的模型配置与运行时模型不一致")
    return model


def assert_model_snapshot_compatible(model, snapshot: dict) -> None:
    expected = (snapshot or {}).get("gateway_key_fingerprint")
    if not expected:
        return
    current_cfg = runtime_config_for_model(model, getattr(model, "use", None))
    if gateway_key_fingerprint(current_cfg) != expected:
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更,该任务快照不能继续使用。请重新生成预览后再试。"
        )


def model_from_snapshot(task: GenTask, fallback_model):
    snapshot = ((task.params or {}).get("_model_snapshot") or {})
    if not snapshot:
        return fallback_model
    assert_model_snapshot_compatible(fallback_model, snapshot)
    return SimpleNamespace(
        id=snapshot.get("model_config_id") or getattr(fallback_model, "id", None),
        display_name=snapshot.get("model_name") or getattr(fallback_model, "display_name", None),
        model_id=snapshot.get("model_id") or fallback_model.model_id,
        cost_credits=int(snapshot.get("cost_credits") or 0),
        unlock_cost=int(snapshot.get("unlock_cost") or 0),
        extra=snapshot.get("extra") or {},
        provider=snapshot.get("provider"),
        base_url=snapshot.get("base_url"),
        api_key_encrypted=getattr(fallback_model, "api_key_encrypted", None),
        gateway_format=snapshot.get("gateway_format"),
        gateway_source=snapshot.get("gateway_source"),
        use=getattr(fallback_model, "use", None) or task.category,
        enabled=True,
    )


def gateway_config_from_model(model, use: str) -> RuntimeGatewayConfig:
    cfg = runtime_config_for_model(model, use)
    expected = getattr(model, "gateway_key_fingerprint", None)
    if expected and expected != gateway_key_fingerprint(cfg):
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更,该任务快照不能继续使用。请重新生成预览后再试。"
        )
    return cfg


def accepts_gateway_config(fn) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return "gateway_config" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


def _accepts_parameter(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return name in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


def gen_image_with_model_config(model, prompt: str, *, n: int, size: str,
                                reference_image_url: str | None,
                                edit_path: str | None,
                                extra_payload: dict | None,
                                reference_image_urls: list[str] | None = None) -> list[bytes]:
    model_extra = dict(model.extra or {})
    configured_payload = dict(model_extra.get("image_payload") or {})
    for field in ("image_transport", "response_format", "message_max_tokens"):
        if model_extra.get(field) not in (None, ""):
            configured_payload[field] = model_extra[field]
    kwargs = {
        "n": n,
        "size": size,
        "reference_image_url": reference_image_url,
        "edit_path": edit_path,
        "extra_payload": {**configured_payload, **(extra_payload or {})},
    }
    if _accepts_parameter(gateway.gen_image, "reference_image_urls"):
        kwargs["reference_image_urls"] = reference_image_urls
    if accepts_gateway_config(gateway.gen_image):
        kwargs["gateway_config"] = gateway_config_from_model(model, "image")
    return gateway.gen_image(prompt, model.model_id, **kwargs)


def submit_video_with_model_config(model, prompt: str, params: dict) -> str:
    kwargs = {"extra": model.extra}
    if accepts_gateway_config(gateway.submit_video):
        kwargs["gateway_config"] = gateway_config_from_model(model, "video")
    return gateway.submit_video(prompt, model.model_id, params, **kwargs)


def poll_video_with_model_config(model, external_task_id: str) -> dict:
    kwargs = {"extra": model.extra}
    if accepts_gateway_config(gateway.poll_video):
        kwargs["gateway_config"] = gateway_config_from_model(model, "video")
    return gateway.poll_video(external_task_id, model.model_id, **kwargs)


def find_video_by_request_id_with_model_config(model, request_id: str) -> dict | None:
    if not request_id or not (model.extra or {}).get("request_query_path"):
        return None
    kwargs = {"extra": model.extra}
    if accepts_gateway_config(gateway.find_video_by_request_id):
        kwargs["gateway_config"] = gateway_config_from_model(model, "video")
    return gateway.find_video_by_request_id(request_id, model.model_id, **kwargs)
