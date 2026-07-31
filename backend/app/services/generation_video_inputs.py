"""Pure helpers for canonical video image-input modes."""
from __future__ import annotations

VIDEO_IMAGE_INPUT_MODES = frozenset({"first_frame", "subject_reference"})

_FIRST_FRAME_INCOMPATIBLE_KEYS = (
    "reference_image_url",
    "product_reference_image",
    "product_detail_images",
    "style_reference_image",
    "character_reference_image",
    "product_lock_mode",
    "product_video_template",
)


def canonicalize_video_image_input_params(params: dict) -> dict:
    """Make the explicit first-frame contract exclusive and retry-safe."""
    if str(params.get("video_image_input_mode") or "").strip().lower() != "first_frame":
        return params
    first_frame = params.get("first_frame_image") or params.get("reference_image_url")
    if first_frame:
        params["first_frame_image"] = first_frame
    for key in _FIRST_FRAME_INCOMPATIBLE_KEYS:
        params.pop(key, None)
    return params
