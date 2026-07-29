"""Parse video prompt text and structured payloads into stable plan fields."""

from __future__ import annotations

import json
import re
from math import isfinite
from typing import Any

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
_SHOT_LABEL_PATTERN = (
    r"(?:Shot|镜头)\s*\d+\s*"
    r"(?:[（(][^）)\n]{1,48}[）)])?\s*[:：]\s*"
)
_CONTEXT_SUBJECT_RE = re.compile(
    r"(?:画面|视频|本片|场景|背景|环境|空间|地点|拍摄地点|故事|广告)(?:设置|设定|发生|位于)?",
    re.IGNORECASE,
)
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

    - verified 与 vlm_only 字段都以可执行动作原文写入提示词；
    - 可信度、证据来源与源视频时间只保留在 shot_evidence 中，不污染生成指令；
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

        action_prompt = action

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
            camera_prompt = camera

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
                transition_prompt = transition

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
            line = "，".join(details)
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
            rf"(?:^|[\n。；;])\s*{_SHOT_LABEL_PATTERN}",
            text,
            flags=re.IGNORECASE,
        )
        candidates = numbered[1:] if len(numbered) > 1 else _clauses(text)
    shots: list[str] = []
    for candidate in candidates:
        shot = clean_video_prompt_section(candidate)
        shot = re.sub(rf"^{_SHOT_LABEL_PATTERN}", "", shot, flags=re.IGNORECASE)
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
    if re.search(rf"(?:^|[\n。；;])\s*{_SHOT_LABEL_PATTERN}", text, re.IGNORECASE):
        return _parse_text_prompt(text)
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
        content = match.group("content").strip()
        if re.match(r"^(?:仅|只|不得|不要|不(?:出现|显示)|无|必须|应当|保持)", content):
            return match.group(0)
        overlays.append(content)
        return match.group("boundary")

    def remove_overlay_sequence(match: re.Match[str]) -> str:
        quoted = match.group("content")
        overlays.extend(
            value.strip()
            for value in re.findall(r"[“\"'‘]([^”\"'’]+)[”\"'’]", quoted)
            if value.strip()
        )
        return match.group("boundary")

    text = re.sub(
        boundary
        + r"(?:画面(?:中)?\s*(?:依次|先后)?\s*(?:文字\s*)?)(?:浮现|出现|显示)\s*"
        + r"(?P<content>(?:[“\"'‘][^”\"'’]+[”\"'’]\s*)+)",
        remove_overlay_sequence,
        text,
        flags=post_flags,
    )

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
        rf"(?:^|[\n。；;])\s*{_SHOT_LABEL_PATTERN}",
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
