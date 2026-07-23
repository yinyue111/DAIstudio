from __future__ import annotations

import os
from dataclasses import dataclass


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(
    name: str, default: float, *, minimum: float, maximum: float
) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    api_key: str = ""
    max_frames: int = 64
    max_frame_bytes: int = 12 * 1024 * 1024
    max_frame_pixels: int = 24_000_000
    min_motion_area_ratio: float = 0.002
    max_motion_area_ratio: float = 0.70
    track_iou_threshold: float = 0.12
    transition_threshold: float = 0.46

    @classmethod
    def from_env(cls) -> "ProviderSettings":
        min_motion = _bounded_float(
            "VIDEO_SEMANTIC_MIN_MOTION_AREA_RATIO", 0.002, minimum=0.0001, maximum=0.2
        )
        max_motion = _bounded_float(
            "VIDEO_SEMANTIC_MAX_MOTION_AREA_RATIO", 0.70, minimum=0.1, maximum=0.95
        )
        if min_motion >= max_motion:
            raise ValueError(
                "VIDEO_SEMANTIC_MIN_MOTION_AREA_RATIO must be below maximum"
            )
        return cls(
            api_key=os.getenv("VIDEO_SEMANTIC_PROVIDER_API_KEY", "").strip(),
            max_frames=_bounded_int(
                "VIDEO_SEMANTIC_MAX_FRAMES", 64, minimum=1, maximum=64
            ),
            max_frame_bytes=_bounded_int(
                "VIDEO_SEMANTIC_MAX_FRAME_BYTES",
                12 * 1024 * 1024,
                minimum=1024,
                maximum=32 * 1024 * 1024,
            ),
            max_frame_pixels=_bounded_int(
                "VIDEO_SEMANTIC_MAX_FRAME_PIXELS",
                24_000_000,
                minimum=1_000_000,
                maximum=64_000_000,
            ),
            min_motion_area_ratio=min_motion,
            max_motion_area_ratio=max_motion,
            track_iou_threshold=_bounded_float(
                "VIDEO_SEMANTIC_TRACK_IOU_THRESHOLD", 0.12, minimum=0.01, maximum=0.95
            ),
            transition_threshold=_bounded_float(
                "VIDEO_SEMANTIC_TRANSITION_THRESHOLD", 0.46, minimum=0.05, maximum=0.99
            ),
        )
