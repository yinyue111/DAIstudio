"""Compile user video instructions without image-prompt truncation."""

from __future__ import annotations

from math import ceil
from typing import Any

from .video_prompt_parsing import (
    _PRODUCT_INTERACTION_RE,
    _clean_identity_profile,
    _direct_passthrough_plan,
    _direct_passthrough_text,
    _extract_single_clip_constraints,
    _join_unique,
    _unique_items,
    compact_single_clip_prompt,
    parse_video_prompt,
    split_video_post_production,
)

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


def _normalize_compile_plan(
    raw_prompt: str | dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    direct_passthrough_text = _direct_passthrough_text(raw_prompt)
    plan = (
        _direct_passthrough_plan(direct_passthrough_text)
        if direct_passthrough_text
        else parse_video_prompt(raw_prompt)
    )
    if direct_passthrough_text:
        direct_passthrough_text = str(
            split_video_post_production(direct_passthrough_text).get("text") or ""
        ).strip()
    plan["global_style"] = _join_unique(plan["global_style"])
    plan["technical_constraints"] = _join_unique(plan.get("technical_constraints", ""))
    plan["shots"] = [str(item).strip() for item in plan["shots"] if str(item).strip()]
    for key in ("post_overlays", "sfx", "warnings"):
        plan[key] = _unique_items(plan[key])
    plan["shot_evidence"] = [
        dict(entry) for entry in plan.get("shot_evidence") or [] if isinstance(entry, dict)
    ]
    return direct_passthrough_text, plan


def _compile_reference_context(
    references: list[dict[str, Any]] | None,
    *,
    product_reference: bool,
    portrait_reference: bool,
) -> tuple[set[str], bool, bool, list[str]]:
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
    return reference_roles, has_product_reference, has_portrait_reference, reference_guidance


def _apply_subject_locks(
    raw_prompt: str | dict[str, Any],
    plan: dict[str, Any],
    *,
    direct_passthrough_text: str,
    has_product_reference: bool,
    has_portrait_reference: bool,
    reference_guidance: list[str],
) -> tuple[str, str]:
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
    return product_lock, portrait_lock


def _normalized_compile_modes(
    *,
    product_lock_mode: str,
    product_video_template: str,
    fit_mode: str,
) -> tuple[str, str, str]:
    normalized_lock_mode = "free" if str(product_lock_mode).lower() == "free" else "locked"
    normalized_template = str(product_video_template or "prompt_driven").lower().strip()
    normalized_fit_mode = (
        "single_clip" if str(fit_mode).strip().lower() == "single_clip" else "strict_sequence"
    )
    return normalized_lock_mode, normalized_template, normalized_fit_mode


def _supports_embedded_av_requirements(
    model_id: str,
    extra: dict[str, Any] | None,
) -> bool:
    capabilities = (extra or {}).get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    explicit = capabilities.get("embedded_av_requirements")
    if explicit is None:
        explicit = capabilities.get("native_audio")
    if explicit is not None:
        return bool(explicit)
    normalized = str(model_id or "").strip().lower().replace("_", "-")
    return "seedance-1-5-pro" in normalized


def _append_embedded_av_requirements(
    prompt: str,
    plan: dict[str, Any],
    *,
    model_id: str,
    extra: dict[str, Any] | None,
) -> tuple[str, bool]:
    if not _supports_embedded_av_requirements(model_id, extra):
        return prompt, False
    requirements: list[str] = []
    overlays = [str(item).strip() for item in plan.get("post_overlays") or [] if str(item).strip()]
    if overlays:
        quoted = "、".join(f"“{item}”" for item in overlays)
        requirements.append(
            f"按对应镜头时间顺序依次清晰显示画面文字{quoted}，除这些文字和包装原字外不生成额外字幕"
        )
    voiceover = str(plan.get("voiceover") or "").strip()
    if voiceover:
        requirements.append(f"生成同步旁白“{voiceover}”")
    sfx = [str(item).strip() for item in plan.get("sfx") or [] if str(item).strip()]
    if sfx:
        requirements.append(f"生成并与动作同步的声音：{'；'.join(sfx)}")
    if not requirements:
        return prompt, False
    return f"{prompt.rstrip()}\n音画生成要求：{'；'.join(requirements)}。", True


def _direct_compile_result(
    *,
    direct_passthrough_text: str,
    plan: dict[str, Any],
    profile: dict[str, Any],
    duration: float,
    model_id: str,
    provider: str,
    normalized_fit_mode: str,
    normalized_lock_mode: str,
    normalized_template: str,
    reference_roles: set[str],
    has_product_reference: bool,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    prompt = direct_passthrough_text
    if has_product_reference:
        prompt = (
            f"产品身份约束：{DIRECT_PRODUCT_SUBJECT_LOCK}\n"
            f"原始生成要求：\n{direct_passthrough_text}"
        )
    prompt, embedded_av = _append_embedded_av_requirements(
        prompt,
        plan,
        model_id=model_id,
        extra=extra,
    )
    plan["embedded_av_requirements"] = embedded_av
    shot_count = max(1, len(plan.get("shots") or []))
    max_shots = max(1, int(profile.get("recommended_max_shots") or 1))
    prompt_char_count = len(prompt)
    prompt_budget_chars = max(1, int(profile.get("prompt_budget_chars") or 1))
    prompt_overload = prompt_char_count > prompt_budget_chars
    shot_overload = shot_count > max_shots
    if shot_overload:
        plan["warnings"].append(
            f"当前 {duration:g} 秒/{profile['family']} 常规建议最多 {max_shots} 个镜头；"
            f"直输提示词包含 {shot_count} 个镜头，模型可能弱化部分动作，建议拆分生成。"
        )
    if prompt_overload:
        plan["warnings"].append(
            f"直输提示词已按原文完整保留；当前长度超过模型建议的 "
            f"{prompt_budget_chars} 字符预算，模型可能弱化部分细节。"
        )
    sequence_required = normalized_fit_mode != "single_clip" and (shot_overload or prompt_overload)
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
            "prompt_mode": "direct_passthrough",
            "shot_count": shot_count,
            "source_shot_count": shot_count,
            "selected_shot_count": shot_count,
            "prompt_char_count": prompt_char_count,
            "prompt_budget_chars": prompt_budget_chars,
            "prompt_over_budget": prompt_overload,
            "omitted_shot_count": 0,
            "condensed_for_single_clip": False,
            "compacted_for_budget": False,
            "fit_mode": normalized_fit_mode,
            "recommended_clip_count": recommended_clip_count,
            "reference_roles": sorted(reference_roles),
            "motion_reference_mode": "",
            "product_lock_mode": normalized_lock_mode if has_product_reference else "",
            "product_video_template": normalized_template if has_product_reference else "",
            "embedded_av_requirements": embedded_av,
            "post_overlays": list(plan["post_overlays"]),
            "voiceover": plan["voiceover"],
            "sfx": list(plan["sfx"]),
            "technical_constraints": "",
            "shot_evidence": [dict(entry) for entry in plan["shot_evidence"]],
        },
    }


def _product_strategy(
    plan: dict[str, Any],
    *,
    has_product_reference: bool,
    normalized_lock_mode: str,
    normalized_template: str,
) -> tuple[str, str]:
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
    return product_strategy, locked_product_guard


def _prepare_compile_shots(
    plan: dict[str, Any],
    *,
    profile: dict[str, Any],
    duration: float,
    normalized_fit_mode: str,
    has_product_reference: bool,
    normalized_template: str,
    product_strategy: str,
    locked_product_guard: str,
) -> tuple[int, int, int, str]:
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
    return source_shot_count, shot_count, max_shots, product_strategy


def _compile_technical_constraints(
    plan: dict[str, Any],
    *,
    product_lock: str,
    portrait_lock: str,
    product_strategy: str,
    reference_guidance: list[str],
) -> tuple[str, str]:
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
    return _join_unique(*technical_parts), _join_unique(*mandatory_technical_parts)


def _render_compile_prompt(plan: dict[str, Any], technical_text: str) -> str:
    prompt_parts = [f"风格设定：{plan['global_style']}", "场景脚本："]
    prompt_parts.extend(
        f"Shot {number}：{shot}" for number, shot in enumerate(plan["shots"], start=1)
    )
    prompt_parts.append(f"技术约束：{technical_text}")
    return "\n".join(prompt_parts)


def _compact_prompt_for_budget(
    plan: dict[str, Any],
    *,
    prompt: str,
    technical_text: str,
    mandatory_technical_text: str,
    prompt_budget_chars: int,
    normalized_fit_mode: str,
) -> tuple[str, str, int, bool]:
    prompt_char_count = len(prompt)
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
    return prompt, technical_text, prompt_char_count, compacted_for_budget


def _compile_capacity_result(
    plan: dict[str, Any],
    *,
    duration: float,
    profile: dict[str, Any],
    shot_count: int,
    max_shots: int,
    prompt_char_count: int,
    prompt_budget_chars: int,
    normalized_fit_mode: str,
) -> tuple[bool, bool, bool, int]:
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
    return sequence_required, shot_overload, prompt_overload, recommended_clip_count


def _compile_result(
    *,
    prompt: str,
    plan: dict[str, Any],
    profile: dict[str, Any],
    sequence_required: bool,
    duration: float,
    model_id: str,
    provider: str,
    source_shot_count: int,
    shot_count: int,
    prompt_char_count: int,
    prompt_budget_chars: int,
    prompt_overload: bool,
    compacted_for_budget: bool,
    normalized_fit_mode: str,
    recommended_clip_count: int,
    reference_roles: set[str],
    normalized_lock_mode: str,
    normalized_template: str,
    has_product_reference: bool,
) -> dict[str, Any]:
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
            "embedded_av_requirements": bool(plan.get("embedded_av_requirements")),
            "technical_constraints": plan["technical_constraints"],
            "shot_evidence": [dict(entry) for entry in plan["shot_evidence"]],
        },
    }


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
    direct_text, plan = _normalize_compile_plan(raw_prompt)
    reference_roles, has_product, has_portrait, reference_guidance = (
        _compile_reference_context(
            references,
            product_reference=product_reference,
            portrait_reference=portrait_reference,
        )
    )
    product_lock, portrait_lock = _apply_subject_locks(
        raw_prompt,
        plan,
        direct_passthrough_text=direct_text,
        has_product_reference=has_product,
        has_portrait_reference=has_portrait,
        reference_guidance=reference_guidance,
    )
    lock_mode, template, fit = _normalized_compile_modes(
        product_lock_mode=product_lock_mode,
        product_video_template=product_video_template,
        fit_mode=fit_mode,
    )
    profile = infer_video_model_profile(
        model_id=model_id,
        provider=provider,
        duration=duration,
        extra=extra,
        model_profiles=model_profiles,
    )
    if direct_text:
        return _direct_compile_result(
            direct_passthrough_text=direct_text,
            plan=plan,
            profile=profile,
            duration=duration,
            model_id=model_id,
            provider=provider,
            normalized_fit_mode=fit,
            normalized_lock_mode=lock_mode,
            normalized_template=template,
            reference_roles=reference_roles,
            has_product_reference=has_product,
            extra=extra,
        )
    strategy, guard = _product_strategy(
        plan,
        has_product_reference=has_product,
        normalized_lock_mode=lock_mode,
        normalized_template=template,
    )
    source_count, shot_count, max_shots, strategy = _prepare_compile_shots(
        plan,
        profile=profile,
        duration=duration,
        normalized_fit_mode=fit,
        has_product_reference=has_product,
        normalized_template=template,
        product_strategy=strategy,
        locked_product_guard=guard,
    )
    technical, mandatory = _compile_technical_constraints(
        plan,
        product_lock=product_lock,
        portrait_lock=portrait_lock,
        product_strategy=strategy,
        reference_guidance=reference_guidance,
    )
    prompt_budget = max(1, int(profile.get("prompt_budget_chars") or 1))
    prompt, technical, prompt_chars, compacted = _compact_prompt_for_budget(
        plan,
        prompt=_render_compile_prompt(plan, technical),
        technical_text=technical,
        mandatory_technical_text=mandatory,
        prompt_budget_chars=prompt_budget,
        normalized_fit_mode=fit,
    )
    prompt, embedded_av = _append_embedded_av_requirements(
        prompt,
        plan,
        model_id=model_id,
        extra=extra,
    )
    plan["embedded_av_requirements"] = embedded_av
    prompt_chars = len(prompt)
    sequence_required, _, prompt_overload, clip_count = _compile_capacity_result(
        plan,
        duration=duration,
        profile=profile,
        shot_count=shot_count,
        max_shots=max_shots,
        prompt_char_count=prompt_chars,
        prompt_budget_chars=prompt_budget,
        normalized_fit_mode=fit,
    )
    return _compile_result(
        prompt=prompt,
        plan=plan,
        profile=profile,
        sequence_required=sequence_required,
        duration=duration,
        model_id=model_id,
        provider=provider,
        source_shot_count=source_count,
        shot_count=shot_count,
        prompt_char_count=prompt_chars,
        prompt_budget_chars=prompt_budget,
        prompt_overload=prompt_overload,
        compacted_for_budget=compacted,
        normalized_fit_mode=fit,
        recommended_clip_count=clip_count,
        reference_roles=reference_roles,
        normalized_lock_mode=lock_mode,
        normalized_template=template,
        has_product_reference=has_product,
    )


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


# 解析层已迁出到 video_prompt_parsing；以下 re-export 保持历史导入路径可达，
# 且与解析模块指向同一对象（gateway、prompt_optimization、tests 均无需改动）。
from .video_prompt_parsing import (  # noqa: E402
    _ACTION_SIGNAL_RE,
    _CONTEXT_SUBJECT_RE,
    _EVIDENCE_CAMERA_LABEL_CLAUSES,
    _EVIDENCE_CAMERA_LABEL_PATTERNS,
    _EVIDENCE_CAMERA_MIN_CONFIDENCE,
    _EXPLICIT_STYLE_CONTEXT_RE,
    _HARD_CUT_SHOT_CLAUSE,
    _HARD_CUT_TRANSITION_RE,
    _NON_PORTRAIT_IDENTITY_RE,
    _NON_PRODUCT_IDENTITY_RE,
    _POST_PLACEHOLDER_VALUES,
    _PRODUCT_CONSTRAINT_RE,
    _SCENE_CONTEXT_RE,
    _SCENE_SUBJECT_STRUCTURE_RE,
    _STRUCTURED_CONTEXT_KEYS,
    _SUBJECT_SIGNAL_RE,
    _TECHNICAL_PRIORITY_RE,
    HARD_CUT_RHYTHM_CLAUSE,
    UNVERIFIED_EVIDENCE_SUFFIX,
    VIDEO_SECTION_ALIASES,
    _clauses,
    _compile_evidence_shots,
    _evidence_marker,
    _extract_evidence_shots,
    _finite_seconds,
    _has_subject_scene_structure,
    _is_post_placeholder,
    _join_voiceovers,
    _layered_authoritative_text,
    _looks_like_global_context,
    _meaningful_post_items,
    _normalized_prompt_layer,
    _parse_text_prompt,
    _reconcile_camera_with_analyzer,
    _structured_video_sections_from_text,
    _unlabelled_parts,
    clean_video_prompt_section,
    merge_video_constraint_clauses,
    parse_structured_video_sections,
    render_structured_video_prompt,
    video_action_requirements,
    video_scene_script_items,
)

__all__ = [
    "COMPILER_VERSION",
    "DEFAULT_VIDEO_MODEL_PROFILES",
    "DIRECT_PRODUCT_SUBJECT_LOCK",
    "PORTRAIT_SUBJECT_LOCK",
    "PRODUCT_SUBJECT_LOCK",
    "PRODUCT_VIDEO_TEMPLATE_KEYS",
    "VIDEO_SUBMIT_CONTRACT_VERSION",
    "build_video_prompt_references",
    "compile_video_prompt",
    "infer_video_model_profile",
    "source_video_is_analysis_only",
    "store_video_prompt_compile",
    "_PRODUCT_INTERACTION_RE",
    "_clean_identity_profile",
    "_direct_passthrough_plan",
    "_direct_passthrough_text",
    "_extract_single_clip_constraints",
    "_join_unique",
    "_unique_items",
    "compact_single_clip_prompt",
    "parse_video_prompt",
    "_ACTION_SIGNAL_RE",
    "_CONTEXT_SUBJECT_RE",
    "_EVIDENCE_CAMERA_LABEL_CLAUSES",
    "_EVIDENCE_CAMERA_LABEL_PATTERNS",
    "_EVIDENCE_CAMERA_MIN_CONFIDENCE",
    "_EXPLICIT_STYLE_CONTEXT_RE",
    "_HARD_CUT_SHOT_CLAUSE",
    "_HARD_CUT_TRANSITION_RE",
    "_NON_PORTRAIT_IDENTITY_RE",
    "_NON_PRODUCT_IDENTITY_RE",
    "_POST_PLACEHOLDER_VALUES",
    "_PRODUCT_CONSTRAINT_RE",
    "_SCENE_CONTEXT_RE",
    "_SCENE_SUBJECT_STRUCTURE_RE",
    "_STRUCTURED_CONTEXT_KEYS",
    "_SUBJECT_SIGNAL_RE",
    "_TECHNICAL_PRIORITY_RE",
    "HARD_CUT_RHYTHM_CLAUSE",
    "UNVERIFIED_EVIDENCE_SUFFIX",
    "VIDEO_SECTION_ALIASES",
    "_clauses",
    "_compile_evidence_shots",
    "_evidence_marker",
    "_extract_evidence_shots",
    "_finite_seconds",
    "_has_subject_scene_structure",
    "_is_post_placeholder",
    "_join_voiceovers",
    "_layered_authoritative_text",
    "_looks_like_global_context",
    "_meaningful_post_items",
    "_normalized_prompt_layer",
    "_parse_text_prompt",
    "_reconcile_camera_with_analyzer",
    "_structured_video_sections_from_text",
    "_unlabelled_parts",
    "clean_video_prompt_section",
    "merge_video_constraint_clauses",
    "parse_structured_video_sections",
    "render_structured_video_prompt",
    "split_video_post_production",
    "video_action_requirements",
    "video_scene_script_items",
]
