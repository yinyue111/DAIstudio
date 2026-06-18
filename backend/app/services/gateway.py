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
import io
import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..config import settings
from . import storage
from .ssrf import (
    MAX_REDIRECTS,
    SsrfError,
    assert_safe_url,
    pinned_safe_resolution,
)

log = logging.getLogger("gateway")
_IMAGE_GATEWAY_SEMAPHORE = threading.BoundedSemaphore(
    max(1, int(settings.image_gateway_parallelism or 1))
)

# Rich multi-dimension template so the regenerated image stays close to the
# reference. Keep keys stable — the frontend renders whatever keys come back.
IMAGE_REVERSE_TEMPLATE = (
    "你是世界顶级的视觉复刻专家。请以像素级的严谨程度观察这张参考图,目标是让另一位画师或模型"
    "仅凭你的文字,就能复刻出与原图几乎无法区分的画面。\n"
    "硬性要求:必须具体、可量化,严禁『一些/可能/比较/大概』等模糊词——\n"
    "  • 颜色:给具体色名 + 十六进制色值(如 暖橙 #E8945A),并标明主色/辅色/点缀色各自的画面占比;\n"
    "  • 位置:用画面百分比坐标描述(如 主体居中偏左、约占画幅 60%、视平线在画面 45% 高度处);\n"
    "  • 数量/角度/比例:给确切数值(如 3 个人物、镜头俯角约 15°、主体与背景虚实比);\n"
    "  • 镜头:估计焦段(mm)、光圈感(景深深浅)、视角(平视/俯/仰)。\n"
    "输出严格的 JSON(不要任何额外文字、不要 markdown)。字段如下:\n"
    "{\n"
    '  "主体": "主要对象:类别、确切数量、姿态、朝向、表情/神态/动作状态",\n'
    '  "细节特征": "主体可识别的关键细节:服饰/材质/造型/发型/纹样/标志性配件等,越细越好",\n'
    '  "场景背景": "环境、地点、背景元素;前景/中景/远景的层次与各自内容",\n'
    '  "风格": "确切的艺术/摄影风格与流派(如 皮克斯3D渲染/日系胶片/赛博朋克插画/产品棚拍),可点名参考美学",\n'
    '  "构图": "构图法(三分/中心/对角/框架等)、主体在画面中的百分比位置、画幅比例、留白分布、引导线",\n'
    '  "景别": "镜头景别:特写/近景/中景/全景/远景,以及主体占画幅的大致比例(决定主体离镜头远近)",\n'
    '  "视角镜头": "拍摄视角与俯仰角、估计焦段(mm)、景深(浅/深及虚化程度)、透视强弱",\n'
    '  "光线": "光源类型与方向(如 左上 45° 主光)、软硬、明暗对比、阴影形状与浓度、色温、时间氛围",\n'
    '  "色调配色": "主色/辅色/点缀色的具体色名+色值与画面占比、整体饱和度、冷暖倾向、对比强弱、是否有色彩滤镜",\n'
    '  "材质纹理": "主要表面的材质与质感(磨砂/反光/金属/布料/颗粒),以及反射/高光/粗糙度细节",\n'
    '  "氛围情绪": "画面传达的情绪与氛围基调",\n'
    '  "后期质感": "渲染/后期特征:颗粒强度、噪点、锐度、光晕、暗角强弱、色彩分级风格、是否 HDR/胶片感",\n'
    '  "文字水印": "画面中的任何文字/logo/水印的内容、字体感、位置;若无则填 无",\n'
    '  "标签": "8-15 个最能定义这张图的精炼英文关键词(主体/风格/媒介/光线/质感/质量词),逗号分隔,可直接喂给图像模型",\n'
    '  "负向": "需要明确避免的元素(多余文字/水印/多余肢体/畸变/低清/AI 痕迹等)",\n'
    '  "final_text": "整合为一段可直接用于文生图的高质量英文提示词。结构建议:[主体+核心特征] → [风格/媒介] '
    "→ [景别/构图] → [光线/色调] → [材质/质感] → [质量词]。以英文关键词短语为主、逗号分隔,把最关键的特征"
    "前置并适度强调,结尾补通用质量词(如 highly detailed, sharp focus, 8k),控制在约 60-90 词,最大化复刻度\"\n"
    "}"
)

# Video template captures temporal / motion dimensions so the model can produce
# a coherent (not static) clip. Reverse runs on the video's cover/keyframe.
VIDEO_REVERSE_TEMPLATE = (
    "你是世界顶级的影视分镜师与视频提示词工程师。下面按时间先后给你若干帧(从一段参考视频中等间隔"
    "抽样,第 1 张为首帧),请把它们当作同一段视频的时间序列来分析,推断帧与帧之间发生的运动,设计"
    "一段同风格、可连续播放、运动连贯自然的短视频提示词。\n"
    "硬性要求:具体可量化,颜色给色值,运镜给方向与幅度,动作按时间顺序拆解,严禁模糊词。\n"
    "输出严格的 JSON(不要任何额外文字、不要 markdown)。字段如下:\n"
    "{\n"
    '  "主体": "主要对象:类别、数量、外观特征与初始状态",\n'
    '  "细节特征": "主体可识别的关键细节(服饰/材质/造型/配件等)",\n'
    '  "场景背景": "环境、地点、前/中/远景层次",\n'
    '  "风格": "确切的影像/视觉风格(如 电影感/动画/广告棚拍/手持纪实)",\n'
    '  "视角构图": "起始画面的拍摄视角与俯仰角、景别(特写/中景/全景)、主体在画面中的位置与占比",\n'
    '  "主体动作": "主体随时间发生的动作/表演,按帧的先后顺序逐步拆解(先…然后…最后…)",\n'
    '  "镜头运动": "运镜方式(推/拉/摇/移/跟/环绕/升降/手持)及方向、幅度、速度",\n'
    '  "运动节奏": "整体节奏(舒缓/中速/快速)与速度感、是否有慢动作",\n'
    '  "时序分镜": "从首帧到末帧的画面演变分镜(0-1s…1-2s…),对应你看到的帧间变化",\n'
    '  "时长建议": "建议时长(秒)与帧率感(如 5s / 24fps)",\n'
    '  "光线": "光线方向/软硬/色温,及其随时间的变化",\n'
    '  "色调配色": "主色/辅色(具体色值)、饱和度、冷暖、整体调色",\n'
    '  "材质纹理": "主要表面材质与质感",\n'
    '  "氛围情绪": "情绪与氛围基调",\n'
    '  "转场": "如有,镜头之间的转场方式;若为单镜头则填 无",\n'
    '  "负向": "需要避免的元素(闪烁/形变/拼接感/穿帮/水印/不自然运动等)",\n'
    '  "final_text": "整合为一段可直接用于文生视频的完整英文提示词,必须包含 主体动作 + 镜头运动 + 时序节奏,'
    '确保生成视频与参考片段风格与运动一致,英文优先"\n'
    "}"
)


def _reverse_template(target: str, n_frames: int = 1) -> str:
    if target == "video":
        if n_frames > 1:
            return (f"(以下共 {n_frames} 帧,按时间先后排列)\n" + VIDEO_REVERSE_TEMPLATE)
        return VIDEO_REVERSE_TEMPLATE
    return IMAGE_REVERSE_TEMPLATE


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


def _gateway_error_message(status_code: int, text: str) -> str:
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
    return f"网关返回 {status_code}: {message}"


def _safe_log_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "<redacted>", parts.fragment))


def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.gateway_api_key}"}


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
            with httpx.Client(timeout=timeout, follow_redirects=True) as c:
                r = c.request(method, url, headers=headers, json=json)
            log.info("gateway %s %s -> %s in %.2fs", method, _safe_log_url(url), r.status_code,
                     time.time() - t0)
            if r.status_code >= 400:
                msg = _gateway_error_message(r.status_code, r.text)
                if r.status_code < 500 and r.status_code != 429:
                    raise GatewayError(msg, status_code=r.status_code, transient=False)
                last = GatewayError(msg, status_code=r.status_code, transient=True)
            else:
                return r
        except GatewayError:
            raise
        except Exception as e:  # noqa: BLE001 — connect/timeout etc.
            last = GatewayError(str(e), transient=True)
            log.warning("gateway %s %s attempt %s failed: %s", method, _safe_log_url(url),
                        attempt + 1, e)
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    if isinstance(last, GatewayError):
        raise GatewayError(f"网关调用失败: {last}", status_code=last.status_code,
                           transient=last.transient)
    raise GatewayError(f"网关调用失败: {last}", transient=True)


def _post(path: str, payload: dict, timeout: int | None = None) -> dict:
    url = f"{settings.gateway_base_url.rstrip('/')}{path}"
    r = _request("POST", url,
                 headers={**_auth(), "Content-Type": "application/json"},
                 json=payload, timeout=timeout or settings.gateway_timeout_seconds,
                 retries=settings.gateway_max_retries)
    return r.json()


def _request_json(method: str, url: str, *, headers: dict, payload: dict | None,
                  timeout: int, retries: int) -> dict:
    r = _request(method, url, headers=headers, json=payload,
                 timeout=timeout, retries=retries)
    return r.json()


def _get(path: str, timeout: int | None = None) -> dict:
    url = f"{settings.gateway_base_url.rstrip('/')}{path}"
    r = _request("GET", url, headers=_auth(),
                 timeout=timeout or settings.gateway_timeout_seconds,
                 retries=settings.gateway_max_retries)
    return r.json()


# ---------------------------------------------------------------- reverse prompt
def reverse_prompt(image_refs, vision_model_id: str, target: str = "image") -> dict:
    """Reverse one or more images into a structured prompt.

    ``image_refs`` is a single URL/data-URI or a list of them (e.g. several
    keyframes sampled from a reference video, in temporal order). ``target``
    selects the image vs video template. Returns the parsed prompt plus the
    provider ``usage`` (real token consumption) and call latency."""
    if settings.effective_mock_mode:
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
    data = _post("/v1/chat/completions", payload, timeout=90)
    latency_ms = int((time.time() - t0) * 1000)
    content_text = data["choices"][0]["message"]["content"]
    result = _parse_structured(content_text)
    result["usage"] = data.get("usage")  # {prompt_tokens, completion_tokens, total_tokens}
    result["latency_ms"] = latency_ms
    return result


def _parse_structured(content: str) -> dict:
    # tolerate ```json fences / surrounding prose
    m = re.search(r"\{.*\}", content, re.S)
    raw = m.group(0) if m else content
    try:
        obj = json.loads(raw)
    except Exception:
        obj = {"主体": content.strip()[:200], "final_text": content.strip()}
    final_text = obj.pop("final_text", None) or _compose_final(obj)
    return {"structured": obj, "final_text": final_text}


def _compose_final(obj: dict) -> str:
    """Fallback final prompt when the model omits final_text: join every
    described dimension (except the negative), then append the negative."""
    neg = obj.get("负向")
    parts = [f"{k}: {v}" for k, v in obj.items()
             if k not in ("负向", "final_text") and v]
    text = ", ".join(parts)
    if neg:
        text += f" | 避免: {neg}"
    return text or "same style, high quality"


def _decode_image_response(data: dict) -> list[bytes]:
    out: list[bytes] = []
    for d in data.get("data", []):
        if d.get("b64_json"):
            out.append(base64.b64decode(d["b64_json"]))
        elif d.get("url"):
            out.append(_download(d["url"]))
    return out


def _post_single_image_repeated(path: str, payload: dict, n: int) -> list[bytes]:
    """Run repeated one-image calls concurrently and return exactly n images.

    The current gateway rejects batch/tool-count params such as ``tools[0].n``.
    Repeating single-image requests preserves compatibility; parallelising them
    preserves the user's expected batch latency.
    """
    n = max(1, int(n))
    url = f"{settings.gateway_base_url.rstrip('/')}{path}"
    headers = {**_auth(), "Content-Type": "application/json"}
    timeout = settings.image_gateway_timeout_seconds
    workers = max(1, min(n, int(settings.image_gateway_parallelism or 1)))

    def one() -> list[bytes]:
        with _IMAGE_GATEWAY_SEMAPHORE:
            data = _request_json(
                "POST",
                url,
                headers=headers,
                payload=payload,
                timeout=timeout,
                retries=0,
            )
        return _decode_image_response(data)

    if workers == 1:
        out: list[bytes] = []
        for _ in range(n):
            try:
                out.extend(one())
            except Exception as e:  # noqa: BLE001
                log.warning("single image sub-request failed: %s", e)
            if len(out) >= n:
                break
        return out[:n]

    out_by_index: dict[int, list[bytes]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one): i for i in range(n)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                out_by_index[idx] = fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("image sub-request %s/%s failed: %s", idx + 1, n, e)
                out_by_index[idx] = []

    out: list[bytes] = []
    for i in range(n):
        out.extend(out_by_index.get(i, []))
        if len(out) >= n:
            break
    return out[:n]


# ----------------------------------------------------------------- text -> image
def gen_image(prompt: str, image_model_id: str, n: int = 4,
              size: str = "1024x1024", reference_image_url: str | None = None,
              edit_path: str | None = None,
              extra_payload: dict | None = None) -> list[bytes]:
    """Returns a list of raw image bytes (already downloaded / decoded).

    If a reference image + an edit endpoint are provided (reverse-off,
    image+instruction -> image), call the image-to-image endpoint; otherwise
    fall back to plain text -> image. This keeps the default path safe even
    when the gateway has no edit endpoint configured.
    """
    if settings.effective_mock_mode:
        return [_mock_image(prompt, size, i) for i in range(n)]

    n = max(1, int(n))
    extra = {k: v for k, v in (extra_payload or {}).items() if v not in (None, "")}
    if reference_image_url and edit_path:
        # Current OpenAI-compatible image-edit gateways often implement edits
        # through the Responses image tool, where `tools[0].n` is invalid.
        # Preserve the user's requested count by issuing single-image edits.
        payload = {
            "model": image_model_id,
            "image": reference_image_url,
            "prompt": prompt,
            "size": size,
            **extra,
        }
        out = _post_single_image_repeated(edit_path, payload, n)
    else:
        # The configured gateway maps image generation through an image tool
        # where `tools[0].n` is invalid. Repeat single-image requests instead.
        payload = {"model": image_model_id, "prompt": prompt, "size": size, **extra}
        out = _post_single_image_repeated("/v1/images/generations", payload, n)
    if not out:
        raise GatewayError("图像网关未返回任何结果")
    return out


_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # generous cap for video results


def _download(
    url: str,
    *,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    allowed_content_types: tuple[str, ...] | None = None,
) -> bytes:
    """Download a gateway-returned result URL with the SSRF guard applied.

    Even though these URLs come from the (trusted) gateway, a poisoned or
    compromised response must not make the worker fetch internal/metadata
    endpoints — so redirects are NOT auto-followed: every hop is re-validated and
    the socket is pinned to the vetted public IP. Failures normalise to
    GatewayError so callers handle them uniformly."""
    try:
        assert_safe_url(url)
        deadline = time.monotonic() + int(settings.image_download_timeout_seconds)
        with httpx.Client(
            follow_redirects=False,
            timeout=settings.image_download_timeout_seconds,
        ) as c:
            for _ in range(MAX_REDIRECTS + 1):
                if time.monotonic() > deadline:
                    raise GatewayError("下载结果超时")
                with pinned_safe_resolution(url), c.stream("GET", url) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        assert_safe_url(url)  # red line: re-check every hop
                        continue
                    if r.status_code >= 400:
                        raise GatewayError(f"下载结果失败 {r.status_code}: {url[:200]}")
                    content_type = (r.headers.get("content-type") or "").lower()
                    if allowed_content_types and not any(
                        content_type.startswith(prefix.lower()) for prefix in allowed_content_types
                    ):
                        raise GatewayError("下载结果类型不支持")
                    content_length = r.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise GatewayError("下载结果超出大小上限")
                    buf = bytearray()
                    for chunk in r.iter_bytes():
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
) -> bytes:
    return _download(
        url,
        max_bytes=max_bytes,
        allowed_content_types=allowed_content_types,
    )


def download_to_path(url: str, path, *, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> int:
    """Download a gateway result directly to disk with SSRF/redirect checks."""
    try:
        assert_safe_url(url)
        deadline = time.monotonic() + int(settings.image_download_timeout_seconds)
        with httpx.Client(
            follow_redirects=False,
            timeout=settings.image_download_timeout_seconds,
        ) as c:
            for _ in range(MAX_REDIRECTS + 1):
                if time.monotonic() > deadline:
                    raise GatewayError("下载结果超时")
                with pinned_safe_resolution(url), c.stream("GET", url) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        assert_safe_url(url)
                        continue
                    if r.status_code >= 400:
                        raise GatewayError(f"下载结果失败 {r.status_code}: {url[:200]}")
                    content_length = r.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise GatewayError("下载结果超出大小上限")
                    total = 0
                    with open(path, "wb") as f:
                        for chunk in r.iter_bytes():
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
) -> str:
    if settings.effective_mock_mode:
        return storage.save_bytes(download_bytes(url), subdir, ext)
    key, path = storage.reserve_key(subdir, ext)
    try:
        download_to_path(url, path, max_bytes=max_bytes)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return key


# ----------------------------------------------------------------- text -> video
# The video gateway may differ from the image/vision gateway (its own base_url +
# api_key + format). Two formats supported: "ark" (Volcengine / Doubao Seedance,
# async /contents/generations/tasks) and a generic OpenAI-ish fallback.

def _video_base() -> str:
    return settings.video_base.rstrip("/")


def _video_auth() -> dict:
    return {"Authorization": f"Bearer {settings.video_key}"}


def _video_post(path: str, payload: dict, timeout: int | None = None) -> dict:
    # submit is non-idempotent -> retries=0 so a retry can't double-submit a job
    url = f"{_video_base()}{path}"
    r = _request("POST", url,
                 headers={**_video_auth(), "Content-Type": "application/json"},
                 json=payload, timeout=timeout or settings.video_submit_timeout_seconds,
                 retries=0)
    return r.json()


def _video_get(path: str, timeout: int = 30) -> dict:
    # poll is idempotent -> safe to retry transient blips
    url = f"{_video_base()}{path}"
    r = _request("GET", url, headers=_video_auth(), timeout=timeout,
                 retries=settings.gateway_max_retries)
    return r.json()


_VIDEO_STATUS = {
    "succeeded": "succeeded", "success": "succeeded", "completed": "succeeded",
    "done": "succeeded",
    "failed": "failed", "error": "failed", "cancelled": "failed", "canceled": "failed",
    "expired": "failed",
    "running": "running", "processing": "running", "in_progress": "running",
    "generating": "running",
    "queued": "queued", "pending": "queued", "submitted": "queued",
}


def submit_video(prompt: str, video_model_id: str, params: dict,
                 extra: dict | None = None) -> str:
    """Submit an async video job; returns an external task id."""
    if settings.effective_video_mock:
        return f"mock-{random.randint(100000, 999999)}"
    if settings.video_gateway_format == "ark":
        return _submit_video_ark(prompt, video_model_id, params)
    # generic OpenAI-ish fallback (configurable paths via model.extra)
    extra = extra or {}
    submit_path = extra.get("submit_path", "/v1/videos/generations")
    id_field = extra.get("id_field", "id")
    payload_params = dict(params or {})
    first_frame = payload_params.pop("first_frame_image", None)
    first_frame_field = extra.get("first_frame_field", "first_frame_image")
    if first_frame and first_frame_field:
        payload_params[first_frame_field] = first_frame
    payload = {"model": video_model_id, "prompt": prompt, **payload_params}
    data = _video_post(submit_path, payload)
    task_id = data.get(id_field) or data.get("task_id") or data.get("id")
    if not task_id:
        raise GatewayError(f"视频网关未返回任务号: {str(data)[:200]}")
    return str(task_id)


def poll_video(external_task_id: str, video_model_id: str,
               extra: dict | None = None) -> dict:
    """Poll one tick. Returns {status: queued|running|succeeded|failed, url?}."""
    if settings.effective_video_mock or str(external_task_id).startswith("mock-"):
        return {"status": "succeeded", "url": None, "mock": True}
    if settings.video_gateway_format == "ark":
        return _poll_video_ark(external_task_id)
    extra = extra or {}
    poll_path = (extra.get("poll_path", "/v1/videos/{id}")).format(id=external_task_id)
    data = _video_get(poll_path, timeout=30)
    norm = _VIDEO_STATUS.get((data.get("status") or "").lower(), "running")
    url = None
    if norm == "succeeded":
        url = (data.get("url")
               or (data.get("data") or [{}])[0].get("url")
               or (data.get("output") or {}).get("url"))
    return {"status": norm, "url": url, "raw": data}


# ----- Volcengine Ark (Doubao Seedance) adapter -----
def _ark_text(prompt: str, params: dict) -> str:
    """Seedance takes generation params as --flags appended to the text prompt."""
    parts = [prompt.strip()]
    res = params.get("resolution")
    dur = params.get("duration")
    ratio = params.get("ratio")
    seed = params.get("seed")
    if res:
        parts.append(f"--resolution {res}")
    if dur:
        parts.append(f"--duration {int(dur)}")
    if ratio:
        parts.append(f"--ratio {ratio}")
    if seed not in (None, ""):
        parts.append(f"--seed {int(seed)}")
    parts.append("--watermark false")
    return "  ".join(parts)


def _ark_content(prompt: str, params: dict) -> list:
    content = [{"type": "text", "text": _ark_text(prompt, params)}]
    img = params.get("first_frame_image")
    if img:
        content.append({"type": "image_url", "image_url": {"url": img}})
    return content


def _submit_video_ark(prompt: str, model_id: str, params: dict) -> str:
    payload = {"model": model_id, "content": _ark_content(prompt, params)}
    data = _video_post("/contents/generations/tasks", payload)
    task_id = data.get("id")
    if not task_id:
        raise GatewayError(f"Ark 未返回任务号: {str(data)[:200]}")
    return str(task_id)


def _poll_video_ark(task_id: str) -> dict:
    data = _video_get(f"/contents/generations/tasks/{task_id}", timeout=30)
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


# ----------------------------------------------------------------------- mocks
def _mock_reverse(target: str = "image") -> dict:
    if target == "video":
        structured = {
            "主体": "示例主体(mock)", "场景背景": "简洁纯色背景", "风格": "极简插画",
            "视角构图": "正面平视,中景,主体居中约占 50%",
            "主体动作": "缓缓转身并微笑", "镜头运动": "缓慢推近(dolly-in)",
            "运动节奏": "舒缓", "时序分镜": "0-2s 静止特写;2-4s 推近;4-6s 主体动作",
            "时长建议": "6 秒 / 24fps", "光线": "柔和顶光,逐渐变亮",
            "色调配色": "暖色低饱和", "氛围情绪": "宁静治愈", "转场": "无",
            "负向": "闪烁, 形变, 拼接感, 水印",
        }
        return {"structured": structured,
                "final_text": "minimal illustration, subject slowly turns and smiles, "
                              "slow dolly-in, soft warm light, calm mood, 6s 24fps, "
                              "smooth coherent motion (mock)"}
    structured = {
        "主体": "示例主体(mock),单个,居中正面", "细节特征": "简约造型,无明显纹样",
        "场景背景": "纯色背景,层次简单", "风格": "极简扁平插画",
        "构图": "居中,1:1,适度留白", "景别": "中近景,主体约占画幅 55%",
        "视角镜头": "正面平视,中焦,浅景深",
        "光线": "柔和顶光,低对比", "色调配色": "主色暖橙 #E8945A、辅色米白,低饱和",
        "材质纹理": "哑光纸质,细微颗粒", "氛围情绪": "宁静",
        "后期质感": "轻微胶片颗粒,柔和暗角",
        "标签": "minimal, flat illustration, centered subject, warm palette, soft light, matte texture, serene, high quality",
        "负向": "文字, 水印, 多余肢体, 畸变",
    }
    return {"structured": structured,
            "final_text": "minimal flat illustration, single centered subject, front view, "
                          "warm muted palette (orange + cream), soft top light, matte paper "
                          "texture, subtle film grain, serene mood, high quality (mock)"}


def _mock_image(prompt: str, size: str, idx: int) -> bytes:
    from PIL import Image, ImageDraw

    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except Exception:
        w, h = 1024, 1024
    img = Image.new("RGB", (w, h))
    px = img.load()
    seed = (hash(prompt) + idx * 97) & 0xFFFFFF
    r0, g0, b0 = (seed >> 16) & 255, (seed >> 8) & 255, seed & 255
    for y in range(h):
        for x in range(0, w, 4):  # step for speed
            r = (r0 + x * 255 // w) % 256
            g = (g0 + y * 255 // h) % 256
            b = (b0 + (x + y) * 255 // (w + h)) % 256
            for dx in range(4):
                if x + dx < w:
                    px[x + dx, y] = (r, g, b)
    d = ImageDraw.Draw(img)
    d.text((24, 24), f"MOCK #{idx + 1}", fill=(255, 255, 255))
    d.text((24, 44), prompt[:60], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def mock_video_preview_image() -> bytes:
    """Placeholder still used as a stand-in for mock video preview/final."""
    return _mock_image("video preview", "640x360", 0)
