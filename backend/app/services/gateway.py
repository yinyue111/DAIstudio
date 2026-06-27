"""Model-gateway client.

All AI capability is collapsed into a small set of calls against your existing
gateway (same base_url + api_key, only the model id changes):

  reverse_prompt()  -> POST /v1/chat/completions   (vision model, image -> words)
  gen_image()       -> POST /v1/images/generations (image model, words -> image)
  submit_video()/poll_video() -> async per-vendor video task (submit -> poll)

Cross-cutting: timeout, retry, error normalisation, call logging (real cost).
When the gateway is not configured (or mock_mode) every call returns locally
generated placeholder media so the whole flow runs offline.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings
from . import locks, storage
from .gateway_mocks import mock_image as _mock_image
from .gateway_mocks import mock_video_preview_image as mock_video_preview_image
from .gateway_prompting import mock_reverse as _mock_reverse
from .gateway_prompting import parse_structured as _parse_structured
from .gateway_prompting import reverse_template as _reverse_template
from .gateway_video_payloads import VIDEO_STATUS as _VIDEO_STATUS
from .gateway_video_payloads import ark_content as _ark_content
from .gateway_video_payloads import ark_text as _ark_text  # noqa: F401 - legacy test/debug hook
from .gateway_video_payloads import extract_by_path as _extract_by_path
from .gateway_video_payloads import generic_video_payload_params as _generic_video_payload_params
from .gateway_video_payloads import nested_video_url as _nested_video_url
from .model_gateway_config import RuntimeGatewayConfig
from .safe_logging import redact_url_for_log
from .ssrf import (
    MAX_REDIRECTS,
    SsrfError,
    assert_safe_url,
    pinned_client,
)

log = logging.getLogger("gateway")
_IMAGE_GATEWAY_SEMAPHORE_KEY = "gateway:image:semaphore"
_DOWNLOAD_HEADERS = {"Accept-Encoding": "identity"}

class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool = False,
        submit_state_unknown: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        self.submit_state_unknown = submit_state_unknown


@dataclass(frozen=True)
class ImageSubrequestFailure:
    index: int
    message: str
    submit_state_unknown: bool
    retryable_refill: bool


class ImageBatchResult(list[bytes]):
    """Image bytes with diagnostics for failed batch slots."""

    def __init__(
        self,
        images: list[bytes],
        failures: list[ImageSubrequestFailure] | None = None,
    ) -> None:
        super().__init__(images)
        self.failures = failures or []


def _gateway_error_message(status_code: int, text: str) -> tuple[str, str]:
    message = text[:300]
    try:
        err = (json.loads(text).get("error") or {})
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
    if config is None:
        return settings.effective_mock_mode
    return not config.base_url or not config.api_key


def _pin_required(url: str) -> bool:
    return bool(urlparse(url).hostname)


@contextmanager
def _guarded_stream(client: httpx.Client, method: str, url: str, **kwargs):
    if _pin_required(url):
        timeout = kwargs.pop("timeout", getattr(client, "timeout", None))
        with pinned_client(
            url,
            follow_redirects=False,
            timeout=timeout,
        ) as guarded_client:
            with guarded_client.stream(method, url, **kwargs) as response:
                yield response
    else:
        with client.stream(method, url, **kwargs) as response:
            yield response


def _request(method: str, url: str, *, headers: dict, json: dict | None = None,
             timeout: int, retries: int) -> httpx.Response:
    """Single HTTP path for every gateway call (image + video).

    Unified timeout + retry + error normalisation. Retries only transient
    failures (connect/timeout, HTTP 429 and >=500); 4xx are terminal (no point
    retrying an auth/bad-request error). ``retries=0`` for non-idempotent
    submits so a retry can never create a duplicate job."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            if _pin_required(url):
                client_ctx = pinned_client(url, timeout=timeout, follow_redirects=False)
            else:
                client_ctx = httpx.Client(timeout=timeout, follow_redirects=False)
            with client_ctx as c:
                r = c.request(method, url, headers=headers, json=json)
            log.info("gateway %s %s -> %s in %.2fs", method, redact_url_for_log(url), r.status_code,
                     time.time() - t0)
            if r.is_redirect:
                location = r.headers.get("location")
                if location:
                    redirect_url = urljoin(url, location)
                    try:
                        assert_safe_url(redirect_url)
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
            log.warning("gateway %s %s attempt %s failed: %s", method, redact_url_for_log(url),
                        attempt + 1, e)
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


def _post(path: str, payload: dict, timeout: int | None = None,
          config: RuntimeGatewayConfig | None = None,
          retries: int | None = None) -> dict:
    url = _join_api_path(config, path)
    r = _request("POST", url,
                 headers={**_auth(config), "Content-Type": "application/json"},
                 json=payload, timeout=timeout or settings.gateway_timeout_seconds,
                 retries=settings.gateway_max_retries if retries is None else retries)
    return r.json()


def _request_json(method: str, url: str, *, headers: dict, payload: dict | None,
                  timeout: int, retries: int) -> dict:
    r = _request(method, url, headers=headers, json=payload,
                 timeout=timeout, retries=retries)
    return r.json()


def _get(path: str, timeout: int | None = None,
         config: RuntimeGatewayConfig | None = None) -> dict:
    url = _join_api_path(config, path)
    r = _request("GET", url, headers=_auth(config),
                 timeout=timeout or settings.gateway_timeout_seconds,
                 retries=settings.gateway_max_retries)
    return r.json()


def list_models(config: RuntimeGatewayConfig) -> list[dict]:
    """Return models advertised by an OpenAI-compatible provider."""
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
                models.append({
                    "id": str(model_id),
                    "owned_by": item.get("owned_by") or item.get("provider"),
                    "object": item.get("object"),
                })
    return models


# ---------------------------------------------------------------- reverse prompt
def reverse_prompt(image_refs, vision_model_id: str, target: str = "image",
                   gateway_config: RuntimeGatewayConfig | None = None) -> dict:
    """Reverse one or more images into a structured prompt.

    ``image_refs`` is a single URL/data-URI or a list of them (e.g. several
    keyframes sampled from a reference video, in temporal order). ``target``
    selects the image vs video template. Returns the parsed prompt plus the
    provider ``usage`` (real token consumption) and call latency."""
    if _gateway_mock(gateway_config):
        return _mock_reverse(target)
    refs = [image_refs] if isinstance(image_refs, str) else [r for r in image_refs if r]
    if not refs:
        raise GatewayError("反推缺少可用的参考图")
    content = [{"type": "image_url", "image_url": {"url": r}} for r in refs]
    content.append({"type": "text", "text": _reverse_template(target, n_frames=len(refs))})
    payload = {
        "model": vision_model_id,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,  # low temp -> more faithful, repeatable description
    }
    t0 = time.time()
    data = _post(
        "/chat/completions",
        payload,
        timeout=int(settings.reverse_gateway_timeout_seconds or 150),
        config=gateway_config,
        retries=0,
    )
    latency_ms = int((time.time() - t0) * 1000)
    content_text = data["choices"][0]["message"]["content"]
    result = _parse_structured(content_text)
    result["usage"] = data.get("usage")  # {prompt_tokens, completion_tokens, total_tokens}
    result["latency_ms"] = latency_ms
    return result



def _decode_image_response(data: dict) -> list[bytes]:
    out: list[bytes] = []
    max_bytes = int(settings.generated_image_max_bytes)
    for d in data.get("data", []):
        if d.get("b64_json"):
            encoded = str(d["b64_json"])
            if len(encoded) > _max_b64_len(max_bytes):
                raise GatewayError("图像结果超出大小上限")
            raw = base64.b64decode(encoded)
            if len(raw) > max_bytes:
                raise GatewayError("图像结果超出大小上限")
            out.append(raw)
        elif d.get("url"):
            out.append(
                _download(
                    d["url"],
                    max_bytes=max_bytes,
                    allowed_content_types=("image/",),
                    timeout_seconds=int(settings.image_download_timeout_seconds),
                )
            )
    return out


def _reject_compressed_download(response: httpx.Response) -> None:
    encoding = (response.headers.get("content-encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        raise GatewayError("下载结果不支持压缩编码")


def _retryable_image_error(exc: Exception) -> bool:
    if not isinstance(exc, GatewayError):
        return False
    return bool(
        exc.transient
        and exc.status_code in (429, 500, 502, 503, 504)
    )


def _image_submit_state_unknown(exc: Exception) -> bool:
    if isinstance(exc, GatewayError):
        explicit = getattr(exc, "submit_state_unknown", None)
        if explicit is not None:
            return bool(explicit)
        status_code = getattr(exc, "status_code", None)
        if status_code is not None and status_code < 500 and status_code != 429:
            return False
        return bool(getattr(exc, "transient", False) or status_code is None or status_code >= 500)
    return True


def _image_subrequest_failure(index: int, exc: Exception) -> ImageSubrequestFailure:
    unknown = _image_submit_state_unknown(exc)
    retryable_refill = bool(
        isinstance(exc, GatewayError)
        and getattr(exc, "transient", False)
        and not unknown
    )
    return ImageSubrequestFailure(
        index=index + 1,
        message=(str(exc) or exc.__class__.__name__)[:300],
        submit_state_unknown=unknown,
        retryable_refill=retryable_refill,
    )


def _raise_image_batch_empty(failures: list[ImageSubrequestFailure]) -> None:
    if not failures:
        raise GatewayError("图像网关未返回任何结果", transient=True, submit_state_unknown=False)
    last = failures[-1]
    unknown = any(f.submit_state_unknown for f in failures)
    transient = unknown or any(f.retryable_refill for f in failures)
    raise GatewayError(
        f"图像网关未返回任何结果: {last.message}",
        transient=transient,
        submit_state_unknown=unknown,
    )


def _post_single_image_repeated(path: str, payload: dict, n: int,
                                config: RuntimeGatewayConfig | None = None) -> ImageBatchResult:
    """Run repeated one-image calls concurrently and return exactly n images.

    The current gateway rejects batch/tool-count params such as ``tools[0].n``.
    Repeating single-image requests preserves compatibility; parallelising them
    preserves the user's expected batch latency.
    """
    n = max(1, int(n))
    url = _join_api_path(config, path)
    headers = {**_auth(config), "Content-Type": "application/json"}
    timeout = settings.image_gateway_timeout_seconds
    workers = max(1, min(n, int(settings.image_gateway_parallelism or 1)))
    max_retries = max(0, int(settings.image_gateway_max_retries or 0))
    batch_max_bytes = max(
        int(settings.generated_image_max_bytes),
        int(settings.generated_image_batch_max_bytes or 0),
    )

    def append_with_budget(out: list[bytes], images: list[bytes], current_bytes: int) -> int:
        for img in images:
            current_bytes += len(img)
            if current_bytes > batch_max_bytes:
                raise GatewayError("图像批量结果超出大小上限")
            out.append(img)
        return current_bytes

    def one() -> list[bytes]:
        last: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with locks.RedisSemaphore(
                    _IMAGE_GATEWAY_SEMAPHORE_KEY,
                    int(settings.image_gateway_parallelism or 1),
                    ttl=max(60, int(settings.image_gateway_timeout_seconds) + 60),
                    wait_timeout=max(30, min(300, int(settings.image_gateway_timeout_seconds))),
                ):
                    data = _request_json(
                        "POST",
                        url,
                        headers=headers,
                        payload=payload,
                        timeout=timeout,
                        retries=0,
                    )
                images = _decode_image_response(data)
                if not images:
                    raise GatewayError(
                        "图像子请求未返回结果",
                        transient=True,
                        submit_state_unknown=False,
                    )
                return images
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt >= max_retries or not _retryable_image_error(e):
                    raise
                log.warning(
                    "single image sub-request transient status failed, retrying (%s/%s): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                time.sleep(1.0 * (attempt + 1))
        raise last or GatewayError("图像子请求失败")

    failures: list[ImageSubrequestFailure] = []
    max_refills = max(0, int(getattr(settings, "image_gateway_refill_attempts", 0) or 0))

    if workers == 1:
        out: list[bytes] = []
        total_bytes = 0
        idx = 0
        while idx < n + max_refills and len(out) < n:
            try:
                total_bytes = append_with_budget(out, one(), total_bytes)
            except Exception as e:  # noqa: BLE001
                log.warning("single image sub-request failed: %s", e)
                failure = _image_subrequest_failure(idx, e)
                failures.append(failure)
                if not failure.retryable_refill:
                    idx = n + max_refills
                    break
            idx += 1
        if not out:
            _raise_image_batch_empty(failures)
        return ImageBatchResult(out[:n], failures=failures)

    out_by_index: dict[int, list[bytes]] = {}
    pending = list(range(n))
    next_index = n
    while pending:
        wave_failures: list[ImageSubrequestFailure] = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
            futures = {pool.submit(one): i for i in pending}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    out_by_index[idx] = fut.result()
                except Exception as e:  # noqa: BLE001
                    failure = _image_subrequest_failure(idx, e)
                    log.warning("image sub-request %s failed: %s", failure.index, e)
                    failures.append(failure)
                    wave_failures.append(failure)
                    out_by_index[idx] = []
        produced = sum(len(images) for images in out_by_index.values())
        missing = max(0, n - produced)
        refill_count = min(
            missing,
            max_refills,
            sum(1 for failure in wave_failures if failure.retryable_refill),
        )
        if refill_count <= 0:
            break
        max_refills -= refill_count
        pending = list(range(next_index, next_index + refill_count))
        next_index += refill_count

    out: list[bytes] = []
    total_bytes = 0
    for i in sorted(out_by_index):
        total_bytes = append_with_budget(out, out_by_index.get(i, []), total_bytes)
        if len(out) >= n:
            break
    if not out:
        _raise_image_batch_empty(failures)
    return ImageBatchResult(out[:n], failures=failures)


# ----------------------------------------------------------------- text -> image
def gen_image(prompt: str, image_model_id: str, n: int = 4,
              size: str = "1024x1024", reference_image_url: str | None = None,
              reference_image_urls: list[str] | None = None,
              edit_path: str | None = None,
              extra_payload: dict | None = None,
              gateway_config: RuntimeGatewayConfig | None = None) -> list[bytes]:
    """Returns a list of raw image bytes (already downloaded / decoded).

    If a reference image + an edit endpoint are provided (reverse-off,
    image+instruction -> image), call the image-to-image endpoint; otherwise
    fall back to plain text -> image. This keeps the default path safe even
    when the gateway has no edit endpoint configured.
    """
    if _gateway_mock(gateway_config):
        return [_mock_image(prompt, size, i) for i in range(n)]

    n = max(1, int(n))
    extra = {k: v for k, v in (extra_payload or {}).items() if v not in (None, "")}
    refs = [str(x) for x in (reference_image_urls or []) if x]
    if reference_image_url and not refs:
        refs = [reference_image_url]
    if refs and edit_path:
        # Current OpenAI-compatible image-edit gateways often implement edits
        # through the Responses image tool, where `tools[0].n` is invalid.
        # Preserve the user's requested count by issuing single-image edits.
        payload = {
            "model": image_model_id,
            "images": [{"image_url": ref} for ref in refs],
            "prompt": prompt,
            "size": size,
            **extra,
        }
        out = _post_single_image_repeated(edit_path, payload, n, config=gateway_config)
    else:
        # The configured gateway maps image generation through an image tool
        # where `tools[0].n` is invalid. Repeat single-image requests instead.
        payload = {"model": image_model_id, "prompt": prompt, "size": size, **extra}
        out = _post_single_image_repeated("/images/generations", payload, n, config=gateway_config)
    if not out:
        raise GatewayError("图像网关未返回任何结果")
    return out


_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # generous cap for video results


def _max_b64_len(max_bytes: int) -> int:
    return ((max_bytes + 2) // 3) * 4 + 8


def _remaining_download_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GatewayError("下载结果超时")
    return remaining


def _download(
    url: str,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    allowed_content_types: tuple[str, ...] | None = None,
    timeout_seconds: int | None = None,
) -> bytes:
    """Download a gateway-returned result URL with the SSRF guard applied.

    Even though these URLs come from the (trusted) gateway, a poisoned or
    compromised response must not make the worker fetch internal/metadata
    endpoints — so redirects are NOT auto-followed: every hop is re-validated and
    the socket is pinned to the vetted public IP. Failures normalise to
    GatewayError so callers handle them uniformly."""
    try:
        assert_safe_url(url)
        timeout = int(timeout_seconds or settings.image_download_timeout_seconds)
        deadline = time.monotonic() + timeout
        with httpx.Client(follow_redirects=False, timeout=timeout) as c:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = _remaining_download_timeout(deadline)
                with _guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=remaining,
                    headers=_DOWNLOAD_HEADERS,
                ) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        assert_safe_url(url)  # red line: re-check every hop
                        continue
                    if r.status_code >= 400:
                        log.warning(
                            "gateway result download failed %s: %s",
                            r.status_code,
                            redact_url_for_log(url),
                        )
                        raise GatewayError(f"下载结果失败 {r.status_code}")
                    _reject_compressed_download(r)
                    content_type = (r.headers.get("content-type") or "").lower()
                    if allowed_content_types and not any(
                        content_type.startswith(prefix.lower()) for prefix in allowed_content_types
                    ):
                        raise GatewayError("下载结果类型不支持")
                    content_length = r.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise GatewayError("下载结果超出大小上限")
                    buf = bytearray()
                    for chunk in r.iter_raw():
                        if time.monotonic() > deadline:
                            raise GatewayError("下载结果超时")
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise GatewayError("下载结果超出大小上限")
                    return bytes(buf)
        raise GatewayError("下载结果重定向次数过多")
    except SsrfError as e:
        raise GatewayError(f"结果地址被安全策略拦截: {e}") from e


def download_bytes(url: str) -> bytes:
    return _download(url)


def download_bytes_limited(
    url: str,
    *,
    max_bytes: int,
    allowed_content_types: tuple[str, ...] | None = None,
    timeout_seconds: int | None = None,
) -> bytes:
    return _download(
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        timeout_seconds=timeout_seconds,
    )


def download_to_path(
    url: str,
    path,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    timeout_seconds: int | None = None,
    allowed_content_types: tuple[str, ...] | None = None,
) -> int:
    """Download a gateway result directly to disk with SSRF/redirect checks."""
    try:
        assert_safe_url(url)
        timeout = int(timeout_seconds or settings.image_download_timeout_seconds)
        deadline = time.monotonic() + timeout
        with httpx.Client(follow_redirects=False, timeout=timeout) as c:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = _remaining_download_timeout(deadline)
                with _guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=remaining,
                    headers=_DOWNLOAD_HEADERS,
                ) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        assert_safe_url(url)
                        continue
                    if r.status_code >= 400:
                        log.warning(
                            "gateway result download failed %s: %s",
                            r.status_code,
                            redact_url_for_log(url),
                        )
                        raise GatewayError(f"下载结果失败 {r.status_code}")
                    _reject_compressed_download(r)
                    content_type = (r.headers.get("content-type") or "").lower()
                    if allowed_content_types and not any(
                        content_type.startswith(prefix.lower()) for prefix in allowed_content_types
                    ):
                        raise GatewayError("下载结果类型不支持")
                    content_length = r.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise GatewayError("下载结果超出大小上限")
                    total = 0
                    with open(path, "wb") as f:
                        for chunk in r.iter_raw():
                            if time.monotonic() > deadline:
                                raise GatewayError("下载结果超时")
                            total += len(chunk)
                            if total > max_bytes:
                                raise GatewayError("下载结果超出大小上限")
                            f.write(chunk)
                    return total
        raise GatewayError("下载结果重定向次数过多")
    except SsrfError as e:
        raise GatewayError(f"结果地址被安全策略拦截: {e}") from e
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise


def download_to_storage(
    url: str,
    subdir: str,
    ext: str,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    timeout_seconds: int | None = None,
    allowed_content_types: tuple[str, ...] | None = None,
) -> str:
    key, path = storage.reserve_key(subdir, ext)
    try:
        download_to_path(
            url,
            path,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            allowed_content_types=allowed_content_types,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return key


# ----------------------------------------------------------------- text -> video
# The video gateway may differ from the image/vision gateway (its own base_url +
# api_key + format). Two formats supported: "ark" (Volcengine / Doubao Seedance,
# async /contents/generations/tasks) and a generic OpenAI-ish fallback.

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


def _video_post(path: str, payload: dict, timeout: int | None = None,
                config: RuntimeGatewayConfig | None = None) -> dict:
    # submit is non-idempotent -> retries=0 so a retry can't double-submit a job
    url = _video_url(path, config)
    r = _request("POST", url,
                 headers={**_video_auth(config), "Content-Type": "application/json"},
                 json=payload, timeout=timeout or settings.video_submit_timeout_seconds,
                 retries=0)
    return r.json()


def _video_get(path: str, timeout: int = 30,
               config: RuntimeGatewayConfig | None = None) -> dict:
    # poll is idempotent -> safe to retry transient blips
    url = _video_url(path, config)
    r = _request("GET", url, headers=_video_auth(config), timeout=timeout,
                 retries=settings.gateway_max_retries)
    return r.json()



def submit_video(prompt: str, video_model_id: str, params: dict,
                 extra: dict | None = None,
                 gateway_config: RuntimeGatewayConfig | None = None) -> str:
    """Submit an async video job; returns an external task id."""
    if settings.mock_mode:
        return f"mock-{random.randint(100000, 999999)}"
    if gateway_config is None and settings.effective_video_mock:
        return f"mock-{random.randint(100000, 999999)}"
    if gateway_config is not None and (not gateway_config.base_url or not gateway_config.api_key):
        return f"mock-{random.randint(100000, 999999)}"
    fmt = gateway_config.gateway_format if gateway_config is not None else settings.video_gateway_format
    if fmt == "ark":
        return _submit_video_ark(prompt, video_model_id, params, gateway_config=gateway_config)
    # generic OpenAI-ish fallback (configurable paths via model.extra)
    extra = extra or {}
    submit_path = extra.get("submit_path", "/v1/videos/generations")
    id_field = extra.get("id_field", "id")
    payload_params = _generic_video_payload_params(params or {}, extra)
    payload = {"model": video_model_id, "prompt": prompt, **payload_params}
    if gateway_config is None:
        data = _video_post(submit_path, payload)
    else:
        data = _video_post(submit_path, payload, config=gateway_config)
    task_id = data.get(id_field) or data.get("task_id") or data.get("id")
    if not task_id:
        raise GatewayError(f"视频网关未返回任务号: {str(data)[:200]}")
    return str(task_id)


def poll_video(external_task_id: str, video_model_id: str,
               extra: dict | None = None,
               gateway_config: RuntimeGatewayConfig | None = None) -> dict:
    """Poll one tick. Returns {status: queued|running|succeeded|failed, url?}."""
    if (
        settings.mock_mode
        or str(external_task_id).startswith("mock-")
        or (gateway_config is None and settings.effective_video_mock)
        or (gateway_config is not None and (not gateway_config.base_url or not gateway_config.api_key))
    ):
        return {"status": "succeeded", "url": None, "mock": True}
    fmt = gateway_config.gateway_format if gateway_config is not None else settings.video_gateway_format
    if fmt == "ark":
        return _poll_video_ark(external_task_id, gateway_config=gateway_config)
    extra = extra or {}
    poll_path = (extra.get("poll_path", "/v1/videos/{id}")).format(id=external_task_id)
    if gateway_config is None:
        data = _video_get(poll_path, timeout=30)
    else:
        data = _video_get(poll_path, timeout=30, config=gateway_config)
    norm = _VIDEO_STATUS.get((data.get("status") or "").lower(), "running")
    url = None
    if norm == "succeeded":
        url = (
            data.get("url")
            or _nested_video_url(data.get("data"))
            or _nested_video_url(data.get("output"))
        )
    return {"status": norm, "url": url, "raw": data}


def find_video_by_request_id(request_id: str, video_model_id: str,
                             extra: dict | None = None,
                             gateway_config: RuntimeGatewayConfig | None = None) -> dict | None:
    """Best-effort provider lookup for non-idempotent submits with unknown state.

    Providers differ on whether they support request_id/idempotency lookup, so
    this is explicitly opt-in through model.extra. When absent, callers keep the
    existing manual reconciliation path.
    """
    if not request_id:
        return None
    extra = extra or {}
    path_template = extra.get("request_query_path")
    if not path_template:
        return None
    path = str(path_template).format(request_id=request_id, model=video_model_id)
    data = _video_get(path, timeout=int(extra.get("request_query_timeout_seconds") or 30),
                      config=gateway_config)
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
    raw_status = str(_extract_by_path(record, status_field) or "").lower()
    norm = _VIDEO_STATUS.get(raw_status, "running" if raw_status else "queued")
    return {
        "external_task_id": str(ext_id),
        "status": norm,
        "url": _nested_video_url(record),
        "raw": data,
    }


def _submit_video_ark(prompt: str, model_id: str, params: dict,
                      gateway_config: RuntimeGatewayConfig | None = None) -> str:
    payload = {"model": model_id, "content": _ark_content(prompt, params)}
    if gateway_config is None:
        data = _video_post("/contents/generations/tasks", payload)
    else:
        data = _video_post("/contents/generations/tasks", payload, config=gateway_config)
    task_id = data.get("id")
    if not task_id:
        raise GatewayError(f"Ark 未返回任务号: {str(data)[:200]}")
    return str(task_id)


def _poll_video_ark(task_id: str, gateway_config: RuntimeGatewayConfig | None = None) -> dict:
    if gateway_config is None:
        data = _video_get(f"/contents/generations/tasks/{task_id}", timeout=30)
    else:
        data = _video_get(f"/contents/generations/tasks/{task_id}", timeout=30, config=gateway_config)
    norm = _VIDEO_STATUS.get((data.get("status") or "").lower(), "running")
    url = None
    err = None
    if norm == "succeeded":
        content = data.get("content") or {}
        url = content.get("video_url") or content.get("url")
        if not url and isinstance(data.get("data"), list) and data["data"]:
            url = data["data"][0].get("url")
    if norm == "failed":
        err = (data.get("error") or {}).get("message") or str(data.get("error") or "")[:200]
    return {"status": norm, "url": url, "error": err, "raw": data}
