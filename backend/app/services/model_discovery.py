"""Capability hints for models returned by provider catalog endpoints."""
from __future__ import annotations

import re
from typing import Any

_SEEDANCE_VERSION_RE = re.compile(r"^doubao-seedance-(\d+)-(\d+)(?:-|$)")


def _ark_seedance_version(
    provider: str,
    gateway_format: str,
    model_id: str,
) -> tuple[int, int] | None:
    if (
        str(provider or "").strip().lower() != "volcengine_ark"
        and str(gateway_format or "").strip().lower() != "ark"
    ):
        return None
    match = _SEEDANCE_VERSION_RE.match(str(model_id or "").strip().lower())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _image_extra(provider: str, gateway_format: str, model_id: str) -> dict[str, Any]:
    if provider == "antigravity" or gateway_format == "anthropic":
        verified_edit = (
            provider == "antigravity"
            and model_id.strip().lower() == "gemini-3.1-flash-image"
        )
        extra: dict[str, Any] = {
            "image_transport": "anthropic_messages",
            "capabilities": {
                "text_to_image": True,
                "image_to_image": verified_edit,
            },
        }
        if verified_edit:
            extra.update(
                {
                    "edit_path": "/messages",
                    "multi_image_edit_enabled": True,
                }
            )
            extra["capabilities"].update(
                {
                    "image_edit": True,
                    "reference_image": True,
                    "multi_reference": True,
                    "max_reference_images": 2,
                }
            )
        return extra
    if provider == "grok":
        verified_edit = model_id.strip().lower() in {
            "grok-imagine-image",
            "grok-imagine-image-quality",
        }
        extra = {
            "image_transport": "grok_images",
            "response_format": "b64_json",
            "capabilities": {
                "text_to_image": True,
                "image_to_image": verified_edit,
            },
        }
        if verified_edit:
            extra.update(
                {
                    "edit_path": "/images/edits",
                    "edit_payload_format": "json",
                    "multi_image_edit_enabled": True,
                }
            )
            extra["capabilities"].update(
                {
                    "image_edit": True,
                    "reference_image": True,
                    "multi_reference": True,
                    "max_reference_images": 3,
                }
            )
        return extra
    return {"capabilities": {"text_to_image": True}}


def _video_extra(provider: str, gateway_format: str, model_id: str) -> dict[str, Any]:
    normalized_model_id = model_id.strip().lower()
    is_grok_reference_video = (
        provider == "grok"
        and normalized_model_id == "grok-imagine-video"
    )
    is_grok_video_15 = (
        provider == "grok"
        and normalized_model_id == "grok-imagine-video-1.5"
    )
    seedance_version = _ark_seedance_version(provider, gateway_format, model_id)
    is_seedance_15 = (
        normalized_model_id == "doubao-seedance-1-5-pro-251215"
        and seedance_version == (1, 5)
    )
    is_seedance_20_plus = seedance_version is not None and seedance_version >= (2, 0)
    is_seedance_video = is_seedance_15 or is_seedance_20_plus
    is_multi_reference = is_grok_reference_video or is_seedance_20_plus
    extra: dict[str, Any] = {
        "capabilities": {
            "text_to_video": True,
            "image_to_video": (
                is_grok_reference_video or is_grok_video_15 or is_seedance_video
            ),
            "multi_reference": is_multi_reference,
        }
    }
    if is_grok_reference_video:
        extra.update(
            {
                "product_images_field": "reference_images",
                "product_images_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
            }
        )
        extra["capabilities"].update(
            {
                "reference_image": True,
                "max_reference_images": 3,
            }
        )
    elif is_grok_video_15:
        extra.update(
            {
                "first_frame_field": "image",
                "first_frame_item_field": "url",
                "negative_prompt_mode": "append_to_prompt",
            }
        )
        extra["capabilities"]["reference_image"] = False
    elif is_seedance_15:
        extra["capabilities"].update(
            {
                "reference_image": False,
                "first_last_frame": True,
            }
        )
    elif is_seedance_20_plus:
        extra["capabilities"]["max_reference_images"] = 10
    if provider == "grok":
        extra.update(
            {
                "submit_path": "/v1/videos/generations",
                "poll_path": "/v1/videos/{id}",
            }
        )
    return extra


def discovered_model_metadata(
    model: dict[str, Any],
    *,
    provider: str,
    gateway_format: str,
) -> dict[str, Any]:
    """Add conservative import hints without claiming unverified capabilities."""
    model_id = str(model.get("id") or "").strip()
    lowered = model_id.lower()
    recommended_uses: list[str] = []
    capability_label = "对话 / 提示词"
    transport = gateway_format
    default_extra: dict[str, Any] | None = None

    seedance_version = _ark_seedance_version(provider, gateway_format, model_id)
    is_seedance_15 = (
        lowered == "doubao-seedance-1-5-pro-251215"
        and seedance_version == (1, 5)
    )
    is_seedance_20_plus = seedance_version is not None and seedance_version >= (2, 0)
    is_video = "video" in lowered or is_seedance_15 or is_seedance_20_plus
    is_explicit_image = "image" in lowered and not is_video
    is_grok_edit_only = provider == "grok" and lowered.endswith("-edit")

    if is_video:
        recommended_uses = ["video"]
        capability_label = "视频生成"
        transport = "openai_async_video"
        default_extra = _video_extra(provider, gateway_format, model_id)
    elif is_explicit_image and not is_grok_edit_only:
        recommended_uses = ["image"]
        capability_label = "图片生成"
        default_extra = _image_extra(provider, gateway_format, model_id)
        transport = str(default_extra.get("image_transport") or "openai_images")
    elif is_grok_edit_only:
        capability_label = "图片编辑（需手动配置）"
        transport = "openai_images_edit"
    else:
        recommended_uses = ["prompt"]
        if provider == "antigravity" or gateway_format == "anthropic":
            transport = "anthropic_messages"
        else:
            transport = "openai_chat"

    return {
        **model,
        "recommended_uses": recommended_uses,
        "capability_label": capability_label,
        "transport": transport,
        "default_extra": default_extra,
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
