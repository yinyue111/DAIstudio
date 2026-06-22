"""Video reverse-prompt analysis presets.

The UI exposes three presets. The backend translates each preset and source
duration into a concrete keyframe budget, then settles credits on the number of
frames actually sent to the vision model.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_VIDEO_ANALYSIS_PRESET = "standard"


@dataclass(frozen=True)
class VideoAnalysisPreset:
    key: str
    label: str
    description: str
    short_min_frames: int
    short_max_frames: int
    long_min_frames: int
    long_max_frames: int

    @property
    def max_frames(self) -> int:
        return self.long_max_frames


VIDEO_ANALYSIS_PRESETS: dict[str, VideoAnalysisPreset] = {
    "fast": VideoAnalysisPreset(
        key="fast",
        label="快速",
        description="便宜、粗略",
        short_min_frames=4,
        short_max_frames=4,
        long_min_frames=8,
        long_max_frames=12,
    ),
    "standard": VideoAnalysisPreset(
        key="standard",
        label="标准",
        description="推荐默认",
        short_min_frames=6,
        short_max_frames=8,
        long_min_frames=16,
        long_max_frames=24,
    ),
    "fine": VideoAnalysisPreset(
        key="fine",
        label="精细",
        description="更接近原视频风格",
        short_min_frames=10,
        short_max_frames=12,
        long_min_frames=24,
        long_max_frames=36,
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

    Mapping follows the product rule:
    - 5-15s: fast 4, standard 6-8, fine 10-12
    - 2-5min: fast 8-12, standard 16-24, fine 24-36

    Durations between 15s and 2min are smoothly interpolated to avoid jumps.
    Unknown duration uses the preset maximum so credit pre-checks remain safe.
    """
    preset = VIDEO_ANALYSIS_PRESETS[normalize_video_analysis_preset(preset_key)]
    if not duration_seconds or duration_seconds <= 0:
        return preset.max_frames
    duration = float(duration_seconds)
    if duration <= 5:
        return preset.short_min_frames
    if duration <= 15:
        return _interp(duration, 5, 15, preset.short_min_frames, preset.short_max_frames)
    if duration <= 120:
        return _interp(duration, 15, 120, preset.short_max_frames, preset.long_min_frames)
    if duration <= 300:
        return _interp(duration, 120, 300, preset.long_min_frames, preset.long_max_frames)
    return preset.long_max_frames


def preset_options(vision_cost: int = 0) -> list[dict]:
    cost = max(0, int(vision_cost or 0))
    return [
        {
            "key": preset.key,
            "label": preset.label,
            "description": preset.description,
            "short_range": (
                f"{preset.short_min_frames}帧"
                if preset.short_min_frames == preset.short_max_frames
                else f"{preset.short_min_frames}-{preset.short_max_frames}帧"
            ),
            "long_range": (
                f"{preset.long_min_frames}帧"
                if preset.long_min_frames == preset.long_max_frames
                else f"{preset.long_min_frames}-{preset.long_max_frames}帧"
            ),
            "max_frames": preset.max_frames,
            "max_cost": cost * preset.max_frames,
        }
        for preset in VIDEO_ANALYSIS_PRESETS.values()
    ]
