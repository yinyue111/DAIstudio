"""Video submission, polling and status normalization."""
from __future__ import annotations

import logging
import random
from urllib.parse import quote, urlparse

from ..config import settings
from .gateway_transport import (
    GatewayError,
    _ensure_gateway_configured,
    _video_gateway_mock,
)
from .gateway_video_payloads import (
    VIDEO_STATUS as _VIDEO_STATUS,
)
from .gateway_video_payloads import (
    ark_payload as _ark_payload,
)
from .gateway_video_payloads import (
    extract_by_path as _extract_by_path,
)
from .gateway_video_payloads import (
    generic_video_payload_params as _generic_video_payload_params,
)
from .gateway_video_payloads import (
    nested_video_url as _nested_video_url,
)
from .model_gateway_config import RuntimeGatewayConfig

log = logging.getLogger("gateway")


def _video_base(config: RuntimeGatewayConfig | None = None) -> str:
    if config is not None:
        return config.base_url.rstrip("/")
    return settings.video_base.rstrip("/")


def _video_auth(config: RuntimeGatewayConfig | None = None) -> dict:
    key = config.api_key if config is not None else settings.video_key
    return {"Authorization": f"Bearer {key}"}


def _video_url(path: str, config: RuntimeGatewayConfig | None = None) -> str:
    base = _video_base(config)
    fmt = config.gateway_format if config is not None else settings.video_gateway_format
    if fmt == "openai" and base.endswith("/v1") and path.startswith("/v1/"):
        return f"{base}{path[3:]}"
    return f"{base}{path}"


def _normalise_video_result_url(
    value, config: RuntimeGatewayConfig | None = None
) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme or url.startswith("//"):
        return url
    path = url if url.startswith("/") else f"/{url}"
    return _video_url(path, config)


def _url_origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        if not scheme or not host:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, host, port


def video_result_download_headers(
    url: str, config: RuntimeGatewayConfig | None = None
) -> dict[str, str]:
    """Authenticate provider-owned content endpoints without leaking to CDNs."""
    if _url_origin(url) != _url_origin(_video_base(config)):
        return {}
    return _video_auth(config)


def _format_gateway_path(template: str, **values: str) -> str:
    encoded = {key: quote(str(value), safe="") for key, value in values.items()}
    return str(template).format(**encoded)


def _compact_text(value) -> str:
    text = str(value or "").strip()
    return text[:300]


def _video_provider_error(data) -> tuple[str | None, str | None]:
    if not isinstance(data, dict):
        return None, None
    err = data.get("error")
    if isinstance(err, dict):
        message = (
            err.get("message")
            or err.get("msg")
            or err.get("detail")
            or err.get("reason")
            or err.get("description")
        )
        code = err.get("code") or err.get("type") or err.get("error_code")
        return _compact_text(message) or None, _compact_text(code) or None
    if err:
        return _compact_text(err), None
    for message_key in ("error_message", "errorMessage", "message", "reason", "fail_reason"):
        if data.get(message_key):
            code = data.get("error_code") or data.get("code")
            return _compact_text(data.get(message_key)), _compact_text(code) or None
    for nested_key in ("data", "output", "content", "result"):
        message, code = _video_provider_error(data.get(nested_key))
        if message or code:
            return message, code
    return None, None


def _normalise_video_status(raw_status, data) -> tuple[str, str | None, str | None, str]:
    raw = str(raw_status or "").strip().lower()
    norm = _VIDEO_STATUS.get(raw)
    error_message, error_code = _video_provider_error(data)
    if norm is None:
        norm = "failed" if (error_message or error_code) else ("running" if raw else "queued")
    return norm, error_message, error_code, raw


def _video_post(
    path: str, payload: dict, timeout: int | None = None, config: RuntimeGatewayConfig | None = None
) -> dict:
    from . import gateway as _gw

    url = _video_url(path, config)
    r = _gw._request(
        "POST",
        url,
        headers={**_video_auth(config), "Content-Type": "application/json"},
        json=payload,
        timeout=timeout or settings.video_submit_timeout_seconds,
        retries=0,
        **_gw._trusted_request_options(config),
    )
    return r.json()


def _video_get(path: str, timeout: int = 30, config: RuntimeGatewayConfig | None = None) -> dict:
    from . import gateway as _gw

    url = _video_url(path, config)
    r = _gw._request(
        "GET",
        url,
        headers=_video_auth(config),
        timeout=timeout,
        retries=settings.gateway_max_retries,
        **_gw._trusted_request_options(config),
    )
    return r.json()


def submit_video(
    prompt: str,
    video_model_id: str,
    params: dict,
    extra: dict | None = None,
    gateway_config: RuntimeGatewayConfig | None = None,
) -> str:
    """Submit an async video job; returns an external task id."""
    from . import gateway as _gw

    if settings.mock_mode:
        return f"mock-{random.randint(100000, 999999)}"
    if _video_gateway_mock(gateway_config):
        return f"mock-{random.randint(100000, 999999)}"
    _ensure_gateway_configured(gateway_config, "视频")
    fmt = (
        gateway_config.gateway_format
        if gateway_config is not None
        else settings.video_gateway_format
    )
    if fmt == "ark":
        return _submit_video_ark(
            prompt,
            video_model_id,
            params,
            extra=extra,
            gateway_config=gateway_config,
        )
    extra = extra or {}
    submit_path = extra.get("submit_path", "/v1/videos/generations")
    id_field = extra.get("id_field", "id")
    payload_params = _generic_video_payload_params(params or {}, extra, video_model_id)
    payload_prompt = prompt
    negative_prompt_mode = str(
        extra.get("negative_prompt_mode")
        or ("append_to_prompt" if "grok" in str(video_model_id).strip().lower() else "")
    ).strip().lower()
    if negative_prompt_mode == "append_to_prompt":
        negative_prompt = str(payload_params.pop("negative_prompt", "") or "").strip()
        if negative_prompt and negative_prompt not in str(payload_prompt):
            payload_prompt = (
                f"{str(payload_prompt).rstrip()}\nNegative constraints: {negative_prompt}"
            )
    payload = {"model": video_model_id, "prompt": payload_prompt, **payload_params}
    if gateway_config is None:
        data = _gw._video_post(submit_path, payload)
    else:
        data = _gw._video_post(submit_path, payload, config=gateway_config)
    task_id = data.get(id_field) or data.get("task_id") or data.get("id") or data.get("request_id")
    if not task_id:
        raise GatewayError(f"视频网关未返回任务号: {str(data)[:200]}")
    return str(task_id)


def poll_video(
    external_task_id: str,
    video_model_id: str,
    extra: dict | None = None,
    gateway_config: RuntimeGatewayConfig | None = None,
) -> dict:
    """Poll one tick. Returns {status: queued|running|succeeded|failed, url?}."""
    from . import gateway as _gw

    if (
        settings.mock_mode
        or str(external_task_id).startswith("mock-")
        or _video_gateway_mock(gateway_config)
    ):
        return {"status": "succeeded", "url": None, "mock": True}
    _ensure_gateway_configured(gateway_config, "视频")
    fmt = (
        gateway_config.gateway_format
        if gateway_config is not None
        else settings.video_gateway_format
    )
    if fmt == "ark":
        return _poll_video_ark(external_task_id, gateway_config=gateway_config)
    extra = extra or {}
    poll_path = _format_gateway_path(extra.get("poll_path", "/v1/videos/{id}"), id=external_task_id)
    if gateway_config is None:
        data = _gw._video_get(poll_path, timeout=30)
    else:
        data = _gw._video_get(poll_path, timeout=30, config=gateway_config)
    norm, err, err_code, raw_status = _normalise_video_status(data.get("status"), data)
    url = None
    if norm == "succeeded":
        url = (
            data.get("url")
            or data.get("video_url")
            or data.get("download_url")
            or _nested_video_url(data)
            or _nested_video_url(data.get("data"))
            or _nested_video_url(data.get("output"))
        )
        url = _normalise_video_result_url(url, gateway_config)
    return {
        "status": norm,
        "url": url,
        "error": err,
        "error_code": err_code,
        "raw_status": raw_status or None,
        "raw": data,
    }


def find_video_by_request_id(
    request_id: str,
    video_model_id: str,
    extra: dict | None = None,
    gateway_config: RuntimeGatewayConfig | None = None,
) -> dict | None:
    """Best-effort provider lookup for non-idempotent submits with unknown state.

    Providers differ on whether they support request_id/idempotency lookup, so
    this is explicitly opt-in through model.extra. When absent, callers keep the
    existing manual reconciliation path.
    """
    from . import gateway as _gw

    if not request_id:
        return None
    extra = extra or {}
    path_template = extra.get("request_query_path")
    if not path_template:
        return None
    path = _format_gateway_path(path_template, request_id=request_id, model=video_model_id)
    data = _gw._video_get(
        path, timeout=int(extra.get("request_query_timeout_seconds") or 30), config=gateway_config
    )
    container_path = extra.get("request_query_result_path")
    record = _extract_by_path(data, container_path) if container_path else data
    if isinstance(record, list):
        record = record[0] if record else None
    if not isinstance(record, dict):
        return None
    id_field = extra.get("request_query_id_field") or extra.get("id_field") or "id"
    status_field = extra.get("request_query_status_field", "status")
    ext_id = _extract_by_path(record, id_field)
    if not ext_id:
        ext_id = record.get("task_id") or record.get("id")
    if not ext_id:
        return None
    raw_status = _extract_by_path(record, status_field)
    norm, err, err_code, raw_status = _normalise_video_status(raw_status, record)
    return {
        "external_task_id": str(ext_id),
        "status": norm,
        "url": _normalise_video_result_url(_nested_video_url(record), gateway_config),
        "error": err,
        "error_code": err_code,
        "raw_status": raw_status or None,
        "raw": data,
    }


def _submit_video_ark(
    prompt: str,
    model_id: str,
    params: dict,
    extra: dict | None = None,
    gateway_config: RuntimeGatewayConfig | None = None,
) -> str:
    from . import gateway as _gw

    payload = _ark_payload(model_id, prompt, params, extra=extra)
    if gateway_config is None:
        data = _gw._video_post("/contents/generations/tasks", payload)
    else:
        data = _gw._video_post("/contents/generations/tasks", payload, config=gateway_config)
    task_id = data.get("id")
    if not task_id:
        raise GatewayError(f"Ark 未返回任务号: {str(data)[:200]}")
    return str(task_id)


def _poll_video_ark(task_id: str, gateway_config: RuntimeGatewayConfig | None = None) -> dict:
    from . import gateway as _gw

    if gateway_config is None:
        data = _gw._video_get(f"/contents/generations/tasks/{task_id}", timeout=30)
    else:
        data = _gw._video_get(
            f"/contents/generations/tasks/{task_id}", timeout=30, config=gateway_config
        )
    norm, err, err_code, raw_status = _normalise_video_status(data.get("status"), data)
    url = None
    if norm == "succeeded":
        content = data.get("content") or {}
        url = (
            _nested_video_url(content)
            or _nested_video_url(data.get("data"))
            or _nested_video_url(data)
        )
        url = _normalise_video_result_url(url, gateway_config)
    return {
        "status": norm,
        "url": url,
        "error": err,
        "error_code": err_code,
        "raw_status": raw_status or None,
        "raw": data,
    }
