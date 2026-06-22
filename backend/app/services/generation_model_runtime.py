"""Model snapshot and runtime gateway helpers for generation tasks."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from ..models import GenTask
from . import gateway
from .model_gateway_config import (
    RuntimeGatewayConfig,
    gateway_key_fingerprint,
    runtime_config_for_model,
)


class ModelSnapshotMismatchError(RuntimeError):
    """A task snapshot no longer matches the configured model gateway secret."""


def model_snapshot(model) -> dict:
    gateway_cfg = runtime_config_for_model(model, getattr(model, "use", None))
    return {
        "model_id": model.model_id,
        "cost_credits": int(model.cost_credits or 0),
        "unlock_cost": int(model.unlock_cost or 0),
        "extra": model.extra or {},
        "provider": gateway_cfg.provider,
        "base_url": gateway_cfg.base_url,
        "gateway_format": gateway_cfg.gateway_format,
        "gateway_source": gateway_cfg.source,
        "gateway_key_fingerprint": gateway_key_fingerprint(gateway_cfg),
    }


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


def gen_image_with_model_config(model, prompt: str, *, n: int, size: str,
                                reference_image_url: str | None,
                                edit_path: str | None,
                                extra_payload: dict | None) -> list[bytes]:
    kwargs = {
        "n": n,
        "size": size,
        "reference_image_url": reference_image_url,
        "edit_path": edit_path,
        "extra_payload": extra_payload,
    }
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
