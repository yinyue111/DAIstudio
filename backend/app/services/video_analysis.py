"""Video reverse-prompt analysis presets.

The UI exposes four presets. The backend translates each preset and source
duration into a concrete keyframe budget; billing uses one fixed reverse price
rather than multiplying by the extracted frame count.
"""
from __future__ import annotations

from dataclasses import dataclass

from .generation_pricing import REVERSE_VIDEO_PRESET_COSTS

DEFAULT_VIDEO_ANALYSIS_PRESET = "standard"


@dataclass(frozen=True)
class VideoAnalysisPreset:
    key: str
    label: str
    description: str
    min_frames: int
    max_frames: int


VIDEO_ANALYSIS_PRESETS: dict[str, VideoAnalysisPreset] = {
    "fast": VideoAnalysisPreset(
        key="fast",
        label="快速",
        description="便宜、粗略",
        min_frames=4,
        max_frames=8,
    ),
    "standard": VideoAnalysisPreset(
        key="standard",
        label="标准",
        description="推荐默认",
        min_frames=8,
        max_frames=14,
    ),
    "fine": VideoAnalysisPreset(
        key="fine",
        label="精细",
        description="更接近原视频风格",
        min_frames=14,
        max_frames=22,
    ),
    "ultra": VideoAnalysisPreset(
        key="ultra",
        label="超精细",
        description="高密度镜头与动作分析",
        min_frames=22,
        max_frames=36,
    ),
}


def normalize_video_analysis_preset(value: str | None) -> str:
    key = (value or DEFAULT_VIDEO_ANALYSIS_PRESET).strip().lower()
    return key if key in VIDEO_ANALYSIS_PRESETS else DEFAULT_VIDEO_ANALYSIS_PRESET


def max_frame_count(preset_key: str | None) -> int:
    preset = VIDEO_ANALYSIS_PRESETS[normalize_video_analysis_preset(preset_key)]
    return preset.max_frames


def _interp(duration: float, start_s: float, end_s: float, start_n: int, end_n: int) -> int:
    if end_s <= start_s:
        return end_n
    ratio = min(1.0, max(0.0, (duration - start_s) / (end_s - start_s)))
    return int(round(start_n + (end_n - start_n) * ratio))


def frame_count_for_duration(duration_seconds: float | None, preset_key: str | None) -> int:
    """Return the target frame budget for a source duration.

    Videos up to 15 seconds use the preset minimum. Longer sources scale
    smoothly to the preset maximum at five minutes.
    Unknown duration uses the preset maximum so extraction remains bounded.
    """
    preset = VIDEO_ANALYSIS_PRESETS[normalize_video_analysis_preset(preset_key)]
    if not duration_seconds or duration_seconds <= 0:
        return preset.max_frames
    duration = float(duration_seconds)
    if duration <= 15:
        return preset.min_frames
    if duration <= 300:
        return _interp(duration, 15, 300, preset.min_frames, preset.max_frames)
    return preset.max_frames


def preset_options(vision_cost: int = 0, preset_costs: dict[str, int] | None = None) -> list[dict]:
    del vision_cost  # Compatibility argument; reverse video billing is preset-based.
    preset_costs = {**REVERSE_VIDEO_PRESET_COSTS, **(preset_costs or {})}
    return [
        {
            "key": preset.key,
            "label": preset.label,
            "description": preset.description,
            "frame_range": f"{preset.min_frames}-{preset.max_frames}帧",
            # Keep both legacy fields until older clients have refreshed config.
            "short_range": f"{preset.min_frames}-{preset.max_frames}帧",
            "long_range": f"{preset.min_frames}-{preset.max_frames}帧",
            "max_frames": preset.max_frames,
            "max_cost": int(preset_costs[preset.key]),
        }
        for preset in VIDEO_ANALYSIS_PRESETS.values()
    ]
