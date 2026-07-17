"""Capability hints for models returned by provider catalog endpoints."""
from __future__ import annotations

from typing import Any


def _image_extra(provider: str, gateway_format: str) -> dict[str, Any]:
    if provider == "antigravity" or gateway_format == "anthropic":
        return {
            "image_transport": "anthropic_messages",
            "capabilities": {
                "text_to_image": True,
                "image_to_image": False,
            },
        }
    if provider == "grok":
        return {
            "image_transport": "grok_images",
            "response_format": "b64_json",
            "capabilities": {
                "text_to_image": True,
                "image_to_image": False,
            },
        }
    return {"capabilities": {"text_to_image": True}}


def _video_extra(provider: str) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "capabilities": {
            "text_to_video": True,
            "image_to_video": False,
            "multi_reference": False,
        }
    }
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

    is_video = "video" in lowered
    is_explicit_image = "image" in lowered and not is_video
    is_grok_edit_only = provider == "grok" and lowered.endswith("-edit")

    if is_video:
        recommended_uses = ["video"]
        capability_label = "视频生成"
        transport = "openai_async_video"
        default_extra = _video_extra(provider)
    elif is_explicit_image and not is_grok_edit_only:
        recommended_uses = ["image"]
        capability_label = "图片生成"
        default_extra = _image_extra(provider, gateway_format)
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
