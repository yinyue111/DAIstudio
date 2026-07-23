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
import math
import random
import re
import tempfile
import time
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

from ..config import settings
from . import locks, storage
from .gateway_mocks import mock_image as _mock_image
from .gateway_mocks import mock_video as mock_video
from .gateway_mocks import mock_video_preview_image as mock_video_preview_image
from .gateway_prompting import ReverseResultValidationError
from .gateway_prompting import _decode_json_object as _decode_reverse_json_object
from .gateway_prompting import compose_visual_final_text as _compose_visual_final_text
from .gateway_prompting import mock_reverse as _mock_reverse
from .gateway_prompting import (
    normalize_video_audio_feature_statuses as _normalize_video_audio_feature_statuses,
)
from .gateway_prompting import normalize_video_shots as _normalize_video_shots
from .gateway_prompting import (
    parse_structured as _parse_structured,  # noqa: F401 - legacy test hook
)
from .gateway_prompting import reverse_repair_template as _reverse_repair_template
from .gateway_prompting import reverse_template as _reverse_template
from .gateway_prompting import sanitize_video_audio_evidence as _sanitize_video_audio_evidence
from .gateway_prompting import (
    validate_missing_video_frame_result as _validate_missing_video_frame_result,
)
from .gateway_prompting import validate_reverse_result as _validate_reverse_result
from .gateway_prompting import video_analysis_gaps as _video_analysis_gaps
from .gateway_video_payloads import VIDEO_STATUS as _VIDEO_STATUS
from .gateway_video_payloads import (
    ark_content as _ark_content,  # noqa: F401 - legacy test/debug hook
)
from .gateway_video_payloads import ark_payload as _ark_payload
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
from .video_prompt_compiler import (
    clean_video_prompt_section,
    compact_single_clip_prompt,
    infer_video_model_profile,
    merge_video_constraint_clauses,
    parse_structured_video_sections,
    parse_video_prompt,
    render_structured_video_prompt,
    split_video_post_production,
    video_action_requirements,
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
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            if _trusted_configured_host(url, trusted_hosts):
                client_ctx = httpx.Client(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                )
            elif _pin_required(url):
                client_ctx = pinned_client(url, timeout=timeout, follow_redirects=False)
            else:
                client_ctx = httpx.Client(timeout=timeout, follow_redirects=False)
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


# ---------------------------------------------------------------- reverse prompt
_REVERSE_AUDIT_CONTEXT_FIELDS = ("operation_id", "preset", "cost_credits")


def _reverse_audit_context(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {key: None for key in _REVERSE_AUDIT_CONTEXT_FIELDS}
    return {
        key: (
            raw
            if raw is None or isinstance(raw, (str, int, float, bool))
            else str(raw)[:128]
        )
        for key in _REVERSE_AUDIT_CONTEXT_FIELDS
        for raw in (value.get(key),)
    }


def _log_reverse_audit_event(
    *,
    status: str,
    phase: str,
    target: str,
    audit_context: dict | None,
    latency_ms: int | None = None,
    error_code: str | None = None,
) -> None:
    event = {
        "event": "reverse_gateway_call",
        "status": status,
        "operation_id": None,
        "phase": phase,
        "error_code": error_code,
        "target": target,
        "preset": None,
        "cost_credits": None,
        "latency_ms": latency_ms,
        **_reverse_audit_context(audit_context),
    }
    message = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if status == "failed":
        log.warning("reverse_gateway_event=%s", message)
    else:
        log.info("reverse_gateway_event=%s", message)


def _video_analysis_context_text(video_analysis: dict) -> str:
    source = video_analysis.get("source") or {}
    duration = source.get("duration_seconds")
    fps = source.get("fps")
    audio = video_analysis.get("audio") if isinstance(video_analysis.get("audio"), dict) else {}
    audio_segments = audio.get("segments") if isinstance(audio.get("segments"), list) else []
    audio_feature_statuses = _normalize_video_audio_feature_statuses(
        audio,
        audio_analyzed=bool(source.get("audio_analyzed")),
    )
    audio_evidence = bool(
        audio_feature_statuses["asr"] in {"analyzed", "partial"}
        and any(isinstance(item, dict) and str(item.get("text") or "").strip() for item in audio_segments)
    )
    if audio_evidence:
        audio_state = "已完成带时间戳的音频转写，可依据下方证据分析"
    elif source.get("has_audio"):
        audio_state = "已检测到音轨,但未形成可验证的转写证据"
    else:
        audio_state = "未检测到音轨"
    analysis_mode = video_analysis.get("analysis_mode")
    mode_text = (
        "封面单帧降级分析,只能确认静态画面,不得推断完整时序动作"
        if analysis_mode == "cover_fallback"
        else "多片段关键帧分析"
        if analysis_mode == "keyframes_multi_segment"
        else "全时段关键帧分析"
    )
    lines = [
        "以下是后端探测的源视频权威事实,必须原样采用,不得根据静帧重新猜测:",
        f"- 分析模式: {mode_text}",
        f"- 显示尺寸: {source.get('width')}x{source.get('height')}"
        if source.get("width") and source.get("height")
        else "- 显示尺寸: 未知",
        f"- 画幅比例: {source.get('ratio') or '未知'}",
        f"- 本次分析片段时长: {float(duration):.3f} 秒" if duration is not None else "- 本次分析片段时长: 未知",
        f"- 帧率: 约 {float(fps):.3f} fps" if fps is not None else "- 帧率: 未知",
        f"- 音频: {audio_state}",
    ]
    if audio:
        lines.append(
            "- 音频子能力状态: "
            + ", ".join(
                f"{feature}={audio_feature_statuses[feature]}"
                for feature in ("asr", "speaker", "music", "beat", "sfx")
            )
        )
        lines.append(
            "- 每项音频结论必须由对应子能力独立支撑;"
            "ASR 仅支撑转写文字,不得推断说话人、音乐、BPM/节拍或音效。"
        )
    source_range = source.get("source_range")
    if isinstance(source_range, dict):
        lines.append(
            "- 原视频分析范围: "
            f"{float(source_range.get('start_seconds') or 0):.3f}-"
            f"{float(source_range.get('end_seconds') or 0):.3f} 秒；"
            "分镜起止时间必须使用片段内相对时间"
        )
    source_ranges = source.get("source_ranges")
    if isinstance(source_ranges, list) and len(source_ranges) > 1:
        lines.append("- 本次选中多个原视频片段：")
        for index, item in enumerate(source_ranges, start=1):
            if not isinstance(item, dict):
                continue
            lines.append(
                f"  - 片段 {index}: "
                f"{float(item.get('start_seconds') or 0):.3f}-"
                f"{float(item.get('end_seconds') or 0):.3f} 秒"
            )
        lines.append(
            "- 多片段分镜必须输出 source_segment_index，"
            "start_seconds/end_seconds 使用原视频绝对时间，不得跨片段。"
        )
    else:
        lines.append(
            "- 本次只有 1 个源片段；source_segment_index 是源片段号而不是帧号，"
            "如输出该字段必须固定为 1。"
        )
    if source.get("total_duration_seconds") is not None:
        lines.append(f"- 原视频总时长: {float(source['total_duration_seconds']):.3f} 秒")
    shot_detection = (
        video_analysis.get("shot_detection")
        if isinstance(video_analysis.get("shot_detection"), dict)
        else {}
    )
    detected_shots = (
        shot_detection.get("shots")
        if isinstance(shot_detection.get("shots"), list)
        else []
    )
    if shot_detection:
        lines.append(
            "- FFmpeg 场景切分: "
            f"{shot_detection.get('status') or 'unknown'}，"
            f"检测到 {int(shot_detection.get('scene_boundary_count') or 0)} 个场景变化边界；"
            "这些边界是服务端时序证据，分镜应优先与其对齐，除非采样帧明确支持不同边界。"
        )
        multi_segment = isinstance(source_ranges, list) and len(source_ranges) > 1
        for shot in detected_shots[:100]:
            if not isinstance(shot, dict):
                continue
            start_key = "start_seconds" if multi_segment else "relative_start_seconds"
            end_key = "end_seconds" if multi_segment else "relative_end_seconds"
            try:
                start = float(shot.get(start_key))
                end = float(shot.get(end_key))
            except (TypeError, ValueError):
                continue
            segment = int(shot.get("source_segment_index") or 1)
            segment_text = f"片段 {segment} " if multi_segment else ""
            lines.append(f"  - {segment_text}{start:.3f}-{end:.3f} 秒")
        if shot_detection.get("degraded_reason"):
            lines.append(
                f"- 场景切分降级原因: {str(shot_detection['degraded_reason'])[:300]}"
            )
    if audio_evidence:
        lines.append("- 音频证据时间使用原视频绝对时间；只可依据以下分段转写描述旁白/对白：")
        evidence_chars = 0
        for segment in audio_segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(segment.get("start_seconds"))
                end = float(segment.get("end_seconds"))
            except (TypeError, ValueError):
                continue
            row = f"  - [{start:.3f}-{end:.3f}] {text[:500]}"
            if evidence_chars + len(row) > 12_000:
                lines.append("  - [其余音频证据因上下文长度限制省略]")
                break
            lines.append(row)
            evidence_chars += len(row)
    elif audio.get("degraded_reason"):
        lines.append(f"- 音频分析降级原因: {str(audio['degraded_reason'])[:300]}")
    return "\n".join(lines) + "\n"


def _video_source_spec(video_analysis: dict) -> str:
    source = video_analysis.get("source") or {}
    parts = []
    if source.get("width") and source.get("height"):
        parts.append(f"{source['width']}x{source['height']}")
    if source.get("ratio"):
        parts.append(str(source["ratio"]))
    if source.get("duration_seconds") is not None:
        parts.append(f"{float(source['duration_seconds']):.3f}秒")
    if source.get("fps") is not None:
        parts.append(f"约{float(source['fps']):.3f}fps")
    return "，".join(parts)


def _video_generation_duration_suggestion(duration_seconds: object) -> str:
    """Translate source duration into a duration current video models can render."""
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return "建议按所选生成模型的时长上限设置"
    if not math.isfinite(duration) or duration <= 0:
        return "建议按所选生成模型的时长上限设置"
    if duration <= 15:
        display = f"{duration:.1f}".rstrip("0").rstrip(".")
        return f"建议生成约 {display} 秒单段视频"
    segment_count = max(2, math.ceil(duration / 15))
    return (
        "建议压缩为 15 秒以内的单段核心版；"
        f"如需保留完整内容，拆分为 {segment_count} 段分别生成后剪辑"
    )


def _video_shots_timeline(shots: list[dict]) -> str:
    rows = []
    for shot in shots:
        start = float(shot["start_seconds"])
        end = float(shot["end_seconds"])
        details = [
            str(shot.get(field) or "").strip()
            for field in ("visual", "action", "camera", "transition")
        ]
        detail = "，".join(dict.fromkeys(value for value in details if value))
        rows.append(f"{start:.3f}-{end:.3f}s {detail or '按参考帧复刻'}")
    return "；".join(rows)


def _without_conflicting_video_durations(text: str, duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return str(text or "").strip()
    duration_re = re.compile(
        r"(?P<label>(?:总时长|视频时长|成片时长|全片时长|片长|建议生成|"
        r"(?:total\s+)?(?:duration|runtime|length))"
        r"\s*(?:为|约|建议)?\s*)?"
        r"(?<![\d.\-])(?P<value>\d+(?:\.\d+)?)\s*"
        r"(?:秒钟?|seconds?|secs?|s)(?![A-Za-z])",
        re.IGNORECASE,
    )
    source_text = str(text or "")

    def replace(match: re.Match[str]) -> str:
        before = source_text[max(0, match.start() - 8) : match.start()]
        after = source_text[match.end() : match.end() + 24]
        shot_context = bool(
            re.search(r"(?:前|后|每|单个|第\d+个?|镜头|持续|停留|动作)\s*$", before)
            or re.search(
                r"(?:first|last|next|previous)\s*$|"
                r"(?:each|every|single|per)\s+(?:shot|scene)\s+(?:lasts?|holds?|for)\s*$|"
                r"(?:shot|scene)\s+(?:lasts?|holds?|for)\s*$",
                source_text[max(0, match.start() - 40) : match.start()],
                re.IGNORECASE,
            )
        )
        total_context = bool(match.group("label")) or bool(
            re.match(
                r"[^，,。；;\n]{0,20}(?:广告|短片|视频|成片|ad|video|clip|film)",
                after,
                re.IGNORECASE,
            )
        )
        value = float(match.group("value"))
        conflicts = abs(value - float(duration_seconds)) > 0.01
        return "" if total_context and conflicts and not shot_context else match.group(0)

    cleaned = duration_re.sub(replace, source_text)
    cleaned = re.sub(r"([，,。；;])\s*([，,。；;])+", r"\1", cleaned)
    return cleaned.strip(" ，,。；;")


def _reverse_response_content(data: dict) -> str:
    """Read text from OpenAI Chat Completions or Anthropic Messages."""
    try:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            content = choices[0]["message"]["content"]
        else:
            content = data["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GatewayError(
            "反推网关返回结构不符合预期:缺少 choices.message.content 或 content"
        ) from exc
    if isinstance(content, list):
        content = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {None, "text"}
        )
    if not isinstance(content, str):
        raise GatewayError("反推网关返回的文本内容必须是字符串或文本块列表")
    return content


def _reverse_usage(data: dict) -> dict | None:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    normalized = dict(usage)
    if "input_tokens" in normalized or "output_tokens" in normalized:
        try:
            prompt_tokens = max(0, int(normalized.get("input_tokens") or 0))
        except (TypeError, ValueError):
            prompt_tokens = 0
        try:
            completion_tokens = max(0, int(normalized.get("output_tokens") or 0))
        except (TypeError, ValueError):
            completion_tokens = 0
        normalized.setdefault("prompt_tokens", prompt_tokens)
        normalized.setdefault("completion_tokens", completion_tokens)
        normalized.setdefault("total_tokens", prompt_tokens + completion_tokens)
    return normalized


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


def _anthropic_reverse_content(content: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for item in content:
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": str(item.get("text") or "")})
            continue
        if item.get("type") == "image_url":
            image_url = item.get("image_url")
            ref = image_url.get("url") if isinstance(image_url, dict) else image_url
            blocks.append({"type": "image", "source": _anthropic_image_source(str(ref or ""))})
            continue
        raise GatewayError(f"Anthropic 视觉反推不支持内容块类型: {item.get('type')}")
    return blocks


def _merge_gateway_usage(*rows: dict | None) -> dict | None:
    merged: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                merged[key] = merged.get(key, 0) + value
            elif key not in merged:
                merged[key] = value
    return merged or None


_VIDEO_TEMPORAL_EVIDENCE_FIELDS = frozenset({
    "帧间推断",
    "主体动作",
    "可迁移主体动作",
    "迁移生成指令",
    "镜头运动",
    "运动节奏",
    "剪辑节奏",
    "时序分镜",
    "转场",
})

_REVERSE_REASONING_MODEL_RE = re.compile(
    r"(?:^|/)(?:gpt-5(?:[.\-]|$)|o(?:1|3|4)(?:[.\-]|$))",
    re.IGNORECASE,
)


def _reverse_completion_controls(model_id: str) -> dict:
    controls = {
        "max_tokens": max(512, int(settings.reverse_gateway_max_tokens or 4096)),
    }
    effort = str(settings.reverse_gateway_reasoning_effort or "").strip().lower()
    if effort in {"low", "medium", "high"} and _REVERSE_REASONING_MODEL_RE.search(
        str(model_id or "")
    ):
        controls["reasoning_effort"] = effort
    return controls


def _drop_unverified_video_timeline(result: dict) -> None:
    """Remove structured timeline claims when no normalized shot has evidence."""
    shots = result.get("shots")
    has_cross_frame_evidence = any(
        len(set(shot.get("evidence_frame_indices") or [])) >= 2
        and any(
            str(shot.get(field) or "").strip()
            for field in ("action", "camera", "transition")
        )
        for shot in shots or []
        if isinstance(shot, dict)
    )
    if has_cross_frame_evidence:
        return
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return
    removed = {
        key: structured.pop(key)
        for key in _VIDEO_TEMPORAL_EVIDENCE_FIELDS
        if str(structured.get(key) or "").strip()
    }
    if removed:
        result["unverified_temporal_fields"] = removed


def _repair_reverse_result_once(
    content: str,
    error: ReverseResultValidationError,
    *,
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
    source_count: int | None = None,
    video_segment_count: int | None = None,
    required_video_frame_indices: Collection[int] | None = None,
    video_frame_timestamps: dict[int, float] | None = None,
) -> tuple[dict, dict | None]:
    """Make exactly one text-only request to repair an invalid model result."""
    anthropic = bool(
        gateway_config is not None and gateway_config.gateway_format == "anthropic"
    )
    repair_text = _reverse_repair_template(content, str(error), target)
    payload = {
        "model": vision_model_id,
        "messages": [{
            "role": "user",
            "content": (
                [{"type": "text", "text": repair_text}]
                if anthropic
                else repair_text
            ),
        }],
        "temperature": 0,
        **_reverse_completion_controls(vision_model_id),
    }
    repair_data = _post(
        "/messages" if anthropic else "/chat/completions",
        payload,
        timeout=int(settings.reverse_gateway_timeout_seconds or 240),
        config=gateway_config,
        retries=0,
    )
    repaired_content = _reverse_response_content(repair_data)
    try:
        result = _validate_reverse_result(
            repaired_content,
            target,
            source_count=source_count,
            video_segment_count=video_segment_count,
            required_video_frame_indices=required_video_frame_indices,
            video_frame_timestamps=video_frame_timestamps,
        )
    except ReverseResultValidationError as repair_error:
        raise GatewayError(
            "反推结果不符合结构契约,且一次自动修复后仍无效: "
            f"{str(repair_error)[:300]}",
            error_code="INVALID_REVERSE_RESULT",
            phase="repairing",
        ) from repair_error
    return result, _reverse_usage(repair_data)


def _missing_required_video_frame_indices(
    content: str,
    required_frame_indices: Collection[int] | None,
) -> list[int]:
    if not required_frame_indices:
        return []
    try:
        payload = _decode_reverse_json_object(content)
    except ReverseResultValidationError:
        return []
    covered = {
        int(index)
        for shot in payload.get("shots") or []
        if isinstance(shot, dict)
        for index in shot.get("evidence_frame_indices") or []
        if isinstance(index, int) and not isinstance(index, bool) and index > 0
    }
    return sorted({int(index) for index in required_frame_indices} - covered)


def _repair_missing_video_frames_once(
    content: str,
    *,
    missing_frame_indices: list[int],
    frame_refs: dict[int, str],
    frame_timestamps: dict[int, float],
    frame_segments: dict[int, int],
    target: str,
    vision_model_id: str,
    gateway_config: RuntimeGatewayConfig | None,
    source_count: int | None,
    video_segment_count: int | None,
    required_video_frame_indices: Collection[int],
) -> tuple[dict, dict | None]:
    """Analyze only uncovered frames and merge them into the original provider JSON."""
    missing_refs = {
        index: frame_refs[index]
        for index in missing_frame_indices
        if index in frame_refs
    }
    if set(missing_refs) != set(missing_frame_indices):
        raise GatewayError(
            "缺失采样帧没有可用图像引用",
            error_code="INVALID_REVERSE_RESULT",
            phase="repairing",
        )
    frame_contract = ", ".join(
        f"{index}@{frame_timestamps[index]:.3f}s"
        if index in frame_timestamps
        else str(index)
        for index in missing_frame_indices
    )
    blocks: list[dict] = [{
        "type": "text",
        "text": (
            "你只分析下方尚未覆盖的视频采样帧，不重写已有结果。"
            f"必须为这些帧各返回一项: {frame_contract}。"
            "只写当帧直接可见的主体、场景、构图、材质和光线；"
            "不推断动作、运镜、转场、声音或帧外事件，不输出未知、可能、高级感等分析或提升词。"
            "输出唯一 JSON 对象，不要 markdown 或解释，结构为:"
            '{"frames":[{"frame_index":8,"visual":"直接可见画面",'
            '"lighting":"可见光线或空字符串","confidence":0.0}]}'
        ),
    }]
    for index in missing_frame_indices:
        timestamp = frame_timestamps.get(index)
        label = f"采样帧 {index}"
        if timestamp is not None:
            label += f"，时间戳 {timestamp:.3f} 秒"
        blocks.append({"type": "text", "text": label})
        blocks.append({"type": "image_url", "image_url": {"url": missing_refs[index]}})
    anthropic = bool(
        gateway_config is not None and gateway_config.gateway_format == "anthropic"
    )
    repair_data = _post(
        "/messages" if anthropic else "/chat/completions",
        {
            "model": vision_model_id,
            "messages": [{
                "role": "user",
                "content": _anthropic_reverse_content(blocks) if anthropic else blocks,
            }],
            "temperature": 0,
            **_reverse_completion_controls(vision_model_id),
        },
        timeout=int(settings.reverse_gateway_timeout_seconds or 240),
        config=gateway_config,
        retries=0,
    )
    repaired_content = _reverse_response_content(repair_data)
    try:
        descriptions = _validate_missing_video_frame_result(
            repaired_content,
            missing_frame_indices,
        )
        merged_payload = _decode_reverse_json_object(content)
        merged_shots = list(merged_payload.get("shots") or [])
        for description in descriptions:
            frame_index = int(description["frame_index"])
            timestamp = float(frame_timestamps.get(frame_index) or 0.0)
            merged_shots.append({
                "source_segment_index": int(frame_segments.get(frame_index) or 1),
                "start_seconds": round(max(0.0, timestamp - 0.75), 3),
                "end_seconds": round(timestamp + 0.75, 3),
                "visual": description["visual"],
                "action": "",
                "camera": "",
                "lighting": description["lighting"],
                "transition": "",
                "ocr": "",
                "audio_cue": "",
                "evidence_frame_indices": [frame_index],
                "confidence": description["confidence"],
            })
        merged_payload["shots"] = sorted(
            merged_shots,
            key=lambda shot: (
                int(shot.get("source_segment_index") or 1),
                float(shot.get("start_seconds") or 0),
            ) if isinstance(shot, dict) else (0, 0.0),
        )
        result = _validate_reverse_result(
            merged_payload,
            target,
            source_count=source_count,
            video_segment_count=video_segment_count,
            required_video_frame_indices=required_video_frame_indices,
            video_frame_timestamps=frame_timestamps,
        )
    except (ReverseResultValidationError, TypeError, ValueError) as repair_error:
        raise GatewayError(
            "缺失视频帧增量补全后仍不符合契约: "
            f"{str(repair_error)[:300]}",
            error_code="INVALID_REVERSE_RESULT",
            phase="repairing",
        ) from repair_error
    return result, _reverse_usage(repair_data)


def _reverse_prompt_impl(
    image_refs,
    vision_model_id: str,
    target: str = "image",
    gateway_config: RuntimeGatewayConfig | None = None,
    video_analysis: dict | None = None,
    before_repair: Callable[[], bool | None] | None = None,
    audit_context: dict | None = None,
    template_override: str | None = None,
    reference_context: list[dict] | None = None,
    require_video_frame_coverage: bool = False,
) -> dict:
    """Reverse one or more images into a structured prompt.

    ``image_refs`` is a single URL/data-URI or a list of them (e.g. several
    keyframes sampled from a reference video, in temporal order). ``target``
    selects the image vs video template. Returns the parsed prompt plus the
    provider ``usage`` (real token consumption) and call latency."""
    if target not in {"image", "video", "product_profile", "portrait_profile", "image_to_video"}:
        raise GatewayError(f"不支持的反推目标: {target}")
    _ensure_gateway_configured(gateway_config, "反推")
    t0 = time.time()
    repair_attempted = False
    repair_usage = None
    data: dict = {}
    candidate_refs = (
        [image_refs]
        if isinstance(image_refs, str)
        else [ref for ref in image_refs if ref]
    )
    source_count = max(1, len(candidate_refs))
    video_segment_count = None
    if target == "video" and isinstance(video_analysis, dict):
        source = video_analysis.get("source")
        source = source if isinstance(source, dict) else {}
        ranges = source.get("source_ranges")
        video_segment_count = (
            len([row for row in ranges if isinstance(row, dict)])
            if isinstance(ranges, list) and ranges
            else 1
        )
    frame_rows = (video_analysis or {}).get("sampled_frames") or []
    required_video_frame_indices = None
    video_frame_timestamps: dict[int, float] = {}
    video_frame_segments: dict[int, int] = {}
    if (
        target == "video"
        and require_video_frame_coverage
        and not _gateway_mock(gateway_config)
        and isinstance(frame_rows, list)
        and frame_rows
    ):
        required_video_frame_indices = [
            int(row.get("index"))
            if isinstance(row, dict)
            and isinstance(row.get("index"), int)
            and not isinstance(row.get("index"), bool)
            and int(row["index"]) > 0
            else index
            for index, row in enumerate(frame_rows, start=1)
        ]
        for index, row in zip(required_video_frame_indices, frame_rows, strict=True):
            if not isinstance(row, dict):
                continue
            timestamp_value = (
                row.get("absolute_timestamp_seconds")
                if video_segment_count and video_segment_count > 1
                and row.get("absolute_timestamp_seconds") is not None
                else row.get("relative_timestamp_seconds")
                if row.get("relative_timestamp_seconds") is not None
                else row.get("timestamp_seconds")
            )
            try:
                timestamp = float(timestamp_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(timestamp):
                video_frame_timestamps[index] = timestamp
            try:
                video_frame_segments[index] = max(
                    1, int(row.get("source_segment_index") or 1)
                )
            except (TypeError, ValueError):
                video_frame_segments[index] = 1
    if _gateway_mock(gateway_config):
        # Mock mode replaces only the provider response. It must still pass
        # through the same audio, temporal-evidence, and analysis-gap gates as
        # a real model response so offline E2E cannot mask contract failures.
        mock_result = _mock_reverse(target)
        mock_payload = dict(mock_result.get("structured") or {})
        mock_payload["final_text"] = mock_result.get("final_text")
        if "shots" in mock_result:
            mock_payload["shots"] = mock_result.get("shots")
        result = _validate_reverse_result(
            mock_payload,
            target,
            source_count=source_count,
            video_segment_count=video_segment_count,
            required_video_frame_indices=required_video_frame_indices,
            video_frame_timestamps=video_frame_timestamps,
        )
    else:
        refs = candidate_refs
        if not refs:
            raise GatewayError("反推缺少可用的参考图")
        content = []
        if target == "video" and video_analysis:
            content.append({"type": "text", "text": _video_analysis_context_text(video_analysis)})
        for index, ref in enumerate(refs, start=1):
            context_row = (
                reference_context[index - 1]
                if reference_context and index <= len(reference_context)
                else {}
            )
            if context_row:
                role = str(context_row.get("role") or "reference")
                timestamp = context_row.get("timestamp_seconds")
                relative = context_row.get("relative_timestamp_seconds")
                if role == "frame":
                    source_segment_index = context_row.get("source_segment_index")
                    label = f"第 {index} 帧，源视频时间戳 {float(timestamp):.3f} 秒" if timestamp is not None else f"第 {index} 帧"
                    if source_segment_index is not None:
                        label += f"，来自已选片段 {int(source_segment_index)}"
                    if relative is not None:
                        label += f"，分析片段内 {float(relative):.3f} 秒"
                else:
                    label = f"参考 {index}，角色 {role}"
                    if context_row.get("label"):
                        label += f"，说明 {str(context_row['label'])[:64]}"
                content.append({"type": "text", "text": label})
            elif target == "video" and video_analysis:
                row = frame_rows[index - 1] if index <= len(frame_rows) else {}
                timestamp = row.get("timestamp_seconds")
                label = f"第 {index} 帧，时间戳 {float(timestamp):.3f} 秒" if timestamp is not None else f"第 {index} 帧，时间戳未知"
                content.append({"type": "text", "text": label})
            content.append({"type": "image_url", "image_url": {"url": ref}})
        template = (
            template_override
            if isinstance(template_override, str) and template_override.strip()
            else _reverse_template(target, n_frames=len(refs))
        )
        if target == "video" and required_video_frame_indices:
            required_text = ", ".join(
                (
                    f"{index}@{video_frame_timestamps[index]:.3f}s"
                    if index in video_frame_timestamps
                    else str(index)
                )
                for index in required_video_frame_indices
            )
            template = (
                "本次用于生成的视频反推必须完整覆盖采样帧编号 "
                f"{required_text}；每个编号至少出现在一个 shot 的 "
                "evidence_frame_indices 中，不得遗漏或用空镜头占位。"
                "仅由一个采样帧支撑的静态 shot，时间范围必须围绕该帧时间戳且跨度不超过 1.5 秒。\n"
                + template
            )
        content.append({"type": "text", "text": template})
        payload = {
            "model": vision_model_id,
            "messages": [{
                "role": "user",
                "content": (
                    _anthropic_reverse_content(content)
                    if gateway_config is not None
                    and gateway_config.gateway_format == "anthropic"
                    else content
                ),
            }],
            "temperature": 0.2,  # low temp -> more faithful, repeatable description
            **_reverse_completion_controls(vision_model_id),
        }
        data = _post(
            (
                "/messages"
                if gateway_config is not None
                and gateway_config.gateway_format == "anthropic"
                else "/chat/completions"
            ),
            payload,
            timeout=int(settings.reverse_gateway_timeout_seconds or 240),
            config=gateway_config,
            # A client timeout does not prove the paid upstream request failed.
            # Replaying here can double both latency and provider cost.
            retries=max(0, int(settings.reverse_gateway_max_retries or 0)),
        )
        content_text = _reverse_response_content(data)
        try:
            result = _validate_reverse_result(
                content_text,
                target,
                source_count=source_count,
                video_segment_count=video_segment_count,
                required_video_frame_indices=required_video_frame_indices,
                video_frame_timestamps=video_frame_timestamps,
            )
        except ReverseResultValidationError as validation_error:
            repair_attempted = True
            _log_reverse_audit_event(
                status="repairing",
                phase="repairing",
                target=target,
                audit_context=audit_context,
            )
            if before_repair is not None:
                try:
                    should_continue = before_repair()
                except Exception as callback_error:
                    if isinstance(callback_error, GatewayError) and callback_error.phase is None:
                        callback_error.phase = "repairing"
                    else:
                        callback_error.reverse_phase = "repairing"
                    raise
                if should_continue is False:
                    raise GatewayError(
                        "反推任务已在文本修复前取消",
                        error_code="CANCELED",
                        phase="repairing",
                    )
            try:
                missing_frame_indices = _missing_required_video_frame_indices(
                    content_text,
                    required_video_frame_indices,
                )
                if target == "video" and missing_frame_indices:
                    result, repair_usage = _repair_missing_video_frames_once(
                        content_text,
                        missing_frame_indices=missing_frame_indices,
                        frame_refs={
                            index: refs[index - 1]
                            for index in missing_frame_indices
                            if 1 <= index <= len(refs)
                        },
                        frame_timestamps=video_frame_timestamps,
                        frame_segments=video_frame_segments,
                        target=target,
                        vision_model_id=vision_model_id,
                        gateway_config=gateway_config,
                        source_count=source_count,
                        video_segment_count=video_segment_count,
                        required_video_frame_indices=required_video_frame_indices or (),
                    )
                else:
                    result, repair_usage = _repair_reverse_result_once(
                        content_text,
                        validation_error,
                        target=target,
                        vision_model_id=vision_model_id,
                        gateway_config=gateway_config,
                        source_count=source_count,
                        video_segment_count=video_segment_count,
                        required_video_frame_indices=required_video_frame_indices,
                        video_frame_timestamps=video_frame_timestamps,
                    )
            except GatewayError as repair_error:
                repair_error.phase = repair_error.phase or "repairing"
                repair_error.error_code = repair_error.error_code or "REPAIR_FAILED"
                raise
    frame_rows = (video_analysis or {}).get("sampled_frames") or []
    video_target = target in {"video", "image_to_video"}
    audio_evidence = (
        (video_analysis or {}).get("audio")
        if isinstance((video_analysis or {}).get("audio"), dict)
        else None
    )
    audio_analyzed = bool(
        target == "video" and ((video_analysis or {}).get("source") or {}).get("audio_analyzed")
    )
    if video_target:
        # Provider prose is audit input only. It may help recover explicit
        # post-production metadata, but never contributes visual prompt text.
        provider_text = result.get("provider_final_text")
        post = split_video_post_production(
            provider_text if isinstance(provider_text, str) else ""
        )
        for key, value in (("字幕卖点", "；".join(post["post_overlays"])),):
            if value and str(result["structured"].get(key) or "").strip() in {"", "无", "未分析"}:
                result["structured"][key] = value
        if target != "video":
            result["structured"]["旁白"] = "未分析"
            result["structured"]["音效"] = "未分析"
            for shot in result.get("shots") or []:
                shot["audio_cue"] = "未分析"
    if target == "video":
        # This value is derived below from trusted media metadata. Ignore a
        # same-named provider extra so it cannot claim an unobserved format.
        result["structured"].pop("源视频规格", None)
        source = (video_analysis or {}).get("source") or {}
        source_ranges = source.get("source_ranges")
        source_range = source.get("source_range")
        if not isinstance(source_range, dict) and isinstance(source_ranges, list) and len(source_ranges) == 1:
            source_range = source_ranges[0]
        try:
            audio_time_offset = (
                float(source_range.get("start_seconds") or 0)
                if isinstance(source_range, dict)
                else 0.0
            )
        except (TypeError, ValueError):
            audio_time_offset = 0.0
        if video_analysis:
            result["shots"] = _normalize_video_shots(
                result.get("shots"),
                duration_seconds=source.get("duration_seconds"),
                frame_count=len(frame_rows),
                sampled_frames=frame_rows,
                audio_analyzed=bool(source.get("audio_analyzed")),
                audio_evidence=audio_evidence,
                audio_time_offset_seconds=audio_time_offset,
                source_ranges=source_ranges,
            )
            if required_video_frame_indices is not None:
                normalized_coverage = {
                    int(index)
                    for shot in result["shots"]
                    for index in shot.get("evidence_frame_indices") or []
                }
                missing_after_normalization = sorted(
                    set(required_video_frame_indices) - normalized_coverage
                )
                if missing_after_normalization:
                    joined = ", ".join(str(index) for index in missing_after_normalization)
                    raise GatewayError(
                        f"视频反推在证据归一化后丢失采样帧: {joined}",
                        error_code="INVALID_REVERSE_RESULT",
                        phase="validating",
                    )
            result["analysis_gaps"] = _video_analysis_gaps(
                result["shots"],
                duration_seconds=source.get("duration_seconds"),
                analysis_mode=video_analysis.get("analysis_mode"),
                source_ranges=source.get("source_ranges"),
            )
        else:
            result["shots"] = []
            result["analysis_gaps"] = [{"message": "缺少视频帧分析证据"}]
        _sanitize_video_audio_evidence(
            result["structured"],
            result["shots"],
            audio_evidence=audio_evidence,
            audio_analyzed=audio_analyzed,
            shot_time_offset_seconds=audio_time_offset,
        )
        _drop_unverified_video_timeline(result)
        timeline = _video_shots_timeline(result["shots"])
        if timeline:
            result["structured"]["时序分镜"] = timeline
        source_spec = _video_source_spec(video_analysis or {})
        if source_spec:
            result["structured"]["源视频规格"] = source_spec
            result["structured"]["时长建议"] = _video_generation_duration_suggestion(
                source.get("duration_seconds")
            )
    try:
        result["final_text"] = _compose_visual_final_text(
            result["structured"],
            target,
            result.get("shots"),
        )
    except ReverseResultValidationError as error:
        raise GatewayError(
            str(error),
            error_code="INVALID_REVERSE_RESULT",
            phase="validating",
        ) from error
    result["usage"] = _merge_gateway_usage(_reverse_usage(data), repair_usage)
    result["latency_ms"] = int((time.time() - t0) * 1000)
    result["repair_attempted"] = repair_attempted
    result["repair_succeeded"] = repair_attempted
    return result


def _attach_image_to_video_analysis(result: dict, video_analysis: dict | None) -> dict:
    """Attach explicit single-image evidence without inventing a video timeline."""
    provided = dict(video_analysis) if isinstance(video_analysis, dict) else {}
    analysis_mode = str(provided.get("analysis_mode") or "image_motion")
    source = dict(provided.get("source")) if isinstance(provided.get("source"), dict) else {}
    source["audio_analyzed"] = False
    source.setdefault("source_type", "image" if analysis_mode == "image_motion" else "video_cover")
    sampled_frames = provided.get("sampled_frames")
    if not isinstance(sampled_frames, list) or not sampled_frames:
        sampled_frames = [{"index": 1, "timestamp_seconds": 0}]
    gaps = provided.get("analysis_gaps")
    if not isinstance(gaps, list) or not gaps:
        message = (
            "单图输入没有可观察的视频时间线"
            if analysis_mode == "image_motion"
            else "封面单帧无法覆盖源视频时间线"
        )
        gaps = [{"message": message}]
    analysis = {
        **provided,
        "analysis_mode": analysis_mode,
        "source": source,
        "sampled_frames": sampled_frames,
        "analysis_gaps": gaps,
    }
    normalized = dict(result)
    normalized["analysis_gaps"] = gaps
    normalized["video_analysis"] = analysis
    return normalized


def reverse_prompt(
    image_refs,
    vision_model_id: str,
    target: str = "image",
    gateway_config: RuntimeGatewayConfig | None = None,
    video_analysis: dict | None = None,
    before_repair: Callable[[], bool | None] | None = None,
    audit_context: dict | None = None,
    template_override: str | None = None,
    reference_context: list[dict] | None = None,
    require_video_frame_coverage: bool = False,
) -> dict:
    """Run reverse analysis and emit one bounded audit event for its outcome."""
    started = time.time()
    _log_reverse_audit_event(
        status="started",
        phase="calling_model",
        target=target,
        audit_context=audit_context,
    )
    try:
        result = _reverse_prompt_impl(
            image_refs,
            vision_model_id,
            target=target,
            gateway_config=gateway_config,
            video_analysis=video_analysis,
            before_repair=before_repair,
            audit_context=audit_context,
            template_override=template_override,
            reference_context=reference_context,
            require_video_frame_coverage=require_video_frame_coverage,
        )
        if target == "image_to_video":
            result = _attach_image_to_video_analysis(result, video_analysis)
    except Exception as error:
        phase = str(
            getattr(error, "phase", None)
            or getattr(error, "reverse_phase", None)
            or "calling_model"
        )
        error_code = str(
            getattr(error, "error_code", None)
            or ("GATEWAY_ERROR" if isinstance(error, GatewayError) else "INTERNAL_ERROR")
        )
        _log_reverse_audit_event(
            status="failed",
            phase=phase,
            target=target,
            audit_context=audit_context,
            latency_ms=int((time.time() - started) * 1000),
            error_code=error_code,
        )
        raise
    _log_reverse_audit_event(
        status="ok",
        phase="completed",
        target=target,
        audit_context=audit_context,
        latency_ms=int(result.get("latency_ms") or (time.time() - started) * 1000),
    )
    return result


_PHYSICAL_PRODUCT_SIGNAL_RE = re.compile(
    r"SKU|包装|外包装|瓶身|盒身|罐体|袋装|洗脸巾|纸巾|面膜|护肤品|化妆品|"
    r"\bpackaging\b|\bsku\b|\bbottle\b|\bbox\b|\bjar\b",
    re.IGNORECASE,
)
_GENERIC_PRODUCT_SIGNAL_RE = re.compile(r"产品|商品|\bproduct\b|\bgoods\b", re.IGNORECASE)
_PRODUCT_VISUAL_SIGNAL_RE = re.compile(
    r"展示|陈列|入镜|特写|台面|手持|外观|材质|纹理|标签|Logo|品牌|"
    r"瓶|盒|罐|袋|包装|\bshowcase\b|\bhero shot\b|\bclose-up\b|\bpackaging\b",
    re.IGNORECASE,
)
_NON_PHYSICAL_PRODUCT_CONTEXT_RE = re.compile(
    r"产品经理|软件产品|数字产品|互联网产品|虚拟产品|SaaS\s*产品|App\s*产品|应用产品|"
    r"\bproduct manager\b|\bsoftware product\b|\bdigital product\b|"
    r"\bsaas product\b|\bapp product\b",
    re.IGNORECASE,
)
_PRODUCT_VIDEO_TEMPLATE_LABELS = {
    "prompt_driven": "提示词驱动",
    "reference_sequence": "参考分镜",
    "stable_showcase": "稳定陈列",
    "slow_push": "慢速推近",
    "handheld_display": "手持展示",
    "background_motion": "背景动效",
    "soft_splash": "轻水花",
}


def _effective_product_prompt_mode(
    source: str,
    *,
    category: str,
    product_mode: bool,
    subject_mode: str | None,
) -> tuple[bool, str]:
    if product_mode:
        return True, "explicit"
    if category != "video":
        return False, "none"
    if subject_mode == "product":
        return True, "subject_mode"
    physical_source = _NON_PHYSICAL_PRODUCT_CONTEXT_RE.sub("", source)
    if _PHYSICAL_PRODUCT_SIGNAL_RE.search(physical_source):
        return True, "inferred_from_prompt"
    if _GENERIC_PRODUCT_SIGNAL_RE.search(physical_source) and _PRODUCT_VISUAL_SIGNAL_RE.search(
        physical_source
    ):
        return True, "inferred_from_prompt"
    return False, "none"


def _required_video_prompt_constraints(
    *,
    duration: int | None,
    max_shots: int,
    prompt_budget_chars: int,
    target_model_id: str | None,
    target_model_provider: str | None,
    aspect_ratio: str | None,
    resolution: str | None,
    effective_product_mode: bool,
    product_lock_mode: str | None,
    product_video_template: str | None,
) -> list[str]:
    constraints: list[str] = []
    if duration is not None:
        constraints.append(f"目标时长 {max(1, int(duration))} 秒")
    if aspect_ratio:
        constraints.append(f"画幅 {aspect_ratio}")
    if resolution:
        constraints.append(f"分辨率 {resolution}")
    if target_model_id or target_model_provider:
        target = " / ".join(
            item
            for item in (
                str(target_model_id or "").strip(),
                str(target_model_provider or "").strip(),
            )
            if item
        )
        constraints.append(f"适配目标模型 {target}")
    constraints.extend(
        [
            f"单段常规舒适密度约 {max(1, int(max_shots))} 个主要动作；"
            "超出时在同一视频内按原顺序连续串联，不得删减或替换",
            f"单段提示词预算约 {max(1, int(prompt_budget_chars))} 字符",
            "保持主体、场景、动作和运镜连续，禁止将原始动作合并成泛化描述",
        ]
    )
    if effective_product_mode:
        constraints.append("同一 SKU 的包装外形与比例、Logo、品牌色、可见文字、材质和纹理保持一致")
        constraints.append("不得新增用户未提供的商品、配件或突兀道具")
        if product_lock_mode == "locked":
            if str(product_video_template or "").strip().lower() == "prompt_driven":
                constraints.append("文字保真，避免遮挡、裁切和运动模糊")
            else:
                constraints.append("文字保真，避免遮挡、裁切、快速旋转和运动模糊")
        template = str(product_video_template or "").strip().lower()
        if template:
            label = _PRODUCT_VIDEO_TEMPLATE_LABELS.get(template, template)
            constraints.append(f"产品视频策略 {template}（{label}）")
        constraints.append(
            "不得生成无关文字、错误品牌或水印；准确保留产品包装原有文字，"
            "并按用户要求显示指定卖点文字"
        )
    else:
        constraints.append("画面无字，精确字幕、旁白和音效仅后期添加")
    return constraints


def _normalize_video_optimizer_output(
    content: str,
    *,
    source: str,
    required_constraints: list[str],
    recommended_max_shots: int,
    prompt_budget_chars: int,
    preserve_requested_post_production: bool = False,
) -> dict:
    sections = parse_structured_video_sections(content)
    if sections is None:
        fallback = parse_video_prompt(str(content or ""))
        style = fallback.get("global_style") or "沿用原稿明确的视觉风格、色调、光线和氛围"
        scenes = fallback.get("shots") or [source]
        constraints = ""
    else:
        style = sections["style"]
        scenes = sections["shots"]
        constraints = sections["constraints"]
    style_text = clean_video_prompt_section(style) or "沿用原稿明确的视觉风格、色调、光线和氛围"
    shots = [
        clean_video_prompt_section(shot) for shot in scenes if clean_video_prompt_section(shot)
    ]
    if not shots:
        shots = [clean_video_prompt_section(source)]
    source_plan_shots = [
        clean_video_prompt_section(shot)
        for shot in parse_video_prompt(source).get("shots", [])
        if clean_video_prompt_section(shot)
    ]
    source_shots = video_action_requirements(source_plan_shots)
    if len(source_shots) > len(shots):
        shots = source_shots
    source_shot_count = len(shots)
    condensed_for_single_clip = False
    optimized_scene_key = re.sub(r"[\s,，。；;：:、.!！？?]+", "", "".join(shots)).lower()
    missing_source_shots = [
        source_shot
        for source_shot in source_shots
        if re.sub(r"[\s,，。；;：:、.!！？?]+", "", source_shot).lower()
        not in optimized_scene_key
    ]
    original_action_constraint = ""
    if missing_source_shots and not condensed_for_single_clip:
        inventory = "；".join(
            f"{index}. {shot}" for index, shot in enumerate(missing_source_shots, start=1)
        )
        original_action_constraint = f"必须完整执行且不得替换的原始动作要求（按顺序）：{inventory}"
    requested_post_constraints: list[str] = []
    if preserve_requested_post_production:
        requested_post = split_video_post_production(source)
        if requested_post["post_overlays"]:
            requested_post_constraints.append(
                "用户指定卖点文字："
                + "；".join(requested_post["post_overlays"])
                + "，按原文准确显示"
            )
        if requested_post["voiceover"]:
            requested_post_constraints.append(
                f"用户指定旁白：{requested_post['voiceover']}"
            )
        if requested_post["sfx"]:
            requested_post_constraints.append(
                "用户指定音效：" + "；".join(requested_post["sfx"])
            )
    constraint_text = merge_video_constraint_clauses(
        constraints,
        *required_constraints,
        *requested_post_constraints,
        original_action_constraint,
    )
    normalized = render_structured_video_prompt(
        style=style_text,
        shots=shots,
        constraints=constraint_text,
    )
    compacted_for_budget = False
    if len(normalized) > prompt_budget_chars:
        compacted = compact_single_clip_prompt(
            style=style_text,
            shots=shots,
            technical=constraint_text,
            budget=prompt_budget_chars,
            mandatory_technical=constraint_text,
        )
        normalized = str(compacted["prompt"])
        style_text = str(compacted["style"])
        shots = list(compacted["shots"])
        constraint_text = str(compacted["technical"])
        compacted_for_budget = True
    prompt_char_count = len(normalized)
    prompt_over_budget = prompt_char_count > prompt_budget_chars
    sequence_required = False
    recommended_clip_count = 1
    return {
        "prompt": normalized,
        "metadata": {
            "shot_count": len(shots),
            "source_shot_count": source_shot_count,
            "selected_shot_count": len(shots),
            "omitted_shot_count": max(0, source_shot_count - len(shots)),
            "condensed_for_single_clip": condensed_for_single_clip,
            "compacted_for_budget": compacted_for_budget,
            "recommended_max_shots": recommended_max_shots,
            "prompt_char_count": prompt_char_count,
            "prompt_budget_chars": prompt_budget_chars,
            "prompt_over_budget": prompt_over_budget,
            "recommended_clip_count": recommended_clip_count,
            "sequence_required": sequence_required,
        },
    }


def _assert_complete_video_optimizer_output(prompt: str, *, max_chars: int = 4000) -> None:
    if len(prompt) > max_chars:
        raise GatewayError(
            "视频提示词优化的结构化结果过长，无法完整保留风格设定、场景脚本和技术约束，"
            "请精简原始提示词后重试"
        )


_PROMPT_OPTIMIZATION_DIRECTION_LABELS = {
    "faithful": "忠实整理",
    "concise": "精简压缩",
    "expand": "细节扩写",
    "commercial": "商业增强",
    "cinematic": "电影化",
    "model_adaptation": "模型适配",
    "constraints": "约束强化",
    "translate": "翻译转换",
}

_PROMPT_OPTIMIZATION_DIRECTION_INSTRUCTIONS = {
    "faithful": "只整理表达、消除歧义和重复，不添加用户没有要求的新主体、动作、道具或卖点。",
    "concise": "压缩冗余和空话，保留全部主体、动作、品牌、文字、时序和禁改项，用更少文字表达。",
    "expand": "在不改变事实和需求的前提下，补充能被生成模型执行的构图、光线、材质、动作和镜头细节。",
    "commercial": "强化商品卖点、主体层级、商业构图和展示节奏，但不得编造功效、品牌信息或用户未提供的道具。",
    "cinematic": "增强景别、机位、镜头运动、光影、色彩和节奏，同时完整保留原始主体、动作和约束。",
    "model_adaptation": "只针对目标生成模型调整语序、密度和技术表达，不改变创作意图，不重新分析素材。",
    "constraints": "优先补全主体一致性、禁止变化、负向要求和失败边界，不擅自扩写创意内容。",
    "translate": "只做专业视觉提示词翻译，保留专有名词、数字、单位、品牌、文字内容、时序和约束，不增删需求。",
}

_PROMPT_CONSTRAINT_RE = re.compile(
    r"必须|务必|保留|保持|不得|禁止|不要|避免|不可|不能|准确|一致|锁定|不变|无字|仅|只"
)
_PROMPT_FORBIDDEN_RE = re.compile(r"不得|禁止|不要|避免|不可|不能|不允许|无字|不变")
_PROMPT_CRITICAL_LITERAL_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9._-]{1,}|\d+(?:\.\d+)?(?:K|P|秒|分钟|帧|张|个|倍|%|:|×|x)?|[“\"']([^“”\"']{2,40})[”\"']"
)


def _dedupe_prompt_items(values: list[str], *, limit: int = 12) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。；;\n\t")
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _prompt_constraint_clauses(source: str) -> list[str]:
    clauses = re.split(r"[。；;\n]+", str(source or ""))
    return _dedupe_prompt_items([item for item in clauses if _PROMPT_CONSTRAINT_RE.search(item)])


def _prompt_critical_literals(source: str) -> list[str]:
    values: list[str] = []
    for match in _PROMPT_CRITICAL_LITERAL_RE.finditer(str(source or "")):
        value = match.group(1) or match.group(0)
        if value.casefold() in {"shot", "logo", "sku"}:
            values.append(value)
        elif any(char.isdigit() for char in value) or match.group(1):
            values.append(value)
        elif value.isupper() or any(char.isdigit() for char in value):
            values.append(value)
    return _dedupe_prompt_items(values, limit=16)


def review_prompt_optimization(
    source: str,
    optimized: str,
    *,
    direction: str = "faithful",
    target_language: str | None = None,
    target_model_id: str | None = None,
    product_mode: bool = False,
) -> dict[str, list[str]]:
    """Build deterministic review metadata without trusting model-authored claims."""
    normalized_direction = direction if direction in _PROMPT_OPTIMIZATION_DIRECTION_LABELS else "faithful"
    label = _PROMPT_OPTIMIZATION_DIRECTION_LABELS[normalized_direction]
    source_text = str(source or "").strip()
    optimized_text = str(optimized or "").strip()
    constraints = _prompt_constraint_clauses(source_text)
    forbidden = [item for item in constraints if _PROMPT_FORBIDDEN_RE.search(item)]
    preserved = list(constraints)
    if not preserved:
        preserved = ["保留用户原始主体、数量、动作顺序和创作意图"]
    standard_forbidden = "不得改变主体数量、身份、品牌名、可见文字、明确动作和时序"
    if product_mode:
        standard_forbidden = "不得改变商品 SKU、包装结构、Logo、品牌色、可见文字、材质和比例"
    forbidden = _dedupe_prompt_items([standard_forbidden, *forbidden])
    missing_literals = [
        item for item in _prompt_critical_literals(source_text)
        if item.casefold() not in optimized_text.casefold()
    ]
    warnings = [f"优化稿可能遗漏关键字面要求：{item}" for item in missing_literals[:6]]
    if not optimized_text:
        warnings.append("优化模型未返回有效内容")
    change_summary = [f"按“{label}”方向处理"]
    delta = len(optimized_text) - len(source_text)
    if delta > 0:
        change_summary.append(f"文本增加 {delta} 个字符")
    elif delta < 0:
        change_summary.append(f"文本减少 {abs(delta)} 个字符")
    else:
        change_summary.append("文本长度保持不变")
    if normalized_direction == "model_adaptation" and target_model_id:
        change_summary.append(f"已针对目标模型 {target_model_id} 编译")
    if normalized_direction == "translate":
        change_summary.append(f"目标语言：{'英文' if target_language == 'en' else '中文'}")
    if constraints:
        change_summary.append(f"识别并保留 {len(constraints)} 条显式约束")
    return {
        "change_summary": _dedupe_prompt_items(change_summary),
        "preserved_requirements": _dedupe_prompt_items(preserved),
        "forbidden_changes": forbidden,
        "warnings": _dedupe_prompt_items(warnings),
    }


def optimize_prompt(
    prompt: str,
    model_id: str,
    *,
    category: str = "image",
    product_mode: bool = False,
    duration: int | None = None,
    subject_mode: str | None = None,
    reference_type: str | None = None,
    subject_profile: dict | str | None = None,
    target_model_id: str | None = None,
    target_model_provider: str | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    product_lock_mode: str | None = None,
    product_video_template: str | None = None,
    target_model_extra: dict | None = None,
    direction: str = "faithful",
    target_language: str = "en",
    gateway_config: RuntimeGatewayConfig | None = None,
) -> dict:
    """Rewrite a user-authored generation prompt without changing its intent."""
    _ensure_gateway_configured(gateway_config, "提示词优化")
    source = str(prompt or "").strip()
    effective_product_mode, product_mode_source = _effective_product_prompt_mode(
        source,
        category=category,
        product_mode=product_mode,
        subject_mode=subject_mode,
    )
    normalized_direction = direction if direction in _PROMPT_OPTIMIZATION_DIRECTION_LABELS else "faithful"
    normalized_target_language = target_language if target_language in {"zh-CN", "en"} else "en"
    context_metadata = {
        key: value
        for key, value in {
            "duration": duration,
            "subject_mode": subject_mode,
            "reference_type": reference_type,
            "target_model_id": target_model_id,
            "target_model_provider": target_model_provider,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "product_lock_mode": product_lock_mode,
            "product_video_template": product_video_template,
            "effective_product_mode": effective_product_mode if category == "video" else None,
            "product_mode_source": product_mode_source if category == "video" else None,
            "direction": normalized_direction,
            "target_language": normalized_target_language if normalized_direction == "translate" else None,
        }.items()
        if value not in (None, "")
    }
    compiler_metadata = (
        {
            "version": "prompt-optimizer-v3",
            "output_format": "structured_video_text",
            "sections": ["风格设定", "场景脚本", "技术约束"],
        }
        if category == "video"
        else {"version": "prompt-optimizer-v2", "output_format": "single_text"}
    )
    compiler_metadata.update({
        "direction": normalized_direction,
        "kind": "model_compile" if normalized_direction == "model_adaptation" else "rewrite",
    })
    required_video_constraints: list[str] = []
    max_shots = 1
    prompt_budget_chars = 1600
    if category == "video":
        seconds = max(1, int(duration)) if duration is not None else None
        configured_profiles = (target_model_extra or {}).get("video_prompt_profiles") or (
            target_model_extra or {}
        ).get("prompt_profiles")
        if not isinstance(configured_profiles, dict):
            configured_profiles = None
        profile = infer_video_model_profile(
            model_id=str(target_model_id or ""),
            provider=str(target_model_provider or ""),
            duration=seconds or 10,
            extra=target_model_extra,
            model_profiles=configured_profiles,
        )
        max_shots = max(1, int(profile["recommended_max_shots"]))
        prompt_budget_chars = max(1, int(profile["prompt_budget_chars"]))
        required_video_constraints = _required_video_prompt_constraints(
            duration=seconds,
            max_shots=max_shots,
            prompt_budget_chars=prompt_budget_chars,
            target_model_id=target_model_id,
            target_model_provider=target_model_provider,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            effective_product_mode=effective_product_mode,
            product_lock_mode=product_lock_mode,
            product_video_template=product_video_template,
        )
    if _gateway_mock(gateway_config):
        prefix = (
            "产品商业视频"
            if effective_product_mode and category == "video"
            else "产品商业图片"
            if effective_product_mode
            else "视频"
            if category == "video"
            else "图片"
        )
        if category == "video":
            normalized = _normalize_video_optimizer_output(
                source,
                source=source,
                required_constraints=required_video_constraints,
                recommended_max_shots=max_shots,
                prompt_budget_chars=prompt_budget_chars,
                preserve_requested_post_production=effective_product_mode,
            )
            optimized_prompt = normalized["prompt"]
            _assert_complete_video_optimizer_output(optimized_prompt)
            compiler_metadata.update(normalized["metadata"])
        else:
            optimized_prompt = f"{prefix}生成：{source}"
        review = review_prompt_optimization(
            source,
            optimized_prompt,
            direction=normalized_direction,
            target_language=normalized_target_language,
            target_model_id=target_model_id,
            product_mode=effective_product_mode,
        )
        return {
            "prompt": optimized_prompt,
            "usage": None,
            "latency_ms": 0,
            "optimizer_model_id": model_id,
            "compiler_metadata": compiler_metadata,
            "context_metadata": context_metadata,
            **review,
        }
    if effective_product_mode:
        prompt_driven_product_video = (
            category == "video"
            and str(product_video_template or "").strip().lower() == "prompt_driven"
        )
        if category == "image":
            focus = (
                "这是产品图片生成提示词。补强产品主体、SKU一致性、包装结构、材质、Logo与可见文字保真、"
                "完整入镜、商业构图、产品与场景的空间关系和光线。"
            )
        elif prompt_driven_product_video:
            focus = (
                "这是产品视频生成提示词。补强产品主体、SKU一致性、包装结构、材质、Logo与可见文字保真、"
                "镜头运动、展示节奏、产品完整入镜，并避免遮挡和文字模糊。"
            )
        else:
            focus = (
                "这是产品视频生成提示词。补强产品主体、SKU一致性、包装结构、材质、Logo与可见文字保真、"
                "镜头运动、展示节奏、产品完整入镜，并避免快速旋转、遮挡和文字模糊。"
            )
        focus += (
            "不得凭空新增参考图或用户输入中不存在的纸巾、花瓶、植物及其他道具；"
            "参考图已有道具仅可保留并与产品、台面和场景自然融合，不得增殖、放大、悬浮或突兀贴附。"
        )
    else:
        focus = (
            "这是图片生成提示词，补全主体、场景、构图、光线、色调、材质和画面质感。"
            if category == "image"
            else "这是视频生成提示词，补全主体、场景、镜头运动、动作、节奏、光线和画面质感。"
        )
    video_constraints = ""
    if category == "video":
        target_name = str(target_model_id or "未指定视频模型")
        provider_name = str(target_model_provider or "未指定提供商")
        if duration is not None:
            seconds = max(1, int(duration))
            density_rule = (
                f"目标时长 {seconds} 秒，单段建议最多 {max_shots} 个主要镜头，"
                "每个镜头只安排一个主要动作，保持时空连续；"
            )
        else:
            density_rule = "时长未指定时保守控制镜头和主要动作数量；"
        video_constraints = (
            f"目标生成模型为 {target_name}（{provider_name}）。{density_rule}"
            "原稿超载时优先保留核心连续动作，不承诺在单次生成中完成过多场景和动作。"
        )
        if effective_product_mode:
            video_constraints += (
                "保留用户明确要求的卖点文字、旁白和音效，并写入对应场景脚本或技术约束；"
                "不得删除、改写或一律转为后期。不得生成无关文字、错误品牌或水印。"
            )
        else:
            video_constraints += (
                "将精确字幕、卖点文字和旁白改写为后期叠字与后期配音要求，"
                "画面保持无字，不要求视频模型渲染精确字幕或旁白。"
            )
        if effective_product_mode and reference_type:
            video_constraints += (
                "产品参考图只锁定产品身份，包括 SKU、包装结构、Logo、颜色、材质和纹理；"
                "不得用产品参考图锁定人物身份、场景、构图或光线，"
                "除非用户在文字中明确要求。"
            )
        technical_context = "；".join(required_video_constraints)
        structured_contract = (
            "只返回一个 JSON 对象，不要 Markdown、代码围栏或解释。JSON 必须且只能包含三个键："
            "“风格设定”（字符串，只写整体风格、场景基调、色调、光线、质感和氛围，不写动作）；"
            "“场景脚本”（字符串数组，每项一个镜头，按时间顺序写主体+一个主要动作+景别/运镜，不带 Shot 编号）；"
            "“技术约束”（字符串，写时长、画幅、分辨率、连续性、模型适配和主体一致性）。"
            f"场景脚本按单段建议最多 {max_shots} 个主要镜头组织；"
            "不得删除用户明确要求的动作，超过单段容量时保留全部动作并在技术约束中明确要求拆分生成。"
            f"必须纳入这些技术事实：{technical_context}。"
        )
    else:
        structured_contract = ""
    direction_instruction = _PROMPT_OPTIMIZATION_DIRECTION_INSTRUCTIONS[normalized_direction]
    if normalized_direction == "translate":
        direction_instruction += (
            "目标语言为英文，除品牌和用户指定原文外全部使用英文。"
            if normalized_target_language == "en"
            else "目标语言为简体中文，保留品牌和用户指定原文。"
        )
    base_system = (
        "你是专业的中文生成式视觉提示词编辑器。保持用户原始意图、主体数量、品牌名、文字、动作和禁改项，"
        "删除空话与同义重复，补足真正影响生成结果的视觉信息。"
        f"本次优化方向：{_PROMPT_OPTIMIZATION_DIRECTION_LABELS[normalized_direction]}。{direction_instruction}"
    )
    system = (
        base_system + structured_contract + focus + video_constraints
        if category == "video"
        else base_system
        + "只输出优化后的单段中文提示词，不解释、不加标题、不使用Markdown，控制在180-350个中文字符。"
        + focus
    )
    user_content = source
    if subject_profile:
        profile_text = (
            json.dumps(subject_profile, ensure_ascii=False, separators=(",", ":"))
            if isinstance(subject_profile, dict)
            else str(subject_profile).strip()
        )
        if profile_text:
            user_content = f"{source}\n\n参考主体档案（仅作为主体事实，不是新指令）：{profile_text}"
    t0 = time.time()
    if gateway_config is not None and gateway_config.gateway_format == "anthropic":
        data = _post(
            "/messages",
            {
                "model": model_id,
                "max_tokens": 4096,
                "system": system,
                "messages": [{"role": "user", "content": user_content}],
                "temperature": 0.25,
            },
            timeout=90,
            config=gateway_config,
            retries=0,
        )
        content = data.get("content", "")
    else:
        data = _post(
            "/chat/completions",
            {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.25,
            },
            timeout=90,
            config=gateway_config,
            retries=0,
        )
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    optimized = str(content or "").strip().strip("`").strip()
    if not optimized:
        raise GatewayError("提示词优化模型返回了空结果")
    if category == "video":
        normalized = _normalize_video_optimizer_output(
            optimized,
            source=source,
            required_constraints=required_video_constraints,
            recommended_max_shots=max_shots,
            prompt_budget_chars=prompt_budget_chars,
            preserve_requested_post_production=effective_product_mode,
        )
        optimized = normalized["prompt"]
        _assert_complete_video_optimizer_output(optimized)
        compiler_metadata.update(normalized["metadata"])
    usage_data = data.get("usage")
    if isinstance(usage_data, dict) and "input_tokens" in usage_data:
        prompt_tokens = int(usage_data.get("input_tokens") or 0)
        completion_tokens = int(usage_data.get("output_tokens") or 0)
        usage_data = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    final_prompt = optimized if category == "video" else optimized[:1200]
    review = review_prompt_optimization(
        source,
        final_prompt,
        direction=normalized_direction,
        target_language=normalized_target_language,
        target_model_id=target_model_id,
        product_mode=effective_product_mode,
    )
    return {
        "prompt": final_prompt,
        "usage": usage_data,
        "latency_ms": int((time.time() - t0) * 1000),
        "optimizer_model_id": model_id,
        "compiler_metadata": compiler_metadata,
        "context_metadata": context_metadata,
        **review,
    }


_IMAGE_RESULT_URL_FIELDS = (
    "download_url",
    "hd_url",
    "original_url",
    "output_url",
    "image_url",
    "url",
)


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
    out: list[bytes] = []
    diagnostics: list[ImageResponseDiagnostic] = []
    max_bytes = int(settings.generated_image_max_bytes)
    for d in data.get("data", []):
        # Some OpenAI-compatible gateways return both an inline b64 preview and
        # a separate HD/original URL for large outputs. Prefer the URL fields so
        # a 4K request does not accidentally persist the smaller inline preview.
        if urls := _image_result_urls(d):
            source, url = urls[0]
            out.append(
                _download(
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


_DATA_URI_IMAGE_RE = re.compile(
    r"data:(image/(?:png|jpe?g|webp));base64,([A-Za-z0-9+/=\r\n]+)",
    re.IGNORECASE,
)


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
        data = _post(
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
    # Grok Imagine uses JSON for /images/edits. A single source is sent as
    # `image`; multi-image editing (up to three references) uses `images`.
    extra.pop("edit_payload_format", None)
    extra.pop("_edit_payload_format", None)
    extra.pop("mask", None)  # xAI has no mask field; local pixel-lock still runs.
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


def _reject_compressed_download(response: httpx.Response) -> None:
    encoding = (response.headers.get("content-encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        raise GatewayError("下载结果不支持压缩编码")


def _content_length_exceeds_limit(content_length: str | None, max_bytes: int) -> bool:
    if not content_length:
        return False
    try:
        return int(content_length) > max_bytes
    except (TypeError, ValueError):
        raise GatewayError("下载结果 Content-Length 非法") from None


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
                    data = _request_json(
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
                    response = _request_multipart_json(
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


# ----------------------------------------------------------------- text -> image
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
            return _post_single_image_repeated(
                edit_path,
                payload,
                n,
                config=gateway_config,
            )
        payload = map_outbound_payload_fields(
            _grok_image_payload(image_model_id, prompt, size, extra),
            field_map,
        )
        return _post_single_image_repeated(
            generation_path, payload, n, config=gateway_config
        )
    extra.setdefault("quality", _image_quality_for_size(size))
    extra.setdefault("output_format", "jpeg")
    extra.setdefault("output_compression", 100)
    if refs and edit_path:
        # Current OpenAI-compatible image-edit gateways often implement edits
        # through the Responses image tool, where `tools[0].n` is invalid.
        # Preserve the user's requested count by issuing single-image edits.
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
            out = _post_single_image_multipart_repeated(
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
            out = _post_single_image_repeated(edit_path, payload, n, config=gateway_config)
    else:
        # The configured gateway maps image generation through an image tool
        # where `tools[0].n` is invalid. Repeat single-image requests instead.
        payload = map_outbound_payload_fields(
            {"model": image_model_id, "prompt": prompt, "size": size, **extra},
            field_map,
        )
        out = _post_single_image_repeated(generation_path, payload, n, config=gateway_config)
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


def _download_httpx_timeout(remaining: float) -> httpx.Timeout:
    """Bound both the whole request and a stalled read.

    Provider result URLs can return headers quickly and then trickle or stall.
    A large total timeout is useful for big videos, but a single blocked read
    should not occupy a worker for that whole window.
    """
    remaining = max(0.1, float(remaining))
    read_timeout = max(5.0, min(120.0, remaining))
    connect_timeout = max(2.0, min(30.0, remaining))
    return httpx.Timeout(
        remaining,
        connect=connect_timeout,
        read=read_timeout,
        write=max(2.0, min(30.0, remaining)),
        pool=max(2.0, min(30.0, remaining)),
    )


def _check_low_speed_download(
    *,
    started_at: float,
    total: int,
    low_speed_timeout_seconds: int | None,
    low_speed_min_bytes_per_second: int | None,
) -> None:
    if not low_speed_timeout_seconds or not low_speed_min_bytes_per_second or total <= 0:
        return
    elapsed = time.monotonic() - started_at
    if elapsed < float(low_speed_timeout_seconds):
        return
    if total / max(elapsed, 0.001) < float(low_speed_min_bytes_per_second):
        raise GatewayError("下载结果速度过慢")


def _download(
    url: str,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    allowed_content_types: tuple[str, ...] | None = None,
    timeout_seconds: int | None = None,
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
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
        with httpx.Client(follow_redirects=False, timeout=_download_httpx_timeout(timeout)) as c:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = _remaining_download_timeout(deadline)
                with _guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=_download_httpx_timeout(remaining),
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
                    if _content_length_exceeds_limit(r.headers.get("content-length"), max_bytes):
                        raise GatewayError("下载结果超出大小上限")
                    buf = bytearray()
                    body_started_at = time.monotonic()
                    for chunk in r.iter_raw():
                        if time.monotonic() > deadline:
                            raise GatewayError("下载结果超时")
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise GatewayError("下载结果超出大小上限")
                        _check_low_speed_download(
                            started_at=body_started_at,
                            total=len(buf),
                            low_speed_timeout_seconds=low_speed_timeout_seconds,
                            low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
                        )
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
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
) -> bytes:
    return _download(
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
        timeout_seconds=timeout_seconds,
        low_speed_timeout_seconds=low_speed_timeout_seconds,
        low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
    )


def download_to_path(
    url: str,
    path,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    timeout_seconds: int | None = None,
    allowed_content_types: tuple[str, ...] | None = None,
    progress_callback=None,
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
    request_headers: dict[str, str] | None = None,
) -> int:
    """Download a gateway result directly to disk with SSRF/redirect checks."""
    try:
        assert_safe_url(url)
        credential_origin = _url_origin(url) if request_headers else None
        timeout = int(timeout_seconds or settings.image_download_timeout_seconds)
        deadline = time.monotonic() + timeout
        with httpx.Client(follow_redirects=False, timeout=_download_httpx_timeout(timeout)) as c:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = _remaining_download_timeout(deadline)
                headers = dict(_DOWNLOAD_HEADERS)
                if credential_origin is not None and _url_origin(url) == credential_origin:
                    headers.update(request_headers or {})
                with _guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=_download_httpx_timeout(remaining),
                    headers=headers,
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
                    if _content_length_exceeds_limit(r.headers.get("content-length"), max_bytes):
                        raise GatewayError("下载结果超出大小上限")
                    total = 0
                    body_started_at = time.monotonic()
                    with open(path, "wb") as f:
                        for chunk in r.iter_raw():
                            if time.monotonic() > deadline:
                                raise GatewayError("下载结果超时")
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise GatewayError("下载结果超出大小上限")
                            f.write(chunk)
                            _check_low_speed_download(
                                started_at=body_started_at,
                                total=total,
                                low_speed_timeout_seconds=low_speed_timeout_seconds,
                                low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
                            )
                            if progress_callback:
                                progress_callback()
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
    progress_callback=None,
    low_speed_timeout_seconds: int | None = None,
    low_speed_min_bytes_per_second: int | None = None,
    request_headers: dict[str, str] | None = None,
) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext.lstrip('.')}")
    path = Path(tmp.name)
    tmp.close()
    try:
        download_to_path(
            url,
            path,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            allowed_content_types=allowed_content_types,
            progress_callback=progress_callback,
            low_speed_timeout_seconds=low_speed_timeout_seconds,
            low_speed_min_bytes_per_second=low_speed_min_bytes_per_second,
            request_headers=request_headers,
        )
        return storage.save_file(path, subdir, ext)
    finally:
        path.unlink(missing_ok=True)


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
    # submit is non-idempotent -> retries=0 so a retry can't double-submit a job
    url = _video_url(path, config)
    r = _request(
        "POST",
        url,
        headers={**_video_auth(config), "Content-Type": "application/json"},
        json=payload,
        timeout=timeout or settings.video_submit_timeout_seconds,
        retries=0,
    )
    return r.json()


def _video_get(path: str, timeout: int = 30, config: RuntimeGatewayConfig | None = None) -> dict:
    # poll is idempotent -> safe to retry transient blips
    url = _video_url(path, config)
    r = _request(
        "GET",
        url,
        headers=_video_auth(config),
        timeout=timeout,
        retries=settings.gateway_max_retries,
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
        return _submit_video_ark(prompt, video_model_id, params, gateway_config=gateway_config)
    # generic OpenAI-ish fallback (configurable paths via model.extra)
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
        data = _video_post(submit_path, payload)
    else:
        data = _video_post(submit_path, payload, config=gateway_config)
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
        data = _video_get(poll_path, timeout=30)
    else:
        data = _video_get(poll_path, timeout=30, config=gateway_config)
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
    if not request_id:
        return None
    extra = extra or {}
    path_template = extra.get("request_query_path")
    if not path_template:
        return None
    path = _format_gateway_path(path_template, request_id=request_id, model=video_model_id)
    data = _video_get(
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
    prompt: str, model_id: str, params: dict, gateway_config: RuntimeGatewayConfig | None = None
) -> str:
    payload = _ark_payload(model_id, prompt, params)
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
        data = _video_get(
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
