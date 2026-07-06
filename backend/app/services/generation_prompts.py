"""Prompt compaction and fidelity guards for generation tasks."""
from __future__ import annotations

import re

from ..models import GenTask

PRODUCT_FIDELITY_GUARD = (
    "产品高保真硬约束：上传产品图是唯一产品身份来源，产品主体、Logo、包装结构、品牌色、形状、"
    "材质、比例、标签版式、表面纹理和所有可见文字必须完整保留；包装上的品牌名、Logo、中文、"
    "英文、韩文、数字、装饰图案、标签位置和排版必须逐字逐形保持原图，不得翻译、改写、补写、"
    "删减、重排、风格化、模糊或替换。只允许改变背景、台面、道具、光线、构图、阴影和广告质感；"
    "视频生成时必须用上传产品替换参考片中的原主体、原商品、原品牌或人物，参考片只提供镜头语言、"
    "展示节奏、可迁移动作、场景和广告质感；如风格迁移与产品保真冲突，优先保证产品和包装文字不变。"
)
PORTRAIT_FIDELITY_GUARD = (
    "人像高保真硬约束：上传人像照片是唯一人物身份来源，必须完整保留同一个人的脸型、五官比例、"
    "眼睛、鼻子、嘴型、发际线、发型特征、肤色、年龄感、性别、体态和可识别身份；不得替换成参考"
    "素材中的人物，不得混合两个人的长相，不得改变面部结构、年龄、性别或关键身份特征。只允许迁移"
    "参考素材的场景、构图、光线、色调、服化道、动作节奏、镜头语言和广告质感；整体表达必须保持"
    "成年、自然、得体、专业商业人像或角色设定语境，避免未成年感、夸张身体展示姿态、身体局部凝视、"
    "私密成人化意图；如风格迁移与人物身份保真冲突，优先保证人物身份、面部结构和自然表情稳定。"
)
GENERATION_PROMPT_MIN_CHARS = 1000
GENERATION_PROMPT_MAX_CHARS = 1500
_GENERATION_PROMPT_KEEP_KEYS = (
    "图像类型", "反推重点", "主体", "人像意图", "人物比例", "身材体态",
    "体态线条", "服装结构", "服装覆盖", "妆发五官",
    "商品服装", "细节特征", "场景背景", "广告目标", "风格", "构图", "景别",
    "视角镜头", "视角构图", "主体动作", "镜头运动", "剪辑节奏", "时序分镜",
    "光线", "色调配色", "材质纹理", "文字版式", "氛围情绪", "后期质感",
    "平台质感", "一致性约束", "标签",
)
_GENERATION_PROMPT_DROP_KEYS = {"身材曲线", "尺码三围", "露肤度", "负向"}
_STYLE_TRANSFER_KEEP_KEYS = (
    "场景背景", "广告目标", "风格", "构图", "景别", "视角镜头", "视角构图",
    "可迁移主体动作", "主体动作", "产品展示方式", "镜头运动", "剪辑节奏", "时序分镜", "字幕卖点", "光线", "色调配色",
    "氛围情绪", "后期质感", "平台质感", "转场", "时长建议",
)
_STYLE_TRANSFER_MOTION_KEYS = {"可迁移主体动作", "主体动作", "产品展示方式"}
_USER_INSTRUCTION_KEYS = ("user_instruction", "补充要求", "编辑要求", "生成要求")
_SUBJECT_PROFILE_KEYS = (
    "产品身份档案",
    "人物身份档案",
    "主体身份档案",
    "product_profile_text",
    "subject_profile_summary",
)
_GENERATION_PROMPT_SENSITIVE_PATTERNS = (
    (re.compile(r"尺码三围[:：]?\s*[^；。,\n]*[；。,\n]?"), ""),
    (re.compile(r"身材曲线[:：]?\s*[^；。,\n]*[；。,\n]?"), ""),
    (re.compile(r"露肤度[:：]?\s*[^；。,\n]*[；。,\n]?"), ""),
    (re.compile(r"胸围/腰围/臀围|胸围|腰围|臀围|三围|罩杯|胸大臀翘|翘臀|乳沟"), "整体体态比例"),
    (re.compile(r"性感化|性感|诱惑|挑逗|勾引|撩人|火辣|擦边|成人写真|情趣"), "成熟得体的商业人像气质"),
    (re.compile(r"胸部特写|臀部特写|身体局部特写|突出胸部|突出臀部|私密部位"), "避免身体局部凝视"),
    (re.compile(r"高露肤|大面积露肤|裸露|半裸|暴露|低胸|透视装|湿身诱惑|衣服滑落"), "服装覆盖自然得体"),
    (re.compile(r"少女感|萝莉|幼态性感|可爱性感|青春诱惑"), "成年、成熟自然、不幼态"),
    (re.compile(r"低机位仰拍|低机位展示"), "平视或自然时尚摄影视角"),
    (re.compile(r"暧昧灯光|私密暧昧|昏暗暧昧"), "柔和明亮的情绪光线"),
    (re.compile(r"画面百分比坐标|百分比坐标"), "画面位置"),
    (re.compile(r"\d{1,3}(?:\.\d+)?\s*[%％]"), ""),
    (re.compile(r"#[0-9A-Fa-f]{6}"), ""),
    (re.compile(r"\s+"), " "),
)


def is_product_generation_task(task: GenTask) -> bool:
    params = task.params or {}
    if str(params.get("subject_mode") or "").lower() == "product":
        return True
    trace = (task.params or {}).get("_source_trace")
    if not isinstance(trace, dict):
        return False
    return str(trace.get("product_generation_mode")).lower() in {"true", "1", "yes"}


def _is_portrait_generation_task(task: GenTask) -> bool:
    params = task.params or {}
    if str(params.get("subject_mode") or "").lower() == "portrait":
        return True
    trace = params.get("_source_trace")
    if not isinstance(trace, dict):
        return False
    return (
        str(trace.get("portrait_generation_mode")).lower() in {"true", "1", "yes"}
        or str(trace.get("subject_mode") or "").lower() == "portrait"
    )


def product_fidelity_prompt(prompt: str, task: GenTask) -> str:
    if _is_portrait_generation_task(task):
        text = str(prompt or "")
        if "人像高保真硬约束" in text:
            return text
        return f"{PORTRAIT_FIDELITY_GUARD}{text}"
    if not is_product_generation_task(task):
        return prompt
    text = str(prompt or "")
    if "产品高保真硬约束" in text:
        return text
    return f"{PRODUCT_FIDELITY_GUARD}{text}"


def _normalise_prompt_fragment(value) -> str:
    text = str(value or "").strip()
    if not text or text in {"无", "未见", "不确定", "不适用"}:
        return ""
    for pattern, replacement in _GENERATION_PROMPT_SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip(" ,，;；。")


def _rewrite_transfer_motion(value, *, product: bool = False, portrait: bool = False) -> str:
    text = _normalise_prompt_fragment(value)
    if not text:
        return ""
    if product:
        return (
            "上传产品作为唯一视频主体，替换参考片中的原主体/原商品/人物；"
            "复用参考片的展示节奏、入镜顺序、角度切换、旋转、推进、环绕、特写和卖点展示等可迁移动作；"
            "不要生成参考片里的原商品、原品牌、人物或服装。"
        )
    if portrait:
        return (
            "上传人物作为唯一视频主体，替换参考片中的原人物身份；"
            "复用参考片的动作节奏、走位、姿态变化、镜头调度和分镜顺序；"
            "不要生成参考片里的原人物、人脸身份、商品品牌或文字水印。"
        )
    return text


def _structured_generation_prompt(prompt_obj: dict, fallback: str) -> str:
    parts: list[str] = []
    for key in _GENERATION_PROMPT_KEEP_KEYS:
        value = prompt_obj.get(key)
        fragment = _normalise_prompt_fragment(value)
        if fragment:
            parts.append(f"{key}: {fragment}")
    for key, value in prompt_obj.items():
        if key in _GENERATION_PROMPT_KEEP_KEYS or key in _GENERATION_PROMPT_DROP_KEYS:
            continue
        if key in {"final_text", "instruction", "negative", "negative_prompt"}:
            continue
        fragment = _normalise_prompt_fragment(value)
        if fragment:
            parts.append(f"{key}: {fragment}")
    base = "；".join(parts)
    if not base:
        base = _normalise_prompt_fragment(fallback)
    return base


def _style_transfer_generation_prompt(
    prompt_obj: dict,
    fallback: str,
    *,
    product: bool = False,
    portrait: bool = False,
) -> str:
    """Keep only transferable visual style fields for product/person edits.

    Reverse prompts for a reference image often contain the reference product
    brand, SKU, person identity, and English tags. In edit/product modes those
    fields describe the style reference, not the user's uploaded subject, so
    they must not be sent to the image model.
    """
    parts: list[str] = []
    if product or portrait:
        for key in _SUBJECT_PROFILE_KEYS:
            fragment = _normalise_prompt_fragment(prompt_obj.get(key))
            if fragment:
                parts.append(f"{key}: {fragment}")
    for key in _STYLE_TRANSFER_KEEP_KEYS:
        if key in _STYLE_TRANSFER_MOTION_KEYS and (product or portrait):
            fragment = _rewrite_transfer_motion(prompt_obj.get(key), product=product, portrait=portrait)
        else:
            fragment = _normalise_prompt_fragment(prompt_obj.get(key))
        if fragment:
            parts.append(f"{key}: {fragment}")
    for key in _USER_INSTRUCTION_KEYS:
        fragment = _normalise_prompt_fragment(prompt_obj.get(key))
        if fragment:
            parts.append(f"用户补充要求: {fragment}")
    base = "；".join(parts)
    if base:
        return base
    return _normalise_prompt_fragment(fallback)


def _trim_generation_prompt(text: str, *, max_chars: int = GENERATION_PROMPT_MAX_CHARS) -> str:
    value = _normalise_prompt_fragment(text)
    if len(value) <= max_chars:
        return value
    sentences = re.split(r"(?<=[。；;.!?！？])", value)
    out = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(out) + len(sentence) > max_chars:
            break
        out += sentence
    if len(out) >= GENERATION_PROMPT_MIN_CHARS:
        return out.strip()
    return value[:max_chars].rstrip(" ,，;；。")


def _is_portrait_prompt(prompt_obj: dict, params: dict | None) -> bool:
    subject_mode = str((params or {}).get("subject_mode") or "").lower()
    image_type = str(prompt_obj.get("图像类型") or "")
    source_trace = (params or {}).get("_source_trace")
    trace_mode = ""
    if isinstance(source_trace, dict):
        trace_mode = str(source_trace.get("subject_mode") or "").lower()
    return subject_mode == "portrait" or trace_mode == "portrait" or "人物" in image_type


def _is_product_prompt(prompt_obj: dict, params: dict | None) -> bool:
    subject_mode = str((params or {}).get("subject_mode") or "").lower()
    image_type = str(prompt_obj.get("图像类型") or "")
    source_trace = (params or {}).get("_source_trace")
    trace_mode = ""
    if isinstance(source_trace, dict):
        trace_mode = str(source_trace.get("subject_mode") or "").lower()
    return subject_mode == "product" or trace_mode == "product" or "产品" in image_type


def compact_generation_prompt_text(
    prompt_obj: dict,
    params: dict | None,
    fallback: str,
    *,
    is_portrait: bool | None = None,
    is_product: bool | None = None,
) -> str:
    portrait = _is_portrait_prompt(prompt_obj, params) if is_portrait is None else is_portrait
    product = _is_product_prompt(prompt_obj, params) if is_product is None else is_product
    is_video = str((params or {}).get("_category") or "").lower() == "video" or any(
        key in (params or {})
        for key in ("duration", "target_duration", "resolution", "target_resolution", "product_lock_mode")
    )
    if product or portrait:
        source = _style_transfer_generation_prompt(prompt_obj, fallback, product=product, portrait=portrait)
    else:
        source = _structured_generation_prompt(prompt_obj, fallback)
    if not source:
        source = str(fallback or "")
    if product:
        if is_video:
            prefix = (
                "生成版提示词：以上传产品图作为唯一商品主体和视频主角，用上传产品替换参考视频里的原主体、"
                "原商品、人物或品牌；参考视频仅迁移场景、构图、镜头运动、剪辑节奏、展示动作、光线、"
                "色调、背景道具和商业广告质感；不要生成参考视频里的商品、品牌、Logo、包装、人物或文字。"
            )
        else:
            prefix = (
                "生成版提示词：以上传产品图作为唯一商品主体，参考图仅迁移场景、构图、光线、色调、"
                "背景道具和商业广告质感；不要生成参考图里的商品、品牌、Logo、包装或文字。"
            )
    elif portrait:
        prefix = (
            "生成版提示词：以上传人像照片作为唯一人物身份，参考图仅迁移场景、构图、光线、色调、"
            "妆造氛围、服装结构、动作节奏、镜头语言和商业质感；不要生成参考图里的人脸身份或具体人物。"
        )
    else:
        prefix = (
            "生成版提示词：参考图复刻，保留主体身份、构图关系、光线方向、色调、场景和商业风格；"
            "使用自然、中性的视觉描述。"
        )
    if portrait:
        prefix += (
            "人像输出必须明确成年、自然、得体、专业商业人像/品牌 Lookbook/角色设定语境；"
            "重点描述脸型五官、发型、妆容、姿态、服装结构、体态比例、整体体态线条、镜头氛围和光影；"
            "避免身体尺寸、三围、罩杯、服装覆盖失衡、湿身成人化、夸张身体展示姿态、低角度身体凝视、"
            "私密成人化或成人化表达。"
        )
    elif product:
        prefix += "产品生成需保持同一商品、Logo、包装结构、品牌色、文字和材质细节稳定。"
    return _trim_generation_prompt(f"{prefix}{source}")


def compact_image_prompt_payload(prompt: dict, params: dict | None = None) -> dict:
    """Return a copy whose generation text is safe-sized for image gateways."""
    if not prompt:
        return prompt
    out = dict(prompt)
    fallback = str(out.get("final_text") or out.get("instruction") or "")
    has_reverse_shape = bool(out.get("final_text")) or any(
        key in out for key in _GENERATION_PROMPT_KEEP_KEYS + tuple(_GENERATION_PROMPT_DROP_KEYS)
    )
    if not has_reverse_shape:
        return out
    compact = compact_generation_prompt_text(out, params or {}, fallback)
    if compact:
        out["final_text"] = compact
        if out.get("instruction"):
            out["instruction"] = compact
    return out


def generation_prompt_for_model(prompt: str, task: GenTask) -> str:
    """Compact reverse-analysis text into a generation-safe image prompt."""
    params = dict(task.params or {})
    params["_category"] = task.category
    return compact_generation_prompt_text(
        task.prompt or {},
        params,
        prompt,
        is_portrait=_is_portrait_generation_task(task),
        is_product=is_product_generation_task(task),
    )
