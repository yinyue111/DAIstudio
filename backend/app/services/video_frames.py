"""Sample keyframes from a reference video for understanding (省带宽).

Instead of feeding a whole video to the vision model, we download it once
(SSRF-guarded, size-capped) and pull scene-change frames first, then fill any
gap with evenly-spaced frames via the ffmpeg CLI. Frames come back as JPEG bytes
so the caller can inline them as base64 data-URIs (no public URL needed, so an
external gateway can still "see" them).

Sampling is best-effort: if ffmpeg is missing or the video cannot be decoded,
we return no frames. The orchestration layer then requests explicit cover-mode
confirmation; this module never silently changes the analysis contract.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from math import gcd

from PIL import Image

from ..config import settings
from .safe_logging import redact_url_for_log
from .ssrf import MAX_REDIRECTS, assert_safe_url, pinned_client
from .video_analysis import frame_count_for_duration

log = logging.getLogger("video_frames")

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MAX_VIDEO_BYTES = 80 * 1024 * 1024  # don't pull more than ~80MB to sample frames
DOWNLOAD_TIMEOUT = 30.0
SCENE_THRESHOLD = 0.28
MIN_FRAME_GAP_SECONDS = 0.7
FRAME_BACKOFF_SECONDS = (0.0, 0.1, 0.25, 0.5)
_SAMPLE_SEMAPHORE = threading.BoundedSemaphore(
    max(1, int(settings.reverse_video_parallelism or 1))
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class VideoMetadata:
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    fps: float | None = None
    has_audio: bool = False

    @property
    def ratio(self) -> str | None:
        if not self.width or not self.height:
            return None
        divisor = gcd(int(self.width), int(self.height))
        return f"{int(self.width) // divisor}:{int(self.height) // divisor}"

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "ratio": self.ratio,
            "duration_seconds": self.duration_seconds,
            "fps": self.fps,
            "has_audio": self.has_audio,
            "audio_analyzed": False,
        }


@dataclass(frozen=True)
class SampledVideoFrame:
    jpeg: bytes
    timestamp_seconds: float
    # Endpoint frames outrank scene-change frames, which outrank uniform fill
    # frames when the aggregate model payload must be reduced.
    priority: int = 1


@dataclass(frozen=True)
class VideoSample:
    frames: tuple[SampledVideoFrame, ...]
    source: VideoMetadata

    def analysis(self) -> dict:
        return {
            "source": self.source.to_dict(),
            "sampled_frames": [
                {"index": index, "timestamp_seconds": frame.timestamp_seconds}
                for index, frame in enumerate(self.frames, start=1)
            ],
        }


def available() -> bool:
    return FFMPEG is not None


def _looks_like_video(data: bytes) -> bool:
    head = data[:512]
    return (
        b"ftyp" in head[:64]  # mp4/mov
        or head.startswith(b"\x1aE\xdf\xa3")  # webm/mkv
        or head.startswith(b"OggS")
        or head.startswith(b"RIFF") and b"AVI " in head[:16]
    )


def _download_capped(url: str, referer: str | None = None) -> bytes | None:
    """SSRF-checked, size-capped, redirect-revalidated streaming download."""
    headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    assert_safe_url(url)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() > deadline:
            log.warning("video sample download timed out")
            return None
        with pinned_client(
            url,
            follow_redirects=False,
            timeout=DOWNLOAD_TIMEOUT,
            headers=headers,
        ) as c:
            with c.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    from urllib.parse import urljoin
                    url = urljoin(url, r.headers["location"])
                    assert_safe_url(url)  # red line: re-check every hop
                    continue
                content_type = (r.headers.get("content-type") or "").lower()
                if content_type and not (
                    content_type.startswith("video/")
                    or content_type in {"application/octet-stream", "binary/octet-stream"}
                ):
                    log.warning("video sample content-type rejected: %s", content_type[:80])
                    return None
                encoding = (r.headers.get("content-encoding") or "").strip().lower()
                if encoding and encoding != "identity":
                    log.warning("video sample compressed response rejected: %s", encoding[:40])
                    return None
                r.raise_for_status()
                buf = bytearray()
                for chunk in r.iter_bytes():
                    if time.monotonic() > deadline:
                        log.warning("video sample download timed out")
                        return None
                    buf += chunk
                    if len(buf) > MAX_VIDEO_BYTES:
                        log.warning("video exceeds %s bytes, aborting sample", MAX_VIDEO_BYTES)
                        return None
                return bytes(buf)
    return None


def _duration_seconds(path: str) -> float | None:
    if not FFPROBE:
        return None
    try:
        out = subprocess.run(
            [
                FFPROBE,
                "-v",
                "quiet",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True, text=True, timeout=20,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def probe_media(path: str) -> dict:
    """Best-effort local video metadata for display/layout."""
    if not FFPROBE:
        return {}
    try:
        out = subprocess.run(
            [
                FFPROBE,
                "-v",
                "quiet",
                "-protocol_whitelist",
                "file,pipe",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                path,
            ],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return {}

    width = height = None
    fps = None
    rotation = 0
    has_audio = False
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            raw_fps = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            try:
                parsed_fps = float(Fraction(str(raw_fps)))
                fps = parsed_fps if parsed_fps > 0 else None
            except (ValueError, ZeroDivisionError):
                fps = None
            try:
                rotation = int((stream.get("tags") or {}).get("rotate") or 0)
            except (TypeError, ValueError):
                rotation = 0
            for side_data in stream.get("side_data_list") or []:
                try:
                    rotation = int(side_data.get("rotation") or rotation)
                except (TypeError, ValueError):
                    pass
            break
    has_audio = any(
        stream.get("codec_type") == "audio"
        for stream in data.get("streams") or []
    )
    if width and height and abs(rotation) % 180 == 90:
        width, height = height, width
    duration = None
    try:
        duration = float((data.get("format") or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None
    metadata = VideoMetadata(
        width=int(width) if width else None,
        height=int(height) if height else None,
        duration_seconds=round(duration, 3) if duration else None,
        fps=round(fps, 3) if fps else None,
        has_audio=has_audio,
    )
    return {
        **metadata.to_dict(),
        # Compatibility for existing upload/generation callers.
        "duration": metadata.duration_seconds,
    }


def acquire_video_slot() -> bool:
    return _SAMPLE_SEMAPHORE.acquire(
        timeout=max(0.0, float(settings.reverse_video_acquire_timeout_seconds or 0))
    )


def release_video_slot() -> None:
    _SAMPLE_SEMAPHORE.release()


def _grab_frame(src: str, ts: float, dst: str) -> bool:
    """Extract a single frame at timestamp ``ts`` seconds."""
    try:
        subprocess.run(
            [FFMPEG, "-y", "-protocol_whitelist", "file,pipe", "-ss", f"{ts:.3f}",
             "-i", src, "-frames:v", "1", "-q:v", "3", "-vf",
             "scale=w='min(1024,iw)':h='min(1024,ih)':"
             "force_original_aspect_ratio=decrease:force_divisible_by=2",
             dst],
            capture_output=True, timeout=30,
        )
    except subprocess.SubprocessError:
        return False
    return os.path.exists(dst) and os.path.getsize(dst) > 0


def _grab_frame_with_backoff(src: str, ts: float, dst: str) -> float | None:
    """Return the actual timestamp used, backing off from an undecodable EOF."""
    for offset in FRAME_BACKOFF_SECONDS:
        candidate = max(0.0, float(ts) - offset)
        if os.path.exists(dst):
            os.remove(dst)
        if _grab_frame(src, candidate, dst):
            return candidate
    return None


def _scene_change_timestamps(src: str, limit: int) -> list[float]:
    """Best-effort ffmpeg scene detection timestamps.

    We prefer cut points because they carry more prompt signal than blind
    interval sampling. Any failure returns [] and callers fall back to uniform
    timestamps.
    """
    if limit < 1:
        return []
    try:
        out = subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                src,
                "-vf",
                f"select='gt(scene,{SCENE_THRESHOLD})',metadata=print:file=-",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    stamps: list[float] = []
    for line in (out.stdout or "").splitlines():
        if "pts_time:" not in line:
            continue
        try:
            ts = float(line.split("pts_time:", 1)[1].split()[0])
        except (IndexError, ValueError):
            continue
        if ts < 0:
            continue
        if stamps and abs(ts - stamps[-1]) < MIN_FRAME_GAP_SECONDS:
            continue
        stamps.append(ts)
    return _spread_timestamps(stamps, limit)


def _uniform_timestamps(dur: float | None, n: int) -> list[float]:
    if n < 1:
        return []
    if dur and dur > 0:
        max_ts = max(0.0, float(dur) - 0.05)
        if n == 1:
            return [0.0]
        return [round(max_ts * i / (n - 1), 3) for i in range(n)]
    return [0.0, 0.5, 1.0, 1.5][:n]


def _spread_timestamps(stamps: list[float], count: int) -> list[float]:
    if count < 1 or not stamps:
        return []
    if len(stamps) <= count:
        return list(stamps)
    picked: list[float] = []
    total = len(stamps)
    for index in range(count):
        position = round((index + 1) * total / (count + 1)) - 1
        picked.append(stamps[max(0, min(total - 1, position))])
    return picked


def _merge_timestamps(primary: list[float], fallback: list[float], n: int, dur: float | None) -> list[float]:
    if n < 1:
        return []
    max_ts = max(0.0, float(dur or 0) - 0.05) if dur else None
    if max_ts is not None:
        anchors = _uniform_timestamps(dur, n)
        if n <= 2:
            return anchors

        candidates = sorted({
            max(0.0, min(float(ts), max_ts))
            for ts in primary
            if 0.0 < float(ts) < max_ts
        })
        merged = [anchors[0]]
        for index in range(1, len(anchors) - 1):
            lower = anchors[index - 1]
            upper = anchors[index]
            in_bucket = [
                ts for ts in candidates
                if lower + MIN_FRAME_GAP_SECONDS <= ts <= upper
                and all(abs(ts - seen) >= MIN_FRAME_GAP_SECONDS for seen in merged)
            ]
            merged.append(max(in_bucket) if in_bucket else anchors[index])
        merged.append(anchors[-1])
        return sorted(merged)

    endpoints = [0.0]
    interior_slots = max(0, n - len(endpoints))
    candidates = [
        max(0.0, float(ts))
        for ts in primary
    ]
    candidates = [
        ts for ts in candidates
        if all(abs(ts - endpoint) >= MIN_FRAME_GAP_SECONDS for endpoint in endpoints)
    ]
    merged = list(endpoints)
    for ts in _spread_timestamps(candidates, interior_slots):
        if any(abs(ts - seen) < MIN_FRAME_GAP_SECONDS for seen in merged):
            continue
        merged.append(ts)
    for ts in fallback:
        if len(merged) >= n:
            break
        clean = max(0.0, float(ts))
        if max_ts is not None:
            clean = min(clean, max_ts)
        if any(abs(clean - seen) < MIN_FRAME_GAP_SECONDS for seen in merged):
            continue
        merged.append(clean)
    return sorted(merged)


def _target_frame_count(n: int, dur: float | None, preset: str | None = None) -> int:
    if preset:
        target = frame_count_for_duration(dur, preset)
        if n > 0:
            target = min(target, n)
        return max(1, target)
    return max(1, frame_count_for_duration(dur, None) if n < 1 else n)


def _reencode_jpeg(jpeg: bytes, *, quality: int, max_edge: int) -> bytes:
    try:
        with Image.open(BytesIO(jpeg)) as image:
            image.load()
            if image.mode != "RGB":
                image = image.convert("RGB")
            width, height = image.size
            scale = min(1.0, float(max_edge) / max(width, height))
            if scale < 1.0:
                resized = (
                    max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))),
                )
                image = image.resize(resized, Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            return output.getvalue()
    except (OSError, ValueError):
        return jpeg


def fit_frame_payload_budget(
    frames: list[SampledVideoFrame] | tuple[SampledVideoFrame, ...],
    max_bytes: int | None = None,
) -> tuple[SampledVideoFrame, ...]:
    """Compress then evidence-prune frames to a bounded raw-JPEG payload."""
    budget = int(
        settings.reverse_video_frame_payload_max_bytes
        if max_bytes is None else max_bytes
    )
    if budget <= 0 or not frames:
        return ()
    candidates = list(frames)
    initial_bytes = sum(len(frame.jpeg) for frame in candidates)
    if initial_bytes <= budget:
        return tuple(candidates)

    # Re-encode every frame from its original bytes at each level so quality
    # loss does not compound. Most real samples fit before evidence pruning.
    compressed = candidates
    for quality, max_edge in ((82, 1024), (70, 896), (58, 768), (46, 640)):
        compressed = [
            SampledVideoFrame(
                jpeg=_reencode_jpeg(frame.jpeg, quality=quality, max_edge=max_edge),
                timestamp_seconds=frame.timestamp_seconds,
                priority=frame.priority,
            )
            for frame in candidates
        ]
        if sum(len(frame.jpeg) for frame in compressed) <= budget:
            log.info(
                "compressed reverse-video frame payload from %s to %s bytes",
                initial_bytes,
                sum(len(frame.jpeg) for frame in compressed),
            )
            return tuple(compressed)

    ranked = sorted(
        enumerate(compressed),
        key=lambda row: (-row[1].priority, row[0]),
    )
    selected_indices: set[int] = set()
    used = 0
    for index, frame in ranked:
        size = len(frame.jpeg)
        if used + size > budget:
            continue
        selected_indices.add(index)
        used += size
    selected = tuple(
        frame for index, frame in enumerate(compressed)
        if index in selected_indices
    )
    log.warning(
        "pruned reverse-video frames for payload budget: %s -> %s frame(s), %s -> %s bytes",
        len(candidates),
        len(selected),
        initial_bytes,
        used,
    )
    return selected


def _sample_video_from_file(
    src: str,
    n: int,
    *,
    duration: float | None = None,
    preset: str | None = None,
) -> VideoSample:
    frames: list[SampledVideoFrame] = []
    probed = probe_media(src)
    dur = duration if duration is not None else (
        probed.get("duration_seconds") or probed.get("duration") or _duration_seconds(src)
    )
    source = VideoMetadata(
        width=probed.get("width"),
        height=probed.get("height"),
        duration_seconds=round(float(dur), 3) if dur else None,
        fps=probed.get("fps"),
        has_audio=bool(probed.get("has_audio")),
    )
    target = _target_frame_count(n, dur, preset)
    scene_stamps = _scene_change_timestamps(src, max(0, target - 1))
    stamps = _merge_timestamps(scene_stamps, _uniform_timestamps(dur, target), target, dur)
    if not stamps:
        return VideoSample(frames=(), source=source)

    with tempfile.TemporaryDirectory() as td:
        for i, ts in enumerate(stamps):
            dst = os.path.join(td, f"f_{i:02d}.jpg")
            actual_ts = _grab_frame_with_backoff(src, max(0.0, ts), dst)
            if actual_ts is not None:
                with open(dst, "rb") as f:
                    frames.append(SampledVideoFrame(
                        jpeg=f.read(),
                        timestamp_seconds=round(float(actual_ts), 3),
                        priority=(
                            3 if i in {0, len(stamps) - 1}
                            else 2 if any(abs(float(ts) - float(scene_ts)) < 0.001 for scene_ts in scene_stamps)
                            else 1
                        ),
                    ))
    return VideoSample(frames=fit_frame_payload_budget(frames), source=source)


def _sample_keyframes_from_file(
    src: str,
    n: int,
    *,
    duration: float | None = None,
    preset: str | None = None,
) -> list[bytes]:
    sample = _sample_video_from_file(src, n, duration=duration, preset=preset)
    return [frame.jpeg for frame in sample.frames]


def sample_video_from_path(
    video_path: str,
    n: int = 4,
    *,
    preset: str | None = None,
) -> VideoSample | None:
    """Return timestamped frames and source facts from an owner-checked video."""
    if not FFMPEG or n < 1:
        return None
    if not acquire_video_slot():
        log.warning("video keyframe sampler is busy, skipping reverse-video frames")
        return None
    try:
        sample = _sample_video_from_file(video_path, n, preset=preset)
        log.info("sampled %s timestamped frame(s) from local video", len(sample.frames))
        return sample
    except Exception as e:  # noqa: BLE001
        log.warning("local keyframe sampling failed: %s", e)
        return None
    finally:
        release_video_slot()


def sample_keyframes_from_path(video_path: str, n: int = 4, *, preset: str | None = None) -> list[bytes]:
    """Return up to ``n`` JPEG frames from a local, owner-checked video path."""
    sample = sample_video_from_path(video_path, n, preset=preset)
    return [frame.jpeg for frame in sample.frames] if sample else []


def extract_poster(video_path: str) -> bytes | None:
    """First-frame JPEG of a LOCAL video, used as a poster for a locked asset
    (e.g. a pure text-to-video final with no reference image). None if ffmpeg is
    unavailable or extraction fails."""
    if not FFMPEG:
        return None
    with tempfile.TemporaryDirectory() as td:
        dst = os.path.join(td, "poster.jpg")
        if _grab_frame(video_path, 0.0, dst):
            with open(dst, "rb") as f:
                return f.read()
    return None


def sample_keyframes(
    video_url: str,
    n: int = 4,
    referer: str | None = None,
    *,
    preset: str | None = None,
) -> list[bytes]:
    """Return up to ``n`` JPEG frames (first + evenly spaced). [] on any failure."""
    sample = sample_video(video_url, n, referer=referer, preset=preset)
    return [frame.jpeg for frame in sample.frames] if sample else []


def sample_video(
    video_url: str,
    n: int = 4,
    referer: str | None = None,
    *,
    preset: str | None = None,
) -> VideoSample | None:
    """Return timestamped frames and source facts from a remote video."""
    if not FFMPEG or n < 1:
        return None
    if not acquire_video_slot():
        log.warning("video keyframe sampler is busy, skipping reverse-video frames")
        return None
    try:
        try:
            data = _download_capped(video_url, referer=referer)
        except Exception as e:  # noqa: BLE001 — SSRF/network/etc. -> graceful fallback
            log.warning("keyframe download failed for %s: %s", redact_url_for_log(video_url), e)
            return None
        if not data:
            return None
        if not _looks_like_video(data):
            log.warning("video sample rejected: unsupported file signature")
            return None

        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "input")
            with open(src, "wb") as f:
                f.write(data)
            sample = _sample_video_from_file(src, n, preset=preset)
        log.info("sampled %s timestamped frame(s) from video", len(sample.frames))
        return sample
    finally:
        release_video_slot()
