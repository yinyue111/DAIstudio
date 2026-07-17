"""Credit pricing for reverse prompt and generation tasks.

The business price is intentionally separate from provider token accounting.
Provider usage is still recorded for margin analysis; user-facing credits are
computed from this table so submit freeze, retry, settlement, and UI estimates
stay consistent.
"""
from __future__ import annotations

import math
import re
from typing import Any

_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")

REVERSE_IMAGE_COST = 5
REVERSE_VIDEO_PRESET_COSTS = {
    "fast": 5,
    "standard": 5,
    "fine": 5,
}

IMAGE_PRICE_TABLE = {
    "1k": 8,
    "2k": 8,
    "4k": 8,
}
IMAGE_EDIT_PRICE_TABLE = {
    "1k": 8,
    "2k": 8,
    "4k": 8,
}

VIDEO_PREVIEW_COST = 50
VIDEO_PER_SECOND = {
    "480p": 10,
    "720p": 10,
    "1080p": 10,
}


def _extra_pricing(extra: dict | None) -> dict[str, Any]:
    if not isinstance(extra, dict):
        return {}
    pricing = extra.get("credit_pricing")
    return pricing if isinstance(pricing, dict) else {}


def snapshot_credit_pricing(extra: dict | None = None) -> dict[str, Any]:
    """Freeze the active credit price table into a generation model snapshot.

    Admin-provided ``extra.credit_pricing`` can override the built-in defaults.
    Persisting the resolved table prevents retries/final renders from silently
    changing price after a code or config upgrade.
    """
    pricing = _extra_pricing(extra)
    image = pricing.get("image") if isinstance(pricing.get("image"), dict) else {}
    image_edit = pricing.get("image_edit") if isinstance(pricing.get("image_edit"), dict) else {}
    video_per_second = (
        pricing.get("video_per_second")
        if isinstance(pricing.get("video_per_second"), dict)
        else {}
    )
    return {
        "image": {
            key: _int_from_mapping(image, key, value)
            for key, value in IMAGE_PRICE_TABLE.items()
        },
        "image_edit": {
            key: _int_from_mapping(image_edit, key, value)
            for key, value in IMAGE_EDIT_PRICE_TABLE.items()
        },
        "video_preview_cost": _int_from_mapping(
            pricing,
            "video_preview_cost",
            VIDEO_PREVIEW_COST,
        ),
        "video_per_second": {
            key: _int_from_mapping(video_per_second, key, value)
            for key, value in VIDEO_PER_SECOND.items()
        },
    }


def _int_from_mapping(mapping: dict, key: str, fallback: int) -> int:
    try:
        value = int(mapping.get(key, fallback))
    except (TypeError, ValueError):
        value = fallback
    return max(0, value)


def image_quality_tier(size: str | None) -> str:
    if not size:
        return "1k"
    match = _SIZE_RE.match(str(size).lower())
    if not match:
        return "1k"
    width = int(match.group(1))
    height = int(match.group(2))
    longest = max(width, height)
    area = width * height
    if longest > 2560 or area >= 3840 * 2160 * 0.9:
        return "4k"
    if longest > 1280:
        return "2k"
    return "1k"


def is_edit_or_subject_task(params: dict | None, source_type: str | None = None) -> bool:
    del source_type
    params = params or {}
    if str(params.get("subject_mode") or "").strip().lower() == "product":
        return True
    if params.get("style_reference_image") or params.get("character_reference_image"):
        return True
    return bool(params.get("reference_image_url") or params.get("first_frame_image"))


def image_unit_cost(
    params: dict | None,
    *,
    source_type: str | None = None,
    model_extra: dict | None = None,
) -> int:
    pricing = _extra_pricing(model_extra)
    default_table = IMAGE_EDIT_PRICE_TABLE if is_edit_or_subject_task(params, source_type) else IMAGE_PRICE_TABLE
    table = pricing.get("image_edit" if default_table is IMAGE_EDIT_PRICE_TABLE else "image")
    if not isinstance(table, dict):
        table = default_table
    tier = image_quality_tier((params or {}).get("size"))
    return _int_from_mapping(table, tier, default_table[tier])


def image_generation_cost(
    params: dict | None,
    *,
    n: int | None = None,
    source_type: str | None = None,
    model_extra: dict | None = None,
) -> int:
    params = params or {}
    try:
        count = int(n if n is not None else params.get("n") or 1)
    except (TypeError, ValueError):
        count = 1
    return image_unit_cost(params, source_type=source_type, model_extra=model_extra) * max(1, count)


def legacy_snapshot_generation_cost(
    *,
    category: str,
    stage: str,
    params: dict | None,
    n: int | None = None,
    source_type: str | None = None,
    model_extra: dict | None = None,
    cost_credits: int | None = None,
) -> int:
    """Cost fallback for pre-dynamic-pricing task snapshots.

    Old tasks only stored ``cost_credits`` and ``extra.preview_cost``. Preserve
    that contract for retries/admin settlement, while all new snapshots include
    ``extra.credit_pricing`` and use ``generation_cost``.
    """
    del source_type
    params = params or {}
    try:
        base = max(0, int(cost_credits or 0))
    except (TypeError, ValueError):
        base = 0
    if category == "video" and stage == "preview":
        extra = model_extra if isinstance(model_extra, dict) else {}
        try:
            return max(0, int(extra.get("preview_cost", max(1, base // 10))))
        except (TypeError, ValueError):
            return max(1, base // 10)
    if category == "image":
        try:
            count = int(n if n is not None else params.get("n") or 1)
        except (TypeError, ValueError):
            count = 1
        return base * max(1, count)
    return base


def _video_duration(params: dict | None, stage: str) -> int:
    params = params or {}
    key_order = ("target_duration", "duration") if stage == "final" else ("preview_duration", "duration")
    for key in key_order:
        try:
            value = int(params.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 5


def _video_resolution(params: dict | None, stage: str) -> str:
    params = params or {}
    key_order = ("target_resolution", "resolution") if stage == "final" else ("preview_resolution", "resolution")
    for key in key_order:
        value = str(params.get(key) or "").strip().lower()
        if value in VIDEO_PER_SECOND:
            return value
    return "720p"


def video_generation_cost(
    params: dict | None,
    *,
    stage: str,
    model_extra: dict | None = None,
) -> int:
    pricing = _extra_pricing(model_extra)
    if stage == "preview":
        return _int_from_mapping(pricing, "video_preview_cost", VIDEO_PREVIEW_COST)
    per_second = pricing.get("video_per_second")
    if not isinstance(per_second, dict):
        per_second = VIDEO_PER_SECOND
    resolution = _video_resolution(params, stage)
    unit = _int_from_mapping(per_second, resolution, VIDEO_PER_SECOND[resolution])
    return int(math.ceil(unit * _video_duration(params, stage)))


def generation_cost(
    *,
    category: str,
    stage: str,
    params: dict | None,
    n: int | None = None,
    source_type: str | None = None,
    model_extra: dict | None = None,
) -> int:
    if category == "image":
        return image_generation_cost(params, n=n, source_type=source_type, model_extra=model_extra)
    if category == "video":
        return video_generation_cost(params, stage=stage, model_extra=model_extra)
    return 0


def generation_cost_from_snapshot(
    snapshot: dict | None,
    *,
    category: str,
    stage: str,
    params: dict | None,
    n: int | None = None,
    source_type: str | None = None,
) -> int:
    snapshot = snapshot or {}
    extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
    if isinstance(extra, dict) and isinstance(extra.get("credit_pricing"), dict):
        return generation_cost(
            category=category,
            stage=stage,
            params=params,
            n=n,
            source_type=source_type,
            model_extra=extra,
        )
    return legacy_snapshot_generation_cost(
        category=category,
        stage=stage,
        params=params,
        n=n,
        source_type=source_type,
        model_extra=extra,
        cost_credits=snapshot.get("cost_credits"),
    )


def reverse_cost(target: str, *, preset: str | None = None) -> int:
    if target == "video":
        return REVERSE_VIDEO_PRESET_COSTS.get((preset or "standard").strip().lower(), REVERSE_VIDEO_PRESET_COSTS["standard"])
    if target == "product_profile":
        return REVERSE_IMAGE_COST
    return REVERSE_IMAGE_COST


def public_pricing_config() -> dict:
    return {
        "credit_value_cny": 0.10,
        "reverse": {
            "image_cost": REVERSE_IMAGE_COST,
            "video_preset_costs": dict(REVERSE_VIDEO_PRESET_COSTS),
        },
        "image": {
            "unit_costs": dict(IMAGE_PRICE_TABLE),
            "edit_unit_costs": dict(IMAGE_EDIT_PRICE_TABLE),
        },
        "video": {
            "preview_cost": VIDEO_PREVIEW_COST,
            "per_second": dict(VIDEO_PER_SECOND),
        },
    }
