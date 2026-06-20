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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings
from . import locks, storage
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
    "输出严格的 JSON(不要任何额外文字、不要 markdown)。final_text 必须从下列维度逐项压缩整合而来,"
    "不得引入维度中没有出现的新主体/场景/风格;主体、细节、场景、风格、构图、景别、镜头、光线、配色、材质、氛围必须能在 final_text 中对应找到。\n"
    "字段如下:\n"
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
    '  "final_text": "按同一维度顺序整合为一段可直接用于文生图的中文提示词:主体与细节 → 场景背景 → 风格 → 景别/构图/视角镜头 → 光线/色调配色 → 材质纹理 → 氛围/后期质感。'
    "必须覆盖所有核心维度,不得与上述字段矛盾;末尾追加英文标签关键词和质量词,控制在 120-180 个中文字符。\"\n"
    "}"
)

# Video template captures temporal / motion dimensions so the model can produce
# a coherent (not static) clip. Reverse runs on sampled keyframes when possible.
VIDEO_REVERSE_TEMPLATE = (
    "你是世界顶级的商业广告导演、剪辑师和视频提示词工程师。下面按时间先后给你若干帧(从一段"
    "参考视频中等间隔抽样,第 1 张为首帧),请把它们当作同一条广告片的时间序列来分析。目标不是"
    "泛化成同类视频,而是最大限度复刻参考片的商业视觉:主体、服装/商品、模特动作、场景、构图、"
    "镜头语言、光线、色彩、字幕/卖点和剪辑节奏都要贴近原片。\n"
    "硬性要求:\n"
    "1. 只能描述你从帧中能确认或高置信推断的内容;不确定处写『未见/不确定』,不要编造新商品、新场景或新品牌。\n"
    "2. 颜色给具体色名+十六进制色值,位置用画面百分比,动作/运镜按时间顺序拆解。\n"
    "3. 如果是女装/电商广告,必须记录服装版型、面料质感、穿搭层次、模特姿态、卖点字幕、商品展示方式。\n"
    "4. final_text 必须可直接用于文生视频,以『参考片复刻』为核心,不要写成普通美图描述。\n"
    "输出严格的 JSON(不要任何额外文字、不要 markdown)。final_text 必须从下列维度逐项整合而来,"
    "不得引入维度中没有出现的新主体/场景/风格/动作;主体、商品/服装、场景、风格、视角构图、动作、运镜、分镜、字幕卖点、光线、配色必须能在 final_text 中对应找到。\n"
    "字段如下:\n"
    "{\n"
    '  "主体": "主要对象:类别、数量、性别/年龄段/体态、外观特征、初始位置和朝向",\n'
    '  "商品服装": "商品或服装的具体类别、颜色色值、版型、剪裁、长度、面料、纹理、搭配单品、配饰;若非商品广告也按可见物体写",\n'
    '  "细节特征": "可识别关键细节:妆发、鞋包、道具、logo、花纹、扣子、褶皱、反光、手部动作等",\n'
    '  "场景背景": "地点/空间、前中后景层次、地面/墙面/家具/道具、背景虚实、画面留白",\n'
    '  "广告目标": "这条片子在卖什么/展示什么卖点;若只看到画面无法确认,写未见明确卖点",\n'
    '  "风格": "确切商业影像风格(如 抖音女装种草/电商棚拍/街拍广告/直播切片/品牌大片),不要泛写电影感",\n'
    '  "视角构图": "每个主要镜头的景别、视角、主体占比、画面百分比位置、横竖画幅、留白和引导线",\n'
    '  "主体动作": "主体动作按时间顺序拆解:走位、转身、摆裙、抬手、看镜头、拿商品、切换姿势等,给方向和幅度",\n'
    '  "镜头运动": "运镜方式(推/拉/摇/移/跟/环绕/升降/手持/固定),方向、速度、幅度、是否有变焦或景深变化",\n'
    '  "剪辑节奏": "镜头数量、切换节奏、每镜头大致秒数、是否卡点、是否慢动作/加速、是否循环",\n'
    '  "时序分镜": "按 0-1s、1-2s 或镜头1/2/3 写画面演变,必须对应所见帧顺序",\n'
    '  "字幕卖点": "画面中文字/logo/价格/促销/卖点文案的内容、位置、字体风格、颜色;若无则填 无",\n'
    '  "时长建议": "建议生成时长、帧率感、是否可扩展为长视频循环段落",\n'
    '  "光线": "主光/辅光方向、软硬、色温、阴影形状、反光、高光、是否随镜头变化",\n'
    '  "色调配色": "主色/辅色/点缀色的色值和画面占比、冷暖、饱和度、对比度、调色滤镜",\n'
    '  "材质纹理": "服装/商品/皮肤/背景主要材质与质感,包括布料垂坠、反光、粗糙度、颗粒",\n'
    '  "氛围情绪": "广告气质和情绪:高级/甜美/通勤/轻奢/活力/松弛等,必须贴合画面",\n'
    '  "转场": "转场方式:硬切/闪白/遮挡/变焦/动作匹配/无,以及出现位置",\n'
    '  "一致性约束": "生成时必须保持不变的元素:主体数量、服装颜色版型、场景、画幅、字幕卖点、镜头顺序等",\n'
    '  "负向": "需要避免的元素:换脸、换衣服颜色、商品漂移、字幕乱字、水印、肢体畸变、闪烁、形变、镜头抖动、拼接感、不自然走路",\n'
    '  "final_text": "按同一维度顺序整合为一段可直接用于文生视频的中文提示词:参考片复刻 → 主体/商品服装/细节 → 场景/广告目标/风格 → 视角构图 → 动作和运镜 → 剪辑节奏/时序分镜/字幕卖点 → 光线/配色/材质/氛围 → 一致性约束。'
    '必须包含时间推进、镜头顺序、商品服装细节和字幕卖点约束,不得与上述字段矛盾;末尾追加英文视频关键词,控制在 280-420 个中文字符。"\n'
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
        submit_state_unknown: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        self.submit_state_unknown = submit_state_unknown


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
    host = (urlparse(url).hostname or "").lower()
    return not (settings.debug or host in settings.trusted_egress_host_list)


@contextmanager
def _guarded_stream(client: httpx.Client, method: str, url: str, **kwargs):
    if _pin_required(url):
        with pinned_client(
            url,
            follow_redirects=False,
            timeout=kwargs.pop("timeout", client.timeout),
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
          config: RuntimeGatewayConfig | None = None) -> dict:
    url = _join_api_path(config, path)
    r = _request("POST", url,
                 headers={**_auth(config), "Content-Type": "application/json"},
                 json=payload, timeout=timeout or settings.gateway_timeout_seconds,
                 retries=settings.gateway_max_retries)
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
    data = _post("/chat/completions", payload, timeout=90, config=gateway_config)
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
    """Fallback final prompt when the model omits final_text.

    Keep the same dimension order as the reverse prompt so the user-facing
    analysis and the generated prompt describe one coherent target.
    """
    neg = obj.get("负向")
    order = [
        "主体", "细节特征", "场景背景", "风格", "景别", "构图", "视角镜头", "视角构图",
        "主体动作", "镜头运动", "运动节奏", "时序分镜", "时长建议", "光线", "色调配色",
        "材质纹理", "氛围情绪", "后期质感", "转场", "文字水印", "标签",
    ]
    used: set[str] = set()
    parts: list[str] = []
    for key in order:
        value = obj.get(key)
        if value and key not in ("负向", "final_text"):
            parts.append(f"{key}: {value}")
            used.add(key)
    for key, value in obj.items():
        if key not in used and key not in ("负向", "final_text") and value:
            parts.append(f"{key}: {value}")
    text = "；".join(parts)
    if neg:
        text += f"；避免: {neg}"
    return text or "same style, high quality"


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
    return bool(exc.transient and exc.status_code in (429, 500, 502, 503, 504))


def _post_single_image_repeated(path: str, payload: dict, n: int,
                                config: RuntimeGatewayConfig | None = None) -> list[bytes]:
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
                return _decode_image_response(data)
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
                if time.monotonic() > deadline:
                    raise GatewayError("下载结果超时")
                with _guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=timeout,
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
                if time.monotonic() > deadline:
                    raise GatewayError("下载结果超时")
                with _guarded_stream(
                    c,
                    "GET",
                    url,
                    timeout=timeout,
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
                    if allowed_content_types and content_type and not any(
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
    if settings.effective_mock_mode:
        return storage.save_bytes(
            download_bytes_limited(
                url,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
                allowed_content_types=allowed_content_types,
            ),
            subdir,
            ext,
        )
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


_VIDEO_STATUS = {
    "succeeded": "succeeded", "success": "succeeded", "completed": "succeeded",
    "done": "succeeded",
    "failed": "failed", "error": "failed", "cancelled": "failed", "canceled": "failed",
    "expired": "failed",
    "running": "running", "processing": "running", "in_progress": "running",
    "generating": "running",
    "queued": "queued", "pending": "queued", "submitted": "queued",
}


_GENERIC_VIDEO_ALLOWED_PARAMS = {
    "duration",
    "resolution",
    "ratio",
    "seed",
    "request_id",
    "negative_prompt",
    "prompt_extend",
}


def _generic_video_payload_params(params: dict, extra: dict) -> dict:
    allowed = set(_GENERIC_VIDEO_ALLOWED_PARAMS)
    allowed.update(str(k) for k in (extra.get("allowed_param_fields") or []))
    payload_params = {
        k: v
        for k, v in dict(params or {}).items()
        if k in allowed and not str(k).startswith("_") and v not in (None, "")
    }
    first_frame = (params or {}).get("first_frame_image")
    first_frame_field = extra.get("first_frame_field", "first_frame_image")
    if first_frame and first_frame_field:
        payload_params[str(first_frame_field)] = first_frame
    return payload_params


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


def _nested_video_url(value) -> str | None:
    if isinstance(value, dict):
        return (
            value.get("url")
            or value.get("video_url")
            or value.get("download_url")
        )
    if isinstance(value, list) and value:
        for item in value:
            url = _nested_video_url(item)
            if url:
                return url
    return None


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
