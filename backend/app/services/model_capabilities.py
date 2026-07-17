"""Server-side enforcement for optional public model capabilities."""
from __future__ import annotations


class ModelCapabilityError(ValueError):
    pass


def _capabilities(model) -> dict:
    extra = getattr(model, "extra", None)
    value = extra.get("capabilities") if isinstance(extra, dict) else None
    return value if isinstance(value, dict) else {}


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
            "mask_image_url",
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


def assert_generation_capability(
    model,
    *,
    category: str,
    source_asset_url: str | None,
    source_type: str | None,
    params: dict | None,
) -> None:
    params = params or {}
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
        if len(unique_reference_urls) > 1:
            _require(model, "multi_reference", "所选图片模型不支持多参考图")
        return
    if len(unique_reference_urls) > 1:
        _require_strict_multi_reference(model, len(unique_reference_urls))
    if source_type == "video" and source_asset_url:
        _require(model, "video_to_video", "所选视频模型不支持视频参考生成")
    elif has_reference:
        _require(model, "image_to_video", "所选视频模型不支持图生视频")
    else:
        _require(model, "text_to_video", "所选视频模型不支持文生视频")


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
