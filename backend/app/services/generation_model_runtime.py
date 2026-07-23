"""Model snapshot and runtime gateway helpers for generation tasks.

``generation-outbound-adapter.v1`` freezes these catalog fields in each quote:

* image: ``generation_path``, ``edit_path``, ``field_map`` (canonical at
  ``extra.field_map``; legacy ``extra.image_payload.field_map`` is accepted),
  and ``prompt_profile``;
* generic video: ``submit_path`` and ``poll_path``.

Ark video endpoints are part of its native protocol and are intentionally not
configurable. A snapshot containing Ark path overrides is rejected.
"""
from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import unquote

from ..config import settings
from ..models import GenTask
from . import gateway
from .config_store import get_model_config, resolve_model_config
from .generation_pricing import snapshot_credit_pricing
from .generation_prompts import compile_image_prompt_profile
from .model_gateway_config import (
    ModelGatewayConfigError,
    RuntimeGatewayConfig,
    gateway_key_fingerprint,
    runtime_config_for_model,
    validate_base_url,
)
from .model_routes import (
    ModelRouteSnapshotError,
    error_counts_toward_circuit,
    record_route_outcome,
    route_id_from_model,
    route_model_for_snapshot,
)


class ModelSnapshotMismatchError(RuntimeError):
    """A task snapshot no longer matches the configured model gateway secret."""


class FrozenAdapterConfigError(ValueError):
    """A frozen outbound adapter cannot be executed safely."""


FROZEN_ADAPTER_SCHEMA_VERSION = "generation-outbound-adapter.v1"
_DEFAULT_IMAGE_GENERATION_PATH = "/images/generations"
_DEFAULT_VIDEO_SUBMIT_PATH = "/v1/videos/generations"
_DEFAULT_VIDEO_POLL_PATH = "/v1/videos/{id}"
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~%{}-]+$")
_IMAGE_PROMPT_PROFILE_FIELDS = frozenset({"name", "prefix", "suffix", "separator"})
_RESERVED_PAYLOAD_FIELDS = frozenset(
    {
        "field_map",
        "prompt_profile",
        "generation_path",
        "edit_path",
        "submit_path",
        "poll_path",
        "schema_version",
    }
)


@dataclass(frozen=True)
class FrozenImageAdapter:
    generation_path: str
    edit_path: str | None
    field_map: dict[str, str]
    prompt_profile: dict[str, str]


@dataclass(frozen=True)
class FrozenVideoAdapter:
    submit_path: str | None
    poll_path: str | None
    request_query_path: str | None


def _validated_adapter_path(
    value,
    *,
    field: str,
    default: str | None = None,
    placeholders: frozenset[str] = frozenset(),
    required_placeholders: frozenset[str] | None = None,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        value = default
    if allow_empty and value in (None, ""):
        return None
    if not isinstance(value, str) or not value:
        raise FrozenAdapterConfigError(f"{field} 必须是非空相对路径")
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FrozenAdapterConfigError(f"{field} 包含空白或控制字符")
    if not value.startswith("/") or value.startswith("//"):
        raise FrozenAdapterConfigError(f"{field} 必须是以 / 开头的相对 API 路径")
    if "?" in value or "#" in value or "\\" in value or "//" in value:
        raise FrozenAdapterConfigError(f"{field} 不允许 query、fragment 或非标准分隔符")
    segments = value[1:].split("/")
    if not segments or any(not segment for segment in segments):
        raise FrozenAdapterConfigError(f"{field} 必须是规范化 API 路径")
    decoded_segments = [unquote(segment) for segment in segments]
    if any(
        segment in {".", ".."}
        or "/" in segment
        or "\\" in segment
        or "?" in segment
        or "#" in segment
        or any(ord(char) < 32 or ord(char) == 127 for char in segment)
        for segment in decoded_segments
    ):
        raise FrozenAdapterConfigError(f"{field} 不允许路径穿越")
    if any(re.search(r"%(?![0-9A-Fa-f]{2})", segment) for segment in segments):
        raise FrozenAdapterConfigError(f"{field} 包含非法百分号编码")
    if any(not _PATH_SEGMENT.fullmatch(segment) for segment in segments):
        raise FrozenAdapterConfigError(f"{field} 包含非法路径字符")
    found_placeholders = set(re.findall(r"\{([^{}]+)\}", value))
    allowed_placeholders = set(placeholders)
    required = (
        allowed_placeholders
        if required_placeholders is None
        else set(required_placeholders)
    )
    if not required.issubset(found_placeholders) or not found_placeholders.issubset(
        allowed_placeholders
    ):
        allowed = ",".join(sorted(allowed_placeholders)) or "无"
        required_text = ",".join(sorted(required)) or "无"
        raise FrozenAdapterConfigError(
            f"{field} 路径占位符非法; 必需: {required_text}; 允许: {allowed}"
        )
    without_placeholders = re.sub(r"\{[^{}]+\}", "", value)
    if "{" in without_placeholders or "}" in without_placeholders:
        raise FrozenAdapterConfigError(f"{field} 包含非法路径占位符")
    return value


def validate_field_map(value) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise FrozenAdapterConfigError("field_map 必须是对象")
    result: dict[str, str] = {}
    targets: set[str] = set()
    for source, target in value.items():
        if not isinstance(source, str) or not source.strip() or source != source.strip():
            raise FrozenAdapterConfigError("field_map 源字段必须是非空规范字符串")
        if not isinstance(target, str) or not target.strip() or target != target.strip():
            raise FrozenAdapterConfigError("field_map 目标字段必须是非空规范字符串")
        if target in _RESERVED_PAYLOAD_FIELDS:
            raise FrozenAdapterConfigError(f"field_map 目标字段 {target} 为保留控制字段")
        if target in targets:
            raise FrozenAdapterConfigError(f"field_map 目标字段 {target} 冲突")
        result[source] = target
        targets.add(target)
    return result


def _validated_image_prompt_profile(value) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise FrozenAdapterConfigError("prompt_profile 必须是对象")
    unknown = set(value) - _IMAGE_PROMPT_PROFILE_FIELDS
    if unknown:
        raise FrozenAdapterConfigError(
            f"prompt_profile 包含不支持字段: {','.join(sorted(str(x) for x in unknown))}"
        )
    result: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(raw, str):
            raise FrozenAdapterConfigError(f"prompt_profile.{key} 必须是字符串")
        if key != "separator" and raw != raw.strip():
            raise FrozenAdapterConfigError(f"prompt_profile.{key} 必须是规范字符串")
        if any(ord(char) < 32 and char not in "\n\t" for char in raw):
            raise FrozenAdapterConfigError(f"prompt_profile.{key} 包含控制字符")
        result[key] = raw
    return result


_IMAGE_PAYLOAD_CONTROL_FIELDS = _RESERVED_PAYLOAD_FIELDS | frozenset(
    {"edit_payload_format", "_edit_payload_format", "image_transport", "message_max_tokens"}
)


def _validate_image_outbound_shapes(
    *,
    image_payload: dict,
    field_map: dict[str, str],
    edit_path: str | None,
) -> None:
    configured_payload = {
        key: value
        for key, value in image_payload.items()
        if key not in _IMAGE_PAYLOAD_CONTROL_FIELDS
    }
    configured_payload.setdefault("quality", None)
    configured_payload.setdefault("output_format", None)
    configured_payload.setdefault("output_compression", None)
    shapes = [
        {
            "model": None,
            "prompt": None,
            "size": None,
            **configured_payload,
        }
    ]
    if edit_path is not None:
        format_probe = dict(image_payload)
        edit_format = gateway._image_edit_payload_format(edit_path, format_probe)
        if edit_format == "multipart":
            multipart_payload = {
                "model": None,
                "prompt": None,
                "size": None,
                **configured_payload,
                "image": None,
                "mask": None,
            }
            multipart_payload.pop("mask", None)
            multipart_payload["image"] = None
            multipart_payload["mask"] = None
            shapes.append(multipart_payload)
        else:
            shapes.append(
                {
                    "model": None,
                    "images": None,
                    "prompt": None,
                    "size": None,
                    **configured_payload,
                }
            )
    for payload in shapes:
        try:
            gateway.map_outbound_payload_fields(payload, field_map)
        except gateway.GatewayError as exc:
            raise FrozenAdapterConfigError(str(exc)) from exc


def frozen_image_adapter(extra: dict | None) -> FrozenImageAdapter:
    if extra is not None and not isinstance(extra, dict):
        raise FrozenAdapterConfigError("图片适配器 extra 必须是对象")
    values = dict(extra or {})
    image_payload = values.get("image_payload")
    if image_payload is None:
        image_payload = {}
    if not isinstance(image_payload, dict):
        raise FrozenAdapterConfigError("image_payload 必须是对象")
    top_level_map = values.get("field_map")
    nested_map = image_payload.get("field_map")
    if top_level_map is not None and nested_map is not None and top_level_map != nested_map:
        raise FrozenAdapterConfigError("field_map 与 image_payload.field_map 冲突")
    field_map = validate_field_map(top_level_map if top_level_map is not None else nested_map)
    generation_path = str(
        _validated_adapter_path(
            values.get("generation_path"),
            field="generation_path",
            default=_DEFAULT_IMAGE_GENERATION_PATH,
        )
    )
    edit_path = _validated_adapter_path(
        values.get("edit_path"),
        field="edit_path",
        allow_empty=True,
    )
    if edit_path is not None and edit_path == generation_path:
        raise FrozenAdapterConfigError("edit_path 必须与 generation_path 不同")
    image_transport = str(
        values.get("image_transport") or image_payload.get("image_transport") or "openai_images"
    )
    if image_transport == "anthropic_messages" and (
        field_map or generation_path != _DEFAULT_IMAGE_GENERATION_PATH
    ):
        raise FrozenAdapterConfigError(
            "Anthropic Messages 图片适配器不支持 generation_path/field_map 覆盖"
        )
    shape_payload = dict(image_payload)
    for field in (
        "image_transport",
        "response_format",
        "message_max_tokens",
        "edit_payload_format",
    ):
        if values.get(field) not in (None, ""):
            shape_payload[field] = values[field]
    _validate_image_outbound_shapes(
        image_payload=shape_payload,
        field_map=field_map,
        edit_path=edit_path,
    )
    return FrozenImageAdapter(
        generation_path=generation_path,
        edit_path=edit_path,
        field_map=field_map,
        prompt_profile=_validated_image_prompt_profile(values.get("prompt_profile")),
    )


def frozen_video_adapter(extra: dict | None, *, gateway_format: str) -> FrozenVideoAdapter:
    if extra is not None and not isinstance(extra, dict):
        raise FrozenAdapterConfigError("视频适配器 extra 必须是对象")
    values = dict(extra or {})
    configured = any(key in values for key in ("submit_path", "poll_path"))
    if gateway_format == "ark":
        if configured:
            raise FrozenAdapterConfigError(
                "Ark 视频适配器不支持 submit_path/poll_path 覆盖;"
                "端点由 Ark 原生协议固定"
            )
        return FrozenVideoAdapter(
            submit_path=None,
            poll_path=None,
            request_query_path=_validated_adapter_path(
                values.get("request_query_path"),
                field="request_query_path",
                placeholders=frozenset({"request_id", "model"}),
                required_placeholders=frozenset({"request_id"}),
                allow_empty=True,
            ),
        )
    return FrozenVideoAdapter(
        submit_path=_validated_adapter_path(
            values.get("submit_path"),
            field="submit_path",
            default=_DEFAULT_VIDEO_SUBMIT_PATH,
        ),
        poll_path=_validated_adapter_path(
            values.get("poll_path"),
            field="poll_path",
            default=_DEFAULT_VIDEO_POLL_PATH,
            placeholders=frozenset({"id"}),
        ),
        request_query_path=_validated_adapter_path(
            values.get("request_query_path"),
            field="request_query_path",
            placeholders=frozenset({"request_id", "model"}),
            required_placeholders=frozenset({"request_id"}),
            allow_empty=True,
        ),
    )


def validate_frozen_adapter_snapshot(snapshot: dict, use: str) -> None:
    values = snapshot if isinstance(snapshot, dict) else {}
    schema_version = values.get("outbound_adapter_schema_version")
    if schema_version is not None and schema_version != FROZEN_ADAPTER_SCHEMA_VERSION:
        raise FrozenAdapterConfigError(
            f"不支持的出站适配器 schema: {schema_version}"
        )
    extra = values.get("extra")
    if extra is not None and not isinstance(extra, dict):
        raise FrozenAdapterConfigError("出站适配器 extra 必须是对象")
    if use == "image":
        frozen_image_adapter(extra)
    elif use == "video":
        frozen_video_adapter(extra, gateway_format=str(values.get("gateway_format") or ""))


def model_snapshot(model) -> dict:
    gateway_cfg = runtime_config_for_model(model, getattr(model, "use", None))
    extra = dict(model.extra or {})
    if gateway_cfg.use == "image":
        extra.setdefault("generation_path", _DEFAULT_IMAGE_GENERATION_PATH)
        extra.setdefault("edit_path", settings.image_edit_path)
    elif gateway_cfg.use == "video" and gateway_cfg.gateway_format != "ark":
        extra.setdefault("submit_path", _DEFAULT_VIDEO_SUBMIT_PATH)
        extra.setdefault("poll_path", _DEFAULT_VIDEO_POLL_PATH)
    extra["credit_pricing"] = snapshot_credit_pricing(extra)
    return {
        "outbound_adapter_schema_version": FROZEN_ADAPTER_SCHEMA_VERSION,
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
    _runtime_gateway_config_for_snapshot(
        model,
        snapshot,
        getattr(model, "use", None),
    )


def _runtime_gateway_config_for_snapshot(
    model,
    snapshot: dict,
    use: str | None,
) -> RuntimeGatewayConfig:
    """Inject only the current secret into an otherwise immutable gateway snapshot."""
    values = snapshot if isinstance(snapshot, dict) else {}
    resolved_use = str(use or getattr(model, "use", None) or "image")
    if "gateway_key_fingerprint" not in values:
        # Legacy snapshots predate secret identity pinning. Keep them on the
        # current complete gateway config instead of mixing an unverified old
        # endpoint with today's secret.
        return runtime_config_for_model(model, resolved_use)

    source = str(values.get("gateway_source") or "")
    if source not in {"model", "env"}:
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更: 任务模型快照的网关来源非法,请重新生成"
        )
    secret_config = (
        runtime_config_for_model(None, resolved_use)
        if source == "env"
        else runtime_config_for_model(model, resolved_use)
    )
    if secret_config.source != source:
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更(密钥来源不一致),"
            "该任务快照不能继续使用。请重新报价。"
        )
    provider = values.get("provider")
    base_url = values.get("base_url")
    gateway_format = values.get("gateway_format")
    if not all(isinstance(value, str) for value in (provider, base_url, gateway_format)):
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更: 任务模型快照的网关配置不完整,请重新生成"
        )
    frozen_config = RuntimeGatewayConfig(
        use=resolved_use,
        provider=provider,
        base_url=base_url,
        api_key=secret_config.api_key,
        gateway_format=gateway_format,
        source=source,
    )
    try:
        validate_base_url("任务快照 Base URL", frozen_config.base_url)
    except ModelGatewayConfigError as exc:
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更: "
            "任务快照中的旧网关地址当前不可执行,请重新报价"
        ) from exc
    if gateway_key_fingerprint(frozen_config) != str(
        values.get("gateway_key_fingerprint") or ""
    ):
        raise ModelSnapshotMismatchError(
            "模型网关配置已变更(密钥指纹不一致),"
            "该任务快照不能继续使用。请重新报价。"
        )
    return frozen_config


def model_from_frozen_snapshot(snapshot: dict, fallback_model, use: str):
    if not snapshot:
        return fallback_model
    runtime_gateway = _runtime_gateway_config_for_snapshot(
        fallback_model,
        snapshot,
        use,
    )
    try:
        validate_frozen_adapter_snapshot(snapshot, use)
    except FrozenAdapterConfigError as exc:
        raise ModelSnapshotMismatchError(
            f"模型网关配置已变更: 任务模型快照的出站适配器非法: {exc}"
        ) from exc
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
        gateway_key_fingerprint=snapshot.get("gateway_key_fingerprint"),
        use=use,
        enabled=True,
        _runtime_gateway_config=runtime_gateway,
        route_id=getattr(fallback_model, "route_id", None),
        route_key=getattr(fallback_model, "route_key", None),
        route_name=getattr(fallback_model, "route_name", None),
        route_config_revision=getattr(fallback_model, "route_config_revision", None),
    )


def model_from_persisted_snapshot(db, snapshot: dict, fallback_model, use: str):
    """Load a frozen model snapshot with the exact route-bound current secret."""
    try:
        route_model = route_model_for_snapshot(db, fallback_model, snapshot)
    except ModelRouteSnapshotError as exc:
        raise ModelSnapshotMismatchError(str(exc)) from exc
    return model_from_frozen_snapshot(snapshot, route_model, use)


def model_from_snapshot(task: GenTask, fallback_model, db=None):
    snapshot = ((task.params or {}).get("_model_snapshot") or {})
    use = getattr(fallback_model, "use", None) or task.category
    if snapshot.get("route_snapshot") is not None:
        if db is None:
            raise ModelSnapshotMismatchError("任务路由快照缺少数据库运行上下文")
        return model_from_persisted_snapshot(db, snapshot, fallback_model, use)
    return model_from_frozen_snapshot(snapshot, fallback_model, use)


def gateway_config_from_model(model, use: str) -> RuntimeGatewayConfig:
    frozen_config = getattr(model, "_runtime_gateway_config", None)
    if isinstance(frozen_config, RuntimeGatewayConfig):
        expected = getattr(model, "gateway_key_fingerprint", None)
        if expected is not None and expected != gateway_key_fingerprint(frozen_config):
            raise ModelSnapshotMismatchError(
                "模型网关配置已变更(密钥指纹不一致),"
                "该任务快照不能继续使用。请重新报价。"
            )
        return frozen_config
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
    adapter = frozen_image_adapter(model_extra)
    configured_payload = dict(model_extra.get("image_payload") or {})
    configured_payload.pop("field_map", None)
    for field in ("image_transport", "response_format", "message_max_tokens"):
        if model_extra.get(field) not in (None, ""):
            configured_payload[field] = model_extra[field]
    kwargs = {
        "n": n,
        "size": size,
        "reference_image_url": reference_image_url,
        "edit_path": adapter.edit_path if "edit_path" in model_extra else edit_path,
        "extra_payload": {**configured_payload, **(extra_payload or {})},
    }
    prompt = compile_image_prompt_profile(prompt, adapter.prompt_profile)
    if _accepts_parameter(gateway.gen_image, "generation_path"):
        kwargs["generation_path"] = adapter.generation_path
    if _accepts_parameter(gateway.gen_image, "field_map"):
        kwargs["field_map"] = adapter.field_map
    if _accepts_parameter(gateway.gen_image, "reference_image_urls"):
        kwargs["reference_image_urls"] = reference_image_urls
    if accepts_gateway_config(gateway.gen_image):
        kwargs["gateway_config"] = gateway_config_from_model(model, "image")
    started = time.monotonic()
    route_id = route_id_from_model(model)
    try:
        result = gateway.gen_image(prompt, model.model_id, **kwargs)
    except Exception as exc:
        record_route_outcome(
            route_id,
            operation="image_generate",
            success=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            error_code=getattr(exc, "error_code", None) or type(exc).__name__,
            counts_toward_circuit=error_counts_toward_circuit(exc),
        )
        raise
    record_route_outcome(
        route_id,
        operation="image_generate",
        success=bool(result),
        latency_ms=round((time.monotonic() - started) * 1000),
        error_code=None if result else "empty_result",
        counts_toward_circuit=not bool(result),
    )
    return result


def submit_video_with_model_config(model, prompt: str, params: dict) -> str:
    gateway_config = gateway_config_from_model(model, "video")
    adapter = frozen_video_adapter(
        model.extra,
        gateway_format=gateway_config.gateway_format,
    )
    extra = dict(model.extra or {})
    if adapter.submit_path is not None:
        extra["submit_path"] = adapter.submit_path
    if adapter.poll_path is not None:
        extra["poll_path"] = adapter.poll_path
    kwargs = {"extra": extra}
    if accepts_gateway_config(gateway.submit_video):
        kwargs["gateway_config"] = gateway_config
    started = time.monotonic()
    route_id = route_id_from_model(model)
    try:
        result = gateway.submit_video(prompt, model.model_id, params, **kwargs)
    except Exception as exc:
        record_route_outcome(
            route_id,
            operation="video_submit",
            success=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            error_code=getattr(exc, "error_code", None) or type(exc).__name__,
            counts_toward_circuit=error_counts_toward_circuit(exc),
        )
        raise
    record_route_outcome(
        route_id,
        operation="video_submit",
        success=bool(result),
        latency_ms=round((time.monotonic() - started) * 1000),
        error_code=None if result else "empty_task_id",
        counts_toward_circuit=not bool(result),
    )
    return result


def poll_video_with_model_config(model, external_task_id: str) -> dict:
    gateway_config = gateway_config_from_model(model, "video")
    adapter = frozen_video_adapter(
        model.extra,
        gateway_format=gateway_config.gateway_format,
    )
    extra = dict(model.extra or {})
    if adapter.submit_path is not None:
        extra["submit_path"] = adapter.submit_path
    if adapter.poll_path is not None:
        extra["poll_path"] = adapter.poll_path
    kwargs = {"extra": extra}
    if accepts_gateway_config(gateway.poll_video):
        kwargs["gateway_config"] = gateway_config
    started = time.monotonic()
    route_id = route_id_from_model(model)
    try:
        result = gateway.poll_video(external_task_id, model.model_id, **kwargs)
    except Exception as exc:
        record_route_outcome(
            route_id,
            operation="video_poll",
            success=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            error_code=getattr(exc, "error_code", None) or type(exc).__name__,
            counts_toward_circuit=error_counts_toward_circuit(exc),
        )
        raise
    record_route_outcome(
        route_id,
        operation="video_poll",
        success=True,
        latency_ms=round((time.monotonic() - started) * 1000),
    )
    return result


def find_video_by_request_id_with_model_config(model, request_id: str) -> dict | None:
    if not request_id or not (model.extra or {}).get("request_query_path"):
        return None
    gateway_config = gateway_config_from_model(model, "video")
    adapter = frozen_video_adapter(
        model.extra,
        gateway_format=gateway_config.gateway_format,
    )
    extra = dict(model.extra or {})
    if adapter.request_query_path is not None:
        extra["request_query_path"] = adapter.request_query_path
    kwargs = {"extra": extra}
    if accepts_gateway_config(gateway.find_video_by_request_id):
        kwargs["gateway_config"] = gateway_config
    started = time.monotonic()
    route_id = route_id_from_model(model)
    try:
        result = gateway.find_video_by_request_id(request_id, model.model_id, **kwargs)
    except Exception as exc:
        record_route_outcome(
            route_id,
            operation="video_recover",
            success=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            error_code=getattr(exc, "error_code", None) or type(exc).__name__,
            counts_toward_circuit=error_counts_toward_circuit(exc),
        )
        raise
    record_route_outcome(
        route_id,
        operation="video_recover",
        success=True,
        latency_ms=round((time.monotonic() - started) * 1000),
    )
    return result
