"""Compile user video instructions without image-prompt truncation."""

from __future__ import annotations

import json
import re
from math import ceil
from typing import Any

COMPILER_VERSION = "video-prompt-v2"
PRODUCT_SUBJECT_LOCK = (
    "上传产品是唯一商品主体；仅锁定同一SKU的包装外形与比例、Logo、"
    "包装结构、品牌色、标签排版、可见文字和材质纹理。"
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
    r"固定镜头|稳定镜头|手持镜头|镜头语言|镜头视角|镜头焦段|镜头运动",
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
}
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


def _clauses(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n。；;]+", str(value or "")) if part.strip()]


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
    assembled = str(raw_prompt.get("assembled_text") or "").strip()
    optimized = str(raw_prompt.get("optimized_text") or "").strip()
    raw = str(raw_prompt.get("raw_text") or "").strip()
    if assembled and (optimized or not raw or assembled != raw):
        return assembled
    return optimized if optimized and not assembled else ""


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
        r"(?:(?:温柔|轻柔|低沉)?\s*(?:男声|女声|女性|男性)?\s*)?(?:旁白|voiceover|后期配音)"
    )
    overlay_label = (
        r"(?:字幕|后期叠字|(?:画面(?:中)?)?文字(?:浮现|出现|显示)?|"
        r"画面(?:中)?(?:浮现|出现|显示)文字)"
    )

    def remove_voiceover(match: re.Match[str]) -> str:
        nonlocal voiceover
        voiceover = match.group("content").strip()
        return match.group("boundary")

    text = re.sub(
        boundary
        + voiceover_label
        + r"\s*[:：]?\s*"
        + open_quote
        + r"(?P<content>[^”\"'’]+)"
        + close_quote,
        remove_voiceover,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + voiceover_label + r"\s*[:：]?\s*(?P<content>[^\n。；;\uff0c,]+)",
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
        + r"\s*[:：]?\s*"
        + open_quote
        + r"(?P<content>[^”\"'’]+)"
        + close_quote
        + r"\s*(?:浮现|出现|显示)?",
        remove_overlay,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + overlay_label + r"\s*[:：]\s*(?P<content>[^\n。；;\uff0c,]+)",
        remove_overlay,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + overlay_label + r"\s*(?P<content>[^\n。；;\uff0c,]+)",
        remove_overlay,
        text,
        flags=post_flags,
    )

    def remove_sfx(match: re.Match[str]) -> str:
        sfx.extend(_clauses(match.group("content")))
        return match.group("boundary")

    text = re.sub(
        boundary
        + r"(?:音效|SFX|环境音)\s*[:：]?\s*"
        + open_quote
        + r"(?P<content>[^”\"'’]+)"
        + close_quote,
        remove_sfx,
        text,
        flags=post_flags,
    )
    text = re.sub(
        boundary + r"(?:音效|SFX|环境音)\s*[:：]?\s*(?P<content>[^\n。；;\uff0c,]+)",
        remove_sfx,
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
        }
    numbered = re.split(
        r"(?:^|[\n。；;])\s*(?:Shot|镜头)\s*\d+\s*[:：]\s*",
        text,
        flags=re.IGNORECASE,
    )
    if len(numbered) > 1:
        style = numbered[0].strip(" ,，。；;")
        shots = [part.strip(" ,，。；;") for part in numbered[1:] if part.strip(" ,，。；;")]
    else:
        parts = _unlabelled_parts(text)
        if parts and _looks_like_global_context(parts[0]):
            style = parts[0]
            shots = parts[1:]
        else:
            style = ""
            shots = parts
    return {
        "global_style": style,
        "subject_lock": "",
        "shots": shots,
        "technical_constraints": "",
        "post_overlays": post["post_overlays"],
        "voiceover": post["voiceover"],
        "sfx": post["sfx"],
        "reference_guidance": [],
        "warnings": [],
    }


def parse_video_prompt(raw_prompt: str | dict[str, Any]) -> dict[str, Any]:
    """Parse a prompt into the stable public video-plan fields."""
    if isinstance(raw_prompt, dict):
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
            [str(item).strip() for item in timeline if str(item).strip()]
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
        if style or shots or technical_constraints or overlays or raw_prompt_voiceover or sfx:
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
                "subject_lock": str(raw_prompt.get("subject_lock") or "").strip(),
                "shots": shots,
                "technical_constraints": technical_constraints,
                "post_overlays": overlays,
                "voiceover": raw_prompt_voiceover,
                "sfx": sfx,
                "reference_guidance": [],
                "warnings": list(raw_prompt.get("warnings") or []),
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
    product_video_template: str = "stable_showcase",
    model_profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a structured plan and a model-ready prompt."""
    plan = parse_video_prompt(raw_prompt)
    plan["global_style"] = _join_unique(plan["global_style"])
    plan["technical_constraints"] = _join_unique(plan.get("technical_constraints", ""))
    plan["shots"] = [str(item).strip() for item in plan["shots"] if str(item).strip()]
    for key in ("post_overlays", "sfx", "warnings"):
        plan[key] = _unique_items(plan[key])
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
            "动作与镜头节奏仅来自原视频的抽帧反推文本；生成阶段只使用关键帧或封面，"
            "不进行原视频逐帧动作复刻，也不迁移其中的人物、商品、品牌、场景或文字。"
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
    normalized_template = str(product_video_template or "stable_showcase").lower().strip()
    product_strategy = ""
    if has_product_reference:
        product_strategy = _PRODUCT_VIDEO_STRATEGIES.get(
            normalized_template,
            _PRODUCT_VIDEO_STRATEGIES["stable_showcase"],
        )
        if normalized_lock_mode == "locked":
            product_strategy = (
                "文字保真模式：包装正面、Logo 和主要文字持续清晰可见，"
                "避免快速旋转、翻面、强运动模糊、遮挡或裁切产品。"
                f"{product_strategy}"
            )
        plan["product_strategy"] = product_strategy
    profile = infer_video_model_profile(
        model_id=model_id,
        provider=provider,
        duration=duration,
        extra=extra,
        model_profiles=model_profiles,
    )
    shot_count = len(plan["shots"])
    max_shots = int(profile["recommended_max_shots"])
    prompt_parts = []
    if plan["global_style"]:
        prompt_parts.append(f"风格设定：{plan['global_style']}")
    if plan["shots"]:
        prompt_parts.append("场景脚本：")
        prompt_parts.extend(
            f"Shot {number}：{shot}" for number, shot in enumerate(plan["shots"], start=1)
        )
    technical_parts = [plan["technical_constraints"]]
    if product_lock:
        technical_parts.append(f"产品身份约束：{product_lock}")
    if portrait_lock:
        technical_parts.append(f"人物身份约束：{portrait_lock}")
    if plan["subject_lock"] and not (product_lock or portrait_lock):
        technical_parts.append(f"主体锁定：{plan['subject_lock']}")
    if product_strategy:
        technical_parts.append(product_strategy)
    technical_parts.extend(reference_guidance)
    technical_text = _join_unique(*technical_parts)
    if technical_text:
        prompt_parts.append(f"技术约束：{technical_text}")
    prompt = "\n".join(prompt_parts)
    prompt_char_count = len(prompt)
    prompt_budget_chars = max(1, int(profile.get("prompt_budget_chars") or 1))
    shot_overload = shot_count > max_shots
    prompt_overload = prompt_char_count > prompt_budget_chars
    sequence_required = shot_overload or prompt_overload
    if shot_overload:
        plan["warnings"].append(
            f"当前 {duration:g} 秒/{profile['family']} 建议最多 {max_shots} 个镜头；"
            f"已保留全部 {shot_count} 个镜头，请拆分为多段生成。"
        )
    if prompt_overload:
        plan["warnings"].append(
            f"当前模型提示词预算约 {prompt_budget_chars} 字符；"
            f"已保留全部 {prompt_char_count} 字符，请拆分为多段生成。"
        )
    recommended_clip_count = max(
        1,
        ceil(shot_count / max_shots),
        ceil(prompt_char_count / prompt_budget_chars),
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
            "prompt_char_count": prompt_char_count,
            "prompt_budget_chars": prompt_budget_chars,
            "omitted_shot_count": 0,
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
        },
    }


def build_video_prompt_references(
    *,
    source_asset_url: str | None,
    source_type: str | None,
    params: dict[str, Any] | None,
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
            add(
                "motion_analysis",
                "source_asset_url",
                source_asset_url,
                mode="analysis_only",
            )
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
    for key, role in (
        ("reference_image_url", reference_role),
        ("first_frame_image", "first_frame"),
        ("last_frame_image", "last_frame"),
        ("style_reference_image", "style"),
        ("character_reference_image", "character"),
    ):
        add(role, key, params.get(key))
    return references


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
    params["_post_overlays"] = list(plan.get("post_overlays") or [])
    params["_voiceover"] = str(plan.get("voiceover") or "").strip()
    params["_sfx"] = list(plan.get("sfx") or [])
    return params
