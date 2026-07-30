"""Gateway HTTP transport layer — shared client, auth, retry, error handling."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Collection
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings
from .model_gateway_config import RuntimeGatewayConfig
from .safe_logging import redact_url_for_log
from .ssrf import (
    SsrfError,
)

log = logging.getLogger("gateway")
_IMAGE_GATEWAY_SEMAPHORE_KEY = "gateway:image:semaphore"


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool = False,
        submit_state_unknown: bool | None = None,
        error_code: str | None = None,
        phase: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        self.submit_state_unknown = submit_state_unknown
        self.error_code = error_code
        self.phase = phase


@dataclass(frozen=True)
class ImageSubrequestFailure:
    index: int
    message: str
    submit_state_unknown: bool
    retryable_refill: bool


@dataclass(frozen=True)
class ImageResponseDiagnostic:
    size: str | None = None
    quality: str | None = None
    output_format: str | None = None
    model: str | None = None
    selected_source: str | None = None


class ImageBatchResult(list[bytes]):
    """Image bytes with diagnostics for failed batch slots."""

    def __init__(
        self,
        images: list[bytes],
        failures: list[ImageSubrequestFailure] | None = None,
        diagnostics: list[ImageResponseDiagnostic] | None = None,
    ) -> None:
        super().__init__(images)
        self.failures = failures or []
        self.diagnostics = diagnostics or []


def _gateway_error_message(status_code: int, text: str) -> tuple[str, str]:
    message = text[:300]
    try:
        err = json.loads(text).get("error") or {}
    except Exception:
        err = {}
    provider_message = str(err.get("message") or "").strip()
    if provider_message == "No available compatible accounts":
        message = (
            "当前网关没有可用账号支持该模型/参数组合。"
            "请稍后重试,或在后台切换到有图像生成额度/能力的模型与网关账号。"
        )
    elif provider_message:
        message = provider_message[:300]
    return f"网关返回 {status_code}: {message}", provider_message


def _auth(config: RuntimeGatewayConfig | None = None) -> dict:
    key = config.api_key if config is not None else settings.gateway_api_key
    if config is not None and config.gateway_format == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {"Authorization": f"Bearer {key}"}


def _base_url(config: RuntimeGatewayConfig | None = None) -> str:
    return (config.base_url if config is not None else settings.gateway_base_url).rstrip("/")


def _join_api_path(config: RuntimeGatewayConfig | None, path: str) -> str:
    base = _base_url(config)
    if config is not None and config.gateway_format == "ark":
        if path.startswith("/v1/"):
            path = path[3:]
        return f"{base}{path}"
    if path.startswith("/v1/"):
        return f"{base}{path}"
    if base.endswith("/v1"):
        return f"{base}{path}"
    return f"{base}/v1{path}"


def _gateway_mock(config: RuntimeGatewayConfig | None = None) -> bool:
    if settings.mock_mode:
        return True
    if config is None or config.source == "env":
        return settings.effective_mock_mode
    return False


def _ensure_gateway_configured(config: RuntimeGatewayConfig | None, label: str) -> None:
    if settings.mock_mode:
        return
    if config is not None and config.source in {"model", "probe"} and not config.configured:
        raise GatewayError(f"{label}网关配置不完整:缺少 Base URL 或 API Key")


def _video_gateway_mock(config: RuntimeGatewayConfig | None = None) -> bool:
    if settings.mock_mode:
        return True
    if config is None or config.source == "env":
        return settings.effective_video_mock
    return False


def _pin_required(url: str) -> bool:
    return bool(urlparse(url).hostname)


def _trusted_configured_host(url: str, trusted_hosts: Collection[str] | None) -> bool:
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    return bool(host and host in {str(item).rstrip(".").lower() for item in trusted_hosts or ()})


def _trusted_request_options(config: RuntimeGatewayConfig | None) -> dict:
    if config is None or not config.trusted_hosts:
        return {}
    return {"trusted_hosts": config.trusted_hosts}


@contextmanager
def _guarded_stream(client: httpx.Client, method: str, url: str, **kwargs):
    from . import gateway as _gw

    trusted_hosts = kwargs.pop("trusted_hosts", None)
    if _trusted_configured_host(url, trusted_hosts):
        with client.stream(method, url, **kwargs) as response:
            yield response
    elif _pin_required(url):
        timeout = kwargs.pop("timeout", getattr(client, "timeout", None))
        with _gw.pinned_client(
            url,
            follow_redirects=False,
            timeout=timeout,
        ) as guarded_client:
            with guarded_client.stream(method, url, **kwargs) as response:
                yield response
    else:
        with client.stream(method, url, **kwargs) as response:
            yield response


def _request(
    method: str,
    url: str,
    *,
    headers: dict,
    json: dict | None = None,
    data: dict | None = None,
    files: list | None = None,
    timeout: int,
    retries: int,
    trusted_hosts: Collection[str] | None = None,
) -> httpx.Response:
    """Single HTTP path for every gateway call (image + video).

    Unified timeout + retry + error normalisation. Retries only transient
    failures (connect/timeout, HTTP 429 and >=500); 4xx are terminal (no point
    retrying an auth/bad-request error). ``retries=0`` for non-idempotent
    submits so a retry can never create a duplicate job."""
    from . import gateway as _gw

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            if _trusted_configured_host(url, trusted_hosts):
                client_ctx = _gw.httpx.Client(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                )
            elif _pin_required(url):
                client_ctx = _gw.pinned_client(url, timeout=timeout, follow_redirects=False)
            else:
                client_ctx = _gw.httpx.Client(timeout=timeout, follow_redirects=False)
            with client_ctx as c:
                request_kwargs = {"headers": headers}
                if json is not None:
                    request_kwargs["json"] = json
                if data is not None:
                    request_kwargs["data"] = data
                if files is not None:
                    request_kwargs["files"] = files
                r = c.request(method, url, **request_kwargs)
            log.info(
                "gateway %s %s -> %s in %.2fs",
                method,
                redact_url_for_log(url),
                r.status_code,
                time.time() - t0,
            )
            if r.is_redirect:
                location = r.headers.get("location")
                if location:
                    redirect_url = urljoin(url, location)
                    try:
                        _gw.assert_safe_url(redirect_url)
                    except SsrfError as e:
                        raise GatewayError(
                            f"网关返回不安全重定向,已拒绝跟随: {e}",
                            status_code=r.status_code,
                            transient=False,
                        ) from e
                raise GatewayError(
                    f"网关返回重定向 {r.status_code},已拒绝自动跟随",
                    status_code=r.status_code,
                    transient=False,
                )
            if r.status_code >= 400:
                msg, provider_message = _gateway_error_message(r.status_code, r.text)
                submit_state_unknown = None
                if provider_message == "No available compatible accounts":
                    submit_state_unknown = False
                if r.status_code < 500 and r.status_code != 429:
                    raise GatewayError(
                        msg,
                        status_code=r.status_code,
                        transient=False,
                        submit_state_unknown=submit_state_unknown,
                    )
                last = GatewayError(
                    msg,
                    status_code=r.status_code,
                    transient=True,
                    submit_state_unknown=submit_state_unknown,
                )
            else:
                return r
        except GatewayError:
            raise
        except Exception as e:  # noqa: BLE001 — connect/timeout etc.
            last = GatewayError(str(e), transient=True)
            log.warning(
                "gateway %s %s attempt %s failed: %s",
                method,
                redact_url_for_log(url),
                attempt + 1,
                e,
            )
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    if isinstance(last, GatewayError):
        raise GatewayError(
            f"网关调用失败: {last}",
            status_code=last.status_code,
            transient=last.transient,
            submit_state_unknown=last.submit_state_unknown,
        )
    raise GatewayError(f"网关调用失败: {last}", transient=True)


def _post(
    path: str,
    payload: dict,
    timeout: int | None = None,
    config: RuntimeGatewayConfig | None = None,
    retries: int | None = None,
) -> dict:
    url = _join_api_path(config, path)
    r = _request(
        "POST",
        url,
        headers={**_auth(config), "Content-Type": "application/json"},
        json=payload,
        timeout=timeout or settings.gateway_timeout_seconds,
        retries=settings.gateway_max_retries if retries is None else retries,
        **_trusted_request_options(config),
    )
    return r.json()


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict,
    payload: dict | None,
    timeout: int,
    retries: int,
    trusted_hosts: Collection[str] | None = None,
) -> dict:
    r = _request(
        method,
        url,
        headers=headers,
        json=payload,
        timeout=timeout,
        retries=retries,
        trusted_hosts=trusted_hosts,
    )
    return r.json()


def _request_multipart_json(
    method: str,
    url: str,
    *,
    headers: dict,
    data: dict,
    files: list,
    timeout: int,
    retries: int,
    trusted_hosts: Collection[str] | None = None,
) -> dict:
    r = _request(
        method,
        url,
        headers=headers,
        data=data,
        files=files,
        timeout=timeout,
        retries=retries,
        trusted_hosts=trusted_hosts,
    )
    return r.json()


def _get(path: str, timeout: int | None = None, config: RuntimeGatewayConfig | None = None) -> dict:
    url = _join_api_path(config, path)
    r = _request(
        "GET",
        url,
        headers=_auth(config),
        timeout=timeout or settings.gateway_timeout_seconds,
        retries=settings.gateway_max_retries,
        **_trusted_request_options(config),
    )
    return r.json()


def list_models(config: RuntimeGatewayConfig) -> list[dict]:
    """Return models advertised by an OpenAI-compatible provider."""
    _ensure_gateway_configured(config, "模型")
    if _gateway_mock(config):
        raise GatewayError("模型提供商未配置 Base URL 或 API Key")
    data = _get("/models", timeout=30, config=config)
    raw = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise GatewayError("模型列表接口返回格式不符合预期")
    models = []
    for item in raw:
        if isinstance(item, str):
            models.append({"id": item})
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("model") or item.get("name")
            if model_id:
                models.append(
                    {
                        "id": str(model_id),
                        "owned_by": item.get("owned_by") or item.get("provider"),
                        "object": item.get("object"),
                    }
                )
    return models
