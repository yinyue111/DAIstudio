"""Server-side enforcement for optional public model capabilities."""
from __future__ import annotations

import re

from .video_prompt_compiler import PRODUCT_VIDEO_TEMPLATE_KEYS


class ModelCapabilityError(ValueError):
    pass


_SEEDANCE_VERSION_RE = re.compile(r"^doubao-seedance-(\d+)-(\d+)(?:-|$)")
_VIDEO_IMAGE_INPUT_MODES = {"first_frame", "subject_reference"}

_IMAGE_CAPABILITY_ALIASES = {
    "text_to_image",
    "image_generation",
    "image",
    "image_to_image",
    "image_edit",
    "edit",
    "reference_image",
    "image_reference",
    "image_input",
    "multi_reference",
    "image_mask",
    "mask_edit",
    "inpainting",
}
_VIDEO_CAPABILITY_ALIASES = {
    "text_to_video",
    "video_generation",
    "video",
    "image_to_video",
    "reference_image",
    "image_reference",
    "video_to_video",
    "reference_video",
    "video_reference",
    "video_edit",
    "audio_reference",
    "multi_reference",
    "first_last_frame",
    "reference_image_mode_exclusive",
    "frame_reference_mode_exclusive",
    "generated_audio",
    "generated_audio_configurable",
}
_VISION_CAPABILITY_ALIASES = {
    "image_analysis",
    "image_input",
    "vision",
    "video_analysis",
    "video_input",
    "video",
    "product_profile",
    "portrait_profile",
}
_PROMPT_CAPABILITY_ALIASES = {
    "prompt_optimization",
    "optimize_prompt",
    "text",
}
_CAPABILITY_ALIASES_BY_USE = {
    "image": _IMAGE_CAPABILITY_ALIASES,
    "video": _VIDEO_CAPABILITY_ALIASES,
    "vision": _VISION_CAPABILITY_ALIASES,
    "prompt": _PROMPT_CAPABILITY_ALIASES,
}

_VERIFIED_MODEL_CAPABILITIES: dict[tuple[str, str], dict] = {
    ("image", "gpt-image-2"): {
        "text_to_image": True,
        "image_to_image": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 2,
        "mask_edit": True,
    },
    ("image", "gemini-3.1-flash-image"): {
        "text_to_image": True,
        "image_to_image": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 2,
        "mask_edit": False,
    },
    ("image", "grok-imagine-image"): {
        "text_to_image": True,
        "image_to_image": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 2,
        "mask_edit": False,
    },
    ("image", "grok-imagine-image-quality"): {
        "text_to_image": True,
        "image_to_image": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 2,
        "mask_edit": False,
    },
    ("video", "doubao-seedance-1-5-pro-251215"): {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": False,
        "first_last_frame": True,
        "multi_reference": False,
        "video_to_video": False,
        "video_reference": False,
        "video_edit": False,
        "audio_reference": False,
        "generated_audio": True,
        "generated_audio_configurable": False,
        "resolutions": ["480p", "720p", "1080p"],
        "durations": list(range(4, 13)),
        "min_duration_seconds": 4,
        "max_duration_seconds": 12,
        "max_reference_videos": 0,
        "max_reference_audio": 0,
    },
    ("video", "grok-imagine-video"): {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 7,
        "max_reference_duration_seconds": 10,
        "durations": list(range(1, 16)),
        "min_duration_seconds": 1,
        "max_duration_seconds": 15,
        "reference_image_mode_exclusive": True,
        "first_last_frame": False,
        "video_to_video": True,
        "video_reference": False,
        "video_edit": True,
        "audio_reference": False,
        "resolutions": ["480p", "720p"],
        "max_reference_videos": 1,
        "max_reference_audio": 0,
    },
    ("video", "grok-imagine-video-1.5"): {
        "text_to_video": False,
        "image_to_video": True,
        "reference_image": False,
        "multi_reference": False,
        "first_last_frame": False,
        "video_to_video": False,
        "video_reference": False,
        "video_edit": False,
        "audio_reference": False,
        "durations": list(range(1, 16)),
        "min_duration_seconds": 1,
        "max_duration_seconds": 15,
        "resolutions": ["480p", "720p", "1080p"],
        "max_reference_videos": 0,
        "max_reference_audio": 0,
    },
    ("vision", "gpt-5.6-sol"): {
        "image_analysis": True,
        "video_analysis": True,
        "product_profile": True,
        "portrait_profile": True,
    },
    ("vision", "gemini-3.1-pro-high"): {
        "image_analysis": True,
        "video_analysis": True,
        "product_profile": True,
        "portrait_profile": True,
    },
    ("prompt", "claude-opus-4-6-thinking"): {"prompt_optimization": True},
    ("prompt", "gemini-3.5-flash-low"): {"prompt_optimization": True},
    ("prompt", "gemini-3.1-pro-high"): {"prompt_optimization": True},
    ("prompt", "grok-4.5"): {"prompt_optimization": True},
}


def _profile_for_model(use: str, model_id: str) -> dict | None:
    capabilities = _VERIFIED_MODEL_CAPABILITIES.get((use, model_id))
    if capabilities is not None:
        return capabilities
    seedance_match = _SEEDANCE_VERSION_RE.match(model_id)
    if use == "video" and seedance_match is not None:
        version = int(seedance_match.group(1)), int(seedance_match.group(2))
        if version == (2, 0):
            standard_model = re.fullmatch(
                r"doubao-seedance-2-0-\d+",
                model_id,
            ) is not None
            return {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "first_last_frame": True,
                "multi_reference": True,
                "max_reference_images": 9,
                "video_to_video": True,
                "video_reference": True,
                "video_edit": False,
                "audio_reference": False,
                "generated_audio": True,
                "generated_audio_configurable": False,
                "frame_reference_mode_exclusive": True,
                "resolutions": (
                    ["480p", "720p", "1080p"]
                    if standard_model
                    else ["480p", "720p"]
                ),
                "durations": list(range(4, 16)),
                "min_duration_seconds": 4,
                "max_duration_seconds": 15,
                "max_reference_videos": 1,
                "max_reference_audio": 0,
            }
    return None


def _profile_route_matches(
    use: str,
    model_id: str,
    provider: str,
    gateway_format: str,
) -> bool:
    if not _profile_protocol_matches(use, model_id, gateway_format):
        return False
    if (use, model_id) in {
        ("image", "gpt-image-2"),
        ("vision", "gpt-5.6-sol"),
    }:
        return True
    if (use, model_id) in {
        ("image", "gemini-3.1-flash-image"),
        ("vision", "gemini-3.1-pro-high"),
        ("prompt", "claude-opus-4-6-thinking"),
        ("prompt", "gemini-3.5-flash-low"),
        ("prompt", "gemini-3.1-pro-high"),
    }:
        return provider == "antigravity"
    if provider == "grok":
        return (use, model_id) in {
            ("image", "grok-imagine-image"),
            ("image", "grok-imagine-image-quality"),
            ("video", "grok-imagine-video"),
            ("video", "grok-imagine-video-1.5"),
            ("prompt", "grok-4.5"),
        }
    return (
        use == "video"
        and _SEEDANCE_VERSION_RE.match(model_id) is not None
        and provider == "volcengine_ark"
    )


def _profile_protocol_matches(use: str, model_id: str, gateway_format: str) -> bool:
    if (use, model_id) in {
        ("image", "gpt-image-2"),
        ("image", "grok-imagine-image"),
        ("image", "grok-imagine-image-quality"),
        ("video", "grok-imagine-video"),
        ("video", "grok-imagine-video-1.5"),
        ("vision", "gpt-5.6-sol"),
        ("prompt", "grok-4.5"),
    }:
        return gateway_format == "openai"
    if (use, model_id) in {
        ("image", "gemini-3.1-flash-image"),
        ("vision", "gemini-3.1-pro-high"),
        ("prompt", "claude-opus-4-6-thinking"),
        ("prompt", "gemini-3.5-flash-low"),
        ("prompt", "gemini-3.1-pro-high"),
    }:
        return gateway_format == "anthropic"
    return (
        use == "video"
        and _SEEDANCE_VERSION_RE.match(model_id) is not None
        and gateway_format == "ark"
    )


def _disabled_profile(use: str) -> dict:
    aliases = _CAPABILITY_ALIASES_BY_USE.get(use, set())
    disabled = {key: False for key in aliases}
    if use in {"image", "video"}:
        disabled["max_reference_images"] = 0
    if use == "video":
        disabled["max_reference_videos"] = 0
        disabled["max_reference_audio"] = 0
        disabled["max_reference_duration_seconds"] = 0
        disabled["min_duration_seconds"] = 0
        disabled["max_duration_seconds"] = 0
        disabled["resolutions"] = []
        disabled["durations"] = []
    return disabled


def _verified_model_capabilities(model) -> dict | None:
    use = str(getattr(model, "use", None) or "").strip().lower()
    model_id = str(getattr(model, "model_id", None) or "").strip().lower()
    provider = str(getattr(model, "provider", None) or "").strip().lower()
    gateway_format = str(
        getattr(model, "gateway_format", None) or ""
    ).strip().lower()
    gateway_source = str(
        getattr(model, "gateway_source", None) or ""
    ).strip().lower()
    profile = _profile_for_model(use, model_id)
    if profile is None:
        return None
    # Rows seeded before per-model gateways existed have neither route field.
    # Keep their explicit metadata compatible; once either field is declared,
    # an incompatible provider/protocol combination must fail closed.
    if not provider and not gateway_format:
        return None
    # Frozen tasks that use environment credentials expose the credential
    # source rather than the upstream vendor name. The immutable protocol and
    # exact model id still provide enough information to apply the verified
    # ceiling without turning a valid quote into a generation-time rejection.
    if gateway_source == "env":
        if not _profile_protocol_matches(use, model_id, gateway_format):
            return _disabled_profile(use)
        return profile
    if not _profile_route_matches(use, model_id, provider, gateway_format):
        return _disabled_profile(use)
    return profile


def effective_model_capabilities(model) -> dict:
    use = str(getattr(model, "use", None) or "").strip().lower()
    extra = getattr(model, "extra", None)
    value = extra.get("capabilities") if isinstance(extra, dict) else None
    configured = value if isinstance(value, dict) else {}
    verified = _verified_model_capabilities(model)
    if verified is None:
        return configured
    # Verified profiles are the provider/platform ceiling, while administrators
    # may still disable a supported path or lower a numeric limit. This keeps
    # known unsupported modes fail-closed without silently re-enabling an
    # intentionally disabled capability.
    merged = dict(verified)
    for key, configured_value in configured.items():
        if key not in verified:
            if key in _CAPABILITY_ALIASES_BY_USE.get(use, set()):
                continue
            merged[key] = configured_value
            continue
        verified_value = verified[key]
        if key in {
            "reference_image_mode_exclusive",
            "frame_reference_mode_exclusive",
        }:
            # This flag is a provider invariant, not a feature administrators
            # may loosen. A false override would create an invalid mixed-mode
            # request even though the verified adapter requires exclusivity.
            continue
        if isinstance(verified_value, bool):
            if verified_value is True:
                merged[key] = configured_value
            continue
        if type(verified_value) is int and type(configured_value) is int:
            merged[key] = (
                max(verified_value, configured_value)
                if key == "min_duration_seconds"
                else min(verified_value, configured_value)
            )
            continue
        if isinstance(verified_value, list):
            if not isinstance(configured_value, list):
                merged[key] = []
                continue
            configured_values = {
                str(value).strip().lower()
                for value in configured_value
                if str(value or "").strip()
            }
            merged[key] = [
                value
                for value in verified_value
                if str(value).strip().lower() in configured_values
            ]
            continue
        merged[key] = configured_value
    if merged.get("multi_reference") is False:
        merged.pop("max_reference_images", None)
    return merged


def _capabilities(model) -> dict:
    return effective_model_capabilities(model)


def _require(model, capability: str | tuple[str, ...], message: str) -> None:
    capabilities = _capabilities(model)
    # Catalog rows created before capability metadata remain compatible. Once a
    # key is declared, false is authoritative on both the UI and API boundary.
    aliases = (capability,) if isinstance(capability, str) else capability
    declared = [alias for alias in aliases if alias in capabilities]
    if declared and not any(capabilities.get(alias) is True for alias in declared):
        raise ModelCapabilityError(message)


def _unique_reference_image_urls(
    source_asset_url: str | None,
    source_type: str | None,
    params: dict,
) -> list[str]:
    values: list[object] = []
    if source_type == "image":
        values.append(source_asset_url)
    values.extend(
        params.get(key)
        for key in (
            "reference_image_url",
            "product_reference_image",
            "first_frame_image",
            "last_frame_image",
            "style_reference_image",
            "character_reference_image",
        )
    )
    details = params.get("product_detail_images")
    if isinstance(details, list):
        values.extend(details)
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _require_strict_multi_reference(model, reference_count: int) -> None:
    capabilities = _capabilities(model)
    if capabilities.get("multi_reference") is not True:
        raise ModelCapabilityError("所选视频模型未明确支持多参考图")
    max_references = capabilities.get("max_reference_images")
    if type(max_references) is not int or max_references < 1:
        raise ModelCapabilityError("所选视频模型未声明有效的参考图数量上限")
    if reference_count > max_references:
        raise ModelCapabilityError(
            f"所选视频模型最多支持 {max_references} 张参考图，"
            f"当前需要 {reference_count} 张"
        )


def _require_strict_first_last_frame(model) -> None:
    if _capabilities(model).get("first_last_frame") is not True:
        raise ModelCapabilityError("所选视频模型未明确支持首尾帧")


def _require_declared_reference_limit(model, reference_count: int, *, label: str) -> None:
    max_references = _capabilities(model).get("max_reference_images")
    if max_references is None:
        return
    if type(max_references) is not int or max_references < 1:
        raise ModelCapabilityError(f"所选{label}模型未声明有效的参考图数量上限")
    if reference_count > max_references:
        raise ModelCapabilityError(
            f"所选{label}模型最多支持 {max_references} 张参考图，"
            f"当前需要 {reference_count} 张"
        )


def assert_generation_capability(
    model,
    *,
    category: str,
    source_asset_url: str | None,
    source_type: str | None,
    params: dict | None,
) -> None:
    params = params or {}
    video_image_input_mode = str(params.get("video_image_input_mode") or "").strip().lower()
    if video_image_input_mode and video_image_input_mode not in _VIDEO_IMAGE_INPUT_MODES:
        raise ModelCapabilityError("视频图片输入模式不受支持")
    if video_image_input_mode and category != "video":
        raise ModelCapabilityError("视频图片输入模式只能用于视频生成")
    subject_mode = str(params.get("subject_mode") or "").strip().lower()
    product_reference = str(params.get("product_reference_image") or "").strip()
    character_reference = str(params.get("character_reference_image") or "").strip()
    details = params.get("product_detail_images")
    independent_reference_values = [
        product_reference,
        params.get("style_reference_image"),
        character_reference,
    ]
    has_independent_reference = any(independent_reference_values) or bool(
        details if isinstance(details, list) else []
    )
    if video_image_input_mode:
        if subject_mode not in {"product", "portrait"}:
            raise ModelCapabilityError("视频图片输入模式只能用于产品或人物主体")
        if video_image_input_mode == "first_frame":
            if has_independent_reference:
                raise ModelCapabilityError("单图首帧模式不能同时提交产品、人物、风格或细节参考图")
            if params.get("last_frame_image"):
                raise ModelCapabilityError("单图首帧模式不能同时提交尾帧")
            if not str(
                params.get("first_frame_image")
                or params.get("reference_image_url")
                or (source_asset_url if source_type == "image" else "")
                or ""
            ).strip():
                raise ModelCapabilityError("单图首帧模式必须提供一张首帧图片")
        else:
            if any(
                params.get(key)
                for key in ("first_frame_image", "reference_image_url", "last_frame_image")
            ):
                raise ModelCapabilityError("主体参考模式不能同时提交首帧、尾帧或通用参考图")
            if subject_mode == "product" and not product_reference:
                raise ModelCapabilityError("产品主体参考模式必须提供产品参考图")
            if subject_mode == "portrait" and not character_reference:
                raise ModelCapabilityError("人物主体参考模式必须提供人物参考图")
    product_video_template = str(params.get("product_video_template") or "").strip()
    if product_video_template:
        if category != "video":
            raise ModelCapabilityError("产品视频策略只能用于视频生成")
        if product_video_template not in PRODUCT_VIDEO_TEMPLATE_KEYS:
            raise ModelCapabilityError("产品视频策略不受支持")
        if not str(params.get("product_reference_image") or "").strip():
            raise ModelCapabilityError("产品视频策略必须配合产品参考图使用")
        declared_templates = _capabilities(model).get("product_video_templates")
        if declared_templates is not None:
            supported_templates = (
                {
                    str(value).strip()
                    for value in declared_templates
                    if isinstance(value, str) and str(value).strip()
                }
                if isinstance(declared_templates, list)
                else set()
            )
            if product_video_template not in supported_templates:
                raise ModelCapabilityError("所选视频模型不支持当前产品视频策略")
    reference_urls = [
        params.get("reference_image_url"),
        params.get("product_reference_image"),
        params.get("first_frame_image"),
        params.get("last_frame_image"),
        params.get("style_reference_image"),
        params.get("character_reference_image"),
        params.get("mask_image_url"),
    ]
    unique_reference_urls = _unique_reference_image_urls(
        source_asset_url,
        source_type,
        params,
    )
    first_frame_url = str(
        params.get("first_frame_image")
        or params.get("reference_image_url")
        or (source_asset_url if source_type == "image" else "")
        or ""
    ).strip()
    last_frame_url = str(params.get("last_frame_image") or "").strip()
    has_first_last_frame = bool(first_frame_url and last_frame_url)
    if last_frame_url and not first_frame_url:
        raise ModelCapabilityError("尾帧素材必须配合首帧素材使用")
    has_reference = bool(source_asset_url or any(reference_urls))
    if category == "image":
        _require(
            model,
            ("image_to_image", "image_edit") if has_reference else "text_to_image",
            "所选图片模型不支持当前的参考图编辑模式" if has_reference else "所选图片模型不支持文生图",
        )
        if has_reference:
            _require(
                model,
                ("reference_image", "image_reference"),
                "所选图片模型不支持参考图输入",
            )
        mask_mode = str(params.get("edit_mask_mode") or "").strip().lower()
        if params.get("mask_image_url") or mask_mode not in {"", "off"}:
            _require(
                model,
                ("mask_edit", "image_mask", "inpainting"),
                "所选图片模型不支持蒙版编辑，请关闭主体保护或切换图片模型",
            )
        if len(unique_reference_urls) > 1:
            _require(model, "multi_reference", "所选图片模型不支持多参考图")
            _require_declared_reference_limit(
                model,
                len(unique_reference_urls),
                label="图片",
            )
        return
    capabilities = _capabilities(model)
    if source_type == "video" and source_asset_url:
        _require(
            model,
            ("video_reference", "video_edit", "video_to_video", "reference_video"),
            "所选视频模型不支持视频输入",
        )
    elif has_reference:
        _require(model, "image_to_video", "所选视频模型不支持图生视频")
    else:
        _require(model, "text_to_video", "所选视频模型不支持文生视频")
    has_source_video = bool(source_type == "video" and source_asset_url)
    is_video_edit_request = bool(
        has_source_video and capabilities.get("video_edit") is True
    )
    requested_duration = params.get("target_duration") or params.get("duration")
    try:
        duration_seconds = float(requested_duration)
    except (TypeError, ValueError):
        duration_seconds = 0
    min_duration = capabilities.get("min_duration_seconds")
    if not is_video_edit_request and min_duration is not None:
        if type(min_duration) is not int or min_duration < 1:
            raise ModelCapabilityError("所选视频模型未声明有效的最短生成时长")
        if duration_seconds and duration_seconds < min_duration:
            raise ModelCapabilityError(
                f"所选视频模型最短支持 {min_duration} 秒，"
                f"当前请求为 {duration_seconds:g} 秒"
            )
    max_duration = capabilities.get("max_duration_seconds")
    if not is_video_edit_request and max_duration is not None:
        if type(max_duration) is not int or max_duration < 1:
            raise ModelCapabilityError("所选视频模型未声明有效的最长生成时长")
        if duration_seconds > max_duration:
            raise ModelCapabilityError(
                f"所选视频模型最长支持 {max_duration} 秒，"
                f"当前请求为 {duration_seconds:g} 秒"
            )
    requested_resolution = str(
        params.get("target_resolution") or params.get("resolution") or ""
    ).strip().lower()
    declared_resolutions = capabilities.get("resolutions")
    if not is_video_edit_request and declared_resolutions is not None:
        supported_resolutions = (
            {
                str(value).strip().lower()
                for value in declared_resolutions
                if str(value or "").strip()
            }
            if isinstance(declared_resolutions, list)
            else set()
        )
        if not supported_resolutions:
            raise ModelCapabilityError("所选视频模型未声明有效的生成分辨率")
        if requested_resolution and requested_resolution not in supported_resolutions:
            options = "/".join(sorted(supported_resolutions))
            raise ModelCapabilityError(
                f"所选视频模型仅支持 {options}，当前请求为 {requested_resolution}"
            )
    if has_independent_reference:
        _require(
            model,
            ("reference_image", "multi_reference"),
            "所选视频模型不支持主体或风格参考图，请改用首帧图生视频或选择支持多参考图的模型",
        )
        max_reference_duration = capabilities.get("max_reference_duration_seconds")
        if max_reference_duration is not None:
            if type(max_reference_duration) is not int or max_reference_duration < 1:
                raise ModelCapabilityError("所选视频模型未声明有效的参考图模式时长上限")
            if duration_seconds > max_reference_duration:
                raise ModelCapabilityError(
                    f"所选视频模型的参考图模式最长支持 {max_reference_duration} 秒，"
                    f"当前请求为 {duration_seconds:g} 秒"
                )
    source_image_is_subject_reference = bool(
        source_type == "image"
        and subject_mode in {"product", "portrait"}
        and (
            params.get("product_reference_image")
            or params.get("character_reference_image")
        )
    )
    has_frame_input = bool(
        params.get("first_frame_image")
        or params.get("reference_image_url")
        or params.get("last_frame_image")
        or (
            source_type == "image"
            and source_asset_url
            and not source_image_is_subject_reference
        )
    )
    if (
        capabilities.get("frame_reference_mode_exclusive") is True
        and has_frame_input
        and (has_independent_reference or has_source_video)
    ):
        raise ModelCapabilityError(
            "所选视频模型的首帧/首尾帧模式不能与多模态参考图或参考视频混用"
        )
    if (
        capabilities.get("reference_image_mode_exclusive") is True
        and has_independent_reference
        and (has_frame_input or has_source_video)
    ):
        raise ModelCapabilityError(
            "所选视频模型不能同时提交独立参考图与首帧、尾帧或源视频"
        )
    if has_first_last_frame:
        _require_strict_first_last_frame(model)
    if len(unique_reference_urls) > 1:
        frame_urls = {first_frame_url, last_frame_url} if has_first_last_frame else set()
        if not has_first_last_frame or not set(unique_reference_urls).issubset(frame_urls):
            _require_strict_multi_reference(model, len(unique_reference_urls))
    if source_type == "video" and source_asset_url:
        capabilities = _capabilities(model)
        max_reference_videos = capabilities.get("max_reference_videos")
        if max_reference_videos is not None and (
            type(max_reference_videos) is not int or max_reference_videos < 1
        ):
            raise ModelCapabilityError("所选视频模型未声明有效的视频输入数量上限")
        if capabilities.get("video_edit") is True and (
            has_independent_reference or bool(first_frame_url) or bool(last_frame_url)
        ):
            raise ModelCapabilityError(
                "所选视频模型的视频编辑模式不能同时提交首帧、尾帧或独立参考图"
            )


def assert_reverse_capability(
    model,
    *,
    target: str,
    source_type: str | None,
) -> None:
    if target == "product_profile":
        _require(model, "product_profile", "所选反推模型不支持产品档案识别")
    elif target == "portrait_profile":
        _require(model, "portrait_profile", "所选反推模型不支持人物档案识别")
    if source_type == "video":
        _require(
            model,
            ("video_analysis", "video_input"),
            "所选反推模型不支持视频分析",
        )
    else:
        _require(
            model,
            ("image_analysis", "image_input"),
            "所选反推模型不支持图片分析",
        )
