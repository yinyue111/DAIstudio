"""Capability hints for models returned by provider catalog endpoints."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

_SEEDANCE_VERSION_RE = re.compile(r"^doubao-seedance-(\d+)-(\d+)(?:-|$)")
_GROK_IMAGE_MODEL_RE = re.compile(
    r"^grok-imagine-image(?:$|-[a-z0-9][a-z0-9.-]*)"
)

_PROMPT_MODEL_PREFIXES = (
    "gpt-",
    "chatgpt-",
    "o1",
    "o3",
    "o4",
    "claude-",
    "gemini-",
    "grok-",
    "deepseek-",
    "moonshot-",
    "kimi-",
    "glm-",
    "qwen-",
    "qwq-",
    "ernie-",
    "hunyuan-",
    "mistral-",
    "llama-",
    "baichuan-",
    "yi-",
)
_NON_PROMPT_MODEL_MARKERS = (
    "embedding",
    "embed-",
    "rerank",
    "moderation",
    "whisper",
    "transcri",
    "speech",
    "tts-",
    "audio",
)


def _ark_seedance_version(
    provider: str,
    gateway_format: str,
    model_id: str,
) -> tuple[int, int] | None:
    if not (
        str(provider or "").strip().lower() == "volcengine_ark"
        and str(gateway_format or "").strip().lower() == "ark"
    ):
        return None
    match = _SEEDANCE_VERSION_RE.match(str(model_id or "").strip().lower())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _image_extra(
    provider: str,
    gateway_format: str,
    model_id: str,
) -> dict[str, Any] | None:
    normalized_model_id = model_id.strip().lower()
    capabilities = {
        "text_to_image": True,
        "image_to_image": True,
        "reference_image": True,
        "multi_reference": True,
        # The Studio image flow currently assembles a primary and one style image.
        "max_reference_images": 2,
    }
    if normalized_model_id == "gpt-image-2" and gateway_format == "openai":
        return {
            "edit_path": "/v1/images/edits",
            "multi_image_edit_enabled": True,
            "capabilities": {**capabilities, "mask_edit": True},
        }
    if (
        provider == "antigravity"
        and gateway_format == "anthropic"
        and normalized_model_id == "gemini-3.1-flash-image"
    ):
        return {
            "image_transport": "anthropic_messages",
            "edit_path": "/messages",
            "multi_image_edit_enabled": True,
            "capabilities": {**capabilities, "mask_edit": False},
        }
    if (
        provider in {"grok", "custom_openai"}
        and gateway_format == "openai"
        and _GROK_IMAGE_MODEL_RE.fullmatch(normalized_model_id)
        and not normalized_model_id.endswith("-edit")
    ):
        return {
            "image_transport": "grok_images",
            "response_format": "b64_json",
            "edit_path": "/images/edits",
            "edit_payload_format": "json",
            "multi_image_edit_enabled": True,
            "capabilities": {**capabilities, "mask_edit": False},
        }
    return None


def model_extra_with_defaults(
    extra: dict[str, Any] | None,
    *,
    use: str,
    provider: str | None,
    gateway_format: str | None,
    model_id: str,
) -> dict[str, Any] | None:
    """Merge verified adapter defaults without overriding explicit admin choices."""
    if str(use or "").strip().lower() != "image":
        return deepcopy(extra) if isinstance(extra, dict) else extra
    defaults = _image_extra(
        str(provider or "").strip().lower(),
        str(gateway_format or "").strip().lower(),
        str(model_id or "").strip(),
    )
    if defaults is None:
        return deepcopy(extra) if isinstance(extra, dict) else extra
    configured = deepcopy(extra) if isinstance(extra, dict) else {}
    merged = {**deepcopy(defaults), **configured}
    default_capabilities = defaults.get("capabilities")
    configured_capabilities = configured.get("capabilities")
    if isinstance(default_capabilities, dict):
        merged["capabilities"] = {
            **deepcopy(default_capabilities),
            **(
                deepcopy(configured_capabilities)
                if isinstance(configured_capabilities, dict)
                else {}
            ),
        }
    return merged


def _video_extra(
    provider: str,
    gateway_format: str,
    model_id: str,
) -> dict[str, Any] | None:
    normalized_model_id = model_id.strip().lower()
    is_grok_reference_video = (
        provider == "grok"
        and gateway_format == "openai"
        and normalized_model_id == "grok-imagine-video"
    )
    is_grok_video_15 = (
        provider == "grok"
        and gateway_format == "openai"
        and normalized_model_id == "grok-imagine-video-1.5"
    )
    seedance_version = _ark_seedance_version(provider, gateway_format, model_id)
    is_seedance_15 = (
        normalized_model_id == "doubao-seedance-1-5-pro-251215"
        and seedance_version == (1, 5)
    )
    is_seedance_20 = seedance_version == (2, 0)
    if not (
        is_grok_reference_video
        or is_grok_video_15
        or is_seedance_15
        or is_seedance_20
    ):
        return None
    extra: dict[str, Any] = {
        "capabilities": {
            "text_to_video": True,
            "image_to_video": True,
        }
    }
    if is_grok_reference_video:
        extra.update(
            {
                "video_transport": "grok_videos",
                "product_images_field": "reference_images",
                "product_images_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
            }
        )
        extra["capabilities"].update(
            {
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
            }
        )
    elif is_grok_video_15:
        extra.update(
            {
                "video_transport": "grok_videos",
                "first_frame_field": "image",
                "first_frame_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
            }
        )
        extra["capabilities"].update(
            {
                "text_to_video": False,
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
            }
        )
    elif is_seedance_15:
        extra["capabilities"].update(
            {
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
            }
        )
    elif is_seedance_20:
        standard_model = re.fullmatch(
            r"doubao-seedance-2-0-\d+",
            normalized_model_id,
        ) is not None
        extra["capabilities"].update(
            {
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
        )
    if provider == "grok":
        extra.update(
            {
                "submit_path": "/v1/videos/generations",
                "poll_path": "/v1/videos/{id}",
            }
        )
    return extra


def _vision_extra(
    provider: str,
    gateway_format: str,
    model_id: str,
) -> dict[str, Any] | None:
    normalized_model_id = model_id.strip().lower()
    is_openai_vision = (
        normalized_model_id == "gpt-5.6-sol" and gateway_format == "openai"
    )
    is_antigravity_vision = (
        normalized_model_id == "gemini-3.1-pro-high"
        and provider == "antigravity"
        and gateway_format == "anthropic"
    )
    if not (is_openai_vision or is_antigravity_vision):
        return None
    return {
        "capabilities": {
            "image_analysis": True,
            "video_analysis": True,
            "product_profile": True,
            "portrait_profile": True,
        }
    }


def _prompt_extra(
    provider: str,
    gateway_format: str,
    model_id: str,
) -> dict[str, Any] | None:
    normalized_model_id = model_id.strip().lower()
    is_antigravity_prompt = (
        provider == "antigravity"
        and gateway_format == "anthropic"
        and normalized_model_id
        in {
            "claude-opus-4-6-thinking",
            "gemini-3.5-flash-low",
            "gemini-3.1-pro-high",
        }
    )
    is_grok_prompt = (
        provider == "grok"
        and gateway_format == "openai"
        and normalized_model_id == "grok-4.5"
    )
    if not (is_antigravity_prompt or is_grok_prompt):
        return None
    return {"capabilities": {"prompt_optimization": True}}


def _is_prompt_model(gateway_format: str, model_id: str) -> bool:
    if gateway_format not in {"openai", "anthropic"}:
        return False
    normalized = str(model_id or "").strip().lower()
    leaf = normalized.rsplit("/", 1)[-1]
    if not leaf or any(marker in leaf for marker in _NON_PROMPT_MODEL_MARKERS):
        return False
    return leaf.startswith(_PROMPT_MODEL_PREFIXES)


def discovered_model_metadata(
    model: dict[str, Any],
    *,
    provider: str,
    gateway_format: str,
) -> dict[str, Any]:
    """Add conservative import hints without claiming unverified capabilities."""
    provider = str(provider or "").strip().lower()
    gateway_format = str(gateway_format or "").strip().lower()
    model_id = str(model.get("id") or "").strip()
    lowered = model_id.lower()
    recommended_uses: list[str] = []
    capability_label = "未识别模型（需手动配置）"
    transport = gateway_format
    default_extra: dict[str, Any] | None = None
    default_extra_by_use: dict[str, dict[str, Any]] = {}

    seedance_version = _ark_seedance_version(provider, gateway_format, model_id)
    is_seedance_15 = (
        lowered == "doubao-seedance-1-5-pro-251215"
        and seedance_version == (1, 5)
    )
    is_seedance_20 = seedance_version == (2, 0)
    is_video = (
        "video" in lowered
        or lowered.startswith("doubao-seedance-")
        or is_seedance_15
        or is_seedance_20
    )
    is_explicit_image = "image" in lowered and not is_video
    is_grok_edit_only = provider == "grok" and lowered.endswith("-edit")
    is_grok_imagine_manual = provider == "grok" and lowered == "grok-imagine"
    vision_extra = _vision_extra(provider, gateway_format, model_id)
    prompt_extra = _prompt_extra(provider, gateway_format, model_id)

    if is_video:
        default_extra = _video_extra(provider, gateway_format, model_id)
        if default_extra is None:
            capability_label = "视频模型（需手动配置）"
            transport = "openai_async_video"
        else:
            recommended_uses = ["video"]
            capability_label = "视频生成"
            transport = "openai_async_video"
    elif is_explicit_image and not is_grok_edit_only:
        default_extra = _image_extra(provider, gateway_format, model_id)
        if default_extra is None:
            capability_label = "图片模型（需手动配置）"
            transport = (
                "anthropic_messages"
                if provider == "antigravity" or gateway_format == "anthropic"
                else "openai_images"
            )
        else:
            recommended_uses = ["image"]
            capability_label = "图片生成 / 编辑"
            transport = str(default_extra.get("image_transport") or "openai_images")
    elif is_grok_edit_only or is_grok_imagine_manual:
        capability_label = "图片模型（需手动配置）"
        transport = "openai_images_edit"
    elif vision_extra is not None:
        recommended_uses = ["vision"]
        default_extra = vision_extra
        default_extra_by_use["vision"] = vision_extra
        capability_label = "视觉反推"
        if prompt_extra is not None:
            recommended_uses.append("prompt")
            default_extra_by_use["prompt"] = prompt_extra
            capability_label = "视觉反推 / 提示词优化"
        transport = (
            "anthropic_messages"
            if gateway_format == "anthropic"
            else "openai_chat"
        )
    elif prompt_extra is not None or _is_prompt_model(gateway_format, model_id):
        recommended_uses = ["prompt"]
        default_extra = prompt_extra or {
            "capabilities": {"prompt_optimization": True}
        }
        default_extra_by_use["prompt"] = default_extra
        capability_label = "对话 / 提示词"
        if gateway_format == "anthropic":
            transport = "anthropic_messages"
        else:
            transport = "openai_chat"

    if default_extra is not None and recommended_uses:
        default_extra_by_use.setdefault(recommended_uses[0], default_extra)

    return {
        **model,
        "recommended_uses": recommended_uses,
        "capability_label": capability_label,
        "transport": transport,
        "default_extra": default_extra,
        "default_extra_by_use": default_extra_by_use,
    }


def annotate_discovered_models(
    models: list[dict[str, Any]],
    *,
    provider: str,
    gateway_format: str,
) -> list[dict[str, Any]]:
    return [
        discovered_model_metadata(
            model,
            provider=provider,
            gateway_format=gateway_format,
        )
        for model in models
    ]
