"""Best-effort timestamped audio evidence for video reverse analysis."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import settings
from . import gateway, video_frames
from .safe_logging import redact_url_for_log

log = logging.getLogger("video_audio")

FFMPEG = shutil.which("ffmpeg")
MAX_TRANSCRIPT_CHARS = 24_000
MAX_SEGMENTS = 1_000
AUDIO_EVIDENCE_CONTRACT_VERSION = 1
AUDIO_HEALTH_CONTRACT_VERSION = "audio-evidence.v1"
SIGNAL_ANALYZER_VERSION = "audio-signal.v1"
SIGNAL_SAMPLE_RATE = 16_000
SIGNAL_MAX_SECONDS = 300
_ASR_LAST_SUCCESS: dict[str, Any] = {}
_ASR_HEALTH_CACHE: dict[str, tuple[str, float, dict[str, Any]]] = {}
_ASR_STATE_LOCK = threading.RLock()
_ASR_HEALTH_PROBE_LOCK = threading.Lock()
_SPEAKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)

_UNSUPPORTED_FEATURE_REASONS = {
    "music": "当前音频链路未配置音乐结构分析器",
    "beat": "当前音频链路未配置 BPM 与节拍分析器",
    "sfx": "当前音频链路未配置音效事件分析器",
}


def _metadata_label(
    value: Any,
    *,
    path: str,
    required: bool = True,
    max_length: int = 128,
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{path} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    label = value.strip()
    if required and not label:
        raise ValueError(f"{path} is required")
    if label and (len(label) > max_length or not label.isprintable()):
        raise ValueError(f"{path} must be at most {max_length} printable characters")
    return label or None


def _finite_timestamp(value: Any, *, path: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite JSON number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{path} must be finite and at least {minimum}")
    return number


def _speaker_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    speaker = value.strip()
    if not _SPEAKER_ID_RE.fullmatch(speaker):
        raise ValueError(f"{path} must be a safe ASCII speaker id")
    return speaker


def _unsupported_feature(reason: str) -> dict[str, Any]:
    return {
        "status": "unsupported",
        "analyzer": None,
        "analyzer_version": None,
        "evidence_count": 0,
        "degraded_reason": reason,
    }


def _asr_feature(result: dict[str, Any]) -> dict[str, Any]:
    segments = result.get("segments")
    evidence_count = len(segments) if isinstance(segments, list) else 0
    return {
        "status": str(result.get("asr_status") or result.get("status") or "failed"),
        "analyzer": "timestamped_asr_gateway",
        "analyzer_version": "audio-evidence.v1",
        "provider_model": result.get("provider_model"),
        "evidence_count": evidence_count,
        "degraded_reason": result.get("degraded_reason"),
    }


def _speaker_feature(result: dict[str, Any]) -> dict[str, Any]:
    segments = result.get("segments")
    rows = [
        row for row in (segments if isinstance(segments, list) else [])
        if isinstance(row, dict) and str(row.get("speaker_id") or "").strip()
    ]
    if not rows:
        claimed = str(result.get("speaker_claim_status") or "")
        if claimed == "available":
            return {
                **_unsupported_feature("转写服务声称支持说话人标签，但未返回可验证标签"),
                "status": "degraded",
                "analyzer": "transcription_provider_speaker_labels",
                "analyzer_version": "audio-evidence.v1",
                "provider_model": result.get("provider_model"),
            }
        if claimed == "degraded":
            return {
                **_unsupported_feature("转写服务报告说话人能力降级"),
                "status": "degraded",
                "analyzer": "transcription_provider_speaker_labels",
                "analyzer_version": "audio-evidence.v1",
                "provider_model": result.get("provider_model"),
            }
        return _unsupported_feature("转写服务未返回可验证的说话人标签")
    speakers = {
        (int(row.get("source_segment_index") or 1), str(row["speaker_id"]))
        for row in rows
    }
    return {
        "status": "analyzed",
        "analyzer": "transcription_provider_speaker_labels",
        "analyzer_version": "audio-evidence.v1",
        "provider_model": result.get("provider_model"),
        "evidence_count": len(rows),
        "speaker_count": len(speakers),
        "degraded_reason": None,
    }


def _audio_feature_contract(result: dict[str, Any]) -> dict[str, Any]:
    """Describe only analyzers that were actually executed by this service."""
    return {
        "asr": _asr_feature(result),
        "speaker": _speaker_feature(result),
        **{
            feature: _unsupported_feature(reason)
            for feature, reason in _UNSUPPORTED_FEATURE_REASONS.items()
        },
    }


def _result(status: str, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    supplied_features = extra.pop("features", None)
    result = {
        "contract_version": AUDIO_EVIDENCE_CONTRACT_VERSION,
        "status": status,
        "transcript": "",
        "segments": [],
        "language": None,
        "provider_model": None,
        "degraded_reason": reason,
        **extra,
    }
    result["features"] = _audio_feature_contract(result)
    if isinstance(supplied_features, dict):
        result["features"].update(deepcopy(supplied_features))
    return result


def configured() -> bool:
    return bool(
        settings.audio_gateway_enabled
        and str(settings.audio_gateway_base_url or "").strip()
        and str(settings.audio_gateway_api_key or "").strip()
        and str(settings.audio_transcription_model or "").strip()
    )


def analysis_enabled() -> bool:
    return bool(settings.audio_signal_analysis_enabled or settings.audio_gateway_enabled)


def _numpy_available() -> bool:
    return importlib.util.find_spec("numpy") is not None


def _record_asr_success(result: dict[str, Any], *, source: str) -> None:
    features = result.get("features") if isinstance(result.get("features"), dict) else {}
    speaker = features.get("speaker") if isinstance(features.get("speaker"), dict) else {}
    provider_model = _metadata_label(
        result.get("provider_model"), path="transcription result.provider_model", required=False
    )
    speaker_status = str(speaker.get("status") or "unsupported")
    if speaker_status == "analyzed":
        speaker_status = "available"
    elif speaker_status not in {"degraded", "unsupported"}:
        speaker_status = "unsupported"
    record = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "last_success_source": source,
        "last_provider_model": provider_model,
        "last_speaker_status": speaker_status,
    }
    with _ASR_STATE_LOCK:
        _ASR_LAST_SUCCESS.clear()
        _ASR_LAST_SUCCESS.update(record)
        _ASR_HEALTH_CACHE.clear()


def _signal_analyzer_health() -> dict[str, Any]:
    enabled = bool(settings.audio_signal_analysis_enabled)
    ffmpeg_available = bool(FFMPEG)
    numpy_available = _numpy_available()
    if not enabled:
        status = "unsupported"
        reason = "本地音频信号分析未启用"
    elif not ffmpeg_available:
        status = "degraded"
        reason = "服务器未安装 FFmpeg，无法解码音频信号"
    elif not numpy_available:
        status = "degraded"
        reason = "服务器未安装 NumPy，无法分析音频信号"
    else:
        status = "available"
        reason = None
    return {
        "status": status,
        "analyzer": "pcm_signal_analysis",
        "analyzer_version": SIGNAL_ANALYZER_VERSION,
        "enabled": enabled,
        "ffmpeg_available": ffmpeg_available,
        "numpy_available": numpy_available,
        "degraded_reason": reason,
    }


def _asr_provider_health() -> dict[str, Any]:
    analyzer = "timestamped_asr_gateway"
    enabled = bool(settings.audio_gateway_enabled)
    complete = configured()
    health_url = str(settings.audio_gateway_health_url or "").strip()
    with _ASR_STATE_LOCK:
        last_success = deepcopy(_ASR_LAST_SUCCESS)
    if not enabled:
        return {
            "status": "unsupported",
            "speaker_status": "unsupported",
            "analyzer": analyzer,
            "enabled": False,
            "configured": False,
            "verification_status": "disabled",
            "health_url_configured": False,
            "degraded_reason": "音频转写未启用",
        }
    if not complete:
        return {
            "status": "degraded",
            "speaker_status": "unsupported",
            "analyzer": analyzer,
            "enabled": True,
            "configured": False,
            "verification_status": "incomplete_configuration",
            "health_url_configured": bool(health_url),
            "degraded_reason": "音频转写网关配置不完整",
        }
    try:
        configured_model = _metadata_label(
            settings.audio_transcription_model,
            path="AUDIO_TRANSCRIPTION_MODEL",
        )
    except ValueError as exc:
        return {
            "status": "degraded",
            "speaker_status": "unsupported",
            "analyzer": analyzer,
            "enabled": True,
            "configured": False,
            "verification_status": "invalid_configuration",
            "health_url_configured": bool(health_url),
            "degraded_reason": str(exc)[:200],
        }
    if not health_url:
        if last_success:
            return {
                "status": "available",
                "speaker_status": last_success.get("last_speaker_status", "unsupported"),
                "analyzer": analyzer,
                "analyzer_version": "audio-evidence.v1",
                "provider_model": last_success.get("last_provider_model"),
                "enabled": True,
                "configured": True,
                "verification_status": "last_success",
                "health_url_configured": False,
                "degraded_reason": None,
                **last_success,
            }
        return {
            "status": "degraded",
            "speaker_status": "unsupported",
            "analyzer": analyzer,
            "provider_model": configured_model,
            "enabled": True,
            "configured": True,
            "verification_status": "configured_unverified",
            "health_url_configured": False,
            "degraded_reason": "音频转写已配置，但尚无健康探针或成功转写记录",
        }
    api_key = str(settings.audio_gateway_api_key or "").strip()
    cache_key = hashlib.sha256(json.dumps(
        {
            "base_url": str(settings.audio_gateway_base_url or "").strip(),
            "health_url": health_url,
            "api_key_sha256": hashlib.sha256(api_key.encode()).hexdigest(),
            "provider_model": configured_model,
            "timeout": settings.audio_gateway_health_timeout_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()

    def cached_result() -> dict[str, Any] | None:
        now = time.monotonic()
        with _ASR_STATE_LOCK:
            cached = _ASR_HEALTH_CACHE.get("asr")
            if cached and cached[0] == cache_key and cached[1] > now:
                return deepcopy(cached[2])
            if cached:
                _ASR_HEALTH_CACHE.pop("asr", None)
        return None

    cached = cached_result()
    if cached is not None:
        return cached

    with _ASR_HEALTH_PROBE_LOCK:
        cached = cached_result()
        if cached is not None:
            return cached
        with _ASR_STATE_LOCK:
            last_success = deepcopy(_ASR_LAST_SUCCESS)
        try:
            payload = gateway._request_json(
                "GET",
                health_url,
                headers={"Authorization": f"Bearer {api_key}"},
                payload=None,
                timeout=min(10, max(1, int(settings.audio_gateway_health_timeout_seconds))),
                retries=0,
                trusted_hosts=settings.trusted_analyzer_host_list,
            )
            if not isinstance(payload, dict):
                raise ValueError("health response must be an object")
            if payload.get("contract_version") != AUDIO_HEALTH_CONTRACT_VERSION:
                raise ValueError(
                    f"health response.contract_version must be {AUDIO_HEALTH_CONTRACT_VERSION}"
                )
            health_status = str(payload.get("status") or "").strip().lower()
            if payload.get("ok") is not True and health_status not in {
                "ok", "healthy", "available", "ready",
            }:
                raise ValueError("health response did not report ready")
            provider_analyzer = _metadata_label(
                payload.get("analyzer"), path="health response.analyzer"
            )
            analyzer_version = _metadata_label(
                payload.get("analyzer_version"), path="health response.analyzer_version"
            )
            provider_model = _metadata_label(
                payload.get("provider_model"), path="health response.provider_model"
            )
            if provider_model != configured_model:
                raise ValueError(
                    "health response.provider_model must match AUDIO_TRANSCRIPTION_MODEL"
                )
            capability_statuses = payload.get("capability_statuses")
            allowed = {"available", "degraded", "unsupported"}
            if (
                not isinstance(capability_statuses, dict)
                or set(capability_statuses) != {"asr", "speaker"}
                or any(status not in allowed for status in capability_statuses.values())
            ):
                raise ValueError(
                    "health response.capability_statuses must report asr and speaker"
                )
            asr_status = str(capability_statuses["asr"])
            speaker_status = str(capability_statuses["speaker"])
            if speaker_status == "available" and asr_status != "available":
                raise ValueError("speaker cannot be available when asr is not available")
            record: dict[str, Any] = {}
            if asr_status == "available":
                record = {
                    "last_success_at": datetime.now(timezone.utc).isoformat(),
                    "last_success_source": "health_probe",
                    "last_provider_model": provider_model,
                    "last_speaker_status": speaker_status,
                }
                with _ASR_STATE_LOCK:
                    _ASR_LAST_SUCCESS.clear()
                    _ASR_LAST_SUCCESS.update(deepcopy(record))
            result = {
                "status": asr_status,
                "speaker_status": speaker_status,
                "analyzer": provider_analyzer,
                "analyzer_version": analyzer_version,
                "provider_model": provider_model,
                "enabled": True,
                "configured": True,
                "verification_status": "health_probe",
                "health_url_configured": True,
                "degraded_reason": (
                    None if asr_status == "available"
                    else "转写健康探针未报告 ASR 可用"
                ),
                **(record or last_success),
            }
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "degraded",
                "speaker_status": last_success.get("last_speaker_status", "unsupported"),
                "analyzer": analyzer,
                "provider_model": configured_model,
                "enabled": True,
                "configured": True,
                "verification_status": "health_probe_failed",
                "health_url_configured": True,
                "degraded_reason": str(exc)[:200],
                **last_success,
            }
        try:
            ttl = min(60, max(1, int(settings.audio_gateway_health_cache_ttl_seconds)))
        except (TypeError, ValueError):
            ttl = 15
        with _ASR_STATE_LOCK:
            _ASR_HEALTH_CACHE["asr"] = (
                cache_key,
                time.monotonic() + ttl,
                deepcopy(result),
            )
        return result


def analyzer_health() -> dict[str, Any]:
    signal = _signal_analyzer_health()
    asr_provider = _asr_provider_health()
    features = {
        "asr": {
            **asr_provider,
            "capability": "asr",
        },
        "speaker": {
            **asr_provider,
            "status": asr_provider["speaker_status"],
            "capability": "speaker",
            "degraded_reason": (
                None
                if asr_provider["speaker_status"] == "available"
                else asr_provider.get("degraded_reason")
                if not asr_provider.get("configured")
                else "转写服务尚未证明可返回说话人标签"
            ),
        },
        "beat": {
            **signal,
            "capability": "beat",
            "analyzer": "pcm_onset_interval",
        },
        "music": {
            **signal,
            "capability": "music",
            "analyzer": "pcm_spectral_rhythm_likelihood",
            "semantic_classification": False,
        },
        "sfx": {
            **signal,
            "capability": "sfx",
            "analyzer": "pcm_transient_detector",
            "semantic_classification": False,
        },
    }
    statuses = [str(feature["status"]) for feature in features.values()]
    overall = (
        "available" if all(status == "available" for status in statuses)
        else "partial" if any(status == "available" for status in statuses)
        else "degraded" if any(status == "degraded" for status in statuses)
        else "unsupported"
    )
    return {
        "contract_version": "audio-evidence.v1",
        "status": overall,
        "analysis_enabled": analysis_enabled(),
        "signal": signal,
        "asr_provider": asr_provider,
        **features,
    }


def _transcription_unavailable() -> dict[str, Any]:
    reason = (
        "音频转写网关未配置完整，仅完成本地信号分析"
        if settings.audio_gateway_enabled
        else "音频转写未启用，仅完成本地信号分析"
    )
    return _result("degraded", reason, asr_status="unsupported")


def _transcription_url() -> str:
    base = str(settings.audio_gateway_base_url or "").strip().rstrip("/")
    if urlparse(base).path.rstrip("/").endswith("/v1"):
        return base + "/audio/transcriptions"
    return base + "/v1/audio/transcriptions"


def _extract_audio(
    video_path: str,
    output_path: str,
    *,
    start_seconds: float,
    end_seconds: float | None,
) -> tuple[bool, str | None]:
    if not FFMPEG:
        return False, "服务器未安装 FFmpeg，无法提取音轨"
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
    ]
    if start_seconds > 0:
        command += ["-ss", f"{start_seconds:.3f}"]
    command += ["-i", video_path]
    if end_seconds is not None:
        duration = max(0.0, float(end_seconds) - float(start_seconds))
        if duration <= 0:
            return False, "音频分析范围为空"
        command += ["-t", f"{duration:.3f}"]
    command += [
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "48k",
        output_path,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=max(30, int(settings.audio_gateway_timeout_seconds or 180)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"音轨提取失败：{str(exc)[:160]}"
    if completed.returncode != 0 or not os.path.exists(output_path):
        return False, "视频未包含可解码音轨"
    size = os.path.getsize(output_path)
    if size <= 0:
        return False, "视频音轨为空"
    if size > int(settings.audio_upload_max_bytes):
        return False, "提取后的音轨超过转写上传限制"
    return True, None


def _signal_evidence_id(kind: str, *values: Any) -> str:
    material = "|".join(str(value) for value in values)
    return f"audio-{kind}-" + hashlib.sha256(material.encode()).hexdigest()[:20]


def _decode_audio_signal(audio_path: str) -> tuple[Any | None, str | None, bool]:
    if not FFMPEG:
        return None, "服务器未安装 FFmpeg，无法解码音频信号", False
    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        return None, "服务器未安装 NumPy，无法分析音频信号", False
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        audio_path,
        "-t",
        str(SIGNAL_MAX_SECONDS),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SIGNAL_SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=max(30, int(settings.audio_gateway_timeout_seconds or 180)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"音频信号解码失败：{str(exc)[:160]}", False
    if completed.returncode != 0 or not completed.stdout:
        return None, "音频信号无法解码", False
    samples = np.frombuffer(completed.stdout, dtype="<f4").astype("float64", copy=False)
    truncated = len(samples) >= SIGNAL_SAMPLE_RATE * SIGNAL_MAX_SECONDS
    return samples, None, truncated


def _analyze_pcm_signal(
    samples: Any,
    *,
    sample_rate: int = SIGNAL_SAMPLE_RATE,
    offset_seconds: float = 0,
) -> dict[str, dict[str, Any]]:
    """Extract conservative beat, music-likelihood and transient evidence."""
    import numpy as np  # type: ignore[import-not-found]

    values = np.asarray(samples, dtype="float64")
    frame_size = 1024
    hop_size = 512
    if values.size < frame_size:
        reason = "音频信号过短，无法形成稳定特征"
        return {
            key: {
                **_unsupported_feature(reason),
                "status": "degraded",
                "analyzer": "pcm_signal_analysis",
                "analyzer_version": SIGNAL_ANALYZER_VERSION,
            }
            for key in ("music", "beat", "sfx")
        }
    squared = values * values
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))
    frame_energy = cumulative[frame_size:] - cumulative[:-frame_size]
    rms = np.sqrt(np.maximum(frame_energy[::hop_size] / frame_size, 0.0))
    if rms.size < 4 or float(np.percentile(rms, 95)) < 1e-5:
        reason = "音频信号过弱，无法可靠判断节拍、音乐或音效"
        return {
            key: {
                "status": "degraded",
                "analyzer": "pcm_signal_analysis",
                "analyzer_version": SIGNAL_ANALYZER_VERSION,
                "evidence_count": 0,
                "degraded_reason": reason,
            }
            for key in ("music", "beat", "sfx")
        }

    onset = np.maximum(0.0, np.diff(rms, prepend=rms[0]))
    median = float(np.median(onset))
    mad = float(np.median(np.abs(onset - median)))
    onset_threshold = max(median + 3.0 * mad, float(np.percentile(onset, 80)) * 0.75, 1e-8)
    candidates = [
        index for index in range(1, len(onset) - 1)
        if onset[index] >= onset_threshold
        and onset[index] >= onset[index - 1]
        and onset[index] >= onset[index + 1]
    ]
    minimum_gap = max(1, int(round(0.12 * sample_rate / hop_size)))
    peak_indices: list[int] = []
    for candidate in candidates:
        if not peak_indices or candidate - peak_indices[-1] >= minimum_gap:
            peak_indices.append(candidate)
        elif onset[candidate] > onset[peak_indices[-1]]:
            peak_indices[-1] = candidate
    tempo_threshold = max(
        onset_threshold,
        float(np.percentile(onset, 90)),
        float(onset.max()) * 0.15,
    )
    tempo_indices = [index for index in peak_indices if onset[index] >= tempo_threshold]
    tempo_times = np.asarray(tempo_indices, dtype="float64") * hop_size / sample_rate

    bpm = None
    beat_confidence = 0.0
    if tempo_times.size >= 4:
        intervals = np.diff(tempo_times)
        intervals = intervals[(intervals >= 0.25) & (intervals <= 1.2)]
        if intervals.size >= 3:
            interval = float(np.median(intervals))
            bpm = 60.0 / interval
            while bpm < 60:
                bpm *= 2
            while bpm > 180:
                bpm /= 2
            consistency = float(np.median(np.abs(intervals - interval))) / max(interval, 1e-6)
            beat_confidence = max(0.0, min(1.0, 1.0 - consistency * 4.0))
    beat_evidence = [
        {
            "evidence_id": _signal_evidence_id("beat", round(offset_seconds + float(value), 3)),
            "evidence_type": "beat",
            "start_seconds": round(offset_seconds + float(value), 3),
            "end_seconds": round(offset_seconds + float(value) + 0.08, 3),
            "strength": round(float(onset[index] / max(float(onset.max()), 1e-8)), 6),
        }
        for index, value in zip(tempo_indices[:500], tempo_times[:500], strict=False)
    ]
    beat = {
        "status": "analyzed" if bpm is not None else "degraded",
        "analyzer": "pcm_onset_interval",
        "analyzer_version": SIGNAL_ANALYZER_VERSION,
        "bpm": round(bpm, 3) if bpm is not None else None,
        "confidence": round(beat_confidence, 6),
        "evidence": beat_evidence,
        "evidence_count": len(beat_evidence),
        "degraded_reason": None if bpm is not None else "未检测到足够稳定的节拍间隔",
    }

    window_size = 2048
    spectral_flatness: list[float] = []
    maximum_windows = 240
    step = max(window_size, int(math.ceil(values.size / maximum_windows)))
    window = np.hanning(window_size)
    for start in range(0, max(1, values.size - window_size + 1), step):
        chunk = values[start:start + window_size]
        if chunk.size < window_size:
            break
        spectrum = np.abs(np.fft.rfft(chunk * window)) + 1e-12
        spectral_flatness.append(float(np.exp(np.mean(np.log(spectrum))) / np.mean(spectrum)))
    flatness = float(np.median(spectral_flatness)) if spectral_flatness else 1.0
    tonality = max(0.0, min(1.0, 1.0 - flatness))
    rhythmicity = beat_confidence if bpm is not None else 0.0
    music_score = max(0.0, min(1.0, 0.65 * tonality + 0.35 * rhythmicity))
    assessment = "likely" if music_score >= 0.65 else "unlikely" if music_score <= 0.3 else "uncertain"
    signal_end = offset_seconds + values.size / sample_rate
    music_evidence = [{
        "evidence_id": _signal_evidence_id("music", round(offset_seconds, 3), round(signal_end, 3)),
        "evidence_type": "music_likelihood",
        "start_seconds": round(offset_seconds, 3),
        "end_seconds": round(signal_end, 3),
        "assessment": assessment,
        "confidence": round(abs(music_score - 0.5) * 2.0, 6),
        "score": round(music_score, 6),
    }]
    music = {
        "status": "analyzed",
        "analyzer": "pcm_spectral_rhythm_likelihood",
        "analyzer_version": SIGNAL_ANALYZER_VERSION,
        "assessment": assessment,
        "score": round(music_score, 6),
        "evidence": music_evidence,
        "evidence_count": len(music_evidence),
        "degraded_reason": (
            "仅为信号特征推断，不包含曲目、乐器或音乐结构语义识别"
        ),
    }

    high_threshold = max(onset_threshold, float(np.percentile(onset, 92)))
    transient_indices = [index for index in peak_indices if onset[index] >= high_threshold]
    transient_evidence = [
        {
            "evidence_id": _signal_evidence_id(
                "transient", round(offset_seconds + index * hop_size / sample_rate, 3)
            ),
            "evidence_type": "transient",
            "start_seconds": round(offset_seconds + index * hop_size / sample_rate, 3),
            "end_seconds": round(offset_seconds + index * hop_size / sample_rate + 0.12, 3),
            "strength": round(float(onset[index] / max(float(onset.max()), 1e-8)), 6),
            "label": "unclassified_transient",
        }
        for index in transient_indices[:500]
    ]
    sfx = {
        "status": "analyzed",
        "analyzer": "pcm_transient_detector",
        "analyzer_version": SIGNAL_ANALYZER_VERSION,
        "evidence": transient_evidence,
        "evidence_count": len(transient_evidence),
        "degraded_reason": (
            "只检测未分类瞬态声学事件，不会臆测具体音效类型"
        ),
    }
    return {"music": music, "beat": beat, "sfx": sfx}


def _signal_features_from_path(
    audio_path: str,
    *,
    offset_seconds: float,
) -> dict[str, dict[str, Any]]:
    samples, reason, truncated = _decode_audio_signal(audio_path)
    if samples is None:
        return {
            key: _unsupported_feature(reason or "音频信号分析不可用")
            for key in ("music", "beat", "sfx")
        }
    features = _analyze_pcm_signal(samples, offset_seconds=offset_seconds)
    if truncated:
        for feature in features.values():
            existing = str(feature.get("degraded_reason") or "").strip()
            feature["degraded_reason"] = "；".join(filter(None, [existing, "信号分析最多覆盖前 300 秒"]))
    return features


def _signal_features_for_path(
    audio_path: str,
    *,
    offset_seconds: float,
) -> dict[str, dict[str, Any]]:
    if settings.audio_signal_analysis_enabled:
        return _signal_features_from_path(audio_path, offset_seconds=offset_seconds)
    return {
        key: _unsupported_feature("本地音频信号分析未启用")
        for key in ("music", "beat", "sfx")
    }


def _merge_signal_features(
    result: dict[str, Any],
    signal_features: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged = deepcopy(result)
    features = _audio_feature_contract(merged)
    if isinstance(merged.get("features"), dict):
        features.update(deepcopy(merged["features"]))
    features.update(deepcopy(signal_features))
    merged["features"] = features
    if merged.get("status") not in {"analyzed", "partial"} and any(
        row.get("status") == "analyzed" for row in signal_features.values()
    ):
        merged["status"] = "partial"
    merged["evidence"] = _collect_audio_evidence(merged)
    return merged


def _collect_audio_evidence(result: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for raw in result.get("segments") or []:
        if isinstance(raw, dict):
            evidence.append({**raw, "evidence_type": "speech"})
    features = result.get("features")
    if isinstance(features, dict):
        for feature_name in ("music", "beat", "sfx"):
            feature = features.get(feature_name)
            if not isinstance(feature, dict):
                continue
            for raw in feature.get("evidence") or []:
                if isinstance(raw, dict):
                    evidence.append({**raw, "evidence_type": raw.get("evidence_type") or feature_name})
    return evidence[:MAX_SEGMENTS]


def _aggregate_segment_features(
    result: dict[str, Any],
    segment_results: list[dict[str, Any]],
) -> dict[str, Any]:
    features = _audio_feature_contract(result)
    for feature_name in ("music", "beat", "sfx"):
        entries = [
            (item, item.get("features", {}).get(feature_name))
            for item in segment_results
            if isinstance(item.get("features"), dict)
            and isinstance(item.get("features", {}).get(feature_name), dict)
        ]
        if not entries:
            continue
        rows = [feature for _item, feature in entries]
        evidence: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for segment_result, feature in entries:
            segment_index = int(segment_result.get("source_segment_index") or 1)
            for raw in feature.get("evidence") or []:
                if isinstance(raw, dict):
                    evidence.append({**raw, "source_segment_index": segment_index})
            summaries.append({
                "source_segment_index": segment_index,
                "status": feature.get("status"),
                "bpm": feature.get("bpm"),
                "assessment": feature.get("assessment"),
                "confidence": feature.get("confidence"),
            })
        statuses = [str(row.get("status") or "unsupported") for row in rows]
        analyzed = sum(status == "analyzed" for status in statuses)
        reasons = [
            str(row.get("degraded_reason") or "").strip()
            for row in rows if str(row.get("degraded_reason") or "").strip()
        ]
        features[feature_name] = {
            "status": (
                "analyzed" if analyzed == len(rows)
                else "degraded" if analyzed
                else "unsupported" if all(status == "unsupported" for status in statuses)
                else "degraded"
            ),
            "analyzer": next((row.get("analyzer") for row in rows if row.get("analyzer")), None),
            "analyzer_version": next(
                (row.get("analyzer_version") for row in rows if row.get("analyzer_version")),
                None,
            ),
            "evidence": evidence[:MAX_SEGMENTS],
            "evidence_count": len(evidence),
            "segments": summaries,
            "degraded_reason": "；".join(dict.fromkeys(reasons))[:1000] or None,
        }
    result["features"] = features
    result["evidence"] = _collect_audio_evidence(result)
    return result


def _has_usable_audio_evidence(result: dict[str, Any]) -> bool:
    if result.get("segments"):
        return True
    features = result.get("features")
    if not isinstance(features, dict):
        return False
    return any(
        isinstance(feature, dict)
        and feature.get("status") == "analyzed"
        and int(feature.get("evidence_count") or 0) > 0
        for feature in features.values()
    )


def _normalize_transcription(
    payload: Any,
    *,
    offset_seconds: float,
    segment_duration: float | None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _result("failed", "音频转写网关返回格式无效")
    try:
        offset = _finite_timestamp(offset_seconds, path="offset_seconds")
        duration = (
            _finite_timestamp(segment_duration, path="segment_duration")
            if segment_duration is not None
            else None
        )
        if duration is not None and duration <= 0:
            raise ValueError("segment_duration must be positive")
        provider_model = _metadata_label(
            settings.audio_transcription_model,
            path="AUDIO_TRANSCRIPTION_MODEL",
        )
        language = _metadata_label(
            payload.get("language"),
            path="transcription response.language",
            required=False,
            max_length=64,
        )
    except ValueError as exc:
        return _result("failed", str(exc)[:200])
    raw_transcript = payload.get("text")
    if raw_transcript is None:
        transcript = ""
    elif not isinstance(raw_transcript, str) or any(
        not char.isprintable() and char not in "\r\n\t" for char in raw_transcript
    ):
        return _result("failed", "音频转写网关返回的 transcript 不是安全文本")
    else:
        transcript = raw_transcript.strip()[:MAX_TRANSCRIPT_CHARS]

    capability_statuses = payload.get("capability_statuses")
    speaker_claim_status: str | None = None
    asr_claim_status: str | None = None
    if capability_statuses is not None:
        allowed = {"available", "analyzed", "partial", "degraded", "unsupported"}
        if (
            not isinstance(capability_statuses, dict)
            or set(capability_statuses) != {"asr", "speaker"}
            or any(status not in allowed for status in capability_statuses.values())
        ):
            return _result(
                "failed",
                "音频转写网关 capability_statuses 必须完整报告 asr 和 speaker",
            )
        asr_claim_status = str(capability_statuses["asr"])
        speaker_claim_status = str(capability_statuses["speaker"])
        if asr_claim_status == "analyzed":
            asr_claim_status = "available"
        if asr_claim_status == "partial":
            asr_claim_status = "degraded"
        if speaker_claim_status == "analyzed":
            speaker_claim_status = "available"
        if speaker_claim_status == "partial":
            speaker_claim_status = "degraded"

    segments: list[dict[str, Any]] = []
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return _result("failed", "音频转写网关 segments 必须是数组")
    rejected: list[str] = []
    for raw in raw_segments[:MAX_SEGMENTS]:
        if not isinstance(raw, dict):
            rejected.append("分段不是对象")
            continue
        raw_text = raw.get("text")
        if (
            not isinstance(raw_text, str)
            or not raw_text.strip()
            or any(not char.isprintable() and char not in "\r\n\t" for char in raw_text)
        ):
            rejected.append("分段文本无效")
            continue
        text = raw_text.strip()
        try:
            relative_start = _finite_timestamp(
                raw.get("start"), path="transcription segment.start"
            )
            relative_end = _finite_timestamp(
                raw.get("end"), path="transcription segment.end"
            )
            if relative_end <= relative_start:
                raise ValueError("transcription segment.end must follow start")
            if duration is not None:
                if relative_start >= duration or relative_end > duration + 0.001:
                    raise ValueError("transcription segment is outside the analyzed source range")
                relative_end = min(relative_end, duration)
        except ValueError as exc:
            rejected.append(str(exc))
            continue
        segment = {
            "start_seconds": round(offset + relative_start, 3),
            "end_seconds": round(offset + relative_end, 3),
            "relative_start_seconds": round(relative_start, 3),
            "relative_end_seconds": round(relative_end, 3),
            "text": text[:2000],
        }
        raw_speaker = (
            raw.get("speaker_id") if "speaker_id" in raw else raw.get("speaker")
        )
        if raw_speaker is not None:
            try:
                normalized_speaker = _speaker_id(
                    raw_speaker, path="transcription segment.speaker_id"
                )
                if speaker_claim_status in {"degraded", "unsupported"}:
                    raise ValueError(
                        "provider returned speaker labels while declaring speaker unavailable"
                    )
                segment["speaker_id"] = normalized_speaker
            except ValueError as exc:
                rejected.append(str(exc))
        segments.append(segment)
    if not segments:
        return _result(
            "failed",
            (
                "转写结果缺少可验证的分段时间戳"
                + (f"：{rejected[0]}" if rejected else "")
            )[:200],
            transcript=transcript,
            language=language,
            provider_model=provider_model,
            speaker_claim_status=speaker_claim_status,
        )
    if asr_claim_status in {"degraded", "unsupported"}:
        return _result(
            "failed",
            "转写服务返回了分段证据，但 capability_statuses 未声明 ASR 可用",
            language=language,
            provider_model=provider_model,
            speaker_claim_status=speaker_claim_status,
        )
    if speaker_claim_status == "available" and not any(
        row.get("speaker_id") for row in segments
    ):
        rejected.append("转写服务声称说话人能力可用，但未返回有效标签")
    if not transcript:
        transcript = " ".join(item["text"] for item in segments)[:MAX_TRANSCRIPT_CHARS]
    status = "partial" if rejected else "analyzed"
    return _result(
        status,
        "；".join(dict.fromkeys(rejected))[:1000] or None,
        asr_status=status,
        transcript=transcript,
        segments=segments,
        language=language,
        provider_model=provider_model,
        speaker_claim_status=speaker_claim_status,
    )


def _transcribe(
    audio_path: str,
    *,
    offset_seconds: float,
    duration: float | None,
) -> dict[str, Any]:
    try:
        audio = Path(audio_path).read_bytes()
        payload = gateway._request_multipart_json(
            "POST",
            _transcription_url(),
            headers={"Authorization": f"Bearer {settings.audio_gateway_api_key}"},
            data={
                "model": settings.audio_transcription_model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
            },
            files=[("file", ("audio.mp3", audio, "audio/mpeg"))],
            timeout=int(settings.audio_gateway_timeout_seconds or 180),
            retries=1,
            trusted_hosts=settings.trusted_analyzer_host_list,
        )
    except Exception as exc:  # noqa: BLE001 - audio failure must not fail visual reverse
        log.warning("audio transcription failed: %s", str(exc)[:300])
        return _result("failed", f"音频转写失败：{str(exc)[:160]}")
    normalized = _normalize_transcription(
        payload,
        offset_seconds=offset_seconds,
        segment_duration=duration,
    )
    if normalized.get("status") in {"analyzed", "partial"} and normalized.get("segments"):
        _record_asr_success(normalized, source="transcription")
    return normalized


def analyze_video_audio_from_path(
    video_path: str,
    *,
    start_seconds: float = 0,
    end_seconds: float | None = None,
) -> dict[str, Any]:
    if not analysis_enabled():
        return _result("disabled", "音频分析能力未启用")
    metadata = video_frames.probe_media(video_path)
    if not metadata.get("has_audio"):
        return _result("no_audio", "源视频未检测到音轨")
    start = max(0.0, float(start_seconds or 0))
    end = float(end_seconds) if end_seconds is not None else None
    duration = max(0.0, end - start) if end is not None else None
    with tempfile.TemporaryDirectory() as temp_dir:
        audio_path = os.path.join(temp_dir, "audio.mp3")
        extracted, reason = _extract_audio(
            video_path,
            audio_path,
            start_seconds=start,
            end_seconds=end,
        )
        if not extracted:
            status = "no_audio" if reason and "音轨" in reason else "failed"
            return _result(status, reason)
        signal_features = _signal_features_for_path(audio_path, offset_seconds=start)
        transcription = (
            _transcribe(audio_path, offset_seconds=start, duration=duration)
            if configured()
            else _transcription_unavailable()
        )
        return _merge_signal_features(transcription, signal_features)


def analyze_video_audio_ranges_from_path(
    video_path: str,
    *,
    source_ranges: list[dict[str, float]],
) -> dict[str, Any]:
    """Analyze selected source segments independently and merge absolute evidence."""
    if not analysis_enabled():
        return _result("disabled", "音频分析能力未启用")
    metadata = video_frames.probe_media(video_path)
    if not metadata.get("has_audio"):
        return _result("no_audio", "源视频未检测到音轨")

    segment_results: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    transcripts: list[str] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        for segment_index, source_range in enumerate(source_ranges, start=1):
            start = max(0.0, float(source_range.get("start_seconds") or 0))
            end = float(source_range.get("end_seconds"))
            audio_path = os.path.join(temp_dir, f"audio-{segment_index}.mp3")
            extracted, reason = _extract_audio(
                video_path,
                audio_path,
                start_seconds=start,
                end_seconds=end,
            )
            if extracted:
                signal_features = _signal_features_for_path(
                    audio_path, offset_seconds=start,
                )
                transcription = (
                    _transcribe(
                        audio_path,
                        offset_seconds=start,
                        duration=end - start,
                    )
                    if configured()
                    else _transcription_unavailable()
                )
                result = _merge_signal_features(transcription, signal_features)
            else:
                status = "no_audio" if reason and "音轨" in reason else "failed"
                result = _result(status, reason)
            rows = []
            for row in result.get("segments") or []:
                normalized = {**row, "source_segment_index": segment_index}
                rows.append(normalized)
                merged_segments.append(normalized)
            transcript = str(result.get("transcript") or "").strip()
            if transcript:
                transcripts.append(transcript)
            segment_results.append({
                **result,
                "segments": rows,
                "source_segment_index": segment_index,
                "source_range": {
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                },
            })

    analyzed_count = sum(_has_usable_audio_evidence(item) for item in segment_results)
    fully_analyzed_count = sum(item.get("status") == "analyzed" for item in segment_results)
    partial_count = sum(item.get("status") == "partial" for item in segment_results)
    failed_count = len(segment_results) - analyzed_count
    if analyzed_count:
        reason_parts = []
        if partial_count:
            reason_parts.append(f"{partial_count} 个片段仅完成部分音频分析")
        if failed_count:
            reason_parts.append(f"{failed_count} 个片段未形成可验证的音频证据")
        degraded_reason = "；".join(reason_parts) or None
        analyzed_ranges = [
            item["source_range"]
            for item in segment_results
            if _has_usable_audio_evidence(item)
        ]
        failed_ranges = [
            item["source_range"]
            for item in segment_results
            if not _has_usable_audio_evidence(item)
        ]
        asr_statuses = [
            str((item.get("features") or {}).get("asr", {}).get("status") or "unsupported")
            for item in segment_results
        ]
        asr_analyzed = sum(status == "analyzed" for status in asr_statuses)
        asr_status = (
            "analyzed" if asr_analyzed == len(asr_statuses)
            else "partial" if asr_analyzed
            else "unsupported" if all(status == "unsupported" for status in asr_statuses)
            else "degraded"
        )
        complete = fully_analyzed_count == len(segment_results)
        merged = _result(
            "analyzed" if complete else "partial",
            degraded_reason,
            asr_status=asr_status,
            transcript=" ".join(transcripts)[:MAX_TRANSCRIPT_CHARS],
            segments=merged_segments[:MAX_SEGMENTS],
            language=next((item.get("language") for item in segment_results if item.get("language")), None),
            provider_model=str(settings.audio_transcription_model or "") or None,
            segment_results=segment_results,
            analyzed_segment_count=analyzed_count,
            selected_segment_count=len(segment_results),
            complete=complete,
            analyzed_ranges=analyzed_ranges,
            failed_ranges=failed_ranges,
        )
        return _aggregate_segment_features(merged, segment_results)
    first_status = next(
        (item.get("status") for item in segment_results if item.get("status")),
        "failed",
    )
    reasons = [
        str(item.get("degraded_reason") or "").strip()
        for item in segment_results
        if str(item.get("degraded_reason") or "").strip()
    ]
    merged = _result(
        first_status,
        "；".join(dict.fromkeys(reasons))[:1000] or "所选片段未形成音频证据",
        segment_results=segment_results,
        analyzed_segment_count=0,
        selected_segment_count=len(segment_results),
        complete=False,
        analyzed_ranges=[],
        failed_ranges=[
            item["source_range"]
            for item in segment_results
            if isinstance(item.get("source_range"), dict)
        ],
    )
    return _aggregate_segment_features(merged, segment_results)


def analyze_video_audio(
    video_url: str,
    *,
    start_seconds: float = 0,
    end_seconds: float | None = None,
) -> dict[str, Any]:
    if not analysis_enabled():
        return _result("disabled", "音频分析能力未启用")
    try:
        data = video_frames._download_capped(video_url)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "audio source download failed for %s: %s",
            redact_url_for_log(video_url),
            str(exc)[:200],
        )
        return _result("failed", "音频分析无法下载源视频")
    if not data:
        return _result("failed", "音频分析无法下载源视频")
    with tempfile.TemporaryDirectory() as temp_dir:
        video_path = os.path.join(temp_dir, "input")
        Path(video_path).write_bytes(data)
        return analyze_video_audio_from_path(
            video_path,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )


def analyze_video_audio_ranges(
    video_url: str,
    *,
    source_ranges: list[dict[str, float]],
) -> dict[str, Any]:
    if not analysis_enabled():
        return _result("disabled", "音频分析能力未启用")
    try:
        data = video_frames._download_capped(video_url)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "audio source download failed for %s: %s",
            redact_url_for_log(video_url),
            str(exc)[:200],
        )
        return _result("failed", "音频分析无法下载源视频")
    if not data:
        return _result("failed", "音频分析无法下载源视频")
    with tempfile.TemporaryDirectory() as temp_dir:
        video_path = os.path.join(temp_dir, "input")
        Path(video_path).write_bytes(data)
        return analyze_video_audio_ranges_from_path(
            video_path,
            source_ranges=source_ranges,
        )
