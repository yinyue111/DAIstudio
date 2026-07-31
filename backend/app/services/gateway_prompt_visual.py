"""Visual-clause cleanup, evidence gates, and prompt composition."""
from __future__ import annotations

import re
from math import isfinite

_VISUAL_FIELD_ORDERS: dict[str, tuple[str, ...]] = {
    "image": (
        "主体", "人像意图", "人物比例", "身材体态", "体态线条", "服装结构", "服装覆盖",
        "妆发五官", "商品服装", "细节特征", "场景背景", "风格", "景别", "构图",
        "视角镜头", "光线", "色调配色", "材质纹理", "文字版式", "氛围情绪", "后期质感",
        "一致性约束",
    ),
    "video": (
        "主体", "人像意图", "人物比例", "身材体态", "体态线条", "服装结构", "服装覆盖",
        "妆发五官", "商品服装", "细节特征", "场景背景", "广告目标", "风格", "视角构图",
        "光线", "色调配色", "材质纹理", "氛围情绪", "一致性约束",
    ),
    "product_profile": (
        "产品品类", "品牌Logo", "包装文字", "包装结构", "主色材质", "形状比例", "关键图案",
        "卖点摘要", "展示角度", "主角约束", "不可改项", "可迁移项",
    ),
    "portrait_profile": (
        "年龄语境", "脸型五官", "妆发", "肤质", "体型比例", "姿态表情", "服装", "配饰",
        "身份稳定特征", "不可改项", "可调整项",
    ),
    "image_to_video": (
        "主体", "主体运动设计", "镜头运动设计", "时序设计", "静态观察", "场景背景",
        "视角构图", "光线", "色调配色", "材质纹理", "可动元素", "一致性约束",
    ),
}
_VISUAL_SHOT_FIELDS = (
    "visual", "subject_tracking", "pose", "action", "camera", "lighting", "transition",
)
_EMPTY_VIDEO_SHOT_RE = re.compile(r"^(?:纯黑(?:画面)?|黑屏|空白(?:画面)?|无画面)$")
_PRODUCT_HERO_SHOT_RE = re.compile(
    r"完整|全貌|正面|居中|矗立|直立|陈列|主视觉|hero\s*shot",
    re.IGNORECASE,
)
_PRODUCT_DETAIL_SHOT_RE = re.compile(
    r"瓶盖|瓶身|滴管|包装|标签|Logo|标志|材质|玻璃|金属|特写|微距|水滴",
    re.IGNORECASE,
)
_VISUAL_PROMPT_PREFIXES = {
    "product_profile": "上传产品是唯一商品主角",
    "portrait_profile": "保持上传人物身份稳定",
    "image_to_video": "基于单图新设计运动",
}
_VISUAL_PROMPT_LIMITS = {
    "image": 420,
    "video": 220,
    "product_profile": 420,
    "portrait_profile": 420,
    "image_to_video": 280,
}
_VISUAL_CLAUSE_LIMITS = {
    "image": 56,
    "video": 64,
    "product_profile": 72,
    "portrait_profile": 72,
    "image_to_video": 64,
}
_IMAGE_VISUAL_FIELD_LIMITS = {
    "主体": 48,
    "商品服装": 52,
    "妆发五官": 48,
    "服装结构": 48,
    "人物比例": 42,
    "身材体态": 42,
    "场景背景": 60,
    "构图": 50,
    "光线": 58,
    "色调配色": 48,
    "视角镜头": 48,
    "景别": 28,
    "风格": 32,
    "材质纹理": 40,
    "文字版式": 48,
    "氛围情绪": 28,
    "后期质感": 36,
    "一致性约束": 48,
    "细节特征": 36,
}
_VISUAL_PLACEHOLDER_VALUES = frozenset({
    "无",
    "暂无",
    "无相关内容",
    "不适用",
    "未见",
    "未见明确卖点",
    "未见明确广告目标",
    "不确定",
    "未知",
    "未分析",
    "未支持",
    "无法确认",
    "无法判断",
    "证据不足",
    "不清晰",
    "看不清",
    "未识别",
})
_VISUAL_PLACEHOLDER_SEPARATORS = ("/", "|", "、", ",", "，", ";", "；", "或")
_VISUAL_REFERENCE_LABEL_RE = re.compile(
    r"(?:第\s*[一二三四五六七八九十\d]+\s*张\s*)?"
    r"参考\s*(?:图|图片|素材)?\s*[一二三四五六七八九十\d]+\s*"
    r"(?:中(?!央)|为|呈现|采用|的)?\s*",
    re.IGNORECASE,
)
_VISUAL_PLATFORM_WORDS = (
    r"(?:小红书|抖音|tiktok|instagram|pinterest|"
    r"社(?:交)?媒体(?:品牌)?(?:广告)?素材|社媒(?:品牌)?(?:广告)?素材|"
    r"品牌网页广告|网页广告|电商(?:详情页|主图|海报|素材|包装视觉升级)|"
    r"发布平台|发布渠道|平台归因)"
)
_VISUAL_PLATFORM_WORD_RE = re.compile(_VISUAL_PLATFORM_WORDS, re.IGNORECASE)
# 平台词后紧跟水印/字样/logo 属于画面可见事实（"右下角有小红书水印字样"），
# 是复刻或去水印决策需要的观察，整句保留，不按归因处理。
_VISUAL_PLATFORM_VISIBLE_FACT_RE = re.compile(
    _VISUAL_PLATFORM_WORDS + r"[^,，、；;。.!！?？\n]{0,8}?(?:水印|字样|logo|标识|标志)",
    re.IGNORECASE,
)
# 平台词作定语修饰视觉事实时（"电商详情页风格的浅灰渐变"、"电商主图常见的
# 价格标签排布"），只摘除归因定语、保留事实本体，避免吞掉事实或留残句。
_VISUAL_PLATFORM_MODIFIER_RE = re.compile(
    _VISUAL_PLATFORM_WORDS + r"(?:风格|同款|式样?|常见)?的",
    re.IGNORECASE,
)
# 意图归因（"可用于/适合迁移为 小红书…"）：从意图前缀删到子句末。
_VISUAL_PLATFORM_INTENT_RE = re.compile(
    r"(?:(?:可|适合)(?:用于|迁移为)?|用于|迁移为)\s*"
    + _VISUAL_PLATFORM_WORDS
    + r"[^,，、；;。.!！?？\n]*",
    re.IGNORECASE,
)
# 子句以平台词开头（"小红书爆款构图"）：整段归因，删到子句末。
_VISUAL_PLATFORM_LEADING_RE = re.compile(
    r"^\s*" + _VISUAL_PLATFORM_WORDS + r"[^,，、；;。.!！?？\n]*",
    re.IGNORECASE,
)
# 归因处理后若子句以悬垂连接词收尾（"背景是"），说明事实主体已被吞掉，
# 残句比空句更糟——直接丢弃整个子句。
_VISUAL_DANGLING_TAIL_RE = re.compile(r"(?:是|有|为|呈|含|显示|采用|的|在)\s*$")


def _strip_platform_attribution(text: str) -> str:
    """按子句粒度删除平台归因，保留可见事实，绝不留下断头残句。

    处理优先级：可见事实(水印/字样)整句放行 > 定语只摘修饰 > 意图归因/
    句首归因删到子句末 > 仍残留平台词则弃整句 > 悬垂残句兜底丢弃。
    残留的多余逗号由 _strip_visual_analysis_scaffolding 末尾的标点清理兜底。
    """
    if not _VISUAL_PLATFORM_WORD_RE.search(text):
        return text
    pieces = re.split(r"([,，、；;。.!！?？\n]+)", text)
    kept: list[str] = []
    for index in range(0, len(pieces), 2):
        clause = pieces[index]
        delimiter = pieces[index + 1] if index + 1 < len(pieces) else ""
        if clause.strip() and _VISUAL_PLATFORM_WORD_RE.search(clause):
            if not _VISUAL_PLATFORM_VISIBLE_FACT_RE.search(clause):
                clause = _VISUAL_PLATFORM_MODIFIER_RE.sub("", clause)
                clause = _VISUAL_PLATFORM_INTENT_RE.sub("", clause)
                clause = _VISUAL_PLATFORM_LEADING_RE.sub("", clause)
                if _VISUAL_PLATFORM_WORD_RE.search(clause) or _VISUAL_DANGLING_TAIL_RE.search(
                    clause.strip()
                ):
                    clause = ""
        kept.append(clause + delimiter)
    return "".join(kept)
_VISUAL_QUALITY_BOOSTER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:masterpiece|best quality|high quality|ultra quality|ultra[- ]?detailed|"
    r"highly detailed|extremely detailed|insanely detailed|ultra[- ]?high resolution|"
    r"high[- ]?resolution|hi[- ]?res|premium texture|award[- ]?winning|"
    r"trending on artstation|uhd|(?:4|8|16)k(?: resolution| quality)?)(?![A-Za-z0-9_])|"
    r"(?:杰作|最佳质量|顶级质量|超高质量|高质量|专业级品质|广告级品质|商业级品质|"
    r"超高清|高清画质|超清画质|高分辨率|超高分辨率|极致细节|细节拉满|获奖作品|"
    r"顶级画质|顶级品质)",
    re.IGNORECASE,
)
_VISUAL_UNCERTAINTY_RE = re.compile(
    # 中英对称：中英混排输出里 possibly/maybe 等词与中文"可能"同样把生成
    # 指令降级为分析备注，须触发同粒度的子句删除。英文词加字母环视边界，
    # 避免误伤 mighty/unclearly 之外的普通词干组合。中文侧同样需要边界：
    # 「尽可能/可能性」是一致性约束里的合法措辞（"尽可能保持产品居中"），
    # 「不猜测/不推测」是模板要求模型复述的禁止性约束（"不猜测色值"），
    # 都不是不确定表达，不能触发子句删除。
    r"(?:不确定|无法确认|无法判断|证据不足|未见|未识别|看不清|"
    r"不清晰|疑似|(?<!不)猜测|(?<!不)推测|(?<!尽)可能(?!性))|"
    r"(?<![A-Za-z])(?:possibly|maybe|unclear|might)(?![A-Za-z])",
    re.IGNORECASE,
)
_VISUAL_HEX_COLOR_RE = re.compile(
    r"(?:视觉估计\s*)?(?:接近|约为|约)?\s*"
    r"#[0-9a-f]{3,8}(?:\s*(?:-|~|至|到)\s*#[0-9a-f]{3,8})?",
    re.IGNORECASE,
)
_VISUAL_EXACT_PERCENT_RE = re.compile(
    # 前缀双向兼容："约占画面 55%" 与 "占画面约 55%" 都要整体删除，
    # 否则会残留"占画面约 "之类的悬垂片段。
    r"(?:约|大约)?\s*(?:占(?:画面)?\s*)?(?:约|大约)?\s*\d+(?:\.\d+)?\s*%"
    r"(?:\s*(?:-|~|至|到)\s*\d+(?:\.\d+)?\s*%)?",
    re.IGNORECASE,
)
_VISUAL_CONFIDENCE_RE = re.compile(
    r"(?:[,，、]\s*)?(?:置信度|可信度)(?:为|约为)?\s*"
    r"(?:高|中等?|低|\d+(?:\.\d+)?\s*%)",
    re.IGNORECASE,
)


def _is_visual_placeholder(value: str) -> bool:
    text = value.strip().strip("。.!！?？,，;；:：、 ")
    if not text:
        return True
    if text in _VISUAL_PLACEHOLDER_VALUES:
        return True
    normalized = text
    for separator in _VISUAL_PLACEHOLDER_SEPARATORS:
        normalized = normalized.replace(separator, "|")
    tokens = [token.strip() for token in normalized.split("|") if token.strip()]
    return bool(tokens) and all(token in _VISUAL_PLACEHOLDER_VALUES for token in tokens)


def _strip_visual_analysis_scaffolding(value: str) -> str:
    """Turn evidence-layer prose into a direct generation clause."""
    text = str(value or "")
    # 历史版本曾把「（未验证）」展示后缀写进 structured/final_text；旧
    # revision 重新合成时在这里剥离，确保它永不进入生成提示词。
    text = re.sub(r"[（(]\s*未验证\s*[）)]", "", text)
    text = re.sub(
        r"(^|[；;。.!！?？,，\n])\s*"
        r"(?:未知|不确定项?|无法确认|无法判断|证据不足)\s*[:：]\s*"
        r"[^；;。.!！?？\n]*(?:[；;。.!！?？]|$)",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:直接可见事实|视觉估计|视觉推断|观察事实|事实层|估计层|"
        r"高置信(?:度)?推断|模型推断|低置信(?:度)?推断)\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Reference ordinals describe the analysis input order, which can differ
    # from the image gateway's product/style input order. They are not visual
    # instructions and can actively point the renderer at the wrong image.
    text = _VISUAL_REFERENCE_LABEL_RE.sub("", text)
    text = _strip_platform_attribution(text)
    text = _VISUAL_QUALITY_BOOSTER_RE.sub("", text)
    text = _VISUAL_HEX_COLOR_RE.sub("", text)
    text = _VISUAL_EXACT_PERCENT_RE.sub("", text)
    text = _VISUAL_CONFIDENCE_RE.sub("", text)
    # A qualifier such as "possibly" changes a generation instruction into an
    # analysis note. Drop the affected clause, but do it at sub-clause
    # granularity: an uncertainty word in one comma-joined clause must not
    # delete the valid observations sharing the same sentence (连坐删除).
    chunks = re.split(r"([；;。.!！?？\n]+)", text)
    direct_chunks: list[str] = []
    for index in range(0, len(chunks), 2):
        sentence = chunks[index].strip()
        delimiter = chunks[index + 1] if index + 1 < len(chunks) else ""
        if not sentence:
            continue
        if _VISUAL_UNCERTAINTY_RE.search(sentence):
            # 按逗号/顿号切成子句逐段判断：只删含不确定措辞的子句，保留
            # 同句里的有效观察（如"主体居中，标签文字看不清"保留前半句）。
            # 整个子句丢弃而非词级挖除，避免留下"镜头"这类残句碎片。
            kept_clauses = [
                clause.strip()
                for clause in re.split(r"[,，、]+", sentence)
                if clause.strip() and not _VISUAL_UNCERTAINTY_RE.search(clause)
            ]
            if not kept_clauses:
                continue
            sentence = "，".join(kept_clauses)
        direct_chunks.append(sentence + delimiter)
    text = "".join(direct_chunks)
    text = re.sub(r"([,，;；、])(?:\s*[,，;；、])+", r"\1", text)
    text = re.sub(r"(^|[；;。.!！?？])\s*[,，、]+", r"\1", text)
    text = re.sub(r"(^|[；;。.!！?？,，、])\s*的(?=\S)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,，;；。、")


def clean_visual_generation_clause(value: object) -> str:
    """Public boundary used when evidence is attached after gateway validation."""
    return _clean_visual_clause(value)


# opencv 光流分类标签 ↔ VLM 运镜文本关键词。用于把 camera_motion_summary 的
# dominant_label 与 VLM 写的运镜描述做标签级对账，而不是仅凭时间窗重叠放行。
_CAMERA_LABEL_TEXT_PATTERNS: dict[str, re.Pattern] = {
    "pan": re.compile(
        r"横摇|左摇|右摇|平移|横移|环绕|摇镜|摇移|甩镜|pan(?:ning)?|orbit",
        re.IGNORECASE,
    ),
    "tilt": re.compile(
        r"仰摇|俯摇|上摇|下摇|俯仰|上仰|下俯|升降镜头|tilt(?:ing)?|crane|pedestal",
        re.IGNORECASE,
    ),
    "zoom": re.compile(
        r"推[近进镜]|拉[远镜]|拉近|推拉|变焦|缩放|zoom|dolly",
        re.IGNORECASE,
    ),
    "static": re.compile(
        r"固定|静止|不动|静态|定镜|static|locked",
        re.IGNORECASE,
    ),
}

# 分析器标签(+方向) → 可执行的运镜生成子句。冲突时"以分析器为准"的替换文本，
# 方向枚举与 video_evidence_analysis.CAMERA_MOTION_DIRECTIONS 一致。
_CAMERA_LABEL_CLAUSES: dict[tuple[str, str | None], str] = {
    ("pan", "left"): "镜头向左横摇",
    ("pan", "right"): "镜头向右横摇",
    ("pan", None): "镜头横向摇移",
    ("tilt", "up"): "镜头向上仰摇",
    ("tilt", "down"): "镜头向下俯摇",
    ("tilt", None): "镜头纵向俯仰",
    ("zoom", "in"): "镜头缓慢推近",
    ("zoom", "out"): "镜头缓慢拉远",
    ("zoom", None): "镜头推拉变焦",
    ("static", None): "固定镜头",
}

# 光流分类置信度低于该值时视为"分类不可判"：既不足以背书、也不足以否决
# VLM 的运镜描述，只降级保留并如实标注。
_CAMERA_MOTION_MIN_CONFIDENCE = 0.4
# Affine motion can score pan and zoom almost equally when a hand pulls an
# object toward the lens. Treat a narrow top-two margin as ambiguous instead
# of replacing the main model's camera description with a brittle label.
_CAMERA_MOTION_MIN_LABEL_MARGIN = 0.1
# A homography can fit a moving person or hand even when the camera is locked.
# Only let OpenCV replace the main model's camera description when most tracked
# points agree on global background motion.
_CAMERA_MOTION_MIN_BACKGROUND_CONFIDENCE = 0.75

# VLM 声称的软转场（叠化/淡入淡出等）。ffmpeg 场景切点只证明硬切；语义
# provider 缺席时，软转场声称与服务器确认的硬切冲突，以分析器为准。
_SOFT_TRANSITION_RE = re.compile(
    r"叠化|溶解|淡入|淡出|渐隐|渐显|闪白|闪黑|划像|翻页|扫像|模糊过渡|"
    r"dissolve|cross\s*fade|fade|wipe",
    re.IGNORECASE,
)


def _gate_entry(
    verified: bool,
    *,
    confidence: str | None = None,
    score: float | None = None,
    source: str | None = None,
    reason: str | None = None,
) -> dict:
    """单个时序字段的证据门判定记录（挂在 shot["evidence_gate"] 上）。

    - verified: 是否有独立分析器背书；
    - confidence: "analyzer"（分析器确认）| "vlm_only"（仅 VLM，降级保留）|
      None（字段被清空或本就为空）；
    - score: 分析器数值置信度（有则填，∈[0,1]）；
    - source: 背书来源（semantic_provider / opencv_lk_homography /
      ffmpeg_scene / cross_frame_vlm / analyzer_status）；
    - reason: 冲突或降级的中文原因，供下游与 UI 展示。
    """
    return {
        "verified": bool(verified),
        "claim_type": "verified_evidence" if verified else (
            "cross_frame_inference" if confidence == "vlm_only" else "unsupported"
        ),
        "level": "verified" if verified else (
            "inferred" if confidence == "vlm_only" else "unavailable"
        ),
        "confidence": confidence,
        "score": score,
        "source": source,
        "reason": reason,
    }


def _shot_has_cross_frame_evidence(shot: dict) -> bool:
    """复用 normalize_video_shots 的跨帧证据契约（本文件下方 :1480 一带）。

    normalize 阶段在帧时间戳可用时已校验"≥2 个不同时间戳帧"，不满足时会把
    subject_tracking/pose/action/camera/transition 清空；能带着非空时序字段走到证据门、且仍有
    ≥2 个不同证据帧序号的 shot，即视为满足跨帧证据契约。
    """
    indices = shot.get("evidence_frame_indices")
    if not isinstance(indices, list):
        return False
    distinct: set[int] = set()
    for value in indices:
        if isinstance(value, bool):
            continue
        try:
            distinct.add(int(value))
        except (TypeError, ValueError):
            continue
    return len(distinct) >= 2


def _camera_labels_in_text(text: str) -> set[str]:
    return {
        label
        for label, pattern in _CAMERA_LABEL_TEXT_PATTERNS.items()
        if pattern.search(text)
    }


def _camera_motion_clause(label: str, direction: object) -> str:
    key = direction if isinstance(direction, str) and direction else None
    return (
        _CAMERA_LABEL_CLAUSES.get((label, key))
        or _CAMERA_LABEL_CLAUSES[(label, None)]
    )


def _field_existence_verified(shot: dict, statuses: dict, capability: str, refs_key: str) -> bool:
    status = str(statuses.get(capability) or "unsupported").strip().lower()
    refs = shot.get(refs_key)
    return status in {"analyzed", "partial"} and isinstance(refs, list) and bool(refs)


def _gate_action_field(shot: dict, statuses: dict) -> tuple[str, dict]:
    """action 三态门：analyzed/partial 放行；unsupported 但满足跨帧证据契约
    时降置信度保留（vlm_only）；两者都不满足才清空。"""
    text = _clean_visual_clause(shot.get("action"))
    if not text:
        return "", _gate_entry(False)
    if _field_existence_verified(shot, statuses, "action", "action_evidence_refs"):
        return text, _gate_entry(True, confidence="analyzer", source="semantic_provider")
    if _shot_has_cross_frame_evidence(shot):
        # 语义分析器缺席（如 video_evidence_semantic_url 未配置）时不再
        # "宁可全删"：VLM 的动作描述已被跨帧证据契约约束，降置信度保留，
        # 并明确标注未经独立分析器验证，让下游与 UI 能区分。
        return text, _gate_entry(
            False,
            confidence="vlm_only",
            source="cross_frame_vlm",
            reason="语义动作分析器不可用，动作描述由多个不同时间戳抽样帧支撑但未经独立验证",
        )
    return "", _gate_entry(False, reason="语义动作分析器不可用且缺乏跨帧证据，动作描述已清除")


def _gate_model_semantic_field(
    shot: dict,
    statuses: dict,
    *,
    field: str,
    capability: str,
    refs_key: str,
    label: str,
) -> tuple[str, dict]:
    """Keep main-VLM semantics as an explicitly unverified multi-frame inference."""
    text = _clean_visual_clause(shot.get(field))
    if not text:
        return "", _gate_entry(False)
    if _field_existence_verified(shot, statuses, capability, refs_key):
        return text, _gate_entry(True, confidence="analyzer", source="semantic_provider")
    if _shot_has_cross_frame_evidence(shot):
        return text, _gate_entry(
            False,
            confidence="vlm_only",
            source="cross_frame_vlm",
            reason=f"独立{label}分析器不可用，由主视觉模型基于多个不同时间戳抽样帧推断",
        )
    return "", _gate_entry(
        False,
        reason=f"独立{label}分析器不可用且缺乏跨帧证据，{label}描述已清除",
    )


def _gate_camera_field(shot: dict, statuses: dict) -> tuple[str, dict]:
    """camera 一致性门：从"存在性验证"（纯时间窗重叠）升级为与光流分类
    结果的标签级对账。一致→保留；冲突→以分析器为准；分类置信度过低→
    降级保留并标注 vlm_only。"""
    text = _clean_visual_clause(shot.get("camera"))
    camera_status = str(
        statuses.get("camera_motion") or statuses.get("motion") or "unsupported"
    ).strip().lower()
    refs = shot.get("camera_motion_evidence_refs")
    existence_ok = (
        camera_status in {"analyzed", "partial"}
        and isinstance(refs, list)
        and bool(refs)
    )
    if not existence_ok:
        # 时间窗内没有任何光流样本背书：维持原有的清空行为。
        return "", _gate_entry(
            False,
            reason="时间窗内无光流运镜证据，运镜描述已清除" if text else None,
        )
    summary = shot.get("camera_motion_summary")
    summary = summary if isinstance(summary, dict) else {}
    label = str(summary.get("dominant_label") or "").strip().lower()
    try:
        score = float(summary.get("confidence"))
    except (TypeError, ValueError):
        score = None
    summary_usable = (
        str(summary.get("status") or "").strip().lower() == "analyzed"
        and label in _CAMERA_LABEL_TEXT_PATTERNS
        and score is not None
    )
    if not summary_usable:
        # 旧数据没有 camera_motion_summary（或无可用样本）：退回存在性
        # 验证以保持兼容，但 source 如实标注为仅状态级背书。
        if not text:
            return "", _gate_entry(False)
        return text, _gate_entry(True, confidence="analyzer", source="analyzer_status")
    if score < _CAMERA_MOTION_MIN_CONFIDENCE:
        # 光流分类置信度过低：弱分类既不能背书也不能否决 VLM，降级保留。
        if not text:
            return "", _gate_entry(False)
        return text, _gate_entry(
            False,
            confidence="vlm_only",
            score=round(score, 6),
            source="opencv_lk_homography",
            reason="光流运镜分类置信度过低，VLM 运镜描述未经对账、降置信度保留",
        )
    try:
        background_score = float(summary.get("background_motion_confidence"))
    except (TypeError, ValueError):
        background_score = None
    if (
        background_score is not None
        and background_score < _CAMERA_MOTION_MIN_BACKGROUND_CONFIDENCE
    ):
        if not text:
            return "", _gate_entry(
                False,
                score=round(background_score, 6),
                source="opencv_lk_homography",
                reason="背景全局运动覆盖不足，光流主要来自主体运动，未补写运镜",
            )
        return text, _gate_entry(
            False,
            confidence="vlm_only",
            score=round(background_score, 6),
            source="cross_frame_vlm",
            reason="背景全局运动覆盖不足，可能主要是主体运动，保留主视觉模型运镜判断",
        )
    label_scores = summary.get("label_scores")
    alternative_scores: list[float] = []
    if isinstance(label_scores, dict):
        for candidate, value in label_scores.items():
            if str(candidate) == label:
                continue
            try:
                alternative_scores.append(float(value))
            except (TypeError, ValueError):
                continue
    label_margin = score - max(alternative_scores) if alternative_scores else None
    if label_margin is not None and label_margin < _CAMERA_MOTION_MIN_LABEL_MARGIN:
        if not text:
            return "", _gate_entry(
                False,
                score=round(score, 6),
                source="opencv_lk_homography",
                reason="光流运镜类型区分度不足，未补写运镜",
            )
        return text, _gate_entry(
            False,
            confidence="vlm_only",
            score=round(score, 6),
            source="cross_frame_vlm",
            reason="光流运镜类型区分度不足，保留主视觉模型运镜判断",
        )
    score = round(score, 6)
    analyzer_clause = _camera_motion_clause(label, summary.get("dominant_direction"))
    text_labels = _camera_labels_in_text(text)
    if label == "static":
        # 契约约定：static 主导即视为无运镜证据。VLM 声称存在运镜即冲突，
        # 以分析器为准替换为"固定镜头"。
        if text and text_labels - {"static"}:
            return analyzer_clause, _gate_entry(
                True,
                confidence="analyzer",
                score=score,
                source="opencv_lk_homography",
                reason="光流分析判定为固定镜头，VLM 声称的运镜与之冲突，已以分析器结论为准",
            )
        return (text or analyzer_clause), _gate_entry(
            True, confidence="analyzer", score=score, source="opencv_lk_homography"
        )
    if not text:
        # VLM 未描述运镜但光流有明确结论：用分析器标签补齐（完全由证据背书）。
        return analyzer_clause, _gate_entry(
            True,
            confidence="analyzer",
            score=score,
            source="opencv_lk_homography",
            reason="VLM 未描述运镜，由光流分析结论补齐",
        )
    if label in text_labels:
        # 一致：VLM 运镜描述与光流主导标签吻合。
        return text, _gate_entry(
            True, confidence="analyzer", score=score, source="opencv_lk_homography"
        )
    if text_labels:
        # 冲突：VLM 声称的运动类型与光流主导标签不一致（如光流判定 pan、
        # VLM 写推近），以分析器为准替换。
        return analyzer_clause, _gate_entry(
            True,
            confidence="analyzer",
            score=score,
            source="opencv_lk_homography",
            reason=f"VLM 运镜描述与光流主导标签 {label} 冲突，已替换为分析器结论",
        )
    # VLM 文本无可识别的运镜关键词，无法对账：降级保留并标注。
    return text, _gate_entry(
        False,
        confidence="vlm_only",
        score=score,
        source="opencv_lk_homography",
        reason="VLM 运镜描述无法与光流标签对账，降置信度保留",
    )


def _gate_transition_field(
    shot: dict,
    statuses: dict,
    cut_confidence_by_id: dict[str, float],
) -> tuple[str, dict]:
    """transition 门：语义 provider 与 ffmpeg 场景切点均为强证据。服务器
    确认的硬切（analyzer_status["shot_transitions"]=="analyzed" 且
    cut_transition_evidence_refs 非空）优先级不低于语义 provider。"""
    text = _clean_visual_clause(shot.get("transition"))
    semantic_ok = _field_existence_verified(
        shot, statuses, "transition", "transition_evidence_refs"
    )
    cut_status = str(statuses.get("shot_transitions") or "unsupported").strip().lower()
    raw_cut_refs = shot.get("cut_transition_evidence_refs")
    cut_refs = [
        str(value)
        for value in (raw_cut_refs if isinstance(raw_cut_refs, list) else [])
        if str(value or "").strip()
    ]
    cut_ok = cut_status == "analyzed" and bool(cut_refs)
    if semantic_ok and text:
        return text, _gate_entry(True, confidence="analyzer", source="semantic_provider")
    if cut_ok:
        scores = [
            cut_confidence_by_id[ref]
            for ref in cut_refs
            if ref in cut_confidence_by_id
        ]
        score = round(max(scores), 6) if scores else None
        if text and _SOFT_TRANSITION_RE.search(text):
            # ffmpeg 场景检测只确认硬切；语义 provider 缺席时 VLM 声称的
            # 软转场与服务器证据冲突，以分析器为准。
            return "硬切", _gate_entry(
                True,
                confidence="analyzer",
                score=score,
                source="ffmpeg_scene",
                reason="ffmpeg 场景检测确认为硬切，VLM 声称的软转场已被替换",
            )
        if text:
            return text, _gate_entry(
                True, confidence="analyzer", score=score, source="ffmpeg_scene"
            )
        return "硬切", _gate_entry(
            True,
            confidence="analyzer",
            score=score,
            source="ffmpeg_scene",
            reason="VLM 未描述转场，由服务器确认的场景切点补齐",
        )
    if text and _shot_has_cross_frame_evidence(shot):
        return text, _gate_entry(
            False,
            confidence="vlm_only",
            source="cross_frame_vlm",
            reason="独立转场分析器不可用，由主视觉模型基于多个不同时间戳抽样帧推断",
        )
    return "", _gate_entry(
        False,
        reason="转场描述缺乏独立分析器证据，已清除" if text else None,
    )


def constrain_video_shots_to_evidence(
    shots: list[dict] | None,
    *,
    evidence: dict | None = None,
) -> list[dict]:
    """Keep executable temporal claims only when independent evidence backs them.

    证据门对称化（该严的严、该放的放，放行必须带置信度标注）：
    - camera：与光流分类结果（camera_motion_summary）做标签级对账，一致
      保留、冲突以分析器为准并记录原因；
    - subject_tracking / pose / action：有语义分析器背书放行；unsupported 但满足跨帧证据
      契约时降置信度保留（confidence="vlm_only", verified=False）；否则清空；
    - transition：承认 ffmpeg 场景切点为强证据；否则满足跨帧契约时由 VLM 降级保留；
    - 每个 shot 附带 shot["evidence_gate"]（subject_tracking/pose/action/camera/transition →
      _gate_entry 结构），下游与 UI 依此区分验证等级。

    evidence 传 analyze_video_evidence() 的返回值（含 shot_transitions 块）
    时，硬切放行会带上 lavfi scene_score 数值置信度；不传则 score 为 None。
    """
    cut_confidence_by_id: dict[str, float] = {}
    if isinstance(evidence, dict):
        block = evidence.get("shot_transitions")
        events = block.get("events") if isinstance(block, dict) else None
        for event in events or []:
            if not isinstance(event, dict) or not event.get("evidence_id"):
                continue
            try:
                confidence = float(event.get("confidence"))
            except (TypeError, ValueError):
                continue
            cut_confidence_by_id[str(event["evidence_id"])] = max(
                0.0, min(1.0, confidence)
            )
    constrained: list[dict] = []
    for raw in shots or []:
        if not isinstance(raw, dict):
            continue
        shot = dict(raw)
        for field in ("visual", "lighting"):
            shot[field] = _clean_visual_clause(shot.get(field))
        statuses = shot.get("analyzer_status")
        statuses = statuses if isinstance(statuses, dict) else {}
        gate: dict[str, dict] = {}
        shot["subject_tracking"], gate["subject_tracking"] = _gate_model_semantic_field(
            shot,
            statuses,
            field="subject_tracking",
            capability="subject_tracking",
            refs_key="subject_track_refs",
            label="主体追踪",
        )
        shot["pose"], gate["pose"] = _gate_model_semantic_field(
            shot,
            statuses,
            field="pose",
            capability="pose",
            refs_key="pose_evidence_refs",
            label="姿态",
        )
        shot["action"], gate["action"] = _gate_action_field(shot, statuses)
        shot["camera"], gate["camera"] = _gate_camera_field(shot, statuses)
        shot["transition"], gate["transition"] = _gate_transition_field(
            shot, statuses, cut_confidence_by_id
        )
        shot["evidence_gate"] = gate
        constrained.append(shot)
    return constrained


def _clean_visual_clause(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _strip_visual_analysis_scaffolding(value)
    if _is_visual_placeholder(cleaned):
        return ""
    return cleaned.strip().strip("；; ")


def _truncate_visual_clause(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    candidate = text[:limit]
    cut = max(candidate.rfind(mark) for mark in ("。", "；", ";", "，", ","))
    if cut >= max(12, limit // 2):
        candidate = candidate[:cut]
    return candidate.rstrip("。；;，,、 ")


def _fit_visual_prompt(parts: list[str], target: str) -> str:
    limit = _VISUAL_PROMPT_LIMITS[target]
    clause_limit = _VISUAL_CLAUSE_LIMITS[target]
    result: list[str] = []
    seen: set[str] = set()
    clauses = [
        clause
        for part in parts
        for clause in re.split(r"[；;\n]+", str(part or ""))
    ]
    for raw_part in clauses:
        part = _clean_visual_clause(raw_part)
        if not part:
            continue
        part = _truncate_visual_clause(part, clause_limit)
        identity = part.rstrip("。；;，,、 ")
        if not identity or identity in seen:
            continue
        used = sum(len(item) for item in result) + max(0, len(result))
        remaining = limit - used
        if remaining <= 0:
            break
        if len(part) > remaining:
            # The remaining budget must never turn a useful field into an
            # incomplete fragment. Lower-priority shorter fields may still fit.
            continue
        result.append(part)
        seen.add(identity)
    return "；".join(result).strip()


def _ordered_visual_fields(structured: dict, target: str) -> tuple[str, ...]:
    base = _VISUAL_FIELD_ORDERS[target]
    if target not in {"image", "video"}:
        return base
    kind = str(structured.get("图像类型") or "")
    person = "人物" in kind
    product = "产品" in kind
    identity: tuple[str, ...] = ()
    if product:
        identity += ("商品服装",)
    if person:
        identity += ("妆发五官", "服装结构", "人物比例", "身材体态")
    core = (
        ("主体",) + identity + (
            "场景背景",
            "构图" if target == "image" else "视角构图",
            "光线",
            "色调配色",
            "视角镜头" if target == "image" else "一致性约束",
            "景别" if target == "image" else "风格",
            "风格",
            "材质纹理",
            "文字版式" if target == "image" else "一致性约束",
            "氛围情绪",
            "后期质感" if target == "image" else "一致性约束",
            "一致性约束",
            "细节特征",
        )
    )
    return tuple(dict.fromkeys(core + base))


def _compact_long_product_video_shots(entries: list[dict]) -> list[str]:
    """Compress a long product timeline without dropping the hero product reveal."""
    meaningful = [
        entry
        for entry in entries
        if not _EMPTY_VIDEO_SHOT_RE.fullmatch(str(entry.get("visual") or "").strip())
    ]
    if not meaningful:
        return []
    opener = meaningful[0]
    hero_candidates = [
        entry
        for entry in meaningful
        if _PRODUCT_HERO_SHOT_RE.search(str(entry.get("visual") or ""))
    ]
    hero = hero_candidates[-1] if hero_candidates else meaningful[-1]
    middle_candidates = [
        entry
        for entry in meaningful
        if entry is not opener
        and entry is not hero
        and _PRODUCT_DETAIL_SHOT_RE.search(str(entry.get("visual") or ""))
    ]
    ranked_middle = sorted(
        middle_candidates,
        key=lambda entry: (
            len(_PRODUCT_DETAIL_SHOT_RE.findall(str(entry.get("visual") or ""))),
            float(entry.get("confidence") or 0),
        ),
        reverse=True,
    )[:2]
    ranked_middle.sort(key=lambda entry: int(entry.get("index") or 0))

    parts = [
        _truncate_visual_clause(f"镜头1：{opener['visual']}", 44),
    ]
    if ranked_middle:
        montage = "、".join(
            _truncate_visual_clause(str(entry["visual"]), 22)
            for entry in ranked_middle
        )
        parts.append(_truncate_visual_clause(f"镜头2：依次展示{montage}", 44))
    if hero is not opener:
        hero_number = len(parts) + 1
        parts.append(
            _truncate_visual_clause(f"镜头{hero_number}：{hero['visual']}", 44)
        )
    return [part for part in parts if part]


class ReverseResultValidationError(ValueError):
    """The provider returned JSON that does not satisfy the reverse contract."""


def compose_visual_final_text(
    structured: dict,
    target: str,
    shots: list[dict] | None = None,
) -> str:
    """Build a bounded positive prompt from target-specific visual fields."""
    if target not in _VISUAL_FIELD_ORDERS:
        raise ReverseResultValidationError(f"不支持的反推目标: {target}")
    static_parts: list[str] = []
    prefix = _VISUAL_PROMPT_PREFIXES.get(target)
    if isinstance(structured, dict):
        for key in _ordered_visual_fields(structured, target):
            text = _clean_visual_clause(structured.get(key))
            if text and target == "image":
                text = _truncate_visual_clause(
                    text,
                    _IMAGE_VISUAL_FIELD_LIMITS.get(key, _VISUAL_CLAUSE_LIMITS[target]),
                )
            elif text and target == "video":
                text = _truncate_visual_clause(text, 52)
            if text and text not in static_parts:
                static_parts.append(text)
    shot_parts: list[str] = []
    shot_entries: list[dict] = []
    compress_long_video = False
    if target == "video" and isinstance(shots, list):
        finite_ends = []
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            try:
                end = float(shot.get("end_seconds"))
            except (TypeError, ValueError):
                continue
            if isfinite(end):
                finite_ends.append(end)
        compress_long_video = bool(finite_ends and max(finite_ends) > 15.0)
    if target == "video" and isinstance(shots, list):
        for index, shot in enumerate(shots, start=1):
            if not isinstance(shot, dict):
                continue
            details = [
                text
                for key in _VISUAL_SHOT_FIELDS
                if (text := _clean_visual_clause(shot.get(key)))
            ]
            details = list(dict.fromkeys(details))
            if details:
                segment_index = shot.get("source_segment_index")
                segment = (
                    f"片段{segment_index} "
                    if isinstance(segment_index, int) and not isinstance(segment_index, bool)
                    else ""
                )
                shot_entries.append({
                    "index": index,
                    "visual": _clean_visual_clause(shot.get("visual")),
                    "confidence": shot.get("confidence"),
                    "text": f"{segment}镜头{index}：{'，'.join(details)}",
                })
    product_video = "产品" in str(structured.get("图像类型") or "")
    if compress_long_video and product_video:
        shot_parts = _compact_long_product_video_shots(shot_entries)
    else:
        shot_parts = [entry["text"] for entry in shot_entries]
    if not static_parts and not shot_parts:
        raise ReverseResultValidationError("反推结果缺少可用于生成的视觉白名单字段")
    if prefix:
        static_parts.insert(0, prefix)
    parts = static_parts
    if shot_parts:
        if compress_long_video:
            shot_parts.insert(0, "按原镜头顺序压缩为单段核心版")
            if product_video:
                shot_parts.insert(1, "镜头间干净硬切")
        # Keep subject/context ahead of the timeline, then prioritize verified
        # shot evidence over lower-value static detail when the budget is tight.
        lead_count = min(2, len(static_parts))
        parts = static_parts[:lead_count] + shot_parts + static_parts[lead_count:]
    text = _fit_visual_prompt(parts, target)
    if not text:
        raise ReverseResultValidationError("反推结果缺少可用于生成的视觉白名单字段")
    return text


_VIDEO_DRAFT_FIELD_LABELS = {
    "主体": "主体",
    "人像意图": "人物",
    "人物比例": "人物比例",
    "身材体态": "体态",
    "体态线条": "体态线条",
    "服装结构": "服装",
    "服装覆盖": "服装覆盖",
    "妆发五官": "妆发",
    "商品服装": "产品/服装",
    "细节特征": "关键细节",
    "场景背景": "场景",
    "风格": "风格",
    "视角构图": "构图",
    "光线": "光线",
    "色调配色": "配色",
    "材质纹理": "材质",
    "一致性约束": "连续性约束",
    "字幕卖点": "后期字幕（后期叠加）",
    "旁白": "旁白/对白",
    "音效": "声音基线",
}
_VIDEO_DRAFT_SHOT_LABELS = (
    ("visual", "初始画面"),
    ("subject_tracking", "主体轨迹"),
    ("pose", "姿态变化"),
    ("action", "动作与可见终态"),
    ("camera", "运镜"),
    ("lighting", "光线/材质反馈"),
    ("transition", "转场"),
)

_VIDEO_DRAFT_PLANNING_FIELDS = frozenset({"广告目标", "氛围情绪"})
_VIDEO_DRAFT_POST_FIELDS = ("字幕卖点", "旁白", "音效")
_VIDEO_DRAFT_PACKAGE_TEXT_LABELS = frozenset({
    "包装原字", "产品原字", "包装文字", "产品文字",
})
_VIDEO_DRAFT_SUBTITLE_LABELS = frozenset({"后期字幕", "画面字幕", "卖点字幕"})
_VIDEO_DRAFT_OCR_MARKER_RE = re.compile(
    r"(包装原字|产品原字|包装文字|产品文字|后期字幕|画面字幕|卖点字幕)"
    r"\s*[:：]\s*",
    re.IGNORECASE,
)
_VIDEO_DRAFT_OVERLAY_CUE_RE = re.compile(
    r"字幕|后期叠加|文字(?:浮现|淡入|出现|显示)|浮现文字",
    re.IGNORECASE,
)
_VIDEO_DRAFT_SPEECH_CUE_RE = re.compile(
    r"对白|旁白|台词|独白|解说|画外音|说话|人声转写",
    re.IGNORECASE,
)
_VIDEO_DRAFT_SPEECH_ROLE_RE = re.compile(
    r"对白|旁白|台词|独白|解说|画外音|说话|人声转写|女声|男声",
    re.IGNORECASE,
)
_VIDEO_DRAFT_MUSIC_CUE_RE = re.compile(
    r"(?:全程|持续|舒缓|轻柔|轻快|紧凑|平稳|低沉|明快|\s)*"
    r"(?:背景音乐|背景乐|音乐铺底|BGM)",
    re.IGNORECASE,
)


_GENERATION_WATERMARK_TEXTS = {
    "生成",
    "ai生成",
    "豆包ai生成",
    "即梦ai生成",
    "可灵ai生成",
}
_GENERATION_WATERMARK_RE = re.compile(
    r"(?:豆包|即梦|可灵)?\s*AI\s*生成",
    re.IGNORECASE,
)
_GENERATION_OUTPUT_SPEC_RE = re.compile(
    r"(?:输出规格|视频规格|生成规格)\s*[:：]\s*[^\n；;。]*",
    re.IGNORECASE,
)
_GENERATION_RESOLUTION_RE = re.compile(r"(?<!\d)\d{3,4}\s*[x×]\s*\d{3,4}(?!\d)")
_GENERATION_RATIO_RE = re.compile(
    r"(?:(?:视频|画面|输出|生成|成片)\s*(?:画幅|比例|宽高比)|"
    r"(?:竖版|横版)\s*(?:画幅|比例)?)\s*(?:为|是|[:：])?\s*"
    r"\d{1,2}\s*:\s*\d{1,2}|"
    r"(?<!\d)\d{1,2}\s*:\s*\d{1,2}(?!\d)\s*"
    r"(?:画幅|竖版|横版|视频比例|画面比例|输出比例|生成比例)",
    re.IGNORECASE,
)
_GENERATION_TIMESTAMP_LIST_RE = re.compile(
    r"(?:在|于)\s*(?:约\s*)?\d+(?:\.\d+)?\s*(?:秒|s)"
    r"(?:\s*[、,，]\s*\d+(?:\.\d+)?\s*(?:秒|s))*\s*(?:等)?\s*",
    re.IGNORECASE,
)
_GENERATION_TIME_RANGE_RE = re.compile(
    r"(?<!\d)\d+(?:\.\d+)?\s*(?:-|–|—|~|至|到)\s*"
    r"\d+(?:\.\d+)?\s*(?:秒|s)(?![A-Za-z])",
    re.IGNORECASE,
)
_GENERATION_RUNTIME_RE = re.compile(
    r"(?:总时长|视频时长|成片时长|全片时长|片长|建议生成)\s*"
    r"(?:为|约|建议)?\s*\d+(?:\.\d+)?\s*(?:秒钟?|seconds?|secs?|s)(?![A-Za-z])|"
    r"(?<!\d)\d+(?:\.\d+)?\s*秒钟?\s*(?=(?:视频|广告|短片|成片))",
    re.IGNORECASE,
)
_GENERATION_RELATIVE_SECONDS_RE = re.compile(
    r"前\s*\d+(?:\.\d+)?\s*秒(?:内)?|"
    r"\d+(?:\.\d+)?\s*秒(?:钟)?后|"
    r"(?:持续|停留)\s*(?:约\s*)?\d+(?:\.\d+)?\s*秒(?:钟)?",
    re.IGNORECASE,
)
_GENERATION_EXACT_BPM_RE = re.compile(
    r"(?:节拍(?:约|为)?\s*)?\d+(?:\.\d+)?\s*BPM",
    re.IGNORECASE,
)
_GENERATION_UNCLASSIFIED_TRANSIENT_RE = re.compile(
    r"(?:检测到\s*)?\d+\s*个?\s*未分类瞬态声|未分类瞬态声",
    re.IGNORECASE,
)
_GENERATION_RAW_AUDIO_SIGNAL_RE = re.compile(
    r"检测到节拍点|节拍点秒数\s*=\s*[^；;。\n]+|"
    r"持续音乐可能性不确定|未检测到明显持续音乐",
    re.IGNORECASE,
)


def _without_generation_watermark(value: str) -> str:
    cleaned = _GENERATION_WATERMARK_RE.sub("", value)
    cleaned = re.sub(
        r"^[\s··,，:：;；._-]+|[\s··,，:：;；._-]+$",
        "",
        cleaned,
    ).strip()
    return "" if cleaned.lower() in _GENERATION_WATERMARK_TEXTS else cleaned


def _without_generation_parameters_and_timing(value: object) -> str:
    """Remove source execution parameters while preserving visual/audio content."""
    text = _clean_visual_clause(value)
    text = _GENERATION_OUTPUT_SPEC_RE.sub("", text)
    text = _GENERATION_RESOLUTION_RE.sub("", text)
    text = _GENERATION_RATIO_RE.sub("", text)
    text = _GENERATION_TIMESTAMP_LIST_RE.sub("", text)
    text = _GENERATION_TIME_RANGE_RE.sub("", text)
    text = _GENERATION_RUNTIME_RE.sub("", text)

    def replace_relative_seconds(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.lstrip().startswith("前"):
            return "开场"
        if re.search(r"后\s*$", token):
            return "随后"
        return ""

    text = _GENERATION_RELATIVE_SECONDS_RE.sub(replace_relative_seconds, text)
    text = re.sub(r"\[\s*\]|【\s*】|\(\s*\)|（\s*）", "", text)
    text = re.sub(r"([,，;；、])(?:\s*[,，;；、])+", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,，;；。、")


def _generation_ready_audio_value(value: object) -> str:
    """Turn analyzer prose into executable audio direction for generation."""
    text = _without_generation_parameters_and_timing(value)
    text = re.sub(r"检测到持续音乐可能性较高", "持续背景音乐", text)
    text = _GENERATION_EXACT_BPM_RE.sub("", text)
    text = _GENERATION_UNCLASSIFIED_TRANSIENT_RE.sub("", text)
    text = _GENERATION_RAW_AUDIO_SIGNAL_RE.sub("", text)
    text = re.sub(r"([,，;；、])(?:\s*[,，;；、])+", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,，;；。、")


def _generation_draft_shot_value(shot: dict, key: str) -> str:
    value = (
        _generation_ready_audio_value(shot.get(key))
        if key == "audio_cue"
        else _without_generation_parameters_and_timing(shot.get(key))
    )
    if key != "ocr":
        return value
    value = _without_generation_watermark(value)
    vlm_text = _without_generation_watermark(
        _clean_visual_clause(shot.get("vlm_text_description"))
    )
    return value or vlm_text


def _generation_text_identity(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value or "")).lower()


def _video_draft_ocr_segments(text: str) -> list[tuple[str, str]]:
    matches = list(_VIDEO_DRAFT_OCR_MARKER_RE.finditer(text))
    if not matches:
        return [
            ("", clause.strip(" ,，;；。、"))
            for clause in re.split(r"[\n；;]+", text)
            if clause.strip(" ,，;；。、")
        ]

    segments: list[tuple[str, str]] = []
    prefix = text[:matches[0].start()]
    segments.extend(
        ("", clause.strip(" ,，;；。、"))
        for clause in re.split(r"[\n；;]+", prefix)
        if clause.strip(" ,，;；。、")
    )
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end():end].strip(" ,，;；。、")
        if content:
            segments.append((match.group(1), content))
    return segments


def _video_draft_ocr_details(shot: dict) -> list[tuple[str, str]]:
    """Separate product identity text from captions intended for post."""
    text = _without_generation_parameters_and_timing(shot.get("ocr"))
    if not _without_generation_watermark(text):
        text = _without_generation_parameters_and_timing(
            shot.get("vlm_text_description")
        )
    details: list[tuple[str, str]] = []
    for marker, raw_clause in _video_draft_ocr_segments(text):
        clause = raw_clause.strip(" ,，;；。、")
        if not clause:
            continue
        if marker in _VIDEO_DRAFT_PACKAGE_TEXT_LABELS:
            label, cleaned = "包装原字", clause
        elif marker in _VIDEO_DRAFT_SUBTITLE_LABELS:
            label, cleaned = "后期字幕（后期叠加）", clause
        else:
            cleaned = clause
            is_subtitle = bool(_VIDEO_DRAFT_OVERLAY_CUE_RE.search(cleaned))
            label = "后期字幕（后期叠加）" if is_subtitle else "可见原字"
        cleaned = _without_generation_watermark(cleaned)
        item = (label, cleaned)
        if cleaned and item not in details:
            details.append(item)
    return details


def _without_global_audio_cues(value: object, global_audio: str) -> str:
    """Keep shot-synchronous sound while avoiding a repeated global music bed."""
    shot_audio = _generation_ready_audio_value(value)
    global_has_music = bool(_VIDEO_DRAFT_MUSIC_CUE_RE.search(global_audio))
    clauses = []
    for raw_clause in re.split(r"[\n；;]+", shot_audio):
        fragments = []
        for raw_fragment in re.split(r"[,，、]+", raw_clause):
            fragment = raw_fragment.strip(" ,，;；。、")
            if global_has_music:
                fragment = _VIDEO_DRAFT_MUSIC_CUE_RE.sub("", fragment)
                fragment = re.sub(r"(?:伴随|配合|叠加|和|与)\s*$", "", fragment)
                fragment = fragment.strip(" ,，;；。、")
            if fragment and fragment not in fragments:
                fragments.append(fragment)
        clause = "，".join(fragments)
        if not clause:
            continue
        if clause not in clauses:
            clauses.append(clause)
    return "；".join(clauses)


def _generation_spoken_identity(value: object) -> str:
    text = _generation_ready_audio_value(value)
    text = _VIDEO_DRAFT_SPEECH_ROLE_RE.sub("", text)
    return _generation_text_identity(text)


def _shot_spoken_cues(value: object) -> list[str]:
    return [
        clause.strip(" ,，;；。、")
        for clause in re.split(r"[\n；;]+", _generation_ready_audio_value(value))
        if _VIDEO_DRAFT_SPEECH_CUE_RE.search(clause)
        and clause.strip(" ,，;；。、")
    ]


def _without_covered_spoken_cues(value: object, shot_speech_cues: list[str]) -> str:
    shot_identities = [
        identity
        for cue in shot_speech_cues
        if (identity := _generation_spoken_identity(cue))
    ]
    clauses = []
    for raw_clause in re.split(
        r"[\n；;]+",
        _without_generation_parameters_and_timing(value),
    ):
        clause = raw_clause.strip(" ,，;；。、")
        identity = _generation_spoken_identity(clause)
        if not clause or (
            identity
            and any(identity in shot_id or shot_id in identity for shot_id in shot_identities)
        ):
            continue
        if clause not in clauses:
            clauses.append(clause)
    return "；".join(clauses)


def _subtitle_summary_is_covered(summary: str, shot_subtitles: list[str]) -> bool:
    summary_parts = [
        _generation_text_identity(clause)
        for clause in re.split(r"[\n；;,，、]+", summary)
        if _generation_text_identity(clause)
    ]
    shot_parts = [_generation_text_identity(value) for value in shot_subtitles]
    return bool(summary_parts) and all(
        any(part in shot_part or shot_part in part for shot_part in shot_parts)
        for part in summary_parts
    )


def compose_video_generation_draft(
    structured: dict,
    shots: list[dict] | None = None,
) -> str:
    """Build the evidence-backed canonical draft shown to users.

    This draft keeps the complete shot order and visual facts. Output ratio,
    resolution, and duration are selected separately at generation submit.
    """
    if not isinstance(structured, dict):
        structured = {}
    lines: list[str] = []

    static_parts = []
    for key in _ordered_visual_fields(structured, "video"):
        if key in {
            "源视频规格", "时长建议", "时序分镜",
            *_VIDEO_DRAFT_PLANNING_FIELDS,
        }:
            continue
        value = _without_generation_parameters_and_timing(structured.get(key))
        if not value or value in static_parts:
            continue
        label = _VIDEO_DRAFT_FIELD_LABELS.get(key, key)
        static_parts.append(value)
        lines.append(f"{label}：{value}")

    valid_shots = [shot for shot in shots or [] if isinstance(shot, dict)]
    global_audio = _generation_ready_audio_value(structured.get("音效"))
    shot_post_subtitles: list[str] = []
    shot_speech_cues: list[str] = []

    for index, shot in enumerate(valid_shots, start=1):
        details = []
        for key, label in _VIDEO_DRAFT_SHOT_LABELS:
            value = _generation_draft_shot_value(shot, key)
            if value:
                details.append(f"{label}：{value}")
        for label, value in _video_draft_ocr_details(shot):
            if label.startswith("后期字幕") and value not in shot_post_subtitles:
                shot_post_subtitles.append(value)
            details.append(f"{label}：{value}")
        shot_speech_cues.extend(
            cue
            for cue in _shot_spoken_cues(shot.get("audio_cue"))
            if cue not in shot_speech_cues
        )
        audio_cue = _without_global_audio_cues(shot.get("audio_cue"), global_audio)
        if audio_cue:
            details.append(f"同步声音：{audio_cue}")
        if not details:
            continue
        segment_index = shot.get("source_segment_index")
        segment = (
            f"片段{segment_index} "
            if isinstance(segment_index, int) and not isinstance(segment_index, bool)
            else ""
        )
        lines.append(f"{segment}镜头{index}：" + "；".join(details))

    for key in _VIDEO_DRAFT_POST_FIELDS:
        value = (
            global_audio
            if key == "音效"
            else _without_covered_spoken_cues(
                structured.get(key),
                shot_speech_cues,
            )
            if key == "旁白"
            else _without_generation_parameters_and_timing(structured.get(key))
        )
        if not value or value in static_parts:
            continue
        if key == "字幕卖点" and _subtitle_summary_is_covered(
            value,
            shot_post_subtitles,
        ):
            continue
        static_parts.append(value)
        lines.append(f"{_VIDEO_DRAFT_FIELD_LABELS[key]}：{value}")

    if not lines:
        raise ReverseResultValidationError("反推结果缺少可用于生成的视觉白名单字段")
    return "\n".join(lines)
