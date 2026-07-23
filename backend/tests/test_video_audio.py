from __future__ import annotations

import math
import shutil
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings, settings
from app.db import SessionLocal
from app.models import CreditTransaction, ReverseOperation, ReverseResultRevision, User
from app.routers import prompt
from app.runtime_config import validate_runtime_config
from app.services import (
    gateway,
    retention,
    reverse_operations,
    video_audio,
    video_evidence_analysis,
)
from tests.reverse_helpers import post_reverse


def _audio_result(status: str, *, start: float = 0, end: float = 0, text: str = "") -> dict:
    segments = []
    if status == "analyzed":
        segments = [
            {
                "start_seconds": start,
                "end_seconds": end,
                "relative_start_seconds": 0.0,
                "relative_end_seconds": max(0.0, end - start),
                "text": text,
            }
        ]
    return {
        "status": status,
        "transcript": text,
        "segments": segments,
        "language": "zh" if text else None,
        "provider_model": "whisper-1" if status == "analyzed" else None,
        "degraded_reason": None if status == "analyzed" else "音频分析不可用",
    }


def _configure_audio_gateway(
    monkeypatch,
    *,
    enabled: bool = True,
    base_url: str = "https://audio.example.com",
    api_key: str = "audio-key",
    model: str = "whisper-test",
    health_url: str = "",
    health_timeout: int = 3,
    health_ttl: int = 15,
) -> None:
    monkeypatch.setattr(settings, "audio_gateway_enabled", enabled)
    monkeypatch.setattr(settings, "audio_gateway_base_url", base_url)
    monkeypatch.setattr(settings, "audio_gateway_api_key", api_key)
    monkeypatch.setattr(settings, "audio_transcription_model", model)
    monkeypatch.setattr(settings, "audio_gateway_health_url", health_url)
    monkeypatch.setattr(settings, "audio_gateway_health_timeout_seconds", health_timeout)
    monkeypatch.setattr(settings, "audio_gateway_health_cache_ttl_seconds", health_ttl)
    video_audio._ASR_HEALTH_CACHE.clear()


def _audio_health_payload(**overrides) -> dict:
    payload = {
        "contract_version": "audio-evidence.v1",
        "ok": True,
        "analyzer": "fixture-asr",
        "analyzer_version": "1.2.3",
        "provider_model": "whisper-test",
        "capability_statuses": {"asr": "available", "speaker": "available"},
    }
    payload.update(overrides)
    return payload


def _video_gateway_result(*_args, **_kwargs):
    return {
        "structured": {"主体": "水感精华广告", "旁白": "供应商声称存在旁白"},
        "final_text": "水感精华广告，女声旁白",
        "shots": [
            {
                "start_seconds": 0,
                "end_seconds": 2,
                "visual": "产品居中",
                "audio_cue": "供应商声称存在女声旁白",
                "evidence_frame_indices": [1],
                "confidence": 0.9,
            }
        ],
        # This provider claim must never override server-side audio evidence.
        "video_analysis": {"source": {"audio_analyzed": True}},
    }


def _stub_video_reverse(monkeypatch, audio_result: dict):
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_operations, "assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_operations, "_remember_history", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        prompt,
        "_collect_refs",
        lambda *_args, **_kwargs: (
            ["data:image/jpeg;base64,YQ=="],
            {
                "analysis_mode": "keyframes",
                "source": {
                    "duration_seconds": 10.0,
                    "total_duration_seconds": 30.0,
                    "source_range": {"start_seconds": 10.0, "end_seconds": 20.0},
                    "has_audio": True,
                    "audio_analyzed": False,
                },
                "sampled_frames": [
                    {
                        "index": 1,
                        "timestamp_seconds": 10.0,
                        "relative_timestamp_seconds": 0.0,
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(
        reverse_operations,
        "_audio_analysis_for_request",
        lambda *_args, **_kwargs: dict(audio_result),
    )
    monkeypatch.setattr(gateway, "reverse_prompt", _video_gateway_result)


def test_timestamped_transcription_uses_absolute_source_time(monkeypatch):
    monkeypatch.setattr(settings, "audio_transcription_model", "whisper-test")
    normalized = video_audio._normalize_transcription(
        {
            "text": "第一句 第二句",
            "language": "zh",
            "segments": [
                {"start": 0.4, "end": 2.8, "text": "第一句"},
                {"start": 3.0, "end": 5.0, "text": "第二句"},
            ],
        },
        offset_seconds=12.0,
        segment_duration=5.0,
    )

    assert normalized["status"] == "analyzed"
    assert normalized["segments"][0] == {
        "start_seconds": 12.4,
        "end_seconds": 14.8,
        "relative_start_seconds": 0.4,
        "relative_end_seconds": 2.8,
        "text": "第一句",
    }
    assert normalized["segments"][1]["start_seconds"] == 15.0
    assert normalized["segments"][1]["end_seconds"] == 17.0
    assert normalized["contract_version"] == 1
    assert normalized["features"]["asr"] == {
        "status": "analyzed",
        "analyzer": "timestamped_asr_gateway",
        "analyzer_version": "audio-evidence.v1",
        "provider_model": "whisper-test",
        "evidence_count": 2,
        "degraded_reason": None,
    }
    for feature in ("speaker", "music", "beat", "sfx"):
        assert normalized["features"][feature]["status"] == "unsupported"
        assert normalized["features"][feature]["analyzer"] is None
        assert normalized["features"][feature]["analyzer_version"] is None
        assert normalized["features"][feature]["evidence_count"] == 0


def test_empty_or_untimestamped_transcription_is_not_analyzed():
    normalized = video_audio._normalize_transcription(
        {"text": "有文字但没有时间戳", "segments": []},
        offset_seconds=0,
        segment_duration=10,
    )

    assert normalized["status"] == "failed"
    assert normalized["segments"] == []
    assert reverse_operations._has_timestamped_audio_evidence(normalized) is False


def test_transcription_provider_speaker_labels_are_preserved(monkeypatch):
    monkeypatch.setattr(settings, "audio_transcription_model", "speaker-aware-asr")
    normalized = video_audio._normalize_transcription(
        {
            "text": "你好 欢迎",
            "segments": [
                {"start": 0, "end": 1, "text": "你好", "speaker": "speaker-1"},
                {"start": 1, "end": 2, "text": "欢迎", "speaker_id": "speaker-2"},
            ],
        },
        offset_seconds=5,
        segment_duration=3,
    )

    assert [row["speaker_id"] for row in normalized["segments"]] == [
        "speaker-1",
        "speaker-2",
    ]
    assert normalized["features"]["speaker"] == {
        "status": "analyzed",
        "analyzer": "transcription_provider_speaker_labels",
        "analyzer_version": "audio-evidence.v1",
        "provider_model": "speaker-aware-asr",
        "evidence_count": 2,
        "speaker_count": 2,
        "degraded_reason": None,
    }


@pytest.mark.parametrize(
    "start,end,duration",
    [
        (math.nan, 1.0, 2.0),
        (0.0, math.inf, 2.0),
        (1.0, 0.5, 2.0),
        (-0.1, 1.0, 2.0),
        (0.5, 2.5, 2.0),
    ],
)
def test_transcription_rejects_nonfinite_reversed_or_out_of_range_timestamps(
    monkeypatch,
    start,
    end,
    duration,
):
    monkeypatch.setattr(settings, "audio_transcription_model", "whisper-test")
    normalized = video_audio._normalize_transcription(
        {"text": "invalid", "segments": [{"start": start, "end": end, "text": "invalid"}]},
        offset_seconds=10.0,
        segment_duration=duration,
    )

    assert normalized["status"] == "failed"
    assert normalized["segments"] == []
    assert normalized["features"]["asr"]["evidence_count"] == 0


def test_transcription_drops_unsafe_speaker_id_without_losing_valid_asr(monkeypatch):
    monkeypatch.setattr(settings, "audio_transcription_model", "speaker-aware-asr")
    normalized = video_audio._normalize_transcription(
        {
            "text": "hello",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hello",
                    "speaker_id": "<script>alert(1)</script>",
                }
            ],
        },
        offset_seconds=5.0,
        segment_duration=2.0,
    )

    assert normalized["status"] == "partial"
    assert normalized["features"]["asr"]["evidence_count"] == 1
    assert normalized["features"]["speaker"]["status"] == "unsupported"
    assert "speaker_id" not in normalized["segments"][0]


def test_speaker_available_claim_without_valid_labels_is_not_success(monkeypatch):
    monkeypatch.setattr(settings, "audio_transcription_model", "speaker-aware-asr")
    normalized = video_audio._normalize_transcription(
        {
            "text": "hello",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
            "capability_statuses": {"asr": "available", "speaker": "available"},
        },
        offset_seconds=0.0,
        segment_duration=2.0,
    )

    assert normalized["status"] == "partial"
    assert normalized["features"]["asr"]["evidence_count"] == 1
    assert normalized["features"]["speaker"]["status"] == "degraded"
    assert normalized["features"]["speaker"]["evidence_count"] == 0


def test_pcm_signal_analysis_produces_timestamped_bpm_music_and_transients():
    import numpy as np

    sample_rate = 16_000
    duration = 10
    timeline = np.arange(sample_rate * duration) / sample_rate
    samples = 0.04 * np.sin(2 * np.pi * 220 * timeline)
    for timestamp in np.arange(0.5, duration, 0.5):
        index = int(timestamp * sample_rate)
        samples[index : index + 320] += np.hanning(320) * 0.9

    features = video_audio._analyze_pcm_signal(
        samples,
        sample_rate=sample_rate,
        offset_seconds=10,
    )

    assert features["beat"]["status"] == "analyzed"
    assert 110 <= features["beat"]["bpm"] <= 125
    assert features["beat"]["evidence"][0]["start_seconds"] >= 10
    assert features["music"]["status"] == "analyzed"
    assert features["music"]["assessment"] in {"likely", "unlikely", "uncertain"}
    assert features["sfx"]["status"] == "analyzed"
    assert features["sfx"]["evidence_count"] > 0
    assert all(row["label"] == "unclassified_transient" for row in features["sfx"]["evidence"])


@pytest.mark.skipif(not video_audio.FFMPEG, reason="ffmpeg not installed")
def test_real_mp4_audio_decode_preserves_absolute_signal_evidence(monkeypatch, tmp_path):
    import numpy as np

    sample_rate = 16_000
    duration = 8
    timeline = np.arange(sample_rate * duration) / sample_rate
    samples = 0.04 * np.sin(2 * np.pi * 220 * timeline)
    for timestamp in np.arange(0.5, duration, 0.5):
        index = int(timestamp * sample_rate)
        samples[index : index + 320] += np.hanning(320) * 0.9
    pcm = np.clip(samples, -1, 1)
    wav_path = tmp_path / "click-track.wav"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((pcm * 32767).astype("<i2").tobytes())

    video_path = tmp_path / "click-track.mp4"
    completed = subprocess.run(
        [
            str(video_audio.FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=160x90:r=25:d={duration}",
            "-i",
            str(wav_path),
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video_path),
        ],
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        pytest.skip(f"ffmpeg fixture encoder unavailable: {completed.stderr[:160]!r}")

    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", True)
    monkeypatch.setattr(settings, "audio_gateway_enabled", False)
    result = video_audio.analyze_video_audio_from_path(
        str(video_path),
        start_seconds=3,
        end_seconds=7,
    )

    assert result["status"] == "partial"
    assert result["features"]["asr"]["status"] == "unsupported"
    assert result["features"]["beat"]["status"] == "analyzed"
    assert 110 <= result["features"]["beat"]["bpm"] <= 125
    assert result["features"]["music"]["status"] == "analyzed"
    assert result["features"]["sfx"]["status"] == "analyzed"
    assert result["evidence"]
    assert all(row["start_seconds"] >= 3 for row in result["evidence"])
    assert shutil.which("ffprobe"), "fixture must be inspectable by the production probe"


def test_audio_evidence_attaches_only_to_overlapping_shots():
    shots = [
        {"shot_id": "one", "start_seconds": 0, "end_seconds": 2},
        {"shot_id": "two", "start_seconds": 2, "end_seconds": 4},
    ]
    audio = {
        "status": "analyzed",
        "evidence": [
            {"evidence_id": "speech-1", "start_seconds": 0.5, "end_seconds": 1.5},
            {"evidence_id": "beat-1", "start_seconds": 2.5, "end_seconds": 2.58},
        ],
    }

    attached = video_evidence_analysis.attach_audio_evidence_to_shots(shots, audio)

    assert attached[0]["audio_refs"] == ["speech-1"]
    assert attached[1]["audio_refs"] == ["beat-1"]
    assert attached[0]["analyzer_status"]["audio"] == "analyzed"


def test_disabled_and_no_audio_degrade_without_gateway_call(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    monkeypatch.setattr(settings, "audio_gateway_enabled", False)
    disabled = video_audio.analyze_video_audio_from_path(str(tmp_path / "video.mp4"))
    assert disabled["status"] == "disabled"
    assert disabled["features"]["asr"]["status"] == "disabled"
    assert disabled["features"]["speaker"]["status"] == "unsupported"

    monkeypatch.setattr(settings, "audio_gateway_enabled", True)
    monkeypatch.setattr(settings, "audio_gateway_base_url", "https://audio.example.com")
    monkeypatch.setattr(settings, "audio_gateway_api_key", "audio-key")
    monkeypatch.setattr(
        video_audio.video_frames, "probe_media", lambda *_args: {"has_audio": False}
    )
    no_audio = video_audio.analyze_video_audio_from_path(str(tmp_path / "video.mp4"))
    assert no_audio["status"] == "no_audio"
    assert no_audio["features"]["asr"]["status"] == "no_audio"


def test_multi_segment_local_signal_analysis_is_partial_without_asr(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", True)
    monkeypatch.setattr(settings, "audio_gateway_enabled", False)
    monkeypatch.setattr(
        video_audio.video_frames,
        "probe_media",
        lambda *_args: {"has_audio": True},
    )
    monkeypatch.setattr(
        video_audio,
        "_extract_audio",
        lambda *_args, **_kwargs: (True, None),
    )

    def fake_signal_features(_path, *, offset_seconds):
        end_seconds = offset_seconds + 5
        return {
            "music": {
                "status": "analyzed",
                "analyzer": "pcm_spectral_rhythm_likelihood",
                "analyzer_version": "audio-signal.v1",
                "assessment": "likely",
                "evidence_count": 1,
                "evidence": [
                    {
                        "evidence_id": f"music-{int(offset_seconds)}",
                        "evidence_type": "music_likelihood",
                        "start_seconds": offset_seconds,
                        "end_seconds": end_seconds,
                    }
                ],
                "degraded_reason": None,
            },
            "beat": video_audio._unsupported_feature("fixture has no beat evidence"),
            "sfx": video_audio._unsupported_feature("fixture has no transient evidence"),
        }

    monkeypatch.setattr(video_audio, "_signal_features_for_path", fake_signal_features)
    result = video_audio.analyze_video_audio_ranges_from_path(
        str(tmp_path / "source.mp4"),
        source_ranges=[
            {"start_seconds": 10, "end_seconds": 15},
            {"start_seconds": 30, "end_seconds": 35},
        ],
    )

    assert result["status"] == "partial"
    assert result["complete"] is False
    assert result["analyzed_segment_count"] == 2
    assert result["selected_segment_count"] == 2
    assert result["analyzed_ranges"] == [
        {"start_seconds": 10.0, "end_seconds": 15.0},
        {"start_seconds": 30.0, "end_seconds": 35.0},
    ]
    assert result["failed_ranges"] == []
    assert result["features"]["asr"]["status"] == "unsupported"
    assert result["features"]["music"]["status"] == "analyzed"
    assert result["features"]["music"]["evidence_count"] == 2
    assert [item["source_segment_index"] for item in result["features"]["music"]["evidence"]] == [
        1,
        2,
    ]


def test_multi_segment_audio_partial_preserves_absolute_evidence_and_failed_range(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    monkeypatch.setattr(video_audio.video_frames, "probe_media", lambda *_args: {"has_audio": True})
    monkeypatch.setattr(video_audio, "configured", lambda: True)
    monkeypatch.setattr(video_audio.settings, "audio_gateway_enabled", True)
    monkeypatch.setattr(video_audio, "_extract_audio", lambda *_args, **_kwargs: (True, None))

    def fake_transcribe(_path, *, offset_seconds, duration):
        if offset_seconds >= 30:
            return video_audio._result("failed", "第二个片段转写超时")
        return video_audio._result(
            "analyzed",
            transcript="欢迎回来",
            segments=[
                {
                    "start_seconds": offset_seconds + 0.4,
                    "end_seconds": offset_seconds + min(1.8, duration),
                    "relative_start_seconds": 0.4,
                    "relative_end_seconds": min(1.8, duration),
                    "text": "欢迎回来",
                }
            ],
            language="zh",
            provider_model="whisper-test",
        )

    monkeypatch.setattr(video_audio, "_transcribe", fake_transcribe)
    result = video_audio.analyze_video_audio_ranges_from_path(
        str(tmp_path / "source.mp4"),
        source_ranges=[
            {"start_seconds": 10, "end_seconds": 15},
            {"start_seconds": 30, "end_seconds": 35},
        ],
    )

    assert result["status"] == "partial"
    assert result["complete"] is False
    assert result["analyzed_segment_count"] == 1
    assert result["selected_segment_count"] == 2
    assert result["segments"][0]["start_seconds"] == 10.4
    assert result["segments"][0]["source_segment_index"] == 1
    assert result["analyzed_ranges"] == [{"start_seconds": 10.0, "end_seconds": 15.0}]
    assert result["failed_ranges"] == [{"start_seconds": 30.0, "end_seconds": 35.0}]
    assert result["features"]["asr"]["status"] == "partial"
    assert result["features"]["asr"]["evidence_count"] == 1
    assert result["features"]["music"]["status"] == "unsupported"


def test_runtime_audio_config_accepts_empty_and_rejects_partial(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "audio_gateway_enabled", False)
    monkeypatch.setattr(settings, "audio_gateway_base_url", "")
    monkeypatch.setattr(settings, "audio_gateway_api_key", "")
    monkeypatch.setattr(settings, "audio_gateway_health_url", "")
    validate_runtime_config()

    monkeypatch.setattr(settings, "audio_gateway_base_url", "https://audio.example.com")
    with pytest.raises(RuntimeError, match="AUDIO_GATEWAY"):
        validate_runtime_config()


def test_runtime_audio_config_rejects_enabled_or_dangling_health_without_gateway(
    monkeypatch,
):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "audio_gateway_enabled", True)
    monkeypatch.setattr(settings, "audio_gateway_base_url", "")
    monkeypatch.setattr(settings, "audio_gateway_api_key", "")
    monkeypatch.setattr(settings, "audio_gateway_health_url", "")
    with pytest.raises(RuntimeError, match="AUDIO_GATEWAY_ENABLED=true"):
        validate_runtime_config()

    monkeypatch.setattr(settings, "audio_gateway_enabled", False)
    monkeypatch.setattr(settings, "audio_gateway_health_url", "https://audio.example.test/health")
    with pytest.raises(RuntimeError, match="AUDIO_GATEWAY_HEALTH_URL"):
        validate_runtime_config()


@pytest.mark.parametrize(
    "field,url",
    [
        ("audio_gateway_base_url", "ftp://audio.example.test"),
        ("audio_gateway_base_url", "https://user:secret@audio.example.test"),
        ("audio_gateway_health_url", "https://audio.example.test/health#fragment"),
    ],
)
def test_runtime_audio_config_rejects_invalid_provider_urls(monkeypatch, field, url):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "audio_gateway_enabled", True)
    monkeypatch.setattr(settings, "audio_gateway_base_url", "https://audio.example.test")
    monkeypatch.setattr(settings, "audio_gateway_api_key", "secret")
    monkeypatch.setattr(settings, "audio_gateway_health_url", "")
    monkeypatch.setattr(settings, field, url)
    with pytest.raises(RuntimeError, match="AUDIO_GATEWAY"):
        validate_runtime_config()


def test_audio_analyzer_health_reports_local_signal_and_truthful_unconfigured_asr(
    monkeypatch,
):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", True)
    monkeypatch.setattr(settings, "audio_gateway_enabled", False)
    monkeypatch.setattr(video_audio, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_audio, "_numpy_available", lambda: True)
    monkeypatch.setattr(video_audio, "_ASR_LAST_SUCCESS", {})

    health = video_audio.analyzer_health()

    assert health["status"] == "partial"
    assert health["signal"]["status"] == "available"
    assert health["asr"]["status"] == "unsupported"
    assert health["speaker"]["status"] == "unsupported"
    assert health["beat"]["status"] == "available"
    assert health["music"]["semantic_classification"] is False
    assert health["sfx"]["semantic_classification"] is False


def test_audio_analyzer_health_requires_probe_or_success_before_claiming_asr(
    monkeypatch,
):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    _configure_audio_gateway(monkeypatch)
    monkeypatch.setattr(video_audio, "_ASR_LAST_SUCCESS", {})

    unverified = video_audio.analyzer_health()
    assert unverified["asr"]["status"] == "degraded"
    assert unverified["asr"]["verification_status"] == "configured_unverified"

    video_audio._record_asr_success(
        video_audio._normalize_transcription(
            {
                "text": "你好",
                "segments": [{"start": 0, "end": 1, "text": "你好", "speaker": "s1"}],
            },
            offset_seconds=0,
            segment_duration=1,
        ),
        source="test",
    )
    verified = video_audio.analyzer_health()
    assert verified["asr"]["status"] == "available"
    assert verified["speaker"]["status"] == "available"
    assert verified["asr"]["verification_status"] == "last_success"


def test_audio_analyzer_health_probe_is_bounded_and_exposes_no_secret(monkeypatch):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    _configure_audio_gateway(
        monkeypatch,
        api_key="health-secret",
        health_url="https://audio.example.com/health",
        health_timeout=99,
    )
    monkeypatch.setattr(video_audio, "_ASR_LAST_SUCCESS", {})
    seen = {}

    def fake_request(method, url, *, headers, payload, timeout, retries, trusted_hosts):
        seen.update(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
                "retries": retries,
                "trusted_hosts": trusted_hosts,
            }
        )
        return _audio_health_payload()

    monkeypatch.setattr(gateway, "_request_json", fake_request)
    health = video_audio.analyzer_health()

    assert seen["timeout"] == 10
    assert seen["headers"] == {"Authorization": "Bearer health-secret"}
    assert health["asr"]["status"] == "available"
    assert health["speaker"]["status"] == "available"
    assert "health-secret" not in str(health)
    assert "https://audio.example.com" not in str(health)


def test_audio_health_probe_caches_success_and_failure_until_ttl(monkeypatch):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    video_audio._ASR_LAST_SUCCESS.clear()
    clock = [10.0]
    monkeypatch.setattr(video_audio.time, "monotonic", lambda: clock[0])
    _configure_audio_gateway(
        monkeypatch,
        health_url="https://audio.example.com/health",
        health_ttl=5,
    )
    calls: list[float] = []

    def healthy(*_args, **_kwargs):
        calls.append(clock[0])
        return _audio_health_payload()

    monkeypatch.setattr(gateway, "_request_json", healthy)
    first = video_audio._asr_provider_health()
    clock[0] = 14.999
    second = video_audio._asr_provider_health()
    clock[0] = 15.001
    third = video_audio._asr_provider_health()

    assert first == second
    assert third["status"] == "available"
    assert calls == [10.0, 15.001]

    video_audio._ASR_HEALTH_CACHE.clear()
    calls.clear()

    def offline(*_args, **_kwargs):
        calls.append(clock[0])
        raise RuntimeError("offline")

    monkeypatch.setattr(gateway, "_request_json", offline)
    failed = video_audio._asr_provider_health()
    cached_failed = video_audio._asr_provider_health()
    assert failed == cached_failed
    assert failed["verification_status"] == "health_probe_failed"
    assert calls == [15.001]


def test_concurrent_audio_health_requests_share_one_probe(monkeypatch):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    video_audio._ASR_LAST_SUCCESS.clear()
    _configure_audio_gateway(monkeypatch, health_url="https://audio.example.com/health")
    entered = threading.Event()
    release = threading.Event()
    second_lock_attempt = threading.Event()
    calls: list[int] = []

    class ObservedLock:
        def __init__(self):
            self._lock = threading.Lock()
            self._guard = threading.Lock()
            self._attempts = 0

        def __enter__(self):
            with self._guard:
                self._attempts += 1
                if self._attempts == 2:
                    second_lock_attempt.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()

    monkeypatch.setattr(video_audio, "_ASR_HEALTH_PROBE_LOCK", ObservedLock())

    def healthy(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return _audio_health_payload()

    monkeypatch.setattr(gateway, "_request_json", healthy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(video_audio._asr_provider_health)
        assert entered.wait(timeout=2)
        second = executor.submit(video_audio._asr_provider_health)
        assert second_lock_attempt.wait(timeout=2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert [result["status"] for result in results] == ["available", "available"]
    assert calls == [1]


def test_successful_asr_invalidates_cached_failed_health(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    video_audio._ASR_LAST_SUCCESS.clear()
    _configure_audio_gateway(monkeypatch, health_url="https://audio.example.com/health")
    ready = [False]
    calls = {"GET": 0, "POST": 0}

    def health(method, _url, **_kwargs):
        calls[method] += 1
        if not ready[0]:
            raise RuntimeError("not ready")
        return _audio_health_payload(
            capability_statuses={"asr": "available", "speaker": "unsupported"}
        )

    def transcribe(method, _url, **_kwargs):
        calls[method] += 1
        ready[0] = True
        return {
            "text": "hello",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
        }

    monkeypatch.setattr(gateway, "_request_json", health)
    monkeypatch.setattr(gateway, "_request_multipart_json", transcribe)
    failed = video_audio._asr_provider_health()
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fixture-audio")
    analyzed = video_audio._transcribe(str(audio_path), offset_seconds=0.0, duration=2.0)
    recovered = video_audio._asr_provider_health()

    assert failed["verification_status"] == "health_probe_failed"
    assert analyzed["status"] == "analyzed"
    assert recovered["verification_status"] == "health_probe"
    assert calls == {"GET": 2, "POST": 1}


@pytest.mark.parametrize(
    "mutation",
    [
        {"contract_version": "wrong.v1"},
        {"analyzer": {"unsafe": True}},
        {"analyzer_version": "bad\nversion"},
        {"provider_model": ["unsafe"]},
        {"provider_model": "different-model"},
        {"capability_statuses": {"asr": "available"}},
        {"capability_statuses": {"asr": "partial", "speaker": "unsupported"}},
        {"capability_statuses": {"asr": "degraded", "speaker": "available"}},
    ],
)
def test_audio_health_rejects_invalid_contract_metadata(monkeypatch, mutation):
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    video_audio._ASR_LAST_SUCCESS.clear()
    _configure_audio_gateway(monkeypatch, health_url="https://audio.example.com/health")
    payload = _audio_health_payload()
    payload.update(mutation)
    monkeypatch.setattr(gateway, "_request_json", lambda *_args, **_kwargs: payload)

    health = video_audio._asr_provider_health()

    assert health["status"] == "degraded"
    assert health["verification_status"] == "health_probe_failed"


def test_reverse_analyzer_status_includes_audio_health(
    client,
    make_user,
    auth,
    monkeypatch,
):
    phone = "13710000064"
    make_user(phone, balance=100)
    sentinel = {"contract_version": "audio-evidence.v1", "status": "partial"}
    monkeypatch.setattr(video_audio, "analyzer_health", lambda: sentinel)

    response = client.get("/api/prompt/reverse-analyzers/status", headers=auth(phone))

    assert response.status_code == 200, response.text
    assert response.json()["audio"] == sentinel


@pytest.mark.parametrize(
    "field,value",
    [
        ("audio_upload_max_bytes", 100),
        ("audio_gateway_timeout_seconds", 1),
        ("audio_gateway_health_cache_ttl_seconds", 0),
        ("audio_gateway_health_cache_ttl_seconds", 61),
        ("audio_transcription_model", "bad\nmodel"),
    ],
)
def test_audio_settings_reject_unsafe_limits(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_audio_gateway_settings_load_from_dotenv(tmp_path, monkeypatch):
    for name in (
        "AUDIO_GATEWAY_ENABLED",
        "AUDIO_GATEWAY_BASE_URL",
        "AUDIO_GATEWAY_API_KEY",
        "AUDIO_TRANSCRIPTION_MODEL",
        "AUDIO_GATEWAY_HEALTH_URL",
        "AUDIO_GATEWAY_HEALTH_CACHE_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "AUDIO_GATEWAY_ENABLED=true\n"
        "AUDIO_GATEWAY_BASE_URL=https://dotenv-audio.example.test\n"
        "AUDIO_GATEWAY_API_KEY=dotenv-audio-secret\n"
        "AUDIO_TRANSCRIPTION_MODEL=dotenv-asr\n"
        "AUDIO_GATEWAY_HEALTH_URL=https://dotenv-audio.example.test/health\n"
        "AUDIO_GATEWAY_HEALTH_CACHE_TTL_SECONDS=12\n",
        encoding="utf-8",
    )

    loaded = Settings(_env_file=dotenv)

    assert loaded.audio_gateway_enabled is True
    assert loaded.audio_gateway_base_url == "https://dotenv-audio.example.test"
    assert loaded.audio_gateway_api_key == "dotenv-audio-secret"
    assert loaded.audio_transcription_model == "dotenv-asr"
    assert loaded.audio_gateway_health_url.endswith("/health")
    assert loaded.audio_gateway_health_cache_ttl_seconds == 12


def test_audio_disabled_refunds_surcharge_and_blocks_provider_claims(
    client, make_user, auth, monkeypatch
):
    phone = "13710000061"
    uid = make_user(phone, balance=100)
    _stub_video_reverse(monkeypatch, _audio_result("disabled"))

    response = post_reverse(
        client,
        {
            "client_request_id": "video-audio-disabled-001",
            "asset_url": "https://cdn.example.com/audio-source.mp4",
            "source_type": "video",
            "target": "video",
            "include_audio": True,
            "source_range": {"start_seconds": 10, "end_seconds": 20},
        },
        headers=auth(phone),
    )

    assert response.status_code == 202, response.text
    done = response.json()
    assert done["status"] == "succeeded"
    assert done["cost_settled"] == 5
    assert done["video_analysis"]["source"]["audio_analyzed"] is False
    assert done["video_analysis"]["audio"]["status"] == "disabled"
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, done["id"])
        user = db.get(User, uid)
        assert operation.pricing_snapshot["visual_cost"] == 5
        assert operation.pricing_snapshot["audio_surcharge"] == 2
        assert operation.pricing_snapshot["frozen"] == 7
        assert user.balance_credits == 95
        assert user.frozen_credits == 0
        settled = (
            db.query(CreditTransaction)
            .filter_by(biz_type="reverse_operation", biz_ref=operation.id, type="settle")
            .one()
        )
        assert settled.reserved_amount == 7
        assert settled.real_cost == 5
    finally:
        db.close()


def test_timestamped_audio_evidence_settles_surcharge_and_survives_range(
    client, make_user, auth, monkeypatch
):
    phone = "13710000062"
    uid = make_user(phone, balance=100)
    _stub_video_reverse(
        monkeypatch,
        _audio_result("analyzed", start=12.4, end=14.8, text="欢迎回来"),
    )

    response = post_reverse(
        client,
        {
            "client_request_id": "video-audio-success-001",
            "asset_url": "https://cdn.example.com/audio-source.mp4",
            "source_type": "video",
            "target": "video",
            "include_audio": True,
            "source_range": {"start_seconds": 10, "end_seconds": 20},
        },
        headers=auth(phone),
    )

    assert response.status_code == 202, response.text
    done = response.json()
    assert done["cost_settled"] == 7
    assert done["video_analysis"]["source"]["audio_analyzed"] is True
    assert done["video_analysis"]["audio"]["segments"][0]["start_seconds"] == 12.4
    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 93
    finally:
        db.close()


def test_reverse_retention_purges_audio_raw_and_revision_content(client, make_user):
    uid = make_user("13710000063", balance=100)
    old = datetime.now(timezone.utc) - timedelta(days=120)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="retention-audio-reverse-001",
            request_fingerprint="a" * 64,
            target="video",
            asset_url="https://cdn.example.com/private.mp4",
            custom_instruction="private instruction",
            request_context={"include_audio": True, "custom_instruction": "private instruction"},
            template_snapshot={"templates": {"video": "private instruction"}},
            result={
                "video_analysis": {
                    "audio": _audio_result("analyzed", start=1, end=2, text="secret")
                }
            },
            normalized_result={"final_text": "secret"},
            raw_provider_result={"text": "secret"},
            status="succeeded",
            created_at=old,
            updated_at=old,
        )
        db.add(operation)
        db.flush()
        db.add(
            ReverseResultRevision(
                operation_id=operation.id,
                user_id=uid,
                version=1,
                source="normalized",
                payload={"transcript": "secret"},
                created_at=old,
            )
        )
        db.commit()
        operation_id = operation.id

        retention._tombstone_named_reverse_operations(
            db, datetime.now(timezone.utc) - timedelta(days=90)
        )
        db.expire_all()
        purged = db.get(ReverseOperation, operation_id)
        assert purged.request_fingerprint == "a" * 64
        assert purged.result is None
        assert purged.normalized_result is None
        assert purged.raw_provider_result is None
        assert purged.request_context is None
        assert purged.template_snapshot is None
        assert purged.custom_instruction is None
        assert db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count() == 0
    finally:
        db.close()
