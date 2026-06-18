"""Sample keyframes from a reference video for understanding (省带宽).

Instead of feeding a whole video to the vision model, we download it once
(SSRF-guarded, size-capped) and pull the first frame plus a few evenly-spaced
frames via the ffmpeg CLI. Frames come back as JPEG bytes so the caller can
inline them as base64 data-URIs (no public URL needed, so an external gateway
can still "see" them).

Everything is best-effort: if ffmpeg is missing or the video can't be decoded,
we return an empty list and the caller falls back to the cover image.
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

import httpx

from ..config import settings
from .ssrf import MAX_REDIRECTS, assert_safe_url, pinned_safe_resolution

log = logging.getLogger("video_frames")

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MAX_VIDEO_BYTES = 80 * 1024 * 1024  # don't pull more than ~80MB to sample frames
DOWNLOAD_TIMEOUT = 30.0
_SAMPLE_SEMAPHORE = threading.BoundedSemaphore(
    max(1, int(settings.reverse_video_parallelism or 1))
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def available() -> bool:
    return FFMPEG is not None


def _download_capped(url: str, referer: str | None = None) -> bytes | None:
    """SSRF-checked, size-capped, redirect-revalidated streaming download."""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    assert_safe_url(url)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    with httpx.Client(follow_redirects=False, timeout=DOWNLOAD_TIMEOUT,
                      headers=headers) as c:
        for _ in range(MAX_REDIRECTS + 1):
            if time.monotonic() > deadline:
                log.warning("video sample download timed out")
                return None
            with pinned_safe_resolution(url), c.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    from urllib.parse import urljoin
                    url = urljoin(url, r.headers["location"])
                    assert_safe_url(url)  # red line: re-check every hop
                    continue
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
            [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
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
                FFPROBE, "-v", "quiet", "-print_format", "json",
                "-show_streams", "-show_format", path,
            ],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return {}

    width = height = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            break
    duration = None
    try:
        duration = float((data.get("format") or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None
    return {"width": width, "height": height, "duration": duration}


def _grab_frame(src: str, ts: float, dst: str) -> bool:
    """Extract a single frame at timestamp ``ts`` seconds."""
    try:
        subprocess.run(
            [FFMPEG, "-y", "-protocol_whitelist", "file,pipe", "-ss", f"{ts:.3f}",
             "-i", src, "-frames:v", "1",
             "-q:v", "3", "-vf", "scale='min(1024,iw)':-1", dst],
            capture_output=True, timeout=30,
        )
    except subprocess.SubprocessError:
        return False
    return os.path.exists(dst) and os.path.getsize(dst) > 0


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


def sample_keyframes(video_url: str, n: int = 4, referer: str | None = None) -> list[bytes]:
    """Return up to ``n`` JPEG frames (first + evenly spaced). [] on any failure."""
    if not FFMPEG or n < 1:
        return []
    if not _SAMPLE_SEMAPHORE.acquire(
        timeout=max(0.0, float(settings.reverse_video_acquire_timeout_seconds or 0))
    ):
        log.warning("video keyframe sampler is busy, skipping reverse-video frames")
        return []
    try:
        try:
            data = _download_capped(video_url, referer=referer)
        except Exception as e:  # noqa: BLE001 — SSRF/network/etc. -> graceful fallback
            log.warning("keyframe download failed: %s", e)
            return []
        if not data:
            return []

        frames: list[bytes] = []
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "input")
            with open(src, "wb") as f:
                f.write(data)
            dur = _duration_seconds(src)
            # choose timestamps: first frame, then evenly spaced through the clip.
            if dur and dur > 0:
                stamps = [min(dur - 0.05, dur * i / n) for i in range(n)]
            else:
                stamps = [0.0, 0.5, 1.0, 1.5][:n]  # unknown duration: early frames
            for i, ts in enumerate(stamps):
                dst = os.path.join(td, f"f_{i:02d}.jpg")
                if _grab_frame(src, max(0.0, ts), dst):
                    with open(dst, "rb") as f:
                        frames.append(f.read())
        log.info("sampled %s keyframe(s) from video", len(frames))
        return frames
    finally:
        _SAMPLE_SEMAPHORE.release()
