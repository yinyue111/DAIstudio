"""Prompt compaction and fidelity guards for generation tasks."""
from __future__ import annotations

import re

from ..models import GenTask

PRODUCT_FIDELITY_GUARD = (
    "产品高保真硬约束：上传产品图是唯一产品身份来源，产品主体、Logo、包装结构、品牌色、形状、"
    "材质、比例、标签版式、表面纹理和所有可见文字必须完整保留；包装上的品牌名、Logo、中文、"
    "英文、韩文、数字、装饰图案、标签位置和排版必须逐字逐形保持原图，不得翻译、改写、补写、"
    "删减、重排、风格化、模糊或替换。只允许改变背景、台面、道具、光线、构图、阴影和广告质感；"
    "图片生成时产品必须作为完整主体清晰入镜，包装正面、顶部、底部、左右边缘、抽口、盖子、提手、"
    "外盒轮廓和所有关键结构都必须可见且连续；画面四周保留安全边距，不得裁切产品、生成半截产品、"
    "缺失边角、压扁盒体、破坏开口结构，或让草叶、水花、道具、前景虚化遮挡包装和文字；"
    "产品边缘必须自然融入真实拍摄场景，不得出现贴纸式白边、抠图描边、剪纸轮廓或悬浮贴片感；"
    "视频生成时必须用上传产品替换参考片中的原主体、原商品、原品牌或人物，参考片只提供镜头语言、"
    "展示节奏、可迁移动作、场景和广告质感；视频镜头需让产品文字面尽量正对镜头，完整包装、Logo"
    "和主要文字应始终留在画面内，避免快速旋转、侧面展示、裁切包装、强运动模糊、遮挡包装文字或"
    "产品正面离焦；如风格迁移与产品保真冲突，优先保证产品和包装文字不变。"
)
PORTRAIT_FIDELITY_GUARD = (
    "人像高保真硬约束：上传人像照片是唯一人物身份来源，必须完整保留同一个人的脸型、五官比例、"
    "眼睛、鼻子、嘴型、发际线、发型特征、肤色、年龄感、性别和可识别身份；同时保持参考人物在"
    "服装覆盖下可见的整体身体轮廓，包括肩宽、胸廓饱满度、躯干长度、自然腰线、胯宽、四肢比例、"
    "姿态重心和胸腰胯过渡，不得主动瘦身、增大、缩小、拉长或重塑体型；不得混合两个人的长相，"
    "不得改变面部结构、年龄、性别或关键身份特征。若另有独立风格参考素材，不得混入其中的人物身份；"
    "独立风格参考只用于迁移场景、构图、光线、色调、服化道、动作节奏、镜头语言和广告质感；整体表达必须保持"
    "成年、自然、得体、专业商业人像或角色设定语境，避免未成年感、夸张身体展示姿态、身体局部凝视、"
    "私密成人化意图；严格复刻原图主光方向、软硬程度、明暗对比、脸部与服装高光落点、补光关系、"
    "阴影浓度、暗部色彩层次、材质反射和柔雾/高光扩散强度；如风格迁移与人物保真冲突，优先保证"
    "人物身份、身体轮廓比例、面部结构、自然表情和原图光影关系稳定。"
)
GENERATION_PROMPT_MIN_CHARS = 1000
GENERATION_PROMPT_MAX_CHARS = 1500
CANONICAL_FINAL_TEXT_MIN_CHARS = 80
_GENERATION_PROMPT_KEEP_KEYS = (
    "图像类型", "反推重点", "主体", "人像意图", "人物比例", "身材体态", "身材曲线",
    "体态线条", "光线", "材质纹理", "后期质感", "服装结构", "服装覆盖", "妆发五官",
    "色调配色", "商品服装", "细节特征", "场景背景", "广告目标", "风格", "构图", "景别",
    "视角镜头", "视角构图", "主体动作", "镜头运动", "剪辑节奏", "时序分镜",
    "文字版式", "氛围情绪",
    "平台质感", "一致性约束", "标签",
)
_GENERATION_PROMPT_DROP_KEYS = {"尺码三围", "露肤度", "负向"}
_STYLE_TRANSFER_KEEP_KEYS = (
    "场景背景", "广告目标", "风格", "构图", "景别", "视角镜头", "视角构图",
    "可迁移主体动作", "主体动作", "产品展示方式", "镜头运动", "剪辑节奏", "时序分镜", "字幕卖点", "光线", "色调配色",
    "氛围情绪", "后期质感", "平台质感", "转场", "时长建议",
)
_STYLE_TRANSFER_MOTION_KEYS = {"可迁移主体动作", "主体动作", "产品展示方式"}
_REFERENCE_PRODUCT_NOUN_RE = re.compile(
    r"KaHi|Estee\s+Lauder|Advanced\s+Night\s+Repair|Eau\s+de\s+Toilette|"
    r"skincare\s+bottle|dropper\s+bottle|bottle|香水瓶|香水|瓶身|瓶盖|滴管瓶|护肤瓶|"
    r"参考商品|参考产品|原商品|原产品",
    re.IGNORECASE,
)
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
    (re.compile(r"露肤度[:：]?\s*[^；。,\n]*[；。,\n]?"), ""),
    (
        re.compile(
            r"(?:胸围\s*[:：]?\s*)?\d{1,3}(?:\.\d+)?\s*(?:cm|厘米)\s*[/／,，、]\s*"
            r"(?:腰围\s*[:：]?\s*)?\d{1,3}(?:\.\d+)?\s*(?:cm|厘米)\s*[/／,，、]\s*"
            r"(?:臀围\s*[:：]?\s*)?\d{1,3}(?:\.\d+)?\s*(?:cm|厘米)\b",
            re.IGNORECASE,
        ),
        "",
    ),
    (re.compile(r"胸围/腰围/臀围|胸围|腰围|臀围|三围|罩杯|胸大臀翘|翘臀|乳沟"), "整体体态比例"),
    (re.compile(r"性感化|性感|诱惑|挑逗|勾引|撩人|火辣|擦边|成人写真|情趣"), "成熟得体的商业人像气质"),
    (re.compile(r"胸部特写|臀部特写|身体局部特写|突出胸部|突出臀部|私密部位"), "避免身体局部凝视"),
    (re.compile(r"高露肤|大面积露肤|裸露|半裸|暴露|低胸|透视装|湿身诱惑|衣服滑落"), "服装覆盖自然得体"),
    (re.compile(r"少女感|萝莉|幼态性感|可爱性感|青春诱惑"), "成年、成熟自然、不幼态"),
    (re.compile(r"低机位展示"), "平视或自然时尚摄影视角"),
    (re.compile(r"暧昧灯光|私密暧昧|昏暗暧昧"), "柔和明亮的情绪光线"),
    (re.compile(r"画面百分比坐标|百分比坐标"), "画面位置"),
    (re.compile(r"\s+"), " "),
)

PRODUCT_VIDEO_TEXT_NEGATIVE_TERMS = (
    "包装文字乱码",
    "Logo扭曲",
    "伪文字",
    "错字",
    "品牌名变化",
    "包装文字被改写",
    "文字被翻译",
    "标签不可读",
    "产品正面离焦",
    "运动模糊遮挡文字",
    "快速旋转导致文字不可读",
    "侧面展示导致文字不可读",
    "裁切包装或Logo",
    "产品主体出画",
)
PRODUCT_IMAGE_NEGATIVE_TERMS = (
    "产品残缺",
    "半截产品",
    "产品被裁切",
    "产品主体出画",
    "只显示产品局部",
    "包装边缘缺失",
    "顶部缺失",
    "底部缺失",
    "左右边缘缺失",
    "抽口缺失",
    "盖子缺失",
    "提手缺失",
    "盒体破损",
    "盒体压扁",
    "包装结构改变",
    "产品变形",
    "产品比例失真",
    "产品被草叶遮挡",
    "产品被前景遮挡",
    "道具遮挡Logo",
    "道具遮挡包装文字",
    "Logo丢失",
    "Logo扭曲",
    "包装文字乱码",
    "包装文字被改写",
    "标签不可读",
    "白色描边",
    "抠图白边",
    "贴纸边框",
    "白色光晕",
    "剪纸边缘",
    "悬浮贴纸感",
)
PORTRAIT_IMAGE_BASE_NEGATIVE_TERMS = (
    "主动瘦身",
    "躯干拉长",
    "颈部拉长",
    "腿部拉长",
    "胸廓-腰线-胯部比例改变",
)
_LOCKED_PRODUCT_VIDEO_USER_REWRITES = (
    (
        re.compile(r"(?:产品|主体|包装)?(?:缓慢|轻微|慢速)?(?:旋转展示|旋转|转动|环绕|绕拍|侧面展示|翻转)"),
        "保持产品正面文字面朝向镜头的稳定展示",
    ),
    (
        re.compile(r"(?:水花|液体|泡沫|烟雾)?(?:飞溅|泼溅|喷溅|遮挡)"),
        "背景水花或光影点缀且不遮挡包装、Logo和文字",
    ),
)
_PRODUCT_VIDEO_TEMPLATE_PROMPTS = {
    "stable_showcase": (
        "产品视频模板：稳定陈列。产品正面文字面保持朝向镜头，主体固定在画面中心，"
        "只让背景光影、台面反射、轻微景深和慢速转场产生变化。"
    ),
    "slow_push": (
        "产品视频模板：慢速推近。镜头做低速推近或轻微拉远，产品不旋转、不翻面，"
        "包装正面、Logo和主要文字保持清晰可读。"
    ),
    "handheld_display": (
        "产品视频模板：手持展示。手部只扶住产品边缘或底部，不遮挡Logo、包装文字、抽口和关键结构，"
        "动作缓慢稳定，产品始终是画面主角。"
    ),
    "background_motion": (
        "产品视频模板：背景动效。产品保持稳定完整入镜，动态主要发生在背景光线、道具、烟雾、"
        "布景或台面反射上，前景元素不得盖住包装正面。"
    ),
    "soft_splash": (
        "产品视频模板：轻水花。水花、泡沫或颗粒只能围绕产品底部和背景边缘运动，"
        "不得覆盖Logo、包装文字、正面标签和产品轮廓。"
    ),
}


def _product_video_template_prompt(template: str | None, *, locked: bool) -> str:
    key = str(template or "stable_showcase").lower().strip()
    text = _PRODUCT_VIDEO_TEMPLATE_PROMPTS.get(key) or _PRODUCT_VIDEO_TEMPLATE_PROMPTS["stable_showcase"]
    if locked:
        return text
    return (
        f"{text} 自由运动模式可以保留更多角度变化，但仍需让上传产品替换参考视频原主体，"
        "避免参考商品、人物或品牌回流。"
    )


def _truthy_subject_flag(value) -> bool:
    return str(value).lower() in {"true", "1", "yes"}


def _subject_mode_from_params(params: dict | None) -> str:
    params = params or {}
    direct_mode = str(params.get("subject_mode") or "").lower()
    trace = params.get("_source_trace")
    trace_mode = str(trace.get("subject_mode") or "").lower() if isinstance(trace, dict) else ""
    if direct_mode == "product" or trace_mode == "product":
        return "product"
    if isinstance(trace, dict) and _truthy_subject_flag(trace.get("product_generation_mode")):
        return "product"
    if direct_mode == "portrait" or trace_mode == "portrait":
        return "portrait"
    if isinstance(trace, dict) and _truthy_subject_flag(trace.get("portrait_generation_mode")):
        return "portrait"
    return ""


def _params_have_image_reference(params: dict | None) -> bool:
    params = params or {}
    return bool(params.get("reference_image_url") or params.get("character_reference_image"))


def _task_has_image_reference(task: GenTask) -> bool:
    params = task.params or {}
    if _params_have_image_reference(params):
        return True
    prompt_obj = task.prompt if isinstance(task.prompt, dict) else {}
    return bool(
        getattr(task, "source_type", None) == "image"
        and getattr(task, "source_asset_url", None)
        and prompt_obj.get("instruction")
    )


def is_product_generation_task(task: GenTask) -> bool:
    return _task_has_image_reference(task) and _subject_mode_from_params(task.params) == "product"


def is_portrait_generation_task(task: GenTask) -> bool:
    if is_product_generation_task(task):
        return False
    if not _task_has_image_reference(task):
        return False
    prompt_obj = task.prompt if isinstance(task.prompt, dict) else {}
    return (
        _subject_mode_from_params(task.params) == "portrait"
        or "人物" in str(prompt_obj.get("图像类型") or "")
    )


def product_fidelity_prompt(prompt: str, task: GenTask) -> str:
    if is_portrait_generation_task(task):
        text = str(prompt or "")
        if "人像高保真硬约束" in text:
            return _trim_generation_prompt(text)
        return _trim_generation_prompt(f"{PORTRAIT_FIDELITY_GUARD}{text}")
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


_PROMPT_FIELD_LIMITS = {
    "反推重点": 140,
    "主体": 160,
    "人像意图": 100,
    "人物比例": 150,
    "身材体态": 170,
    "身材曲线": 170,
    "体态线条": 170,
    "光线": 220,
    "材质纹理": 150,
    "后期质感": 170,
    "服装结构": 150,
    "服装覆盖": 100,
    "妆发五官": 130,
}


def _compact_prompt_field(key: str, value) -> str:
    text = _normalise_prompt_fragment(value)
    if not text:
        return ""
    chunks = [item.strip() for item in re.split(r"(?<=[。；;.!?！？])", text) if item.strip()]
    if chunks:
        unique: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            normalized = chunk.rstrip("。；;.!?！？")
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(chunk)
        text = "".join(unique)
    limit = _PROMPT_FIELD_LIMITS.get(key, 120)
    return text[:limit].rstrip(" ,，;；。")


def _meaningful_canonical_prompt(value, *, allow_short: bool = False) -> str:
    text = _normalise_prompt_fragment(value)
    if not text:
        return ""
    compact = re.sub(r"[\s,，;；。.!！?？:：]+", "", text).lower()
    if compact in {"参考图复刻", "参考片复刻", "同款复刻", "samestylehighquality"}:
        return ""
    if len(text) < CANONICAL_FINAL_TEXT_MIN_CHARS and not allow_short:
        return ""
    return text


def _explicit_canonical_matches(prompt_obj: dict, fallback: str) -> bool:
    target = _normalise_prompt_fragment(fallback)
    if not target:
        return False
    return any(
        _normalise_prompt_fragment(prompt_obj.get(key)) == target
        for key in ("instruction", "user_instruction")
    )


def _has_subject_profile(prompt_obj: dict) -> bool:
    return any(_normalise_prompt_fragment(prompt_obj.get(key)) for key in _SUBJECT_PROFILE_KEYS)


def _rewrite_transfer_motion(
    value,
    *,
    product: bool = False,
    portrait: bool = False,
    product_lock_mode: str = "locked",
    product_video_template: str = "stable_showcase",
) -> str:
    text = _normalise_prompt_fragment(value)
    if not text:
        return ""
    if product:
        if product_lock_mode != "free":
            return (
                "上传产品作为唯一视频主体，替换参考片中的原主体/原商品/人物；"
                "复用参考片的展示节奏、入镜顺序、稳定特写、慢速推拉和卖点展示等可迁移动作；"
                "文字保真模式下保持完整包装、Logo和主要文字始终在画面内，避免裁切主体、"
                "侧面展示或快速旋转；不要生成参考片里的原商品、原品牌、人物或服装。"
                f"{_product_video_template_prompt(product_video_template, locked=True)}"
            )
        return (
            "上传产品作为唯一视频主体，替换参考片中的原主体/原商品/人物；"
            "复用参考片的展示节奏、入镜顺序、角度切换、慢速推拉、稳定特写和卖点展示等可迁移动作；"
            "不要生成参考片里的原商品、原品牌、人物或服装。"
            f"{_product_video_template_prompt(product_video_template, locked=False)}"
        )
    if portrait:
        return (
            "上传人物作为唯一视频主体，替换参考片中的原人物身份；"
            "复用参考片的动作节奏、走位、姿态变化、镜头调度和分镜顺序；"
            "不要生成参考片里的原人物、人脸身份、商品品牌或文字水印。"
        )
    return text


def _rewrite_locked_product_user_instruction(value) -> str:
    text = _normalise_prompt_fragment(value)
    if not text:
        return ""
    for pattern, replacement in _LOCKED_PRODUCT_VIDEO_USER_REWRITES:
        text = pattern.sub(replacement, text)
    return (
        "用户补充要求必须服从文字保真模式：保持产品正面、完整包装、Logo和主要文字始终在画面内；"
        f"{text}"
    )


def _rewrite_product_style_fragment(key: str, value, *, is_video: bool = False) -> str:
    text = _normalise_prompt_fragment(value)
    if not text:
        return ""
    if key == "广告目标":
        return (
            "展示上传产品作为唯一商品主角，迁移参考素材的生活方式场景、消费联想、光线、色调、"
            "质感和广告氛围；不保留参考商品名称、品类、品牌、SKU或卖点。"
        )
    if key == "景别":
        return (
            "上传产品中近景/近景，完整产品主体入镜，主体占画幅约55%-75%，"
            "包装、Logo和主要文字清晰可读。"
        )
    if key in {"构图", "视角构图"}:
        cleaned = _REFERENCE_PRODUCT_NOUN_RE.sub("上传产品", text)
        return (
            "上传产品为画面唯一视觉中心，完整入镜，四周保留安全边距，包装正面、顶部、底部、"
            "左右边缘、抽口/盖子/提手和外盒轮廓都可见；主体占画幅约55%-75%。"
            f"参考构图只迁移背景留白、视线引导、景深和光影平衡：{cleaned}"
        )
    if key == "场景背景":
        cleaned = re.sub(r"前景[^；。]*覆盖[^；。]*[；。]?", "前景元素只围绕产品底部和边缘，", text)
        cleaned = _REFERENCE_PRODUCT_NOUN_RE.sub("上传产品", cleaned)
        return f"{cleaned} 前景草叶、水花、道具和虚化元素不得遮挡上传产品包装、Logo和主要文字。"
    cleaned = _REFERENCE_PRODUCT_NOUN_RE.sub("上传产品", text)
    if key in {"主体动作", "可迁移主体动作", "产品展示方式"} and not is_video:
        return "上传产品保持静态完整展示，只迁移参考素材的商业展示氛围和视觉节奏。"
    return cleaned


def _structured_generation_prompt(prompt_obj: dict, fallback: str) -> str:
    canonical = _meaningful_canonical_prompt(
        fallback,
        allow_short=_explicit_canonical_matches(prompt_obj, fallback),
    )
    if canonical:
        return canonical
    parts: list[str] = []
    for key in _GENERATION_PROMPT_KEEP_KEYS:
        value = prompt_obj.get(key)
        fragment = _compact_prompt_field(key, value)
        if fragment:
            parts.append(f"{key}: {fragment}")
    for key, value in prompt_obj.items():
        if key in _GENERATION_PROMPT_KEEP_KEYS or key in _GENERATION_PROMPT_DROP_KEYS:
            continue
        if key in {"final_text", "instruction", "negative", "negative_prompt"}:
            continue
        fragment = _compact_prompt_field(key, value)
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
    is_video: bool = False,
    product_lock_mode: str = "locked",
    product_video_template: str = "stable_showcase",
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
            fragment = _rewrite_transfer_motion(
                prompt_obj.get(key),
                product=product,
                portrait=portrait,
                product_lock_mode=product_lock_mode,
                product_video_template=product_video_template,
            )
        elif product and is_video and product_lock_mode != "free" and key == "镜头运动":
            fragment = (
                "固定正面或轻微推拉镜头，稳定展示上传产品，保持完整包装、Logo和主要文字始终在画面内，"
                "避免环绕、旋转、侧面展示、强运动模糊或裁切主体。"
                f"{_product_video_template_prompt(product_video_template, locked=True)}"
            )
        elif product:
            fragment = _rewrite_product_style_fragment(key, prompt_obj.get(key), is_video=is_video)
        else:
            fragment = _normalise_prompt_fragment(prompt_obj.get(key))
        if fragment:
            parts.append(f"{key}: {fragment}")
    for key in _USER_INSTRUCTION_KEYS:
        if product and is_video and product_lock_mode != "free":
            fragment = _rewrite_locked_product_user_instruction(prompt_obj.get(key))
        else:
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
    subject_mode = _subject_mode_from_params(params)
    image_type = str(prompt_obj.get("图像类型") or "")
    if subject_mode:
        return subject_mode == "portrait"
    return "人物" in image_type


def _is_product_prompt(prompt_obj: dict, params: dict | None) -> bool:
    subject_mode = _subject_mode_from_params(params)
    image_type = str(prompt_obj.get("图像类型") or "")
    if subject_mode:
        return subject_mode == "product"
    return "产品" in image_type


def _has_explicit_subject_mode(params: dict | None, mode: str) -> bool:
    return _subject_mode_from_params(params) == mode


def compact_generation_prompt_text(
    prompt_obj: dict,
    params: dict | None,
    fallback: str,
    *,
    is_portrait: bool | None = None,
    is_product: bool | None = None,
) -> str:
    explicit_product = is_product is True or _has_explicit_subject_mode(params, "product")
    portrait = _is_portrait_prompt(prompt_obj, params) if is_portrait is None else is_portrait
    product = _is_product_prompt(prompt_obj, params) if is_product is None else is_product
    if explicit_product:
        portrait = False
    is_video = str((params or {}).get("_category") or "").lower() == "video" or any(
        key in (params or {})
        for key in ("duration", "target_duration", "resolution", "target_resolution", "product_lock_mode")
    )
    portrait_mode = portrait and _has_explicit_subject_mode(params, "portrait")
    portrait_reference = portrait_mode and bool(
        (params or {}).get("_has_image_reference") or _params_have_image_reference(params)
    )
    canonical = _meaningful_canonical_prompt(
        fallback,
        allow_short=_explicit_canonical_matches(prompt_obj, fallback),
    )
    if canonical and not product and not (portrait_mode and _has_subject_profile(prompt_obj)):
        source = canonical
    elif product or portrait_mode:
        product_lock_mode = str((params or {}).get("product_lock_mode") or "locked").lower()
        product_video_template = str((params or {}).get("product_video_template") or "stable_showcase").lower()
        source = _style_transfer_generation_prompt(
            prompt_obj,
            fallback,
            product=product,
            portrait=portrait,
            is_video=is_video,
            product_lock_mode=product_lock_mode,
            product_video_template=product_video_template,
        )
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
        if portrait_reference:
            prefix = (
                "生成版提示词：以上传人像照片作为唯一人物身份，参考图只迁移场景、构图、光影、妆造氛围、"
                "动作与镜头语言；不得混入参考图人物身份。"
            )
        else:
            prefix = "生成版提示词：严格按反推记录复刻原图成年人物、构图、身体轮廓、服装、光影和质感。"
    else:
        prefix = (
            "生成版提示词：参考图复刻，保留主体身份、构图关系、光线方向、色调、场景和商业风格；"
            "使用自然、中性的视觉描述。"
        )
    if portrait:
        prefix += (
            "保持专业商业人像、品牌 Lookbook 或角色设定语境；复刻脸型五官、服装结构、肩宽、胸廓、"
            "躯干长度、腰线、胯宽、四肢比例、姿态重心和整体体态线条，不得主动瘦身、增大、缩小或拉长。"
            "复刻主光方向与软硬、脸部和服装"
            "高光落点、补光、阴影、暗部色彩、材质反射、柔雾和高光扩散；避免身体尺寸、身体局部凝视、"
            "服装覆盖失衡、夸张展示姿态或成人化表达。"
        )
    elif product:
        product_lock_mode = str((params or {}).get("product_lock_mode") or "locked").lower()
        if is_video and product_lock_mode != "free":
            prefix += (
                "产品生成需保持同一商品、Logo、包装结构、品牌色、文字和材质细节稳定；"
                "文字保真模式下采用固定正面、慢速轻推/轻拉、稳定特写或克制转场，"
                "保持完整包装、Logo 和主要文字始终在画面内，避免裁切主体、侧面展示、快速旋转、"
                "强运动模糊、遮挡包装文字或产品正面离焦。"
                f"{_product_video_template_prompt(product_video_template, locked=True)}"
            )
        elif is_video:
            prefix += (
                "产品生成需保持同一商品、Logo、包装结构、品牌色、文字和材质细节稳定；"
                "视频镜头让产品文字面尽量正对镜头，采用慢速推拉或克制转场，"
                "避免快速旋转、强运动模糊、遮挡包装文字、裁切主体或产品正面离焦。"
                f"{_product_video_template_prompt(product_video_template, locked=False)}"
            )
        else:
            prefix += (
                "产品生成需保持同一商品、Logo、包装结构、品牌色、文字和材质细节稳定；"
                "图片构图必须让完整产品主体入镜，包装正面、顶部、底部、左右边缘、抽口、盖子、提手"
                "和外盒轮廓全部可见，主体占画面五成五到七成五并保留安全边距；"
                "不得裁掉包装、不得只显示局部、不得让草叶/水花/道具遮挡Logo和主要文字。"
            )
    return _trim_generation_prompt(f"{prefix}{source}")


def _merge_negative_terms(value: str | None, terms: tuple[str, ...]) -> str:
    parts = [
        item.strip()
        for item in re.split(r"[,，、\n]", str(value or ""))
        if item.strip()
    ]
    seen = {item.lower() for item in parts}
    for item in terms:
        if item.lower() not in seen:
            seen.add(item.lower())
            parts.append(item)
    return "，".join(parts)


def product_image_negative_prompt(value: str | None = None) -> str:
    """Merge product-completeness guards into image negative prompts."""
    cleaned = _REFERENCE_PRODUCT_NOUN_RE.sub("上传产品", str(value or ""))
    return _merge_negative_terms(cleaned, PRODUCT_IMAGE_NEGATIVE_TERMS)


def portrait_image_negative_prompt(value: str | None = None, prompt: str | None = None) -> str:
    """Merge portrait fidelity guards that are supported by the active prompt."""
    prompt_text = str(prompt or "")
    terms = list(PORTRAIT_IMAGE_BASE_NEGATIVE_TERMS)
    if re.search(r"后仰|斜向|斜坐|对角构图|对角线构图", prompt_text, re.IGNORECASE):
        terms.append("直立居中姿态")
    if re.search(r"低机位|仰拍|近距离透视|前景.{0,12}放大|透视.{0,12}缩短", prompt_text, re.IGNORECASE):
        terms.append("前景透视丢失")
    wedding_rejected = re.search(
        r"非婚纱|不是婚纱|避免.{0,8}婚纱|不得.{0,8}婚纱",
        prompt_text,
        re.IGNORECASE,
    )
    wedding_requested = re.search(
        r"白色婚纱|婚纱礼服|新娘|bridal|wedding\s*dress",
        prompt_text,
        re.IGNORECASE,
    )
    if wedding_rejected or not wedding_requested:
        terms.append("通用婚纱蕾丝")
    light_contacts_rejected = re.search(
        r"无美瞳|非美瞳|不得.{0,12}(?:浅色|美瞳|隐形眼镜)|"
        r"避免.{0,12}(?:浅色|美瞳|隐形眼镜)|深色.{0,6}(?:眼睛|虹膜)|"
        r"(?:眼睛|虹膜).{0,6}深色",
        prompt_text,
        re.IGNORECASE,
    )
    light_contacts_requested = re.search(
        r"浅色(?:隐形眼镜|美瞳|眼睛|虹膜)|"
        r"(?:蓝色|绿色|灰色|琥珀色|金色)(?:眼睛|虹膜|美瞳)|"
        r"light[-\s]?(?:colored\s+)?eyes|blue\s+eyes|green\s+eyes|gray\s+eyes",
        prompt_text,
        re.IGNORECASE,
    )
    if light_contacts_rejected or not light_contacts_requested:
        terms.append("浅色隐形眼镜")
    if re.search(r"柔光|大面积光源|低对比|柔和阴影", prompt_text, re.IGNORECASE):
        terms.extend(("硬质影棚光", "平坦阴影"))
    if re.search(r"暗部.{0,16}层次|抬升黑位|抬起黑位|暗部不死黑", prompt_text, re.IGNORECASE):
        terms.append("暗部死黑")
    if re.search(r"柔雾|柔焦|soft\s*focus|雾化", prompt_text, re.IGNORECASE):
        terms.append("柔雾丢失")
    if re.search(r"光晕|高光扩散|bloom|halation", prompt_text, re.IGNORECASE):
        terms.append("光晕丢失")
    if re.search(r"非\s*HDR|non[-\s]?HDR", prompt_text, re.IGNORECASE):
        terms.append("HDR")
    if re.search(r"低锐化|低锐度|中低锐度|低微对比|柔焦", prompt_text, re.IGNORECASE):
        terms.append("硬锐化")
    return _merge_negative_terms(value, tuple(terms))


def product_video_negative_prompt(value: str | None = None) -> str:
    """Merge cheap product-text fidelity guards into video negative prompts."""
    cleaned = _REFERENCE_PRODUCT_NOUN_RE.sub("上传产品", str(value or ""))
    return _merge_negative_terms(cleaned, PRODUCT_VIDEO_TEXT_NEGATIVE_TERMS)


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
    params["_has_image_reference"] = _task_has_image_reference(task)
    explicit_portrait = is_portrait_generation_task(task)
    return compact_generation_prompt_text(
        task.prompt or {},
        params,
        prompt,
        is_portrait=True if explicit_portrait else None,
        is_product=is_product_generation_task(task),
    )
