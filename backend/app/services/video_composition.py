"""Owner-gated storyboard composition, subtitle burn-in, audio mix, and export."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageColor, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import GenAsset, GenTask, ToolRun
from . import asset_refs, storage, user_assets, video_frames

FFMPEG = shutil.which("ffmpeg")
MAX_SHOTS = 64
MAX_SUBTITLES = 200
MAX_AUDIO_TRACKS = 8
MAX_DURATION_SECONDS = 20 * 60
MAX_OUTPUT_PIXELS = 1920 * 1920
RENDER_TIMEOUT_SECONDS = 30 * 60


class VideoCompositionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "VIDEO_COMPOSITION_FAILED",
        capability_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.capability_status = capability_status


class CanvasSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1280, ge=64, le=1920)
    height: int = Field(default=720, ge=64, le=1920)
    fps: int = Field(default=24, ge=12, le=60)
    background_color: str = Field(default="#000000", max_length=16)

    @field_validator("background_color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        try:
            rgb = ImageColor.getrgb(value)
        except ValueError as exc:
            raise ValueError("画布背景颜色无效") from exc
        if len(rgb) not in {3, 4}:
            raise ValueError("画布背景颜色无效")
        return value

    @model_validator(mode="after")
    def _bounded_pixels(self):
        if self.width * self.height > MAX_OUTPUT_PIXELS:
            raise ValueError("画布像素超过上限")
        if self.width % 2 or self.height % 2:
            raise ValueError("画布宽高必须为偶数")
        return self


class TransitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["cut", "fade", "crossfade", "wipeleft", "slideright"] = "cut"
    duration_seconds: float = Field(default=0, ge=0, le=3)

    @model_validator(mode="after")
    def _duration_matches_type(self):
        if self.type == "cut":
            self.duration_seconds = 0
        elif self.duration_seconds <= 0:
            raise ValueError("非硬切转场必须设置持续时间")
        return self


class ShotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_id: str = Field(min_length=1, max_length=128)
    asset_ref: str = Field(min_length=3, max_length=1024)
    generation_task_id: int | None = Field(default=None, ge=1)
    source_start_seconds: float = Field(default=0, ge=0, le=MAX_DURATION_SECONDS)
    source_end_seconds: float | None = Field(default=None, gt=0, le=MAX_DURATION_SECONDS)
    duration_seconds: float | None = Field(default=None, gt=0, le=MAX_DURATION_SECONDS)
    transition: TransitionSpec = Field(default_factory=TransitionSpec)

    @model_validator(mode="after")
    def _valid_range(self):
        if (
            self.source_end_seconds is not None
            and self.source_end_seconds <= self.source_start_seconds
        ):
            raise ValueError("镜头结束时间必须晚于开始时间")
        return self


class SubtitleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0, le=MAX_DURATION_SECONDS)
    end_seconds: float = Field(gt=0, le=MAX_DURATION_SECONDS)
    text: str = Field(min_length=1, max_length=500)
    font_size: int = Field(default=42, ge=16, le=96)
    text_color: str = Field(default="#ffffff", max_length=16)
    background_color: str = Field(default="#000000b8", max_length=16)
    bottom_margin: int = Field(default=48, ge=0, le=600)

    @field_validator("text_color", "background_color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        try:
            ImageColor.getcolor(value, "RGBA")
        except ValueError as exc:
            raise ValueError("字幕颜色无效") from exc
        return value

    @model_validator(mode="after")
    def _valid_range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("字幕结束时间必须晚于开始时间")
        return self


class AudioTrackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ref: str = Field(min_length=3, max_length=1024)
    start_seconds: float = Field(default=0, ge=0, le=MAX_DURATION_SECONDS)
    source_start_seconds: float = Field(default=0, ge=0, le=MAX_DURATION_SECONDS)
    source_end_seconds: float | None = Field(default=None, gt=0, le=MAX_DURATION_SECONDS)
    volume: float = Field(default=1, ge=0, le=4)
    loop: bool = False

    @model_validator(mode="after")
    def _valid_range(self):
        if (
            self.source_end_seconds is not None
            and self.source_end_seconds <= self.source_start_seconds
        ):
            raise ValueError("音轨结束时间必须晚于开始时间")
        return self


class VideoCompositionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["video-composition.v1"] = "video-composition.v1"
    title: str = Field(default="分镜合成", min_length=1, max_length=128)
    reverse_operation_id: int | None = Field(default=None, ge=1)
    canvas: CanvasSpec = Field(default_factory=CanvasSpec)
    shots: list[ShotSpec] = Field(min_length=1, max_length=MAX_SHOTS)
    subtitles: list[SubtitleSpec] = Field(default_factory=list, max_length=MAX_SUBTITLES)
    audio_tracks: list[AudioTrackSpec] = Field(default_factory=list, max_length=MAX_AUDIO_TRACKS)
    original_audio_volume: float = Field(default=1, ge=0, le=2)

    @model_validator(mode="after")
    def _unique_shots(self):
        shot_ids = [shot.shot_id for shot in self.shots]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("shot_id 不能重复")
        return self


def _parse_spec(payload: dict[str, Any]) -> VideoCompositionSpec:
    try:
        return VideoCompositionSpec.model_validate(payload)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg") if exc.errors() else "合成参数无效"
        raise VideoCompositionError(
            str(message),
            code="VIDEO_COMPOSITION_INVALID_INPUT",
        ) from exc


def _request_fingerprint(spec: VideoCompositionSpec) -> str:
    canonical = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _run_ffmpeg(command: list[str], *, timeout: int = RENDER_TIMEOUT_SECONDS) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoCompositionError(
            "视频合成超时",
            code="VIDEO_COMPOSITION_TIMEOUT",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg 执行失败").strip()[-1600:]
        raise VideoCompositionError(
            f"视频合成失败: {detail}",
            code="VIDEO_COMPOSITION_RENDER_FAILED",
        )


def _resolved_video_source(
    db: Session,
    user_id: int,
    asset_ref: str,
) -> tuple[Path, int | None]:
    try:
        resolved = user_assets.resolve_asset_ref(db, user_id, asset_ref)
    except (user_assets.AssetNotFound, user_assets.InvalidAssetRef) as exc:
        raise VideoCompositionError("视频素材不存在", code="VIDEO_COMPOSITION_ASSET_NOT_FOUND") from exc
    if resolved.origin == "uploaded":
        generation_task_id = None
        key = str(resolved.row.key)
        if not key.startswith("upload_video/"):
            raise VideoCompositionError("合成镜头必须使用视频素材", code="VIDEO_COMPOSITION_ASSET_TYPE")
    else:
        row = resolved.row
        if not isinstance(row, GenAsset) or row.type != "video":
            raise VideoCompositionError("合成镜头必须使用视频素材", code="VIDEO_COMPOSITION_ASSET_TYPE")
        candidate = row.hd_url if row.unlocked and row.hd_url else row.preview_url
        generation_task_id = int(row.task_id) if row.task_id is not None else None
        key = storage.key_from_url(str(candidate or "")) or ""
        if not key or not key.startswith(("video_hd/", "video_preview/")):
            raise VideoCompositionError(
                "该生成视频尚未解锁或缺少可合成文件",
                code="VIDEO_COMPOSITION_ASSET_LOCKED",
            )
    try:
        path = asset_refs.generated_video_reference_path(db, user_id, key)
    except asset_refs.AssetRefError as exc:
        raise VideoCompositionError(str(exc), code="VIDEO_COMPOSITION_ASSET_UNAVAILABLE") from exc
    if not path.is_file():
        raise VideoCompositionError("视频素材文件不存在", code="VIDEO_COMPOSITION_ASSET_UNAVAILABLE")
    return path, generation_task_id


def _resolved_video_path(db: Session, user_id: int, asset_ref: str) -> Path:
    return _resolved_video_source(db, user_id, asset_ref)[0]


def _resolved_audio_track_path(db: Session, user_id: int, asset_ref: str) -> Path:
    """音轨素材:优先支持 upload_audio/ 音频素材,兼容含音轨的视频素材。"""
    try:
        resolved = user_assets.resolve_asset_ref(db, user_id, asset_ref)
    except (user_assets.AssetNotFound, user_assets.InvalidAssetRef) as exc:
        raise VideoCompositionError("音轨素材不存在", code="VIDEO_COMPOSITION_ASSET_NOT_FOUND") from exc
    if resolved.origin == "uploaded":
        key = str(resolved.row.key)
        if key.startswith("upload_audio/"):
            # resolve_asset_ref 已做归属校验;这里只需拿到本地文件路径。
            try:
                path = storage.download_to_local_temp(key)
            except Exception as exc:  # noqa: BLE001 — 存储读取失败统一转业务错误
                raise VideoCompositionError(
                    "音频素材文件读取失败",
                    code="VIDEO_COMPOSITION_ASSET_UNAVAILABLE",
                ) from exc
            if not path.is_file():
                raise VideoCompositionError(
                    "音频素材文件不存在",
                    code="VIDEO_COMPOSITION_ASSET_UNAVAILABLE",
                )
            return path
    return _resolved_video_path(db, user_id, asset_ref)


def _duration_for_shot(shot: ShotSpec, metadata: dict[str, Any]) -> float:
    source_duration = float(metadata.get("duration_seconds") or metadata.get("duration") or 0)
    source_end = shot.source_end_seconds
    if source_end is None and source_duration > 0:
        source_end = source_duration
    available = (float(source_end) - shot.source_start_seconds) if source_end is not None else 0
    duration = float(shot.duration_seconds or available or 0)
    if duration <= 0:
        raise VideoCompositionError(
            f"镜头 {shot.shot_id} 无法确定持续时间",
            code="VIDEO_COMPOSITION_DURATION_UNKNOWN",
        )
    if source_duration > 0 and shot.source_start_seconds >= source_duration:
        raise VideoCompositionError(
            f"镜头 {shot.shot_id} 起始时间超出素材时长",
            code="VIDEO_COMPOSITION_RANGE_INVALID",
        )
    return min(duration, MAX_DURATION_SECONDS)


def _ffmpeg_color(value: str) -> str:
    red, green, blue, *_alpha = ImageColor.getcolor(value, "RGBA")
    return f"0x{red:02x}{green:02x}{blue:02x}"


def _normalize_clip(
    path: Path,
    output: Path,
    *,
    shot: ShotSpec,
    duration: float,
    canvas: CanvasSpec,
    has_audio: bool,
) -> None:
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y"]
    if shot.source_start_seconds > 0:
        command.extend(["-ss", f"{shot.source_start_seconds:.6f}"])
    command.extend(["-i", str(path)])
    audio_input = "0:a:0"
    if not has_audio:
        command.extend([
            "-f",
            "lavfi",
            "-t",
            f"{duration:.6f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ])
        audio_input = "1:a:0"
    video_filter = (
        f"[0:v:0]scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=decrease,"
        f"pad={canvas.width}:{canvas.height}:(ow-iw)/2:(oh-ih)/2:color={_ffmpeg_color(canvas.background_color)},"
        f"setsar=1,fps={canvas.fps},tpad=stop_mode=clone:stop_duration={duration:.6f},"
        f"trim=duration={duration:.6f},setpts=PTS-STARTPTS[v]"
    )
    audio_filter = (
        f"[{audio_input}]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a]"
    )
    command.extend([
        "-filter_complex",
        f"{video_filter};{audio_filter}",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output),
    ])
    _run_ffmpeg(command)


def _concat_clips(clips: list[Path], output: Path, workdir: Path) -> float:
    concat_file = workdir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for path in clips),
        encoding="utf-8",
    )
    _run_ffmpeg([
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ])
    return sum(float(video_frames.probe_media(str(path)).get("duration") or 0) for path in clips)


_XFADE_TYPES = {
    "fade": "fade",
    "crossfade": "fade",
    "wipeleft": "wipeleft",
    "slideright": "slideright",
}


def _transition_clips(
    clips: list[Path],
    durations: list[float],
    shots: list[ShotSpec],
    output: Path,
    *,
    fps: int,
) -> float:
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y"]
    for path in clips:
        command.extend(["-i", str(path)])
    filters: list[str] = []
    video_label = "0:v:0"
    audio_label = "0:a:0"
    running_duration = durations[0]
    for index in range(1, len(clips)):
        transition = shots[index - 1].transition
        requested = float(transition.duration_seconds or 0)
        duration = requested if transition.type != "cut" else 1 / max(1, fps)
        duration = max(0.001, min(duration, durations[index - 1] / 2, durations[index] / 2))
        offset = max(0, running_duration - duration)
        next_video = f"v{index}"
        next_audio = f"a{index}"
        xfade = _XFADE_TYPES.get(transition.type, "fade")
        filters.append(
            f"[{video_label}][{index}:v:0]xfade=transition={xfade}:"
            f"duration={duration:.6f}:offset={offset:.6f}[{next_video}]"
        )
        filters.append(
            f"[{audio_label}][{index}:a:0]acrossfade=d={duration:.6f}:c1=tri:c2=tri[{next_audio}]"
        )
        video_label = next_video
        audio_label = next_audio
        running_duration += durations[index] - duration
    command.extend([
        "-filter_complex",
        ";".join(filters),
        "-map",
        f"[{video_label}]",
        "-map",
        f"[{audio_label}]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output),
    ])
    _run_ffmpeg(command)
    return running_duration


_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font_for_text(text: str, size: int) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str]:
    non_ascii = any(ord(char) > 127 for char in text)
    for candidate in _FONT_CANDIDATES:
        path = Path(candidate)
        if not path.is_file():
            continue
        if non_ascii and "DejaVu" in candidate:
            continue
        try:
            return ImageFont.truetype(str(path), size=size), str(path)
        except OSError:
            continue
    if non_ascii:
        raise VideoCompositionError(
            "服务器缺少中文字体，无法烧录中文字幕",
            code="VIDEO_COMPOSITION_CJK_FONT_UNAVAILABLE",
            capability_status="unsupported",
        )
    try:
        return ImageFont.load_default(size=size), "pillow-default"
    except TypeError:
        return ImageFont.load_default(), "pillow-default"


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        current = ""
        for char in paragraph:
            candidate = current + char
            width = draw.textbbox((0, 0), candidate, font=font)[2]
            if current and width > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current or " ")
    return lines[:8]


def _render_subtitle_image(subtitle: SubtitleSpec, canvas: CanvasSpec, path: Path) -> str:
    font, font_source = _font_for_text(subtitle.text, subtitle.font_size)
    scratch = Image.new("RGBA", (canvas.width, canvas.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scratch)
    padding_x = max(16, subtitle.font_size // 2)
    padding_y = max(10, subtitle.font_size // 3)
    max_text_width = max(64, int(canvas.width * 0.88) - padding_x * 2)
    lines = _wrap_text(draw, subtitle.text, font, max_text_width)
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    text_width = max(box[2] - box[0] for box in line_boxes)
    line_height = max(box[3] - box[1] for box in line_boxes)
    gap = max(4, subtitle.font_size // 5)
    image_width = min(canvas.width, text_width + padding_x * 2)
    image_height = line_height * len(lines) + gap * (len(lines) - 1) + padding_y * 2
    image = Image.new(
        "RGBA",
        (image_width, image_height),
        ImageColor.getcolor(subtitle.background_color, "RGBA"),
    )
    image_draw = ImageDraw.Draw(image)
    y = padding_y
    color = ImageColor.getcolor(subtitle.text_color, "RGBA")
    for line in lines:
        box = image_draw.textbbox((0, 0), line, font=font)
        width = box[2] - box[0]
        image_draw.text(((image_width - width) / 2, y), line, font=font, fill=color)
        y += line_height + gap
    image.save(path, format="PNG")
    return font_source


def _postprocess(
    source: Path,
    output: Path,
    *,
    spec: VideoCompositionSpec,
    duration: float,
    audio_paths: list[Path],
    workdir: Path,
) -> list[str]:
    if not spec.subtitles and not spec.audio_tracks:
        shutil.copyfile(source, output)
        return []
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    subtitle_inputs: list[tuple[int, SubtitleSpec, Path]] = []
    font_sources: list[str] = []
    for index, subtitle in enumerate(spec.subtitles):
        subtitle_path = workdir / f"subtitle-{index}.png"
        font_sources.append(_render_subtitle_image(subtitle, spec.canvas, subtitle_path))
        input_index = 1 + len(subtitle_inputs)
        command.extend(["-loop", "1", "-framerate", str(spec.canvas.fps), "-i", str(subtitle_path)])
        subtitle_inputs.append((input_index, subtitle, subtitle_path))
    audio_inputs: list[tuple[int, AudioTrackSpec, Path]] = []
    for track, path in zip(spec.audio_tracks, audio_paths, strict=True):
        input_index = 1 + len(subtitle_inputs) + len(audio_inputs)
        if track.loop:
            command.extend(["-stream_loop", "-1"])
        if track.source_start_seconds > 0:
            command.extend(["-ss", f"{track.source_start_seconds:.6f}"])
        command.extend(["-i", str(path)])
        audio_inputs.append((input_index, track, path))

    filters: list[str] = ["[0:v:0]setpts=PTS-STARTPTS[vbase]"]
    video_label = "vbase"
    for index, (input_index, subtitle, _path) in enumerate(subtitle_inputs):
        next_label = f"vsub{index}"
        start = min(duration, subtitle.start_seconds)
        end = min(duration, subtitle.end_seconds)
        filters.append(
            f"[{video_label}][{input_index}:v:0]overlay=(W-w)/2:H-h-{subtitle.bottom_margin}:"
            f"enable='between(t,{start:.6f},{end:.6f})':eof_action=pass[{next_label}]"
        )
        video_label = next_label

    filters.append(f"[0:a:0]volume={spec.original_audio_volume:.6f}[abase]")
    audio_labels = ["abase"]
    for index, (input_index, track, _path) in enumerate(audio_inputs):
        available = max(0.001, duration - track.start_seconds)
        if track.source_end_seconds is not None:
            available = min(available, track.source_end_seconds - track.source_start_seconds)
        label = f"atrack{index}"
        delay_ms = int(round(track.start_seconds * 1000))
        filters.append(
            f"[{input_index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=duration={available:.6f},asetpts=PTS-STARTPTS,volume={track.volume:.6f},"
            f"adelay={delay_ms}|{delay_ms},apad,atrim=duration={duration:.6f}[{label}]"
        )
        audio_labels.append(label)
    if len(audio_labels) > 1:
        joined = "".join(f"[{label}]" for label in audio_labels)
        filters.append(
            f"{joined}amix=inputs={len(audio_labels)}:duration=first:dropout_transition=2:normalize=0[aout]"
        )
        audio_label = "aout"
    else:
        audio_label = "abase"

    command.extend([
        "-filter_complex",
        ";".join(filters),
        "-map",
        f"[{video_label}]",
        "-map",
        f"[{audio_label}]",
        "-t",
        f"{duration:.6f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output),
    ])
    _run_ffmpeg(command)
    return list(dict.fromkeys(font_sources))


def _manifest(
    spec: VideoCompositionSpec,
    *,
    durations: list[float],
    generation_task_ids: list[int | None],
    actual_duration: float,
    font_sources: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": spec.schema_version,
        "title": spec.title,
        "reverse_operation_id": spec.reverse_operation_id,
        "canvas": spec.canvas.model_dump(mode="json"),
        "shots": [
            {
                **shot.model_dump(mode="json"),
                "generation_task_id": generation_task_id,
                "resolved_duration_seconds": round(duration, 3),
            }
            for shot, duration, generation_task_id in zip(
                spec.shots,
                durations,
                generation_task_ids,
                strict=True,
            )
        ],
        "subtitles": [item.model_dump(mode="json") for item in spec.subtitles],
        "audio_tracks": [item.model_dump(mode="json") for item in spec.audio_tracks],
        "original_audio_volume": spec.original_audio_volume,
        "actual_duration_seconds": round(actual_duration, 3),
        "subtitle_font_sources": font_sources,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }


def _existing_result(db: Session, task: GenTask) -> dict[str, Any] | None:
    if task.status != "succeeded":
        return None
    assets = db.scalars(
        select(GenAsset).where(
            GenAsset.task_id == task.id,
            GenAsset.user_id == task.user_id,
            GenAsset.type == "video",
        ).order_by(GenAsset.id)
    )
    asset = next(
        (
            item
            for item in assets
            if (
                (key := storage.key_from_url(str(item.hd_url or item.preview_url or "")))
                and storage.exists(key)
            )
        ),
        None,
    )
    if asset is None:
        return None
    return {
        "schema_version": "video-composition-result.v1",
        "task_id": int(task.id),
        "asset_id": int(asset.id),
        "asset_ref": user_assets.generated_asset_ref(int(asset.id)),
        "download_url": f"/api/me/assets/download?asset_ref={user_assets.generated_asset_ref(int(asset.id))}",
        "manifest": dict((task.params or {}).get("composition_manifest") or {}),
        "idempotent_replay": True,
    }


def compose_video(
    db: Session,
    *,
    user_id: int,
    payload: dict[str, Any],
    client_request_id: str,
) -> dict[str, Any]:
    """Render one composition and persist it as a zero-credit internal GenTask."""
    spec = _parse_spec(payload)
    request_fingerprint = _request_fingerprint(spec)
    input_snapshot = spec.model_dump(mode="json")
    existing = db.scalar(
        select(GenTask).where(
            GenTask.user_id == int(user_id),
            GenTask.client_request_id == str(client_request_id),
        )
    )
    if existing is not None:
        existing_params = dict(existing.params or {})
        existing_fingerprint = str(existing_params.get("composition_request_fingerprint") or "")
        if existing_params.get("workflow_kind") != "video_composition" or (
            existing_fingerprint and existing_fingerprint != request_fingerprint
        ):
            raise VideoCompositionError(
                "client_request_id 已用于不同的视频合成输入",
                code="VIDEO_COMPOSITION_IDEMPOTENCY_CONFLICT",
            )
        replay = _existing_result(db, existing)
        if replay is not None:
            return replay
        task = existing
        task.params = {
            **existing_params,
            "workflow_kind": "video_composition",
            "composition_request_fingerprint": request_fingerprint,
            "composition_input": input_snapshot,
        }
        task.status = "running"
        task.phase = "rendering"
        task.error = None
        task.finished_at = None
    else:
        task = GenTask(
            user_id=int(user_id),
            model_config_id=None,
            quote_id=None,
            source_asset_url=None,
            source_type="video",
            category="video",
            stage="final",
            prompt={"final_text": spec.title},
            model_use="video",
            params={
                "workflow_kind": "video_composition",
                "composition_request_fingerprint": request_fingerprint,
                "composition_input": input_snapshot,
            },
            client_request_id=str(client_request_id)[:128],
            status="running",
            phase="rendering",
            cost_frozen=0,
            cost_settled=0,
        )
        db.add(task)
    db.commit()
    db.refresh(task)

    if not FFMPEG or not video_frames.FFPROBE:
        task.status = "failed"
        task.phase = None
        task.error = "服务器缺少 ffmpeg/ffprobe，无法合成视频"
        task.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise VideoCompositionError(
            task.error,
            code="VIDEO_COMPOSITION_FFMPEG_UNAVAILABLE",
            capability_status="unsupported",
        )

    written_keys: list[str] = []
    acquired = video_frames.acquire_video_slot()
    if not acquired:
        task.status = "failed"
        task.phase = None
        task.error = "视频处理资源繁忙，请稍后重试"
        task.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise VideoCompositionError(task.error, code="VIDEO_COMPOSITION_BUSY")
    try:
        shot_sources = [
            _resolved_video_source(db, user_id, shot.asset_ref)
            for shot in spec.shots
        ]
        shot_paths = [path for path, _generation_task_id in shot_sources]
        generation_task_ids = [generation_task_id for _path, generation_task_id in shot_sources]
        for shot, generation_task_id in zip(spec.shots, generation_task_ids, strict=True):
            if (
                shot.generation_task_id is not None
                and shot.generation_task_id != generation_task_id
            ):
                raise VideoCompositionError(
                    f"镜头 {shot.shot_id} 的生成任务血缘与素材不一致",
                    code="VIDEO_COMPOSITION_LINEAGE_MISMATCH",
                )
        audio_paths = [
            _resolved_audio_track_path(db, user_id, track.asset_ref)
            for track in spec.audio_tracks
        ]
        audio_metadata = [video_frames.probe_media(str(path)) for path in audio_paths]
        if any(not metadata.get("has_audio") for metadata in audio_metadata):
            raise VideoCompositionError(
                "背景音轨素材不包含音频流",
                code="VIDEO_COMPOSITION_AUDIO_MISSING",
            )
        with tempfile.TemporaryDirectory(prefix="video-composition-") as temp_dir:
            workdir = Path(temp_dir)
            normalized: list[Path] = []
            durations: list[float] = []
            for index, (shot, path) in enumerate(zip(spec.shots, shot_paths, strict=True)):
                metadata = video_frames.probe_media(str(path))
                if not metadata.get("width") or not metadata.get("height"):
                    raise VideoCompositionError(
                        f"镜头 {shot.shot_id} 不是有效视频",
                        code="VIDEO_COMPOSITION_MEDIA_INVALID",
                    )
                duration = _duration_for_shot(shot, metadata)
                durations.append(duration)
                normalized_path = workdir / f"normalized-{index}.mp4"
                _normalize_clip(
                    path,
                    normalized_path,
                    shot=shot,
                    duration=duration,
                    canvas=spec.canvas,
                    has_audio=bool(metadata.get("has_audio")),
                )
                normalized.append(normalized_path)

            joined = workdir / "joined.mp4"
            if len(normalized) == 1:
                shutil.copyfile(normalized[0], joined)
                actual_duration = durations[0]
            elif all(shot.transition.type == "cut" for shot in spec.shots[:-1]):
                actual_duration = _concat_clips(normalized, joined, workdir)
            else:
                actual_duration = _transition_clips(
                    normalized,
                    durations,
                    spec.shots,
                    joined,
                    fps=spec.canvas.fps,
                )
            if actual_duration <= 0 or actual_duration > MAX_DURATION_SECONDS:
                raise VideoCompositionError(
                    "合成后视频时长无效或超过上限",
                    code="VIDEO_COMPOSITION_DURATION_INVALID",
                )
            final_path = workdir / "final.mp4"
            font_sources = _postprocess(
                joined,
                final_path,
                spec=spec,
                duration=actual_duration,
                audio_paths=audio_paths,
                workdir=workdir,
            )
            metadata = video_frames.probe_media(str(final_path))
            if not metadata.get("width") or not metadata.get("height"):
                raise VideoCompositionError(
                    "合成结果缺少有效视频流",
                    code="VIDEO_COMPOSITION_OUTPUT_INVALID",
                )
            media_key = storage.save_file(final_path, "video_hd", "mp4")
            written_keys.append(media_key)
            poster = video_frames.extract_poster(str(final_path))
            preview_url = None
            if poster:
                poster_key = storage.save_bytes(poster, "preview", "jpg")
                written_keys.append(poster_key)
                preview_url = storage.public_url(poster_key)
            manifest = _manifest(
                spec,
                durations=durations,
                generation_task_ids=generation_task_ids,
                actual_duration=float(metadata.get("duration") or actual_duration),
                font_sources=font_sources,
            )
            manifest["output"] = {
                "storage_key": media_key,
                "width": metadata.get("width"),
                "height": metadata.get("height"),
                "duration_seconds": metadata.get("duration"),
            }
            task.params = {
                "workflow_kind": "video_composition",
                "composition_request_fingerprint": request_fingerprint,
                "composition_input": input_snapshot,
                "composition_manifest": manifest,
                "_video_result_keys": list(written_keys),
            }
            task.prompt = {"final_text": spec.title, "composition_manifest": manifest}
            task.status = "succeeded"
            task.phase = None
            task.error = None
            task.finished_at = datetime.now(timezone.utc)
            asset = GenAsset(
                task_id=int(task.id),
                user_id=int(user_id),
                type="video",
                preview_url=preview_url or storage.public_url(media_key),
                hd_url=storage.public_url(media_key),
                watermarked=False,
                unlocked=True,
                width=int(metadata.get("width") or spec.canvas.width),
                height=int(metadata.get("height") or spec.canvas.height),
                duration=max(1, int(math.ceil(float(metadata.get("duration") or actual_duration)))),
                bytes=sum(int(storage.object_size(key) or 0) for key in written_keys),
            )
            db.add(asset)
            db.commit()
            db.refresh(asset)
            asset_ref = user_assets.generated_asset_ref(int(asset.id))
            return {
                "schema_version": "video-composition-result.v1",
                "task_id": int(task.id),
                "asset_id": int(asset.id),
                "asset_ref": asset_ref,
                "download_url": f"/api/me/assets/download?asset_ref={asset_ref}",
                "manifest": manifest,
                "idempotent_replay": False,
            }
    except Exception as exc:
        db.rollback()
        for key in written_keys:
            try:
                storage.delete(key)
            except Exception:  # noqa: BLE001 - retain the primary render failure
                pass
        failed = db.get(GenTask, int(task.id))
        if failed is not None and failed.status != "succeeded":
            failed.status = "failed"
            failed.phase = None
            failed.error = str(exc)[:2000]
            failed.finished_at = datetime.now(timezone.utc)
            db.commit()
        if isinstance(exc, VideoCompositionError):
            raise
        raise VideoCompositionError(str(exc)) from exc
    finally:
        video_frames.release_video_slot()


def _context_user_id(context: Any, db: Session) -> int:
    tool_run = db.get(ToolRun, int(context.tool_run_id))
    if tool_run is None:
        raise VideoCompositionError("工具运行不存在", code="VIDEO_COMPOSITION_TOOL_RUN_MISSING")
    return int(tool_run.user_id)


def _context_payload(context: Any) -> dict[str, Any]:
    workflow_input = dict(getattr(context, "workflow_input", None) or {})
    payload = workflow_input.get("composition")
    if isinstance(payload, dict):
        return payload
    return workflow_input


def compose_workflow_node(context: Any) -> dict[str, Any]:
    db = SessionLocal()
    try:
        user_id = _context_user_id(context, db)
        return compose_video(
            db,
            user_id=user_id,
            payload=_context_payload(context),
            client_request_id=(
                f"workflow-compose-{int(context.tool_run_id)}-{str(context.node_key)}"
            )[:128],
        )
    finally:
        db.close()


def _composition_dependency(context: Any) -> dict[str, Any]:
    dependencies = dict(getattr(context, "dependency_outputs", None) or {})
    for value in reversed(list(dependencies.values())):
        if isinstance(value, dict) and value.get("schema_version") == "video-composition-result.v1":
            return value
    previous = dict(getattr(context, "previous_output", None) or {})
    if previous.get("schema_version") == "video-composition-result.v1":
        return previous
    raise VideoCompositionError(
        "导出节点缺少已完成的视频合成结果",
        code="VIDEO_COMPOSITION_EXPORT_INPUT_MISSING",
    )


def export_workflow_node(context: Any) -> dict[str, Any]:
    db = SessionLocal()
    try:
        user_id = _context_user_id(context, db)
        result = _composition_dependency(context)
        asset_ref = str(result.get("asset_ref") or "")
        try:
            resolved = user_assets.resolve_asset_ref(db, user_id, asset_ref)
        except (user_assets.AssetNotFound, user_assets.InvalidAssetRef) as exc:
            raise VideoCompositionError(
                "合成结果不存在或无权导出",
                code="VIDEO_COMPOSITION_EXPORT_FORBIDDEN",
            ) from exc
        if resolved.origin != "generated" or not isinstance(resolved.row, GenAsset):
            raise VideoCompositionError(
                "合成结果类型不支持导出",
                code="VIDEO_COMPOSITION_EXPORT_INVALID",
            )
        title = str((result.get("manifest") or {}).get("title") or "storyboard-export").strip()
        safe_title = "".join(char for char in title if char.isalnum() or char in "-_ ").strip()
        return {
            "schema_version": "video-composition-export.v1",
            "asset_ref": asset_ref,
            "asset_id": int(resolved.row.id),
            "download_url": f"/api/me/assets/download?asset_ref={asset_ref}",
            "filename": f"{safe_title or 'storyboard-export'}.mp4",
            "manifest": dict(result.get("manifest") or {}),
        }
    finally:
        db.close()


def compensate_workflow_node(context: Any) -> dict[str, Any]:
    db = SessionLocal()
    deleted_keys: list[str] = []
    try:
        user_id = _context_user_id(context, db)
        output = dict(getattr(context, "previous_output", None) or {})
        task_id = int(output.get("task_id") or 0)
        task = db.scalar(
            select(GenTask).where(GenTask.id == task_id, GenTask.user_id == user_id)
        )
        if task is None:
            return {"status": "already_absent", "task_id": task_id}
        assets = list(db.scalars(select(GenAsset).where(GenAsset.task_id == task.id)))
        for asset in assets:
            for url in (asset.preview_url, asset.hd_url):
                key = storage.key_from_url(str(url or ""))
                if key and key not in deleted_keys:
                    storage.delete(key)
                    deleted_keys.append(key)
            db.delete(asset)
        task.status = "canceled"
        task.phase = None
        task.error = "工作流补偿已清理合成结果"
        task.finished_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": "compensated", "task_id": task_id, "deleted_keys": deleted_keys}
    finally:
        db.close()
