"""Image generation via gateway."""
from __future__ import annotations

import base64
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from ..config import settings
from . import locks
from .gateway_mocks import mock_image as _mock_image
from .gateway_transport import (
    _IMAGE_GATEWAY_SEMAPHORE_KEY,
    GatewayError,
    ImageBatchResult,
    ImageResponseDiagnostic,
    ImageSubrequestFailure,
    _auth,
    _ensure_gateway_configured,
    _gateway_mock,
    _join_api_path,
)
from .model_gateway_config import RuntimeGatewayConfig

log = logging.getLogger("gateway")

_IMAGE_RESULT_URL_FIELDS = (
    "download_url",
    "hd_url",
    "original_url",
    "output_url",
    "image_url",
    "url",
)

_RESERVED_OUTBOUND_PAYLOAD_FIELDS = frozenset(
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

_DATA_URI_IMAGE_RE = re.compile(
    r"data:(image/(?:png|jpe?g|webp));base64,([A-Za-z0-9+/=\r\n]+)",
    re.IGNORECASE,
)


def _max_b64_len(max_bytes: int) -> int:
    return ((max_bytes + 2) // 3) * 4 + 8


def _anthropic_image_source(ref: str) -> dict:
    value = str(ref or "").strip()
    if value.startswith("data:"):
        match = re.fullmatch(
            r"data:(?P<media_type>image/[A-Za-z0-9.+-]+);base64,(?P<data>.+)",
            value,
            flags=re.DOTALL,
        )
        if match is None:
            raise GatewayError("Anthropic 视觉反推仅支持 Base64 data URI 图片")
        media_type = match.group("media_type").lower()
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            raise GatewayError(f"Anthropic 视觉反推不支持图片格式: {media_type}")
        encoded = "".join(match.group("data").split())
        if not encoded:
            raise GatewayError("Anthropic 视觉反推图片 Base64 数据为空")
        return {
            "type": "base64",
            "media_type": media_type,
            "data": encoded,
        }
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return {"type": "url", "url": value}
    raise GatewayError("Anthropic 视觉反推图片必须是 HTTP(S) URL 或 Base64 data URI")


def _image_result_urls(item: dict) -> list[tuple[str, str]]:
    urls: list[str] = []
    for field in _IMAGE_RESULT_URL_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            urls.append((field, value.strip()))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for field, url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append((field, url))
    return unique


def _image_response_diagnostic(
    data: dict, item: dict, source: str | None
) -> ImageResponseDiagnostic:
    return ImageResponseDiagnostic(
        size=str(item.get("size") or data.get("size") or "") or None,
        quality=str(item.get("quality") or data.get("quality") or "") or None,
        output_format=str(item.get("output_format") or data.get("output_format") or "") or None,
        model=str(item.get("model") or data.get("model") or "") or None,
        selected_source=source,
    )


def _decode_image_response(data: dict) -> list[bytes]:
    return _decode_image_response_with_diagnostics(data)[0]


def _decode_image_response_with_diagnostics(
    data: dict,
) -> tuple[list[bytes], list[ImageResponseDiagnostic]]:
    from . import gateway as _gw

    out: list[bytes] = []
    diagnostics: list[ImageResponseDiagnostic] = []
    max_bytes = int(settings.generated_image_max_bytes)
    for d in data.get("data", []):
        if urls := _image_result_urls(d):
            source, url = urls[0]
            out.append(
                _gw._download(
                    url,
                    max_bytes=max_bytes,
                    allowed_content_types=("image/",),
                    timeout_seconds=int(settings.image_download_timeout_seconds),
                )
            )
            diagnostics.append(_image_response_diagnostic(data, d, source))
        elif d.get("b64_json"):
            encoded = str(d["b64_json"])
            if len(encoded) > _max_b64_len(max_bytes):
                raise GatewayError("图像结果超出大小上限")
            raw = base64.b64decode(encoded)
            if len(raw) > max_bytes:
                raise GatewayError("图像结果超出大小上限")
            out.append(raw)
            diagnostics.append(_image_response_diagnostic(data, d, "b64_json"))
    return out, diagnostics


def _anthropic_message_text(data: dict) -> str:
    content = data.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _decode_message_data_uri_images(data: dict) -> list[bytes]:
    text = _anthropic_message_text(data)
    max_bytes = int(settings.generated_image_max_bytes)
    images: list[bytes] = []
    for match in _DATA_URI_IMAGE_RE.finditer(text):
        encoded = re.sub(r"\s+", "", match.group(2))
        if len(encoded) > _max_b64_len(max_bytes):
            raise GatewayError("图像结果超出大小上限")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise GatewayError("Anthropic Messages 返回的图像数据无法解码") from exc
        if not raw:
            continue
        if len(raw) > max_bytes:
            raise GatewayError("图像结果超出大小上限")
        images.append(raw)
    return images


def _gen_image_via_anthropic_messages(
    prompt: str,
    image_model_id: str,
    *,
    n: int,
    size: str,
    refs: list[str],
    extra: dict,
    gateway_config: RuntimeGatewayConfig,
) -> ImageBatchResult:
    from . import gateway as _gw

    max_tokens = int(extra.pop("message_max_tokens", 4096) or 4096)
    if refs:
        request_prompt = (
            f"{prompt.strip()}\n\n"
            "Edit the supplied reference image(s) according to the instruction. "
            f"Return exactly one edited image. Target canvas: {size}. "
            "Return the edited image directly."
        )
        content: str | list[dict] = [
            *(
                {"type": "image", "source": _anthropic_image_source(ref)}
                for ref in refs
            ),
            {"type": "text", "text": request_prompt},
        ]
    else:
        request_prompt = (
            f"{prompt.strip()}\n\n"
            f"Generate exactly one image. Target canvas: {size}. "
            "Return the generated image directly."
        )
        content = request_prompt
    payload = {
        "model": image_model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }

    def one() -> bytes:
        data = _gw._post(
            "/messages",
            payload,
            timeout=settings.image_gateway_timeout_seconds,
            config=gateway_config,
            retries=0,
        )
        images = _decode_message_data_uri_images(data)
        if not images:
            raise GatewayError(
                "Anthropic Messages 未返回 Data URI 图像",
                transient=False,
                submit_state_unknown=False,
            )
        return images[0]

    workers = max(1, min(n, int(settings.image_gateway_parallelism or 1)))
    if workers == 1:
        return ImageBatchResult([one() for _ in range(n)])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return ImageBatchResult(list(pool.map(lambda _: one(), range(n))))


def _grok_image_payload(
    image_model_id: str,
    prompt: str,
    size: str,
    extra: dict,
) -> dict:
    try:
        width, height = (int(value) for value in str(size).lower().split("x", 1))
    except (TypeError, ValueError):
        width, height = 1024, 1024
    aspect_ratio = "1:1"
    if width > height:
        aspect_ratio = "16:9" if width / max(height, 1) >= 1.5 else "4:3"
    elif height > width:
        aspect_ratio = "9:16" if height / max(width, 1) >= 1.5 else "3:4"
    resolution = "2k" if max(width, height) > 1536 else "1k"
    return {
        "model": image_model_id,
        "prompt": prompt,
        "aspect_ratio": extra.pop("aspect_ratio", aspect_ratio),
        "resolution": extra.pop("resolution", resolution),
        "response_format": extra.pop("response_format", "b64_json"),
        **extra,
    }


def _grok_image_edit_payload(
    image_model_id: str,
    prompt: str,
    size: str,
    refs: list[str],
    extra: dict,
) -> dict:
    extra.pop("edit_payload_format", None)
    extra.pop("_edit_payload_format", None)
    extra.pop("mask", None)
    payload = _grok_image_payload(image_model_id, prompt, size, extra)
    sources = [{"type": "image_url", "url": ref} for ref in refs]
    if len(sources) == 1:
        payload["image"] = sources[0]
    else:
        payload["images"] = sources
    return payload


def _image_quality_for_size(size: str | None) -> str:
    try:
        w, h = (int(x) for x in str(size or "").lower().split("x", 1))
    except (TypeError, ValueError):
        return "medium"
    longest = max(w, h)
    if longest > 2560:
        return "high"
    if longest > 1280:
        return "medium"
    return "low"


def _data_uri_file(value: str, fallback_name: str) -> tuple[str, bytes, str]:
    prefix, _, encoded = str(value or "").partition(",")
    if not encoded or not prefix.startswith("data:image/") or ";base64" not in prefix:
        raise GatewayError("OpenAI 图片编辑需要平台内图片文件引用")
    mime = prefix[5:].split(";", 1)[0] or "image/png"
    try:
        raw = base64.b64decode(encoded)
    except Exception as e:  # noqa: BLE001
        raise GatewayError("图片编辑引用解码失败") from e
    ext = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(mime.lower(), "png")
    return f"{fallback_name}.{ext}", raw, mime


def _is_openai_images_edit_path(path: str | None) -> bool:
    return str(path or "").rstrip("/").endswith("/images/edits")


def _image_edit_payload_format(path: str | None, extra: dict) -> str:
    explicit = (
        str(extra.pop("edit_payload_format", "") or extra.pop("_edit_payload_format", ""))
        .strip()
        .lower()
    )
    if explicit in {"json", "responses_json", "openai_json"}:
        return "json"
    if explicit in {"multipart", "openai_multipart"}:
        return "multipart"
    return "multipart" if _is_openai_images_edit_path(path) else "json"


def _image_edit_multipart_parts(
    *,
    model: str,
    prompt: str,
    size: str,
    refs: list[str],
    extra: dict,
) -> tuple[dict, list]:
    mask = extra.pop("mask", None)
    data = {
        "model": model,
        "prompt": prompt,
        "size": size,
    }
    for key, value in extra.items():
        if value in (None, ""):
            continue
        data[key] = str(value).lower() if isinstance(value, bool) else str(value)
    files = [
        ("image", _data_uri_file(ref, f"image-{index}")) for index, ref in enumerate(refs, start=1)
    ]
    if mask:
        files.append(("mask", _data_uri_file(str(mask), "mask")))
    return data, files


def map_outbound_payload_fields(payload: dict, field_map: dict[str, str] | None) -> dict:
    """Map one concrete outbound shape and reject mapped/unmapped collisions."""
    mapping = field_map or {}
    mapped: dict = {}
    owners: dict[str, str] = {}
    for logical_key, value in payload.items():
        target = mapping.get(logical_key, logical_key)
        if target in _RESERVED_OUTBOUND_PAYLOAD_FIELDS:
            raise GatewayError(f"出站字段 {target} 为保留控制字段")
        previous = owners.get(target)
        if previous is not None and previous != logical_key:
            raise GatewayError(
                f"field_map 将 {previous} 与 {logical_key} 映射到冲突字段 {target}"
            )
        owners[target] = logical_key
        mapped[target] = value
    return mapped


def _mapped_multipart_parts(
    data: dict,
    files: list,
    field_map: dict[str, str] | None,
) -> tuple[dict, list]:
    mapping = field_map or {}
    logical_shape = dict(data)
    logical_shape.update({logical_key: None for logical_key, _value in files})
    map_outbound_payload_fields(logical_shape, mapping)
    mapped_data = map_outbound_payload_fields(data, mapping)
    target_owners = {mapping.get(source, source): source for source in data}
    mapped_files = []
    for logical_key, value in files:
        target = mapping.get(logical_key, logical_key)
        if target in _RESERVED_OUTBOUND_PAYLOAD_FIELDS:
            raise GatewayError(f"出站字段 {target} 为保留控制字段")
        previous = target_owners.get(target)
        if previous is not None and previous != logical_key:
            raise GatewayError(
                f"field_map 将 {previous} 与 {logical_key} 映射到冲突字段 {target}"
            )
        target_owners[target] = logical_key
        mapped_files.append((target, value))
    return mapped_data, mapped_files


def _retryable_image_error(exc: Exception) -> bool:
    if not isinstance(exc, GatewayError):
        return False
    return bool(
        exc.transient
        and exc.status_code in (429, 500, 502, 503, 504)
        and getattr(exc, "submit_state_unknown", None) is False
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
        isinstance(exc, GatewayError) and getattr(exc, "transient", False) and not unknown
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


def _post_single_image_repeated(
    path: str, payload: dict, n: int, config: RuntimeGatewayConfig | None = None
) -> ImageBatchResult:
    """Run repeated one-image calls concurrently and return exactly n images.

    The current gateway rejects batch/tool-count params such as ``tools[0].n``.
    Repeating single-image requests preserves compatibility; parallelising them
    preserves the user's expected batch latency.
    """
    from . import gateway as _gw
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

    def one() -> tuple[list[bytes], list[ImageResponseDiagnostic]]:
        last: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with locks.RedisSemaphore(
                    _IMAGE_GATEWAY_SEMAPHORE_KEY,
                    int(settings.image_gateway_parallelism or 1),
                    ttl=max(60, int(settings.image_gateway_timeout_seconds) + 60),
                    wait_timeout=max(30, min(300, int(settings.image_gateway_timeout_seconds))),
                ):
                    data = _gw._request_json(
                        "POST",
                        url,
                        headers=headers,
                        payload=payload,
                        timeout=timeout,
                        retries=0,
                    )
                images, diagnostics = _decode_image_response_with_diagnostics(data)
                if not images:
                    raise GatewayError(
                        "图像子请求未返回结果",
                        transient=True,
                        submit_state_unknown=False,
                    )
                return images, diagnostics
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
        diagnostics: list[ImageResponseDiagnostic] = []
        total_bytes = 0
        idx = 0
        while idx < n + max_refills and len(out) < n:
            try:
                images, item_diagnostics = one()
                total_bytes = append_with_budget(out, images, total_bytes)
                diagnostics.extend(item_diagnostics)
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
        return ImageBatchResult(out[:n], failures=failures, diagnostics=diagnostics[:n])

    out_by_index: dict[int, list[bytes]] = {}
    diagnostics_by_index: dict[int, list[ImageResponseDiagnostic]] = {}
    pending = list(range(n))
    next_index = n
    while pending:
        wave_failures: list[ImageSubrequestFailure] = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
            futures = {pool.submit(one): i for i in pending}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    images, item_diagnostics = fut.result()
                    out_by_index[idx] = images
                    diagnostics_by_index[idx] = item_diagnostics
                except Exception as e:  # noqa: BLE001
                    failure = _image_subrequest_failure(idx, e)
                    log.warning("image sub-request %s failed: %s", failure.index, e)
                    failures.append(failure)
                    wave_failures.append(failure)
                    out_by_index[idx] = []
                    diagnostics_by_index[idx] = []
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
    diagnostics: list[ImageResponseDiagnostic] = []
    total_bytes = 0
    for i in sorted(out_by_index):
        total_bytes = append_with_budget(out, out_by_index.get(i, []), total_bytes)
        diagnostics.extend(diagnostics_by_index.get(i, []))
        if len(out) >= n:
            break
    if not out:
        _raise_image_batch_empty(failures)
    return ImageBatchResult(out[:n], failures=failures, diagnostics=diagnostics[:n])


def _post_single_image_multipart_repeated(
    path: str,
    data: dict,
    files: list,
    n: int,
    config: RuntimeGatewayConfig | None = None,
) -> ImageBatchResult:
    from . import gateway as _gw

    n = max(1, int(n))
    url = _join_api_path(config, path)
    headers = _auth(config)
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

    def one() -> tuple[list[bytes], list[ImageResponseDiagnostic]]:
        last: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with locks.RedisSemaphore(
                    _IMAGE_GATEWAY_SEMAPHORE_KEY,
                    int(settings.image_gateway_parallelism or 1),
                    ttl=max(60, int(settings.image_gateway_timeout_seconds) + 60),
                    wait_timeout=max(30, min(300, int(settings.image_gateway_timeout_seconds))),
                ):
                    response = _gw._request_multipart_json(
                        "POST",
                        url,
                        headers=headers,
                        data=data,
                        files=files,
                        timeout=timeout,
                        retries=0,
                    )
                images, diagnostics = _decode_image_response_with_diagnostics(response)
                if not images:
                    raise GatewayError(
                        "图像网关未返回任何结果", transient=True, submit_state_unknown=False
                    )
                return images, diagnostics
            except GatewayError as e:
                last = e
                if attempt < max_retries and _retryable_image_error(e):
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise
        raise last or GatewayError("图像网关未返回任何结果", transient=True)

    out_by_index: dict[int, list[bytes]] = {}
    diagnostics_by_index: dict[int, list[ImageResponseDiagnostic]] = {}
    failures: list[ImageSubrequestFailure] = []
    pending = list(range(n))
    next_index = n
    max_refills = max(0, int(getattr(settings, "image_gateway_refill_attempts", 0) or 0))
    while pending:
        wave_failures: list[ImageSubrequestFailure] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one): idx for idx in pending}
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    images, diagnostics = fut.result()
                    out_by_index[idx] = images
                    diagnostics_by_index[idx] = diagnostics
                except Exception as e:  # noqa: BLE001
                    failure = _image_subrequest_failure(idx, e)
                    log.warning("image edit sub-request %s failed: %s", failure.index, e)
                    failures.append(failure)
                    wave_failures.append(failure)
                    out_by_index[idx] = []
                    diagnostics_by_index[idx] = []
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
    diagnostics: list[ImageResponseDiagnostic] = []
    total_bytes = 0
    for i in sorted(out_by_index):
        total_bytes = append_with_budget(out, out_by_index.get(i, []), total_bytes)
        diagnostics.extend(diagnostics_by_index.get(i, []))
        if len(out) >= n:
            break
    if not out:
        _raise_image_batch_empty(failures)
    return ImageBatchResult(out[:n], failures=failures, diagnostics=diagnostics[:n])


def gen_image(
    prompt: str,
    image_model_id: str,
    n: int = 4,
    size: str = "1024x1024",
    reference_image_url: str | None = None,
    reference_image_urls: list[str] | None = None,
    edit_path: str | None = None,
    extra_payload: dict | None = None,
    gateway_config: RuntimeGatewayConfig | None = None,
    generation_path: str = "/images/generations",
    field_map: dict[str, str] | None = None,
) -> list[bytes]:
    """Returns a list of raw image bytes (already downloaded / decoded).

    If a reference image + an edit endpoint are provided (reverse-off,
    image+instruction -> image), call the image-to-image endpoint; otherwise
    fall back to plain text -> image. This keeps the default path safe even
    when the gateway has no edit endpoint configured.
    """
    from . import gateway as _gw

    _ensure_gateway_configured(gateway_config, "图像")
    if _gateway_mock(gateway_config):
        return [_mock_image(prompt, size, i) for i in range(n)]

    n = max(1, int(n))
    extra = {k: v for k, v in (extra_payload or {}).items() if v not in (None, "")}
    for control_field in _RESERVED_OUTBOUND_PAYLOAD_FIELDS:
        extra.pop(control_field, None)
    refs = [str(x) for x in (reference_image_urls or []) if x]
    if reference_image_url and not refs:
        refs = [reference_image_url]
    image_transport = str(extra.pop("image_transport", "openai_images") or "openai_images")
    if image_transport == "anthropic_messages":
        if gateway_config is None or gateway_config.gateway_format != "anthropic":
            raise GatewayError("Anthropic Messages 图片模型需要 Anthropic 网关格式")
        if field_map or generation_path != "/images/generations":
            raise GatewayError(
                "Anthropic Messages 图片适配器不支持 generation_path/field_map 覆盖"
            )
        return _gen_image_via_anthropic_messages(
            prompt,
            image_model_id,
            n=n,
            size=size,
            refs=refs,
            extra=extra,
            gateway_config=gateway_config,
        )
    if image_transport == "grok_images":
        if refs:
            if not edit_path:
                raise GatewayError("Grok 图片编辑需要配置 edit_path")
            payload = map_outbound_payload_fields(
                _grok_image_edit_payload(
                    image_model_id,
                    prompt,
                    size,
                    refs,
                    extra,
                ),
                field_map,
            )
            return _gw._post_single_image_repeated(
                edit_path,
                payload,
                n,
                config=gateway_config,
            )
        payload = map_outbound_payload_fields(
            _grok_image_payload(image_model_id, prompt, size, extra),
            field_map,
        )
        return _gw._post_single_image_repeated(
            generation_path, payload, n, config=gateway_config
        )
    extra.setdefault("quality", _image_quality_for_size(size))
    extra.setdefault("output_format", "jpeg")
    extra.setdefault("output_compression", 100)
    if refs and edit_path:
        edit_payload_format = _image_edit_payload_format(edit_path, extra)
        if edit_payload_format == "multipart":
            form_data, files = _image_edit_multipart_parts(
                model=image_model_id,
                prompt=prompt,
                size=size,
                refs=refs,
                extra=extra,
            )
            form_data, files = _mapped_multipart_parts(form_data, files, field_map)
            out = _gw._post_single_image_multipart_repeated(
                edit_path, form_data, files, n, config=gateway_config
            )
        else:
            payload = map_outbound_payload_fields(
                {
                    "model": image_model_id,
                    "images": [{"image_url": ref} for ref in refs],
                    "prompt": prompt,
                    "size": size,
                    **extra,
                },
                field_map,
            )
            out = _gw._post_single_image_repeated(edit_path, payload, n, config=gateway_config)
    else:
        payload = map_outbound_payload_fields(
            {"model": image_model_id, "prompt": prompt, "size": size, **extra},
            field_map,
        )
        out = _gw._post_single_image_repeated(generation_path, payload, n, config=gateway_config)
    if not out:
        raise GatewayError("图像网关未返回任何结果")
    return out
