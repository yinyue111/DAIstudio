"""Compile user video instructions without image-prompt truncation."""

from __future__ import annotations

import json
import re
from math import ceil, isfinite
from typing import Any

COMPILER_VERSION = "video-prompt-v6"
VIDEO_SUBMIT_CONTRACT_VERSION = "video-submit-v3"
PRODUCT_SUBJECT_LOCK = (
    "上传产品是唯一商品主体；仅锁定同一SKU的包装外形与比例、Logo、"
    "包装结构、品牌色、标签排版、可见文字和材质纹理。"
)
DIRECT_PRODUCT_SUBJECT_LOCK = (
    "以上传产品图为唯一商品主体，保持同一SKU的包装结构、Logo、包装文字、"
    "颜色、材质和纹理一致。"
)
PORTRAIT_SUBJECT_LOCK = (
    "上传人像是唯一人物身份；保持同一成年人的脸型、五官比例、发际线、"
    "发型、肤色、年龄感、可识别特征和自然身体比例。"
)
_NON_PRODUCT_IDENTITY_RE = re.compile(r"人物|女主|男主|场景|背景|光线|灯光|构图|镜头")
_NON_PORTRAIT_IDENTITY_RE = re.compile(r"场景|背景|光线|灯光|构图|镜头|道具")
_ACTION_SIGNAL_RE = re.compile(
    r"走(?:近|到|向|入|进|出)?|伸懒腰|抽出|取出|拉出|展开|浸入|浸水|拧干|"
    r"洗脸(?!巾)|擦拭|轻拭|擦脸|擦手|撕开|撕扯|"
    r"打开|关闭|按压|倒(?:入|出|水)?|涂抹|挤出|揭开|拧开|盖上|倾倒|喷洒|泵出|"
    r"旋转|转动|移动|推进|推近|拉远|切到|切换|入镜|出镜|展示|"
    r"拿起|捏住|对折|落下|飞溅|喷溅|后退|抬起|放下|抓住|"
    r"闭眼|微笑|说话|跳起|跑动|坐下|站起|躺下|转身|摇镜|环绕|跟拍|聚焦|静置",
    re.IGNORECASE,
)
_EXPLICIT_STYLE_CONTEXT_RE = re.compile(
    r"风格|广告|色调|配色|光线|灯光|氛围|质感|日系|电影感|商业|写实|影棚|"
    r"自然光|柔光|侧光|逆光|轮廓光|顶光|微距|浅景深|景深|"
    r"特写|近景|中景|远景|广角|长焦|构图|视角|运镜|"
    r"固定镜头|稳定镜头|手持镜头|镜头语言|镜头视角|镜头焦段|镜头运动|"
    r"色系|主调|冷暖|偏冷|偏暖|慢动作|稳定器|晃动|推拉",
    re.IGNORECASE,
)
_SCENE_CONTEXT_RE = re.compile(r"家庭浴室|浴室|卫生间|卧室|场景|背景", re.IGNORECASE)
_SUBJECT_SIGNAL_RE = re.compile(
    r"女主|男主|主角|模特|人物|她|他|产品|商品|包装|洗脸巾|手部|双手|单手",
    re.IGNORECASE,
)
_SCENE_SUBJECT_STRUCTURE_RE = re.compile(
    r"(?P<subject>[^,，。；;]{1,40}?)(?:位于|在|于)(?:家庭浴室|浴室|卫生间|卧室|场景|背景|环境)",
    re.IGNORECASE,
)
_CONTEXT_SUBJECT_RE = re.compile(
    r"(?:画面|视频|本片|场景|背景|环境|空间|地点|拍摄地点|故事|广告)(?:设置|设定|发生|位于)?",
    re.IGNORECASE,
)
_PRODUCT_VIDEO_STRATEGIES = {
    "prompt_driven": (
        "产品视频策略：提示词驱动。严格执行场景脚本中的产品动作、运镜和节奏，"
        "不额外添加、替换或限制动作；外观身份锁定只用于保持同一 SKU 的包装和纹理。"
    ),
    "reference_sequence": (
        "产品视频策略：参考分镜。依次执行当前分镜的动作、景别和转场，"
        "最后以清晰完整的产品镜头收尾；不强制每个镜头静态正面。"
    ),
    "stable_showcase": (
        "产品视频策略：稳定陈列。产品居中且正面文字面朝向镜头，"
        "主要变化仅来自背景光影、台面反射、轻微景深和慢速转场。"
    ),
    "slow_push": (
        "产品视频策略：慢速推近。镜头仅做低速推近或轻微拉远，"
        "产品不翻面，Logo 和主要文字保持清晰。"
    ),
    "handheld_display": (
        "产品视频策略：手持展示。手部只扶住产品边缘或底部，"
        "不遮挡 Logo、包装文字、抽口和关键结构。"
    ),
    "background_motion": (
        "产品视频策略：背景动效。产品稳定完整入镜，"
        "动态主要发生在背景光线、道具、烟雾或台面反射上。"
    ),
    "soft_splash": (
        "产品视频策略：轻水花。水花、泡沫或颗粒只在产品底部和背景边缘运动，"
        "不覆盖 Logo、包装文字、正面标签和产品轮廓。"
    ),
    "single_clip_action": (
        "产品视频策略：单段动作展示。按场景脚本连续完成一次核心产品交互，"
        "动作后以产品结构或材质细节清晰可见的近景收束。"
    ),
}
PRODUCT_VIDEO_TEMPLATE_KEYS = frozenset(_PRODUCT_VIDEO_STRATEGIES)
DEFAULT_VIDEO_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "generic": {
        "family": "generic",
        "seconds_per_shot": 5,
        "max_shots_cap": 3,
        "prompt_budget_chars": 1600,
    },
    "seedance": {
        "family": "seedance",
        "seconds_per_shot": 5,
        "max_shots_cap": 3,
        "prompt_budget_chars": 1800,
    },
    "seedance_mini": {
        "family": "seedance_mini",
        "seconds_per_shot": 6,
        "max_shots_cap": 2,
        "prompt_budget_chars": 1200,
    },
    "grok": {
        "family": "grok",
        "seconds_per_shot": 4,
        "max_shots_cap": 4,
        "prompt_budget_chars": 1800,
    },
}
_STRUCTURED_CONTEXT_KEYS = (
    "图像类型",
    "反推重点",
    "参考片复刻",
    "主体",
    "人像意图",
    "人物比例",
    "身材体态",
    "体态线条",
    "服装结构",
    "服装覆盖",
    "妆发五官",
    "商品服装",
    "细节特征",
    "场景背景",
    "广告目标",
    "景别",
    "构图",
    "视角镜头",
    "视角构图",
    "镜头运动",
    "运动节奏",
    "剪辑节奏",
    "时长建议",
    "色调配色",
    "材质纹理",
    "氛围情绪",
    "后期质感",
    "转场",
    "一致性约束",
)


# —— 证据门契约（gateway_prompting.constrain_video_shots_to_evidence 的输出）——
# 每个 shot 携带 evidence_gate = {"action"/"camera"/"transition": _gate_entry}，
# _gate_entry: {verified: bool, confidence: "analyzer"|"vlm_only"|None,
# score: float|None, source: str|None, reason: str|None}。编译器按三态措辞：
# verified 正常写入；vlm_only 写入但如实标注推断；清空(空文本)不写入。
# 标签表与 gateway_prompting._CAMERA_LABEL_TEXT_PATTERNS/_CAMERA_LABEL_CLAUSES
# 保持同步，用于"冲突时以分析器标签为准"的兜底对账（正常情况下证据门已替换）。
_EVIDENCE_CAMERA_LABEL_PATTERNS: dict[str, re.Pattern] = {
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
_EVIDENCE_CAMERA_LABEL_CLAUSES: dict[tuple[str, str | None], str] = {
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
_EVIDENCE_CAMERA_MIN_CONFIDENCE = 0.4
_HARD_CUT_TRANSITION_RE = re.compile(r"硬切|直切|跳切|hard\s*cut", re.IGNORECASE)
# 未经独立分析器验证、由证据门降级保留的描述，写入提示词时的如实措辞后缀。
UNVERIFIED_EVIDENCE_SUFFIX = "（依据抽样帧推断）"
# 服务器确认硬切时编译出的剪辑节奏描述（写入技术约束段）。
HARD_CUT_RHYTHM_CLAUSE = "剪辑节奏：镜头间使用干净硬切衔接，不使用叠化、淡入淡出等软转场"
_HARD_CUT_SHOT_CLAUSE = "镜头末尾干净硬切进入下一镜"


def _clauses(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n。；;]+", str(value or "")) if part.strip()]


def _finite_seconds(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _evidence_marker(entry: Any) -> dict[str, Any]:
    """Normalize one evidence_gate entry into the stable frontend-facing marker."""
    entry = entry if isinstance(entry, dict) else {}
    confidence = entry.get("confidence")
    source = entry.get("source")
    reason = entry.get("reason")
    try:
        score = float(entry.get("score"))
    except (TypeError, ValueError):
        score = None
    if score is not None and not isfinite(score):
        score = None
    return {
        "verified": bool(entry.get("verified")),
        "confidence": str(confidence) if isinstance(confidence, str) and confidence else None,
        "score": score,
        "source": str(source) if isinstance(source, str) and source else None,
        "reason": str(reason) if isinstance(reason, str) and reason else None,
    }


def _extract_evidence_shots(raw_prompt: dict[str, Any]) -> list[dict]:
    """Find evidence-gated shot dicts attached by the reverse analysis pipeline."""
    analysis = raw_prompt.get("video_analysis")
    if isinstance(analysis, dict):
        rows = analysis.get("shots")
        if isinstance(rows, list):
            gated = [
                row
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("evidence_gate"), dict)
            ]
            if gated:
                return gated
    timeline = raw_prompt.get("时序分镜") or raw_prompt.get("shots")
    if isinstance(timeline, list):
        gated = [
            row
            for row in timeline
            if isinstance(row, dict) and isinstance(row.get("evidence_gate"), dict)
        ]
        if gated:
            return gated
    return []


def _reconcile_camera_with_analyzer(shot: dict, text: str) -> tuple[str, bool]:
    """Prefer the optical-flow label over conflicting VLM camera wording.

    正常情况下证据门已完成替换，这里是兜底：当上游传入未对账的 shot、
    且光流分类可用（analyzed、标签已知、置信度达标）而 VLM 文本声称
    另一种可识别运镜时，以分析器标签编译运镜子句。
    """
    summary = shot.get("camera_motion_summary")
    summary = summary if isinstance(summary, dict) else {}
    label = str(summary.get("dominant_label") or "").strip().lower()
    try:
        score = float(summary.get("confidence"))
    except (TypeError, ValueError):
        return text, False
    if (
        str(summary.get("status") or "").strip().lower() != "analyzed"
        or label not in _EVIDENCE_CAMERA_LABEL_PATTERNS
        or score < _EVIDENCE_CAMERA_MIN_CONFIDENCE
        or not text
    ):
        return text, False
    text_labels = {
        key
        for key, pattern in _EVIDENCE_CAMERA_LABEL_PATTERNS.items()
        if pattern.search(text)
    }
    if not text_labels or label in text_labels:
        # 无法对账（无可识别关键词）或本就一致：保持证据门的判定，不强行替换。
        return text, False
    direction = summary.get("dominant_direction")
    key = direction if isinstance(direction, str) and direction else None
    clause = (
        _EVIDENCE_CAMERA_LABEL_CLAUSES.get((label, key))
        or _EVIDENCE_CAMERA_LABEL_CLAUSES[(label, None)]
    )
    return clause, True


def _compile_evidence_shots(shots: list[dict]) -> dict[str, Any]:
    """Compile evidence-gated shots into prompt lines plus per-field trust markers.

    - verified 字段照常写入提示词；
    - vlm_only（证据门降级保留）也写入，但追加"依据抽样帧推断"措辞，
      并在 shot_evidence 里保留 verified=false 供前端展示可信度差异；
    - 服务器确认的硬切编译为剪辑节奏描述（每镜子句 + 全局 rhythm 子句）；
    - 三个字段全空的 shot 不产出提示词行，但保留判定记录。
    """
    compiled: list[str] = []
    shot_evidence: list[dict[str, Any]] = []
    has_hard_cut = False
    for index, shot in enumerate(shots, start=1):
        gate = shot.get("evidence_gate")
        gate = gate if isinstance(gate, dict) else {}
        action_marker = _evidence_marker(gate.get("action"))
        camera_marker = _evidence_marker(gate.get("camera"))
        transition_marker = _evidence_marker(gate.get("transition"))
        visual = clean_video_prompt_section(shot.get("visual"))
        lighting = clean_video_prompt_section(shot.get("lighting"))
        action = clean_video_prompt_section(shot.get("action"))
        camera = clean_video_prompt_section(shot.get("camera"))
        transition = clean_video_prompt_section(shot.get("transition"))

        action_prompt = ""
        if action:
            action_prompt = (
                action
                if action_marker["verified"]
                else f"{action}{UNVERIFIED_EVIDENCE_SUFFIX}"
            )

        camera_prompt = ""
        if camera:
            camera, replaced = _reconcile_camera_with_analyzer(shot, camera)
            if replaced:
                camera_marker = {
                    **camera_marker,
                    "verified": True,
                    "confidence": "analyzer",
                    "source": "opencv_lk_homography",
                    "reason": "VLM 运镜描述与光流主导标签冲突，编译时已以分析器结论为准",
                }
            camera_prompt = (
                camera
                if camera_marker["verified"]
                else f"{camera}{UNVERIFIED_EVIDENCE_SUFFIX}"
            )

        transition_prompt = ""
        if transition:
            is_hard_cut = bool(
                _HARD_CUT_TRANSITION_RE.search(transition)
                or transition_marker["source"] == "ffmpeg_scene"
            )
            if transition_marker["verified"] and is_hard_cut:
                transition_prompt = _HARD_CUT_SHOT_CLAUSE
                has_hard_cut = True
            elif transition_marker["verified"]:
                transition_prompt = transition
            else:
                transition_prompt = f"{transition}{UNVERIFIED_EVIDENCE_SUFFIX}"

        details = [
            part
            for part in dict.fromkeys(
                (visual, lighting, action_prompt, camera_prompt, transition_prompt)
            )
            if part
        ]
        start = _finite_seconds(shot.get("start_seconds"))
        end = _finite_seconds(shot.get("end_seconds"))
        line = ""
        if details:
            detail = "，".join(details)
            timing = (
                f"{start:.3f}-{end:.3f}s "
                if start is not None and end is not None and end >= start
                else ""
            )
            line = f"{timing}{detail}"
            compiled.append(line)
        shot_evidence.append({
            "index": index,
            "start_seconds": start,
            "end_seconds": end,
            "included": bool(line),
            "action": {**action_marker, "text": action, "prompt_text": action_prompt},
            "camera": {**camera_marker, "text": camera, "prompt_text": camera_prompt},
            "transition": {
                **transition_marker,
                "text": transition,
                "prompt_text": transition_prompt,
            },
        })
    return {
        "shots": compiled,
        "shot_evidence": shot_evidence,
        "rhythm": HARD_CUT_RHYTHM_CLAUSE if has_hard_cut else "",
    }


def _clean_identity_profile(value: Any, *, product: bool) -> str:
    if isinstance(value, dict):
        parts = [f"{key}：{item}" for key, item in value.items() if str(item).strip()]
    else:
        parts = _clauses(str(value or ""))
    if product:
        parts = [part for part in parts if not _NON_PRODUCT_IDENTITY_RE.search(part)]
    else:
        parts = [part for part in parts if not _NON_PORTRAIT_IDENTITY_RE.search(part)]
    cleaned = "；".join(parts)
    return re.sub(r"^(?:产品|人物)?身份约束\s*[:：]\s*", "", cleaned).strip()


def _join_unique(*values: str) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in _clauses(value):
            key = re.sub(r"\s+", "", part).lower()
            if key and key not in seen:
                seen.add(key)
                parts.append(part)
    return "；".join(parts)


def _join_voiceovers(*values: str) -> str:
    """Deduplicate voiceover blocks without stripping sentence punctuation."""
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in re.split(r"[\n；;]+", str(value or "")):
            item = part.strip()
            key = re.sub(r"\s+", "", item).lower()
            if key and key not in seen and not _is_post_placeholder(item):
                seen.add(key)
                parts.append(item)
    return "；".join(parts)


def _unique_items(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        key = re.sub(r"\s+", "", item).lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


_POST_PLACEHOLDER_VALUES = frozenset({
    "无",
    "暂无",
    "不适用",
    "未知",
    "未分析",
    "未提供",
    "未识别",
    "未支持",
    "无法确认",
})


def _is_post_placeholder(value: Any) -> bool:
    return str(value or "").strip().lower() in _POST_PLACEHOLDER_VALUES


def _meaningful_post_items(values: list[str]) -> list[str]:
    return _unique_items([value for value in values if not _is_post_placeholder(value)])


def _has_subject_scene_structure(value: str) -> bool:
    match = _SCENE_SUBJECT_STRUCTURE_RE.search(value)
    if not match:
        return False
    subject = match.group("subject").strip()
    subject = re.sub(
        r"^(?:(?:整体|主要|当前|真实|生活化|极简|高端|日系|写实)\s*)+",
        "",
        subject,
    )
    return bool(subject) and _CONTEXT_SUBJECT_RE.fullmatch(subject) is None


def _looks_like_global_context(value: str) -> bool:
    if _ACTION_SIGNAL_RE.search(value):
        return False
    if _has_subject_scene_structure(value):
        return False
    if _SUBJECT_SIGNAL_RE.search(value) and _SCENE_CONTEXT_RE.search(value):
        return False
    if _EXPLICIT_STYLE_CONTEXT_RE.search(value):
        return True
    return bool(_SCENE_CONTEXT_RE.search(value)) and not _SUBJECT_SIGNAL_RE.search(value)


def _unlabelled_parts(text: str) -> list[str]:
    parts: list[str] = []
    for clause in _clauses(text):
        comma_parts = [part.strip() for part in re.split(r"[,，]+", clause) if part.strip()]
        action_count = sum(1 for part in comma_parts if _ACTION_SIGNAL_RE.search(part))
        parts.extend(comma_parts if action_count >= 2 else [clause])
    return parts


def video_action_requirements(shots: list[str]) -> list[str]:
    """Strip leading visual context from immutable source action requirements."""
    requirements: list[str] = []
    for shot in shots:
        parts = [part.strip() for part in re.split(r"[,，]+", str(shot or "")) if part.strip()]
        while len(parts) > 1 and _looks_like_global_context(parts[0]):
            parts.pop(0)
        requirement = "，".join(parts).strip(" ,，。；;")
        if requirement:
            requirements.append(requirement)
    return requirements


_PRODUCT_INTERACTION_RE = re.compile(
    r"抽出|拉出|取出|泵出|挤出|打开|揭开|拿起|按压",
    re.IGNORECASE,
)
_PRODUCT_CONSTRAINT_RE = re.compile(
    r"尺寸\s*[:：]|长\s*\d|宽\s*\d|高\s*\d|\d+(?:\.\d+)?\s*cm\s*见方|"
    r"与?参考图保持相同|同一\s*SKU|包装图.*保持相同|纹路\s*(?:为|是|[:：])",
    re.IGNORECASE,
)


def _extract_single_clip_constraints(shots: list[str]) -> tuple[list[str], str]:
    cleaned_shots: list[str] = []
    constraints: list[str] = []
    for shot in shots:
        parts = re.split(r"视频画面要求\s*[:：]", shot, maxsplit=1)
        if len(parts) == 2 and _PRODUCT_CONSTRAINT_RE.search(parts[0]):
            constraints.append(parts[0].strip())
            if parts[1].strip():
                cleaned_shots.append(parts[1].strip())
            continue
        cleaned_shots.append(shot)
    return cleaned_shots, _join_unique(*constraints)


_TECHNICAL_PRIORITY_RE = re.compile(
    r"参考|同一|SKU|Logo|文字|包装|尺寸|比例|纹理|纹路|材质|身份|时长|画面无字",
    re.IGNORECASE,
)


def compact_single_clip_prompt(
    *,
    style: str,
    shots: list[str],
    technical: str,
    budget: int,
    mandatory_technical: str = "",
) -> dict[str, Any]:
    """Compact optional context without shortening any user action."""
    budget = max(80, int(budget or 80))
    compact_style = clean_video_prompt_section(style)
    compact_shots = [clean_video_prompt_section(shot) for shot in shots if clean_video_prompt_section(shot)]
    compact_technical = clean_video_prompt_section(technical)

    def render() -> str:
        parts = [f"风格设定：{compact_style}", "场景脚本："]
        parts.extend(
            f"Shot {index}：{shot}" for index, shot in enumerate(compact_shots, start=1)
        )
        parts.append(f"技术约束：{compact_technical}")
        return "\n".join(parts)

    prompt = render()
    if len(prompt) > budget:
        style_clauses = _clauses(compact_style)
        technical_clauses = _clauses(compact_technical)
        mandatory_clauses = _clauses(mandatory_technical)
        mandatory_keys = {
            re.sub(r"\s+", "", clause).lower() for clause in mandatory_clauses
        }
        required_technical = [
            clause
            for clause in technical_clauses
            if re.sub(r"\s+", "", clause).lower() in mandatory_keys
            or _TECHNICAL_PRIORITY_RE.search(clause)
        ]
        optional_technical = [
            clause for clause in technical_clauses if clause not in required_technical
        ]

        # Keep the three-section contract and never remove product identity rules.
        compact_style = style_clauses[0] if style_clauses else ""
        compact_technical = "；".join(
            required_technical or technical_clauses[:1]
        )

        def append_if_fits(current: str, clause: str, *, target: str) -> str:
            nonlocal compact_style, compact_technical
            candidate = "；".join(part for part in (current, clause) if part)
            previous = compact_style if target == "style" else compact_technical
            if target == "style":
                compact_style = candidate
            else:
                compact_technical = candidate
            if len(render()) <= budget:
                return candidate
            if target == "style":
                compact_style = previous
            else:
                compact_technical = previous
            return current

        for clause in optional_technical:
            compact_technical = append_if_fits(
                compact_technical,
                clause,
                target="technical",
            )
        for clause in style_clauses[1:]:
            compact_style = append_if_fits(compact_style, clause, target="style")
        prompt = render()
    return {
        "prompt": prompt,
        "style": compact_style,
        "shots": compact_shots,
        "technical": compact_technical,
    }


VIDEO_SECTION_ALIASES = {
    "风格设定": "style",
    "视觉风格": "style",
    "全局风格": "style",
    "场景脚本": "scenes",
    "分镜脚本": "scenes",
    "镜头脚本": "scenes",
    "技术约束": "constraints",
    "生成约束": "constraints",
    "制作约束": "constraints",
}


def clean_video_prompt_section(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.strip(" `#*[]，,。；;")


def video_scene_script_items(value: object) -> list[str]:
    if isinstance(value, list):
        candidates = value
    else:
        text = str(value or "").strip()
        numbered = re.split(
            r"(?:^|[\n。；;])\s*(?:Shot|镜头)\s*\d+\s*[:：]\s*",
            text,
            flags=re.IGNORECASE,
        )
        candidates = numbered[1:] if len(numbered) > 1 else _clauses(text)
    shots: list[str] = []
    for candidate in candidates:
        shot = clean_video_prompt_section(candidate)
        shot = re.sub(r"^(?:Shot|镜头)\s*\d+\s*[:：]\s*", "", shot, flags=re.IGNORECASE)
        if shot:
            shots.append(shot)
    return shots


def _structured_video_sections_from_text(text: str) -> dict[str, object] | None:
    sections: dict[str, list[str]] = {"style": [], "scenes": [], "constraints": []}
    current = ""
    found: set[str] = set()
    label_pattern = "|".join(re.escape(label) for label in VIDEO_SECTION_ALIASES)
    for raw_line in str(text or "").replace("\r", "").split("\n"):
        line = raw_line.strip()
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = line.strip(" *`[]")
        match = re.match(
            rf"^(?P<label>{label_pattern})\s*[:：]?\s*(?P<value>.*)$",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            current = VIDEO_SECTION_ALIASES[match.group("label")]
            found.add(current)
            if match.group("value").strip():
                sections[current].append(match.group("value").strip())
            continue
        if current and line:
            sections[current].append(line)
    if not {"style", "scenes", "constraints"}.issubset(found):
        return None
    return {
        "style": " ".join(sections["style"]),
        "scenes": "\n".join(sections["scenes"]),
        "constraints": " ".join(sections["constraints"]),
    }


def parse_structured_video_sections(content: str | dict[str, Any]) -> dict[str, Any] | None:
    """Parse the optimizer's JSON or titled text into three canonical sections."""
    payload: dict[str, Any] | None = content if isinstance(content, dict) else None
    raw = "" if payload is not None else str(content or "").strip().strip("`").strip()
    if payload is None:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                decoded = json.loads(match.group(0))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, dict):
                payload = decoded
    sections: dict[str, object] | None = None
    if payload is not None:
        values: dict[str, object] = {"style": "", "scenes": [], "constraints": ""}
        for label, section in VIDEO_SECTION_ALIASES.items():
            if not values[section] and payload.get(label):
                values[section] = payload[label]
        if values["style"] and values["scenes"] and values["constraints"]:
            sections = values
    if sections is None:
        sections = _structured_video_sections_from_text(raw)
    if sections is None:
        return None
    style = clean_video_prompt_section(sections["style"])
    shots = video_scene_script_items(sections["scenes"])
    constraints = clean_video_prompt_section(sections["constraints"])
    if not style or not shots or not constraints:
        return None
    return {"style": style, "shots": shots, "constraints": constraints}


def merge_video_constraint_clauses(*values: object) -> str:
    clauses: list[str] = []
    seen: set[str] = set()
    for value in values:
        for clause in re.split(r"[\n。；;]+", str(value or "")):
            cleaned = clean_video_prompt_section(clause)
            key = re.sub(r"\s+", "", cleaned).lower()
            if key and key not in seen:
                seen.add(key)
                clauses.append(cleaned)
    return "；".join(clauses)


def render_structured_video_prompt(
    *,
    style: object,
    shots: list[str],
    constraints: object,
) -> str:
    """Render the canonical style + scene script + technical constraints contract."""
    style_text = clean_video_prompt_section(style)
    constraint_text = merge_video_constraint_clauses(constraints)
    clean_shots = [clean_video_prompt_section(shot) for shot in shots]
    shot_lines = "\n".join(
        f"Shot {index}：{shot}"
        for index, shot in enumerate((shot for shot in clean_shots if shot), start=1)
    )
    return (
        f"风格设定：{style_text}。\n" f"场景脚本：\n{shot_lines}\n" f"技术约束：{constraint_text}。"
    )


def _layered_authoritative_text(raw_prompt: dict[str, Any]) -> str:
    user_instruction = str(raw_prompt.get("user_instruction") or "").strip()
    if user_instruction:
        return user_instruction
    input_mode = str(raw_prompt.get("input_mode") or "").strip().lower()
    assembled = str(raw_prompt.get("assembled_text") or "").strip()
    optimized = str(raw_prompt.get("optimized_text") or "").strip()
    raw = str(raw_prompt.get("raw_text") or "").strip()
    if input_mode == "structured_reverse":
        return assembled or str(raw_prompt.get("final_text") or "").strip()
    if assembled and (optimized or not raw or assembled != raw):
        return assembled
    return optimized if optimized and not assembled else ""


def _normalized_prompt_layer(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _direct_passthrough_text(raw_prompt: str | dict[str, Any]) -> str:
    """Return an explicit user draft only when every persisted text layer agrees."""
    if not isinstance(raw_prompt, dict):
        return ""
    input_mode = str(raw_prompt.get("input_mode") or "").strip().lower()
    if input_mode and input_mode not in {"direct", "direct_input", "manual"}:
        return ""
    layers = [
        str(raw_prompt.get(key) or "").strip()
        for key in ("user_instruction", "raw_text", "assembled_text", "final_text")
    ]
    if any(not layer for layer in layers):
        return ""
    normalized = [_normalized_prompt_layer(layer) for layer in layers]
    if len(set(normalized)) != 1:
        return ""
    optimized = _normalized_prompt_layer(raw_prompt.get("optimized_text"))
    if optimized and optimized != normalized[0]:
        return ""
    return layers[0]


def _direct_passthrough_plan(text: str) -> dict[str, Any]:
    """Extract history metadata without rewriting the model-facing user draft."""
    post = split_video_post_production(text)
    return {
        "global_style": "",
        "subject_lock": "",
        "shots": [post["text"]] if post["text"] else [],
        "technical_constraints": "",
        "post_overlays": post["post_overlays"],
        "voiceover": post["voiceover"],
        "sfx": post["sfx"],
        "reference_guidance": [],
        "warnings": [],
        "shot_evidence": [],
    }


def split_video_post_production(text: str) -> dict[str, Any]:
    """Remove explicit post-production clauses from model-facing prompt text."""
    overlays: list[str] = []
    voiceover = ""
    sfx: list[str] = []
    post_flags = re.IGNORECASE | re.MULTILINE
    boundary = r"(?P<boundary>^|[\n。；;\uff0c,])\s*"
    open_quote = "[“\\\"'‘]"
    close_quote = "[”\\\"'’]"
    voiceover_label = (
        r"(?:(?:温柔|轻柔|低沉)?\s*(?:男声|女声|女性|男性)?\s*)?"
        r"(?:旁白|voiceover|后期配音|人物对白|角色对白|对白|台词)"
    )
    label_separator = r"\s*(?:(?:[:：]|为|是|说)|内容\s*(?:为|是))?\s*"
    overlay_label = (
        r"(?:字幕(?:卖点)?|后期叠字|OCR(?:识别)?(?:文字|内容)?|"
        r"(?:画面(?:中)?)?文字(?:浮现|出现|显示)?|"
        r"画面(?:中)?(?:浮现|出现|显示)文字)"
    )
    overlay_separator = (
        r"\s*(?:(?:[:：]|为|是|写着)|内容\s*(?:为|是)|显示\s*(?:为|是)?)?\s*"
    )
    sfx_label = (
        r"(?:音效|SFX|环境音|背景配乐|背景音乐|配乐|音乐|BGM|声音设计|音频)"
    )
    sfx_separator = r"\s*(?:(?:[:：]|为|是|随|随着)|内容\s*(?:为|是))?\s*"

    def remove_voiceover(match: re.Match[str]) -> str:
        nonlocal voiceover
        voiceover = match.group("content").strip()
        return match.group("boundary")

    text = re.sub(
        boundary
        + voiceover_label
        + label_separator
        + open_quote
        + r"(?P<content>[^”\"'’]+)"
        + close_quote,
        remove_voiceover,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary
        + voiceover_label
        + label_separator
        + open_quote
        + r"(?P<content>[^\n。；;]+)$",
        remove_voiceover,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + voiceover_label + label_separator + r"(?P<content>[^\n。；;\uff0c,]+)",
        remove_voiceover,
        text,
        flags=post_flags,
    )

    def remove_overlay(match: re.Match[str]) -> str:
        overlays.append(match.group("content").strip())
        return match.group("boundary")

    text = re.sub(
        boundary
        + overlay_label
        + overlay_separator
        + open_quote
        + r"(?P<content>[^”\"'’]+)"
        + close_quote
        + r"\s*(?:浮现|出现|显示)?",
        remove_overlay,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + overlay_label + overlay_separator + r"(?P<content>[^\n。；;\uff0c,]+)",
        remove_overlay,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + overlay_label + overlay_separator + r"(?P<content>[^\n。；;\uff0c,]+)",
        remove_overlay,
        text,
        flags=post_flags,
    )

    def remove_sfx(match: re.Match[str]) -> str:
        sfx.extend(_clauses(match.group("content")))
        return match.group("boundary")

    text = re.sub(
        boundary
        + sfx_label
        + sfx_separator
        + open_quote
        + r"(?P<content>[^”\"'’]+)"
        + close_quote,
        remove_sfx,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + sfx_label + sfx_separator + r"(?P<content>[^\n。；;\uff0c,]+)",
        remove_sfx,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary
        + r"(?P<content>(?:伴随|配合|同步(?:出现|响起)?)"
        + r"[^\n。；;\uff0c,]*(?:声音|声|音效|配乐|音乐|鼓点|节拍)"
        + r"[^\n。；;\uff0c,]*)",
        remove_sfx,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary
        + r"(?P<content>[^\n。；;\uff0c,]{0,64}"
        + r"(?:水滴声|落水声|脚步声|环境声|人声|鼓点|配乐|背景音乐|音效)"
        + r"[^\n。；;\uff0c,]*)",
        remove_sfx,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary
        + r"(?:OCR(?:识别)?(?:文字|内容)?|观察事实|帧间推断|"
        + r"证据(?:帧|描述|索引)?|analysis\s+evidence)"
        + r"\s*(?:(?:[:：]|为|是|写着)|内容\s*(?:为|是)|显示\s*(?:为|是)?)"
        + r"\s*[^\n。；;]*",
        lambda match: match.group("boundary"),
        text,
        flags=post_flags,
    )
    text = re.sub(r"([。；;，,])\s*(?=[。；;，,])", "", text)
    return {
        "text": text.strip(" \n。；;"),
        "post_overlays": overlays,
        "voiceover": voiceover,
        "sfx": sfx,
    }


def _parse_text_prompt(text: str) -> dict[str, Any]:
    post = split_video_post_production(text)
    text = post["text"]
    structured_sections = parse_structured_video_sections(text)
    if structured_sections is not None:
        return {
            "global_style": structured_sections["style"],
            "subject_lock": "",
            "shots": structured_sections["shots"],
            "technical_constraints": structured_sections["constraints"],
            "post_overlays": post["post_overlays"],
            "voiceover": post["voiceover"],
            "sfx": post["sfx"],
            "reference_guidance": [],
            "warnings": [],
            "shot_evidence": [],
        }
    numbered = re.split(
        r"(?:^|[\n。；;])\s*(?:Shot|镜头)\s*\d+\s*[:：]\s*",
        text,
        flags=re.IGNORECASE,
    )
    if len(numbered) > 1:
        style = numbered[0].strip(" ,，。；;")
        shots = [part.strip(" ,，。；;") for part in numbered[1:] if part.strip(" ,，。；;")]
        technical_constraints = ""
    else:
        action_marker = re.search(r"视频画面要求\s*[:：]", text)
        technical_constraints = ""
        if action_marker:
            prefix_parts = _clauses(text[: action_marker.start()])
            constraint_index = next(
                (
                    index
                    for index, part in enumerate(prefix_parts)
                    if _PRODUCT_CONSTRAINT_RE.search(part)
                ),
                len(prefix_parts),
            )
            style = _join_unique(*prefix_parts[:constraint_index])
            technical_constraints = _join_unique(*prefix_parts[constraint_index:])
            shots = _unlabelled_parts(text[action_marker.end() :])
        else:
            parts = _unlabelled_parts(text)
            style_parts: list[str] = []
            while parts and _looks_like_global_context(parts[0]):
                style_parts.append(parts.pop(0))
            style = _join_unique(*style_parts)
            shots = parts
    return {
        "global_style": style,
        "subject_lock": "",
        "shots": shots,
        "technical_constraints": technical_constraints,
        "post_overlays": post["post_overlays"],
        "voiceover": post["voiceover"],
        "sfx": post["sfx"],
        "reference_guidance": [],
        "warnings": [],
        "shot_evidence": [],
    }


def parse_video_prompt(raw_prompt: str | dict[str, Any]) -> dict[str, Any]:
    """Parse a prompt into the stable public video-plan fields."""
    if isinstance(raw_prompt, dict):
        subject_lock = str(
            raw_prompt.get("subject_lock") or raw_prompt.get("一致性约束") or ""
        ).strip()
        subject = str(raw_prompt.get("主体") or "").strip()
        if not subject_lock and "产品" in str(raw_prompt.get("图像类型") or "") and subject:
            subject_lock = (
                f"全程保持同一产品主体：{subject[:120]}；"
                "包装结构、比例、主色和材质连续一致"
            )
        style = "；".join(
            str(raw_prompt.get(key) or "").strip()
            for key in ("风格", "风格设定", "global_style", "光线")
            if str(raw_prompt.get(key) or "").strip()
        )
        technical_constraints = "；".join(
            str(raw_prompt.get(key) or "").strip()
            for key in ("技术约束", "technical_constraints")
            if str(raw_prompt.get(key) or "").strip()
        )
        context = "；".join(
            f"{key}：{str(raw_prompt.get(key) or '').strip()}"
            for key in _STRUCTURED_CONTEXT_KEYS
            if str(raw_prompt.get(key) or "").strip()
        )
        style = _join_unique(style, context)
        timeline = raw_prompt.get("时序分镜") or raw_prompt.get("shots") or []
        shots = (
            # 证据门产出的 shot 是 dict（见 _extract_evidence_shots），由证据
            # 编译路径处理；这里只接收字符串条目，避免 dict 被 str() 成噪声。
            [
                str(item).strip()
                for item in timeline
                if not isinstance(item, dict) and str(item).strip()
            ]
            if isinstance(timeline, list)
            else _clauses(str(timeline))
        )
        if not shots:
            action = next(
                (
                    str(raw_prompt.get(key) or "").strip()
                    for key in ("可迁移主体动作", "主体动作", "产品展示方式")
                    if str(raw_prompt.get(key) or "").strip()
                ),
                "",
            )
            if action:
                shots = _clauses(action)
        overlay_text = raw_prompt.get("字幕卖点") or raw_prompt.get("post_overlays") or []
        overlays = (
            [str(item).strip() for item in overlay_text if str(item).strip()]
            if isinstance(overlay_text, list)
            else _clauses(str(overlay_text))
        )
        sfx_text = raw_prompt.get("音效") or raw_prompt.get("sfx") or []
        sfx = (
            [str(item).strip() for item in sfx_text if str(item).strip()]
            if isinstance(sfx_text, list)
            else _clauses(str(sfx_text))
        )
        structured_voiceover = str(
            raw_prompt.get("旁白") or raw_prompt.get("voiceover") or ""
        ).strip()
        manual_text = _layered_authoritative_text(raw_prompt)
        if manual_text:
            manual = _parse_text_prompt(manual_text)
            style = manual["global_style"]
            shots = manual["shots"]
            technical_constraints = manual["technical_constraints"] or technical_constraints
            overlays = manual["post_overlays"] or overlays
            raw_prompt_voiceover = manual["voiceover"] or structured_voiceover
            sfx = manual["sfx"] or sfx
        else:
            raw_prompt_voiceover = structured_voiceover
        # 证据门产出的三态运动信息：只要上游附带了 evidence_gate 的 shot
        # 结构、且用户没有手改提示词（user_instruction 为最高优先级），就用
        # 证据编译结果替换从字符串时间轴/组装文本反解出来的分镜——后者已
        # 丢失 verified/vlm_only 标注。
        shot_evidence: list[dict[str, Any]] = []
        evidence_shots = _extract_evidence_shots(raw_prompt)
        if evidence_shots and not str(raw_prompt.get("user_instruction") or "").strip():
            gated = _compile_evidence_shots(evidence_shots)
            shot_evidence = gated["shot_evidence"]
            if gated["shots"]:
                shots = gated["shots"]
                if gated["rhythm"]:
                    technical_constraints = _join_unique(
                        technical_constraints, gated["rhythm"]
                    )
        cleaned_shots: list[str] = []
        shot_overlays: list[str] = []
        shot_voiceovers: list[str] = []
        shot_sfx: list[str] = []
        for shot in shots:
            shot_post = split_video_post_production(shot)
            if shot_post["text"]:
                cleaned_shots.append(shot_post["text"])
            shot_overlays.extend(shot_post["post_overlays"])
            if shot_post["voiceover"]:
                shot_voiceovers.append(shot_post["voiceover"])
            shot_sfx.extend(shot_post["sfx"])
        shots = cleaned_shots
        overlays = _meaningful_post_items([*overlays, *shot_overlays])
        raw_prompt_voiceover = _join_voiceovers(raw_prompt_voiceover, *shot_voiceovers)
        sfx = _meaningful_post_items([*sfx, *shot_sfx])
        if (
            style
            or shots
            or technical_constraints
            or overlays
            or raw_prompt_voiceover
            or sfx
            or shot_evidence
        ):
            if not shots:
                fallback = _parse_text_prompt(
                    str(raw_prompt.get("final_text") or raw_prompt.get("instruction") or "")
                )
                style = _join_unique(style, fallback["global_style"])
                shots = fallback["shots"]
                technical_constraints = _join_unique(
                    technical_constraints,
                    fallback["technical_constraints"],
                )
            return {
                "global_style": style,
                "subject_lock": subject_lock,
                "shots": shots,
                "technical_constraints": technical_constraints,
                "post_overlays": overlays,
                "voiceover": raw_prompt_voiceover,
                "sfx": sfx,
                "reference_guidance": [],
                "warnings": list(raw_prompt.get("warnings") or []),
                "shot_evidence": shot_evidence,
            }
        text = str(raw_prompt.get("final_text") or raw_prompt.get("instruction") or "")
    else:
        text = str(raw_prompt or "")
    return _parse_text_prompt(text)


def _model_family(model_id: str, provider: str) -> str:
    value = f"{provider} {model_id}".lower()
    if "seedance" in value and "mini" in value:
        return "seedance_mini"
    if "seedance" in value or "doubao-seedance" in value:
        return "seedance"
    if "grok" in value:
        return "grok"
    return "generic"


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _duration_shot_limit(profile: dict[str, Any], duration: float) -> int:
    explicit = profile.get("recommended_max_shots")
    if explicit not in (None, ""):
        parsed_explicit = _optional_positive_int(explicit)
        if parsed_explicit is not None:
            return parsed_explicit
    duration_limits = profile.get("max_shots_by_duration")
    if isinstance(duration_limits, dict) and duration_limits:
        parsed = []
        for key, value in duration_limits.items():
            threshold = _optional_positive_float(key)
            limit = _optional_positive_int(value)
            if threshold is not None and limit is not None:
                parsed.append((threshold, limit))
        parsed.sort()
        if parsed:
            for threshold, limit in parsed:
                if duration <= threshold:
                    return limit
            return parsed[-1][1]
    seconds_per_shot = _optional_positive_float(profile.get("seconds_per_shot")) or 5.0
    cap = _optional_positive_int(profile.get("max_shots_cap")) or 3
    return min(cap, max(1, ceil(max(1.0, duration) / seconds_per_shot)))


def infer_video_model_profile(
    *,
    model_id: str = "",
    provider: str = "",
    duration: float = 10,
    extra: dict[str, Any] | None = None,
    model_profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve a configurable capacity profile from provider and model identity."""
    family = _model_family(model_id, provider)
    default_profile = dict(DEFAULT_VIDEO_MODEL_PROFILES[family])
    profile = dict(default_profile)
    configured = model_profiles or {}
    for key in (family, provider, model_id):
        override = configured.get(key)
        if isinstance(override, dict):
            profile.update(override)
    legacy_runtime = (extra or {}).get("prompt_profile")
    if isinstance(legacy_runtime, dict):
        profile.update(legacy_runtime)
    runtime = (extra or {}).get("video_prompt_profile")
    if isinstance(runtime, dict):
        profile.update(runtime)
    profile["seconds_per_shot"] = _optional_positive_float(
        profile.get("seconds_per_shot")
    ) or float(default_profile["seconds_per_shot"])
    profile["max_shots_cap"] = _optional_positive_int(profile.get("max_shots_cap")) or int(
        default_profile["max_shots_cap"]
    )
    prompt_budget = _optional_positive_int(profile.get("max_prompt_chars"))
    if prompt_budget is None:
        prompt_budget = _optional_positive_int(profile.get("prompt_budget_chars"))
    profile["prompt_budget_chars"] = prompt_budget or int(default_profile["prompt_budget_chars"])
    profile["family"] = str(profile.get("family") or family)
    normalized_duration = _optional_positive_float(duration) or 10.0
    profile["recommended_max_shots"] = _duration_shot_limit(
        profile,
        normalized_duration,
    )
    return profile


def compile_video_prompt(
    raw_prompt: str | dict[str, Any],
    *,
    duration: float = 10,
    model_id: str = "",
    provider: str = "",
    extra: dict[str, Any] | None = None,
    references: list[dict[str, Any]] | None = None,
    product_reference: bool = False,
    portrait_reference: bool = False,
    product_lock_mode: str = "locked",
    product_video_template: str = "prompt_driven",
    model_profiles: dict[str, dict[str, Any]] | None = None,
    fit_mode: str = "strict_sequence",
) -> dict[str, Any]:
    """Return a structured plan and a model-ready prompt."""
    direct_passthrough_text = _direct_passthrough_text(raw_prompt)
    plan = (
        _direct_passthrough_plan(direct_passthrough_text)
        if direct_passthrough_text
        else parse_video_prompt(raw_prompt)
    )
    if direct_passthrough_text:
        direct_passthrough_text = plan["shots"][0] if plan["shots"] else ""
    plan["global_style"] = _join_unique(plan["global_style"])
    plan["technical_constraints"] = _join_unique(plan.get("technical_constraints", ""))
    plan["shots"] = [str(item).strip() for item in plan["shots"] if str(item).strip()]
    for key in ("post_overlays", "sfx", "warnings"):
        plan[key] = _unique_items(plan[key])
    plan["shot_evidence"] = [
        dict(entry) for entry in plan.get("shot_evidence") or [] if isinstance(entry, dict)
    ]
    reference_roles = {
        str(item.get("role") or "").strip().lower()
        for item in (references or [])
        if isinstance(item, dict)
    }
    has_product_reference = product_reference or "product" in reference_roles
    has_portrait_reference = portrait_reference or bool(
        reference_roles.intersection({"character", "portrait", "person"})
    )
    reference_guidance: list[str] = []
    if "style" in reference_roles:
        reference_guidance.append(
            "风格参考仅迁移色调、光线、材质和商业质感，" "不迁移主体身份、商品、Logo、文字或动作。"
        )
    if "motion" in reference_roles:
        reference_guidance.append(
            "动作参考仅迁移动作节奏、走位、镜头节奏和转场，"
            "不迁移人物身份、商品、品牌、场景或文字。"
        )
    if "motion_analysis" in reference_roles:
        reference_guidance.append(
            "镜头顺序与节奏以原视频抽帧反推后的当前场景脚本为准。"
        )
    has_first_frame = "first_frame" in reference_roles
    has_last_frame = "last_frame" in reference_roles
    if has_first_frame and has_last_frame:
        reference_guidance.append(
            "首帧定义开始状态，尾帧定义结束状态；"
            "只生成两帧之间的连续过渡，保持同一主体和场景逻辑。"
        )
    elif has_first_frame:
        reference_guidance.append("首帧定义开始状态；从该状态连续运动，不重设主体和场景。")
    elif has_last_frame:
        reference_guidance.append("尾帧定义结束状态；动作和镜头平滑收束到该目标帧。")
    plan["reference_guidance"] = reference_guidance
    product_lock = ""
    portrait_lock = ""
    if has_product_reference:
        if direct_passthrough_text:
            product_lock = DIRECT_PRODUCT_SUBJECT_LOCK
        else:
            product_profile = (
                raw_prompt.get("产品身份档案") or raw_prompt.get("product_profile_text") or ""
                if isinstance(raw_prompt, dict)
                else ""
            )
            product_lock = _join_unique(
                _clean_identity_profile(product_profile, product=True),
                PRODUCT_SUBJECT_LOCK,
            )
    if has_portrait_reference:
        portrait_profile = (
            raw_prompt.get("人物身份档案") or raw_prompt.get("subject_profile_summary") or ""
            if isinstance(raw_prompt, dict)
            else ""
        )
        portrait_lock = _join_unique(
            _clean_identity_profile(portrait_profile, product=False),
            PORTRAIT_SUBJECT_LOCK,
        )
    plan["subject_lock"] = _join_unique(plan["subject_lock"], product_lock, portrait_lock)
    normalized_lock_mode = "free" if str(product_lock_mode).lower() == "free" else "locked"
    normalized_template = str(product_video_template or "prompt_driven").lower().strip()
    normalized_fit_mode = (
        "single_clip" if str(fit_mode).strip().lower() == "single_clip" else "strict_sequence"
    )
    profile = infer_video_model_profile(
        model_id=model_id,
        provider=provider,
        duration=duration,
        extra=extra,
        model_profiles=model_profiles,
    )
    if direct_passthrough_text and has_product_reference:
        prompt = (
            f"产品身份约束：{DIRECT_PRODUCT_SUBJECT_LOCK}\n"
            f"原始生成要求：\n{direct_passthrough_text}"
        )
        prompt_char_count = len(prompt)
        prompt_budget_chars = max(1, int(profile.get("prompt_budget_chars") or 1))
        prompt_overload = prompt_char_count > prompt_budget_chars
        if prompt_overload:
            plan["warnings"].append(
                f"直输提示词已按原文完整保留；当前长度超过模型建议的 "
                f"{prompt_budget_chars} 字符预算，模型可能弱化部分细节。"
            )
        return {
            "prompt": prompt,
            "plan": plan,
            "sequence_required": False,
            "profile": profile,
            "compiler_version": COMPILER_VERSION,
            "metadata": {
                "duration": duration,
                "model_id": model_id,
                "provider": provider,
                "prompt_mode": "direct_passthrough",
                "shot_count": 1,
                "source_shot_count": 1,
                "selected_shot_count": 1,
                "prompt_char_count": prompt_char_count,
                "prompt_budget_chars": prompt_budget_chars,
                "prompt_over_budget": prompt_overload,
                "omitted_shot_count": 0,
                "condensed_for_single_clip": False,
                "compacted_for_budget": False,
                "fit_mode": normalized_fit_mode,
                "recommended_clip_count": 1,
                "reference_roles": sorted(reference_roles),
                "motion_reference_mode": "",
                "product_lock_mode": normalized_lock_mode,
                "product_video_template": normalized_template,
                "post_overlays": list(plan["post_overlays"]),
                "voiceover": plan["voiceover"],
                "sfx": list(plan["sfx"]),
                "technical_constraints": "",
                "shot_evidence": [dict(entry) for entry in plan["shot_evidence"]],
            },
        }
    product_strategy = ""
    locked_product_guard = ""
    if has_product_reference:
        product_strategy = _PRODUCT_VIDEO_STRATEGIES.get(
            normalized_template,
            _PRODUCT_VIDEO_STRATEGIES["prompt_driven"],
        )
        if normalized_lock_mode == "locked" and normalized_template == "prompt_driven":
            locked_product_guard = (
                "产品身份保真：在用户指定动作和运镜过程中，保持同一 SKU 的包装结构、"
                "Logo、可见文字、颜色和材质纹理连续一致；不得据此删除、替换或降速用户动作。"
            )
            product_strategy = f"{locked_product_guard}{product_strategy}"
        elif normalized_lock_mode == "locked":
            locked_product_guard = (
                "文字保真模式：包装正面、Logo 和主要文字持续清晰可见，"
                "避免快速旋转、翻面、强运动模糊、遮挡或裁切产品。"
            )
            product_strategy = f"{locked_product_guard}{product_strategy}"
        plan["product_strategy"] = product_strategy
    if normalized_fit_mode == "single_clip":
        plan["shots"], promoted_constraints = _extract_single_clip_constraints(plan["shots"])
        plan["technical_constraints"] = _join_unique(
            plan["technical_constraints"],
            promoted_constraints,
        )
    source_shots = list(plan["shots"])
    source_shot_count = len(source_shots)
    max_shots = int(profile["recommended_max_shots"])
    if normalized_fit_mode == "single_clip" and source_shot_count > max_shots:
        plan["warnings"].append(
            f"当前 {duration:g} 秒单视频包含 {source_shot_count} 个动作段落，动作密度较高；"
            "已保留全部用户动作，模型可能弱化部分细节。"
        )
    if (
        normalized_fit_mode == "single_clip"
        and has_product_reference
        and normalized_template == "stable_showcase"
        and any(_PRODUCT_INTERACTION_RE.search(shot) for shot in plan["shots"])
    ):
        product_strategy = (
            f"{locked_product_guard}{_PRODUCT_VIDEO_STRATEGIES['single_clip_action']}"
        )
        plan["product_strategy"] = product_strategy
    shot_count = len(plan["shots"])
    prompt_parts = [f"风格设定：{plan['global_style']}", "场景脚本："]
    prompt_parts.extend(
        f"Shot {number}：{shot}" for number, shot in enumerate(plan["shots"], start=1)
    )
    technical_parts = [plan["technical_constraints"]]
    mandatory_technical_parts = [plan["technical_constraints"]]
    if product_lock:
        product_identity_constraint = f"产品身份约束：{product_lock}"
        technical_parts.append(product_identity_constraint)
        mandatory_technical_parts.append(product_identity_constraint)
    if portrait_lock:
        portrait_identity_constraint = f"人物身份约束：{portrait_lock}"
        technical_parts.append(portrait_identity_constraint)
        mandatory_technical_parts.append(portrait_identity_constraint)
    if plan["subject_lock"] and not (product_lock or portrait_lock):
        subject_constraint = f"主体锁定：{plan['subject_lock']}"
        technical_parts.append(subject_constraint)
        mandatory_technical_parts.append(subject_constraint)
    if product_strategy:
        technical_parts.append(product_strategy)
    technical_parts.extend(reference_guidance)
    mandatory_technical_parts.extend(reference_guidance)
    technical_text = _join_unique(*technical_parts)
    mandatory_technical_text = _join_unique(*mandatory_technical_parts)
    prompt_parts.append(f"技术约束：{technical_text}")
    prompt = "\n".join(prompt_parts)
    prompt_char_count = len(prompt)
    prompt_budget_chars = max(1, int(profile.get("prompt_budget_chars") or 1))
    compacted_for_budget = False
    if normalized_fit_mode == "single_clip" and prompt_char_count > prompt_budget_chars:
        compacted = compact_single_clip_prompt(
            style=plan["global_style"],
            shots=plan["shots"],
            technical=technical_text,
            budget=prompt_budget_chars,
            mandatory_technical=mandatory_technical_text,
        )
        prompt = str(compacted["prompt"])
        plan["global_style"] = str(compacted["style"])
        plan["shots"] = list(compacted["shots"])
        technical_text = str(compacted["technical"])
        prompt_char_count = len(prompt)
        compacted_for_budget = True
        plan["warnings"].append(
            f"已按单视频提示词预算将模型输入精简至 {prompt_char_count} 字符。"
        )
    shot_overload = shot_count > max_shots
    prompt_overload = prompt_char_count > prompt_budget_chars
    sequence_required = normalized_fit_mode != "single_clip" and (
        prompt_overload or shot_overload
    )
    if shot_overload:
        if normalized_fit_mode == "single_clip":
            plan["warnings"].append(
                f"当前 {duration:g} 秒/{profile['family']} 常规建议最多 {max_shots} 个镜头；"
                f"单视频模式已保留全部 {shot_count} 个动作段落。"
            )
        else:
            plan["warnings"].append(
                f"当前 {duration:g} 秒/{profile['family']} 建议最多 {max_shots} 个镜头；"
                f"已保留全部 {shot_count} 个镜头，请拆分为多段生成。"
            )
    if prompt_overload:
        if normalized_fit_mode == "single_clip":
            plan["warnings"].append(
                f"当前模型提示词预算约 {prompt_budget_chars} 字符；"
                "单视频模式已保留全部动作和产品硬约束，模型可能弱化部分细节。"
            )
        else:
            plan["warnings"].append(
                f"当前模型提示词预算约 {prompt_budget_chars} 字符；"
                f"已保留全部 {prompt_char_count} 字符，请拆分为多段生成。"
            )
    recommended_clip_count = (
        1
        if normalized_fit_mode == "single_clip"
        else max(
            1,
            ceil(shot_count / max_shots),
            ceil(prompt_char_count / prompt_budget_chars),
        )
    )
    return {
        "prompt": prompt,
        "plan": plan,
        "sequence_required": sequence_required,
        "profile": profile,
        "compiler_version": COMPILER_VERSION,
        "metadata": {
            "duration": duration,
            "model_id": model_id,
            "provider": provider,
            "shot_count": shot_count,
            "source_shot_count": source_shot_count,
            "selected_shot_count": shot_count,
            "prompt_char_count": prompt_char_count,
            "prompt_budget_chars": prompt_budget_chars,
            "prompt_over_budget": prompt_overload,
            "omitted_shot_count": max(0, source_shot_count - shot_count),
            "condensed_for_single_clip": (
                normalized_fit_mode == "single_clip" and source_shot_count > shot_count
            ),
            "compacted_for_budget": compacted_for_budget,
            "fit_mode": normalized_fit_mode,
            "recommended_clip_count": recommended_clip_count,
            "reference_roles": sorted(reference_roles),
            "motion_reference_mode": (
                "provider_reference"
                if "motion" in reference_roles
                else "analysis_only"
                if "motion_analysis" in reference_roles
                else ""
            ),
            "product_lock_mode": normalized_lock_mode if has_product_reference else "",
            "product_video_template": normalized_template if has_product_reference else "",
            "post_overlays": list(plan["post_overlays"]),
            "voiceover": plan["voiceover"],
            "sfx": list(plan["sfx"]),
            "technical_constraints": plan["technical_constraints"],
            "shot_evidence": [dict(entry) for entry in plan["shot_evidence"]],
        },
    }


def build_video_prompt_references(
    *,
    source_asset_url: str | None,
    source_type: str | None,
    params: dict[str, Any] | None,
    source_video_analysis_only: bool = True,
) -> list[dict[str, str]]:
    """Assign each supplied asset one stable prompt role."""
    params = params or {}
    subject_mode = str(params.get("subject_mode") or "general").strip().lower()
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(role: str, source: str, value: Any, *, mode: str = "") -> None:
        normalized = str(value or "").strip()
        key = (role, normalized)
        if not normalized or key in seen:
            return
        seen.add(key)
        reference = {"role": role, "source": source}
        if mode:
            reference["mode"] = mode
        references.append(reference)

    if source_asset_url:
        if source_type == "video":
            if source_video_analysis_only:
                add(
                    "motion_analysis",
                    "source_asset_url",
                    source_asset_url,
                    mode="analysis_only",
                )
            else:
                add("first_frame", "source_asset_url", source_asset_url)
            source_role = ""
        elif subject_mode == "product":
            source_role = "product"
        elif subject_mode == "portrait":
            source_role = "character"
        else:
            source_role = "first_frame"
        if source_role:
            add(source_role, "source_asset_url", source_asset_url)

    reference_role = (
        "product"
        if subject_mode == "product"
        else "character"
        if subject_mode == "portrait"
        else "first_frame"
    )
    add("product", "product_reference_image", params.get("product_reference_image"))
    product_details = params.get("product_detail_images")
    if isinstance(product_details, list):
        for index, value in enumerate(product_details, start=1):
            add(
                f"product_detail_{index}",
                f"product_detail_images[{index - 1}]",
                value,
            )
    for key, role in (
        ("reference_image_url", reference_role),
        ("first_frame_image", "first_frame"),
        ("last_frame_image", "last_frame"),
        ("style_reference_image", "style"),
        ("character_reference_image", "character"),
    ):
        add(role, key, params.get(key))
    return references


def source_video_is_analysis_only(params: dict[str, Any] | None) -> bool:
    """Return whether a persisted source video is reverse-analysis provenance only."""
    roles = params.get("_video_reference_roles") if isinstance(params, dict) else None
    if not isinstance(roles, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("role") == "motion_analysis"
        and item.get("source") == "source_asset_url"
        and item.get("mode") == "analysis_only"
        for item in roles
    )


def store_video_prompt_compile(
    params: dict[str, Any],
    compiled: dict[str, Any],
    references: list[dict[str, str]],
) -> dict[str, Any]:
    """Persist one compiler result using the same fields in router and worker paths."""
    plan = compiled.get("plan") if isinstance(compiled.get("plan"), dict) else {}
    params["_generation_prompt"] = str(compiled.get("prompt") or "").strip()
    params["_video_prompt_plan"] = plan
    params["_video_prompt_profile"] = dict(compiled.get("profile") or {})
    params["_video_prompt_warnings"] = list(plan.get("warnings") or [])
    params["_video_prompt_sequence_required"] = bool(compiled.get("sequence_required"))
    params["_video_prompt_metadata"] = dict(compiled.get("metadata") or {})
    params["_video_reference_roles"] = references
    params["_prompt_compiler_version"] = str(compiled.get("compiler_version") or "").strip()
    params["_video_submit_contract_version"] = VIDEO_SUBMIT_CONTRACT_VERSION
    params["_post_overlays"] = list(plan.get("post_overlays") or [])
    params["_voiceover"] = str(plan.get("voiceover") or "").strip()
    params["_sfx"] = list(plan.get("sfx") or [])
    params["_video_shot_evidence"] = [
        dict(entry) for entry in plan.get("shot_evidence") or [] if isinstance(entry, dict)
    ]
    return params
