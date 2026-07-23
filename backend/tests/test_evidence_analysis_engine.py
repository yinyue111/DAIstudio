from __future__ import annotations

import base64
import io
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from PIL import Image

from app.config import Settings, settings
from app.runtime_config import validate_runtime_config
from app.services import image_evidence_analysis, video_audio, video_evidence_analysis


def _data_uri(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def _ocr(text: str, bbox: dict, confidence: float = 0.9):
    def analyzer(_image):
        return {
            "status": "analyzed",
            "analyzer": "fixture_ocr",
            "analyzer_version": "1.0",
            "degraded_reason": None,
            "evidence": [{
                "evidence_type": "ocr",
                "field_key": "ocr_text",
                "label": "visible_text",
                "evidence_text": text,
                "bbox": bbox,
                "confidence": confidence,
                "fact_status": "visible",
            }],
        }
    return analyzer


def _unsupported(name):
    return lambda _image: {
        "status": "unsupported", "analyzer": name, "analyzer_version": "none",
        "degraded_reason": "fixture unavailable", "evidence": [],
    }


def _configure_region_provider(
    monkeypatch,
    capability: str,
    *,
    url: str,
    health_url: str = "",
    api_key: str = "",
    timeout: int = 30,
    health_timeout: int = 3,
) -> None:
    prefix = f"image_evidence_{capability}"
    monkeypatch.setattr(image_evidence_analysis.settings, f"{prefix}_url", url)
    monkeypatch.setattr(image_evidence_analysis.settings, f"{prefix}_health_url", health_url)
    monkeypatch.setattr(image_evidence_analysis.settings, f"{prefix}_api_key", api_key)
    monkeypatch.setattr(image_evidence_analysis.settings, f"{prefix}_timeout_seconds", timeout)
    monkeypatch.setattr(
        image_evidence_analysis.settings,
        f"{prefix}_health_timeout_seconds",
        health_timeout,
    )


def _region_provider_payload(capability: str = "detector") -> dict:
    return {
        "contract_version": "region-analyzer.v1",
        "capability": capability,
        "status": "analyzed",
        "analyzer": f"fixture_{capability}",
        "analyzer_version": "2026.07",
        "evidence": [{
            "label": "product",
            "text": "visible product",
            "confidence": 0.9,
            "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
        }],
    }


def _semantic_provider_payload() -> dict:
    return {
        "contract_version": "video-semantic-evidence.v1",
        "analyzer": "fixture_video_semantic",
        "analyzer_version": "2026.07",
        "capabilities": {
            "subject_tracking": {
                "status": "analyzed",
                "evidence": [{
                    "evidence_id": "provider-subject-1",
                    "label": "person",
                    "source_segment_index": 1,
                    "start_seconds": 10.0,
                    "end_seconds": 12.0,
                    "confidence": 0.96,
                    "observations": [
                        {
                            "frame_index": 1,
                            "absolute_timestamp_seconds": 10.0,
                            "bbox": {"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.8},
                            "confidence": 0.95,
                        },
                        {
                            "frame_index": 2,
                            "absolute_timestamp_seconds": 12.0,
                            "bbox": {"x": 0.2, "y": 0.1, "width": 0.3, "height": 0.8},
                            "confidence": 0.97,
                        },
                    ],
                }],
            },
            "pose": {
                "status": "analyzed",
                "evidence": [{
                    "evidence_id": "provider-pose-1",
                    "subject_evidence_id": "provider-subject-1",
                    "source_segment_index": 1,
                    "frame_index": 1,
                    "absolute_timestamp_seconds": 10.0,
                    "confidence": 0.92,
                    "keypoints": [
                        {"name": "nose", "x": 0.2, "y": 0.2, "confidence": 0.93},
                        {"name": "left_wrist", "x": 0.1, "y": 0.5, "confidence": 0.88},
                    ],
                }],
            },
            "action": {
                "status": "analyzed",
                "evidence": [{
                    "evidence_id": "provider-action-1",
                    "subject_evidence_id": "provider-subject-1",
                    "label": "walking",
                    "source_segment_index": 1,
                    "start_seconds": 10.0,
                    "end_seconds": 12.0,
                    "frame_indices": [1, 2],
                    "confidence": 0.91,
                }],
            },
            "transition": {
                "status": "analyzed",
                "evidence": [{
                    "evidence_id": "provider-transition-1",
                    "label": "hard_cut",
                    "source_segment_index": 1,
                    "timestamp_seconds": 11.0,
                    "before_frame_index": 1,
                    "after_frame_index": 2,
                    "confidence": 0.89,
                }],
            },
        },
    }


def _semantic_health_payload(**overrides) -> dict:
    payload = {
        "contract_version": "video-semantic-evidence.v1",
        "ok": True,
        "analyzer": "fixture_video_semantic",
        "analyzer_version": "2026.07",
        "capability_statuses": {
            "subject_tracking": "available",
            "pose": "available",
            "action": "available",
            "transition": "available",
        },
    }
    payload.update(overrides)
    return payload


def _semantic_frames() -> tuple[list[Image.Image], list[dict]]:
    images = [
        Image.new("RGB", (20, 20), "white"),
        Image.new("RGB", (20, 20), "black"),
    ]
    frames = [
        {
            "frame_index": 1,
            "timestamp_seconds": 10.0,
            "source_segment_index": 1,
            "source_content_hash": "a" * 64,
        },
        {
            "frame_index": 2,
            "timestamp_seconds": 12.0,
            "source_segment_index": 1,
            "source_content_hash": "b" * 64,
        },
    ]
    return images, frames


def _configure_semantic_provider(
    monkeypatch,
    *,
    url: str = "https://semantic.example.test/analyze",
    api_key: str = "secret-key",
    timeout: int = 45,
    health_url: str = "",
    health_timeout: int = 3,
    health_ttl: int = 15,
) -> None:
    monkeypatch.setattr(
        video_evidence_analysis.settings, "video_evidence_semantic_url", url
    )
    monkeypatch.setattr(
        video_evidence_analysis.settings, "video_evidence_semantic_api_key", api_key
    )
    monkeypatch.setattr(
        video_evidence_analysis.settings,
        "video_evidence_semantic_timeout_seconds",
        timeout,
    )
    monkeypatch.setattr(
        video_evidence_analysis.settings,
        "video_evidence_semantic_health_url",
        health_url,
    )
    monkeypatch.setattr(
        video_evidence_analysis.settings,
        "video_evidence_semantic_health_timeout_seconds",
        health_timeout,
    )
    monkeypatch.setattr(
        video_evidence_analysis.settings,
        "video_evidence_semantic_health_cache_ttl_seconds",
        health_ttl,
    )
    video_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()


def test_image_sources_merge_independent_evidence_and_surface_region_conflict():
    image = Image.new("RGB", (100, 100), "white")
    bbox = {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.2}
    result = image_evidence_analysis.analyze_image_sources(
        [_data_uri(image)],
        vlm_evidence=[{
            "evidence_type": "ocr", "field_key": "文字版式", "evidence_text": "ACNE",
            "bbox": bbox, "confidence": 0.7, "source_index": 1,
            "fact_status": "visible", "protected": False, "editable": False,
        }],
        ocr_analyzer=_ocr("ACME", bbox),
        region_analyzer=_unsupported("proposal"),
        detector_analyzer=_unsupported("detector"),
        segmenter_analyzer=_unsupported("segmenter"),
    )

    assert result["contract_version"] == "image-evidence.v1"
    assert len(result["evidence"]) == 2
    assert all(row["evidence_id"].startswith("ev-") for row in result["evidence"])
    assert {row["analyzer_source"] for row in result["evidence"]} == {
        "fixture_ocr", "vision_language_model",
    }
    assert all(row["conflict_status"] == "conflict" for row in result["evidence"])
    replay = image_evidence_analysis.analyze_image_sources(
        [_data_uri(image)], ocr_analyzer=_ocr("ACME", bbox),
        region_analyzer=_unsupported("proposal"), detector_analyzer=_unsupported("detector"),
        segmenter_analyzer=_unsupported("segmenter"),
    )
    assert replay["evidence"][0]["evidence_id"] == result["evidence"][0]["evidence_id"]


def test_unconfigured_semantic_region_providers_are_truthfully_unsupported(monkeypatch):
    _configure_region_provider(monkeypatch, "detector", url="")
    _configure_region_provider(monkeypatch, "segmenter", url="")
    image = Image.new("RGB", (10, 10), "white")

    assert image_evidence_analysis.http_region_provider(
        image, capability="detector"
    )["status"] == "unsupported"
    assert image_evidence_analysis.http_region_provider(
        image, capability="segmenter"
    )["status"] == "unsupported"


def test_configured_detector_provider_contract_is_validated_without_network(monkeypatch):
    _configure_region_provider(
        monkeypatch,
        "detector",
        url="https://detector.example.test/analyze",
        api_key="settings-secret",
    )
    monkeypatch.setenv(
        "IMAGE_EVIDENCE_DETECTOR_URL", "https://ignored-env.example.test/analyze"
    )
    captured = {}

    def provider(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {
            "contract_version": "region-analyzer.v1",
            "capability": "detector",
            "status": "analyzed",
            "analyzer": "fixture_detector", "analyzer_version": "2026.07",
            "evidence": [{
                "label": "bottle", "text": "visible bottle", "confidence": .93,
                "bbox": {"x": .2, "y": .1, "width": .5, "height": .8},
            }],
        }

    monkeypatch.setattr(image_evidence_analysis.gateway, "_request_json", provider)
    result = image_evidence_analysis.http_region_provider(
        Image.new("RGB", (20, 20), "white"), capability="detector",
    )

    assert result["status"] == "analyzed"
    assert result["analyzer"] == "fixture_detector"
    assert result["evidence"][0]["label"] == "bottle"
    assert captured["url"] == "https://detector.example.test/analyze"
    assert captured["headers"] == {"Authorization": "Bearer settings-secret"}
    assert captured["timeout"] == 30
    assert captured["retries"] == 0
    assert captured["trusted_hosts"] == settings.trusted_analyzer_host_list


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**_region_provider_payload(), "contract_version": "region-analyzer.v0"},
        {**_region_provider_payload(), "capability": "segmenter"},
        {**_region_provider_payload(), "status": "ready"},
        {**_region_provider_payload(), "analyzer": ""},
        {**_region_provider_payload(), "analyzer": 123},
        {**_region_provider_payload(), "analyzer_version": "bad\x00version"},
        {**_region_provider_payload(), "evidence": "not-an-array"},
        {
            **_region_provider_payload(),
            "status": "unsupported",
            "degraded_reason": "not available",
        },
        {
            **_region_provider_payload(),
            "status": "degraded",
            "evidence": [],
        },
        {
            **_region_provider_payload(),
            "degraded_reason": "provider admitted a failure",
        },
    ],
)
def test_region_provider_rejects_invalid_contract_without_emitting_evidence(
    monkeypatch, payload,
):
    _configure_region_provider(
        monkeypatch, "detector", url="https://detector.example.test/analyze"
    )
    monkeypatch.setattr(
        image_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: deepcopy(payload),
    )

    result = image_evidence_analysis.http_region_provider(
        Image.new("RGB", (20, 20), "white"), capability="detector"
    )

    assert result["status"] == "degraded"
    assert result["contract_version"] == "region-analyzer.v1"
    assert result["capability"] == "detector"
    assert result["evidence"] == []
    assert result["evidence_count"] == 0
    assert result["degraded_reason"]


def test_region_provider_preserves_truthful_unsupported_status(monkeypatch):
    _configure_region_provider(
        monkeypatch, "detector", url="https://detector.example.test/analyze"
    )
    payload = {
        **_region_provider_payload(),
        "status": "unsupported",
        "evidence": [],
        "degraded_reason": "model does not support this image type",
    }
    monkeypatch.setattr(
        image_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: payload,
    )

    result = image_evidence_analysis.http_region_provider(
        Image.new("RGB", (20, 20), "white"), capability="detector"
    )

    assert result["status"] == "unsupported"
    assert result["evidence"] == []
    assert result["degraded_reason"] == "model does not support this image type"


def test_image_region_provider_settings_load_from_dotenv(tmp_path, monkeypatch):
    for name in (
        "IMAGE_EVIDENCE_DETECTOR_URL",
        "IMAGE_EVIDENCE_DETECTOR_API_KEY",
        "IMAGE_EVIDENCE_DETECTOR_TIMEOUT_SECONDS",
        "IMAGE_EVIDENCE_HEALTH_CACHE_TTL_SECONDS",
        "VIDEO_EVIDENCE_SEMANTIC_URL",
        "VIDEO_EVIDENCE_SEMANTIC_API_KEY",
        "VIDEO_EVIDENCE_SEMANTIC_HEALTH_URL",
        "VIDEO_EVIDENCE_SEMANTIC_HEALTH_CACHE_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "IMAGE_EVIDENCE_DETECTOR_URL=https://dotenv-detector.example.test/analyze\n"
        "IMAGE_EVIDENCE_DETECTOR_API_KEY=dotenv-secret\n"
        "IMAGE_EVIDENCE_DETECTOR_TIMEOUT_SECONDS=47\n"
        "IMAGE_EVIDENCE_HEALTH_CACHE_TTL_SECONDS=12\n"
        "VIDEO_EVIDENCE_SEMANTIC_URL=https://dotenv-semantic.example.test/analyze\n"
        "VIDEO_EVIDENCE_SEMANTIC_API_KEY=dotenv-semantic-secret\n"
        "VIDEO_EVIDENCE_SEMANTIC_HEALTH_URL=https://dotenv-semantic.example.test/health\n"
        "VIDEO_EVIDENCE_SEMANTIC_HEALTH_CACHE_TTL_SECONDS=13\n",
        encoding="utf-8",
    )

    loaded = Settings(_env_file=dotenv)

    assert loaded.image_evidence_detector_url.endswith("/analyze")
    assert loaded.image_evidence_detector_api_key == "dotenv-secret"
    assert loaded.image_evidence_detector_timeout_seconds == 47
    assert loaded.image_evidence_health_cache_ttl_seconds == 12
    assert loaded.video_evidence_semantic_url.endswith("/analyze")
    assert loaded.video_evidence_semantic_api_key == "dotenv-semantic-secret"
    assert loaded.video_evidence_semantic_health_url.endswith("/health")
    assert loaded.video_evidence_semantic_health_cache_ttl_seconds == 13


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_evidence_detector_timeout_seconds", 0),
        ("image_evidence_segmenter_timeout_seconds", 121),
        ("image_evidence_detector_health_timeout_seconds", 0),
        ("image_evidence_segmenter_health_timeout_seconds", 11),
        ("image_evidence_health_cache_ttl_seconds", 0),
        ("image_evidence_health_cache_ttl_seconds", 61),
    ],
)
def test_image_region_provider_runtime_limits_are_bounded(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "region",
    [
        {"bbox": {"x": "0.2", "y": 0.1, "width": 0.5, "height": 0.8}},
        {"bbox": {"x": math.nan, "y": 0.1, "width": 0.5, "height": 0.8}},
        {"bbox": {"x": 0.2, "y": math.inf, "width": 0.5, "height": 0.8}},
        {"bbox": {"x": 0.8, "y": 0.1, "width": 0.5, "height": 0.8}},
        {"bbox": {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.8, "z": 0}},
        {"polygon": [{"x": 0.1, "y": 0.1}, {"x": 1.1, "y": 0.2}, {"x": 0.2, "y": 0.9}]},
        {"polygon": [{"x": 0.1, "y": 0.1}, {"x": math.nan, "y": 0.2}, {"x": 0.2, "y": 0.9}]},
        {"polygon": [{"x": 0.1, "y": 0.1}, {"x": 0.8}, {"x": 0.2, "y": 0.9}]},
        {"polygon": [{"x": 0.1, "y": 0.1}, {"x": 0.8, "y": 0.2, "z": 0}, {"x": 0.2, "y": 0.9}]},
        {"polygon": [{"x": 0.1, "y": 0.1}, {"x": 0.8, "y": 0.2}, {"x": 0.1, "y": 0.1}]},
        {"polygon": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}, {"x": 0.3, "y": 0.3}]},
        {"polygon": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}, {"x": 0.1, "y": 0.9}, {"x": 0.9, "y": 0.1}]},
        {"bbox": {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.8}, "confidence": "0.9"},
        {"bbox": {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.8}, "confidence": math.nan},
        {"bbox": {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.8}, "confidence": math.inf},
        {"bbox": {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.8}, "confidence": 1.1},
    ],
)
def test_configured_region_provider_rejects_malformed_nonfinite_and_out_of_bounds_geometry(
    monkeypatch, region,
):
    _configure_region_provider(
        monkeypatch, "segmenter", url="https://segmenter.example.test/analyze"
    )

    def provider(*_args, **_kwargs):
        return {
            "contract_version": "region-analyzer.v1",
            "capability": "segmenter",
            "status": "analyzed",
            "analyzer": "fixture_segmenter",
            "analyzer_version": "2026.07",
            "evidence": [{"label": "product", "confidence": 0.9, **region}],
        }

    monkeypatch.setattr(image_evidence_analysis.gateway, "_request_json", provider)
    result = image_evidence_analysis.http_region_provider(
        Image.new("RGB", (20, 20), "white"), capability="segmenter",
    )

    assert result["status"] == "degraded"
    assert result["evidence"] == []
    assert result["degraded_reason"]


def test_configured_provider_health_requires_probe_or_last_success(monkeypatch):
    image_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    image_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    _configure_region_provider(
        monkeypatch, "detector", url="https://detector.example.test/analyze"
    )

    unverified = image_evidence_analysis.analyzer_health()["detector"]
    assert unverified["status"] == "degraded"
    assert unverified["verification_status"] == "configured_unverified"

    monkeypatch.setattr(
        image_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: {
            "contract_version": "region-analyzer.v1",
            "capability": "detector",
            "status": "analyzed",
            "analyzer": "fixture_detector",
            "analyzer_version": "2026.07",
            "evidence": [{
                "label": "product",
                "confidence": 0.9,
                "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
            }],
        },
    )
    analyzed = image_evidence_analysis.http_region_provider(
        Image.new("RGB", (20, 20), "white"), capability="detector",
    )
    assert analyzed["status"] == "analyzed"

    verified = image_evidence_analysis.analyzer_health()["detector"]
    assert verified["status"] == "available"
    assert verified["verification_status"] == "last_success"
    assert verified["last_success_source"] == "analysis"
    assert verified["last_success_at"]


def test_configured_provider_health_probe_is_bounded_and_reports_failure(monkeypatch):
    image_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    image_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    _configure_region_provider(
        monkeypatch,
        "segmenter",
        url="https://segmenter.example.test/analyze",
        health_url="https://segmenter.example.test/health",
        health_timeout=99,
    )
    captured = {}

    def healthy(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {
            "ok": True,
            "analyzer": "fixture_segmenter",
            "analyzer_version": "2026.07",
        }

    monkeypatch.setattr(image_evidence_analysis.gateway, "_request_json", healthy)
    available = image_evidence_analysis.analyzer_health()["segmenter"]
    assert available["status"] == "available"
    assert available["verification_status"] == "health_probe"
    assert available["last_success_source"] == "health_probe"
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/health")
    assert captured["payload"] is None
    assert captured["timeout"] == 10
    assert captured["retries"] == 0
    assert captured["trusted_hosts"] == settings.trusted_analyzer_host_list

    monkeypatch.setattr(
        image_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe unavailable")),
    )
    image_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    degraded = image_evidence_analysis.analyzer_health()["segmenter"]
    assert degraded["status"] == "degraded"
    assert degraded["verification_status"] == "health_probe_failed"
    assert degraded["last_success_at"] == available["last_success_at"]
    assert "probe unavailable" in degraded["degraded_reason"]


def test_provider_health_probe_uses_short_ttl_cache(monkeypatch):
    image_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    image_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    _configure_region_provider(
        monkeypatch,
        "detector",
        url="https://detector.example.test/analyze",
        health_url="https://detector.example.test/health",
    )
    monkeypatch.setattr(
        image_evidence_analysis.settings,
        "image_evidence_health_cache_ttl_seconds",
        15,
    )
    clock = [100.0]
    monkeypatch.setattr(image_evidence_analysis.time, "monotonic", lambda: clock[0])
    calls = []

    def healthy(*_args, **_kwargs):
        calls.append(clock[0])
        return {
            "ok": True,
            "contract_version": "region-analyzer.v1",
            "capability": "detector",
            "analyzer": "fixture_detector",
            "analyzer_version": "2026.07",
        }

    monkeypatch.setattr(image_evidence_analysis.gateway, "_request_json", healthy)

    first = image_evidence_analysis.analyzer_health()["detector"]
    clock[0] = 114.999
    second = image_evidence_analysis.analyzer_health()["detector"]
    clock[0] = 115.001
    third = image_evidence_analysis.analyzer_health()["detector"]

    assert first == second
    assert third["status"] == "available"
    assert calls == [100.0, 115.001]


def test_concurrent_provider_health_requests_share_one_probe(monkeypatch):
    image_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    image_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    _configure_region_provider(
        monkeypatch,
        "detector",
        url="https://detector.example.test/analyze",
        health_url="https://detector.example.test/health",
    )
    entered = threading.Event()
    release = threading.Event()
    second_lock_attempt = threading.Event()
    calls = []

    class ObservedLock:
        def __init__(self):
            self._lock = threading.Lock()
            self._attempt_guard = threading.Lock()
            self._attempts = 0

        def __enter__(self):
            with self._attempt_guard:
                self._attempts += 1
                if self._attempts == 2:
                    second_lock_attempt.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()

    monkeypatch.setitem(
        image_evidence_analysis._PROVIDER_HEALTH_PROBE_LOCKS,
        "detector",
        ObservedLock(),
    )

    def healthy(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return {
            "ok": True,
            "analyzer": "fixture_detector",
            "analyzer_version": "2026.07",
        }

    monkeypatch.setattr(image_evidence_analysis.gateway, "_request_json", healthy)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(image_evidence_analysis._provider_health, "detector")
        assert entered.wait(timeout=2)
        second = executor.submit(image_evidence_analysis._provider_health, "detector")
        assert second_lock_attempt.wait(timeout=2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert [result["status"] for result in results] == ["available", "available"]
    assert calls == [1]


def test_successful_analysis_invalidates_cached_failed_health_probe(monkeypatch):
    image_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    image_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    _configure_region_provider(
        monkeypatch,
        "detector",
        url="https://detector.example.test/analyze",
        health_url="https://detector.example.test/health",
    )
    health_ready = [False]
    calls = {"GET": 0, "POST": 0}

    def provider(method, _url, **_kwargs):
        calls[method] += 1
        if method == "POST":
            health_ready[0] = True
            return _region_provider_payload()
        if not health_ready[0]:
            raise RuntimeError("not ready")
        return {
            "ok": True,
            "analyzer": "fixture_detector",
            "analyzer_version": "2026.07",
        }

    monkeypatch.setattr(image_evidence_analysis.gateway, "_request_json", provider)

    failed = image_evidence_analysis.analyzer_health()["detector"]
    analyzed = image_evidence_analysis.http_region_provider(
        Image.new("RGB", (20, 20), "white"), capability="detector"
    )
    recovered = image_evidence_analysis.analyzer_health()["detector"]

    assert failed["verification_status"] == "health_probe_failed"
    assert analyzed["status"] == "analyzed"
    assert recovered["verification_status"] == "health_probe"
    assert calls == {"GET": 2, "POST": 1}


def test_reverse_analyzer_status_reuses_image_health_cache(
    client, make_user, auth, monkeypatch,
):
    image_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    image_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    _configure_region_provider(
        monkeypatch,
        "detector",
        url="https://detector.example.test/analyze",
        health_url="https://detector.example.test/health",
    )
    _configure_region_provider(monkeypatch, "segmenter", url="")
    monkeypatch.setattr(image_evidence_analysis, "TESSERACT", None)
    monkeypatch.setattr(
        video_evidence_analysis,
        "analyzer_health",
        lambda: {"contract_version": "video-evidence.v1"},
    )
    monkeypatch.setattr(
        video_audio,
        "analyzer_health",
        lambda: {"contract_version": "audio-evidence.v1"},
    )
    calls = []
    monkeypatch.setattr(
        image_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: calls.append(1) or {
            "ok": True,
            "analyzer": "fixture_detector",
            "analyzer_version": "2026.07",
        },
    )
    phone = "13710000065"
    make_user(phone, balance=100)
    headers = auth(phone)

    first = client.get("/api/prompt/reverse-analyzers/status", headers=headers)
    second = client.get("/api/prompt/reverse-analyzers/status", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["image"]["detector"]["status"] == "available"
    assert second.json()["image"]["detector"]["status"] == "available"
    assert calls == [1]


def test_reverse_analyzer_status_reuses_semantic_and_audio_health_caches(
    client, make_user, auth, monkeypatch,
):
    _configure_semantic_provider(
        monkeypatch,
        health_url="https://semantic.example.test/health",
    )
    video_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    video_audio._ASR_LAST_SUCCESS.clear()
    video_audio._ASR_HEALTH_CACHE.clear()
    monkeypatch.setattr(settings, "audio_signal_analysis_enabled", False)
    monkeypatch.setattr(settings, "audio_gateway_enabled", True)
    monkeypatch.setattr(settings, "audio_gateway_base_url", "https://audio.example.test")
    monkeypatch.setattr(settings, "audio_gateway_api_key", "audio-key")
    monkeypatch.setattr(settings, "audio_transcription_model", "whisper-test")
    monkeypatch.setattr(
        settings, "audio_gateway_health_url", "https://audio.example.test/health"
    )
    monkeypatch.setattr(settings, "audio_gateway_health_timeout_seconds", 3)
    monkeypatch.setattr(settings, "audio_gateway_health_cache_ttl_seconds", 15)
    monkeypatch.setattr(
        image_evidence_analysis,
        "analyzer_health",
        lambda: {"contract_version": "image-evidence.v1"},
    )
    calls = {"semantic": 0, "audio": 0}

    def provider(_method, url, **_kwargs):
        if "semantic" in url:
            calls["semantic"] += 1
            return _semantic_health_payload()
        calls["audio"] += 1
        return {
            "contract_version": "audio-evidence.v1",
            "ok": True,
            "analyzer": "fixture-asr",
            "analyzer_version": "2026.07",
            "provider_model": "whisper-test",
            "capability_statuses": {"asr": "available", "speaker": "unsupported"},
        }

    monkeypatch.setattr(video_evidence_analysis.gateway, "_request_json", provider)
    phone = "13710000066"
    make_user(phone, balance=100)
    headers = auth(phone)

    first = client.get("/api/prompt/reverse-analyzers/status", headers=headers)
    second = client.get("/api/prompt/reverse-analyzers/status", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["video"]["semantic_provider"]["status"] == "available"
    assert first.json()["audio"]["asr"]["status"] == "available"
    assert calls == {"semantic": 1, "audio": 1}


def test_tesseract_health_reports_languages_and_chinese_support(monkeypatch):
    monkeypatch.setattr(image_evidence_analysis, "TESSERACT", "/usr/local/bin/tesseract")
    monkeypatch.setattr(
        image_evidence_analysis.settings,
        "image_evidence_ocr_languages",
        "chi_sim+eng",
    )
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_version", lambda: "tesseract 5.5.2")
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_languages", lambda: ["eng", "osd"])

    missing_chinese = image_evidence_analysis.analyzer_health()["ocr"]
    assert missing_chinese["status"] == "degraded"
    assert missing_chinese["languages"] == ["eng", "osd"]
    assert missing_chinese["requested_languages"] == ["chi_sim", "eng"]
    assert missing_chinese["effective_languages"] == []
    assert missing_chinese["missing_languages"] == ["chi_sim"]
    assert missing_chinese["language_argument"] is None
    assert missing_chinese["chinese_status"] == "unsupported"
    assert "chi_sim" in missing_chinese["degraded_reason"]

    monkeypatch.setattr(
        image_evidence_analysis,
        "_tesseract_languages",
        lambda: ["chi_sim", "eng", "osd"],
    )
    with_chinese = image_evidence_analysis.analyzer_health()["ocr"]
    assert with_chinese["status"] == "available"
    assert with_chinese["chinese_status"] == "available"
    assert with_chinese["effective_languages"] == ["chi_sim", "eng"]
    assert with_chinese["language_argument"] == "chi_sim+eng"
    assert with_chinese["degraded_reason"] is None


def test_tesseract_ocr_executes_with_configured_language_argument(monkeypatch):
    monkeypatch.setattr(image_evidence_analysis, "TESSERACT", "/usr/local/bin/tesseract")
    monkeypatch.setattr(
        image_evidence_analysis.settings,
        "image_evidence_ocr_languages",
        "chi_sim+eng",
    )
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_version", lambda: "tesseract 5.5.2")
    monkeypatch.setattr(
        image_evidence_analysis,
        "_tesseract_languages",
        lambda: ["chi_sim", "eng", "osd"],
    )
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return type("Completed", (), {
            "returncode": 0,
            "stderr": b"",
            "stdout": (
                b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
                b"left\ttop\twidth\theight\tconf\ttext\n"
                b"5\t1\t1\t1\t1\t1\t1\t2\t3\t4\t95\tACME\n"
            ),
        })()

    monkeypatch.setattr(image_evidence_analysis.subprocess, "run", run)
    result = image_evidence_analysis.tesseract_ocr(Image.new("RGB", (10, 10), "white"))

    assert result["status"] == "analyzed"
    assert result["language_argument"] == "chi_sim+eng"
    assert result["effective_languages"] == ["chi_sim", "eng"]
    assert captured["command"] == [
        "/usr/local/bin/tesseract",
        "stdin",
        "stdout",
        "-l",
        "chi_sim+eng",
        "--psm",
        "11",
        "tsv",
    ]
    assert captured["kwargs"]["input"].startswith(b"\x89PNG")
    assert result["evidence"][0]["evidence_text"] == "ACME"


def test_tesseract_ocr_does_not_execute_when_configured_language_is_missing(monkeypatch):
    monkeypatch.setattr(image_evidence_analysis, "TESSERACT", "/usr/local/bin/tesseract")
    monkeypatch.setattr(
        image_evidence_analysis.settings,
        "image_evidence_ocr_languages",
        "chi_sim+eng",
    )
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_version", lambda: "tesseract 5.5.2")
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_languages", lambda: ["eng"])
    monkeypatch.setattr(
        image_evidence_analysis.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("OCR must not execute with missing languages"),
    )

    result = image_evidence_analysis.tesseract_ocr(Image.new("RGB", (10, 10), "white"))

    assert result["status"] == "degraded"
    assert result["missing_languages"] == ["chi_sim"]
    assert result["effective_languages"] == []
    assert "chi_sim" in result["degraded_reason"]


def test_tesseract_ocr_rejects_invalid_runtime_language_configuration(monkeypatch):
    monkeypatch.setattr(image_evidence_analysis, "TESSERACT", "/usr/local/bin/tesseract")
    monkeypatch.setattr(
        image_evidence_analysis.settings,
        "image_evidence_ocr_languages",
        "eng+../../etc/passwd",
    )
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_version", lambda: "tesseract 5.5.2")
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_languages", lambda: ["eng"])
    monkeypatch.setattr(
        image_evidence_analysis.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("OCR must not execute with invalid configuration"),
    )

    result = image_evidence_analysis.tesseract_ocr(Image.new("RGB", (10, 10), "white"))

    assert result["status"] == "degraded"
    assert result["configuration_error"]
    assert result["language_argument"] is None


def test_tesseract_health_does_not_require_chinese_when_only_english_is_configured(monkeypatch):
    monkeypatch.setattr(image_evidence_analysis, "TESSERACT", "/usr/local/bin/tesseract")
    monkeypatch.setattr(
        image_evidence_analysis.settings,
        "image_evidence_ocr_languages",
        "eng",
    )
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_version", lambda: "tesseract 5.5.2")
    monkeypatch.setattr(image_evidence_analysis, "_tesseract_languages", lambda: ["eng", "osd"])

    health = image_evidence_analysis.analyzer_health()["ocr"]

    assert health["status"] == "available"
    assert health["requested_languages"] == ["eng"]
    assert health["effective_languages"] == ["eng"]
    assert health["language_argument"] == "eng"
    assert health["chinese_status"] == "not_requested"
    assert health["degraded_reason"] is None


def _video_ocr_status(status):
    return lambda _image: {
        "status": status,
        "analyzer": "fixture_ocr",
        "analyzer_version": "1.0",
        "degraded_reason": None if status == "analyzed" else f"fixture {status}",
        "evidence": [],
    }


def _motion_unsupported(_images, _metadata):
    return {
        "status": "unsupported",
        "analyzer": "fixture_motion",
        "analyzer_version": "none",
        "degraded_reason": "fixture unavailable",
        "samples": [],
    }


@pytest.mark.parametrize(
    "frame_statuses,expected",
    [
        (["analyzed", "analyzed"], "analyzed"),
        (["unsupported", "unsupported"], "unsupported"),
        (["analyzed", "unsupported"], "degraded"),
        (["analyzed", "degraded"], "degraded"),
        (["unsupported", "degraded"], "degraded"),
    ],
)
def test_frame_ocr_aggregate_status_matrix(frame_statuses, expected):
    image = _data_uri(Image.new("RGB", (20, 20), "white"))
    analyzers = iter(_video_ocr_status(status) for status in frame_statuses)

    def per_frame_ocr(source):
        return next(analyzers)(source)

    result = video_evidence_analysis.analyze_video_evidence(
        [image for _status in frame_statuses],
        [
            {
                "index": index,
                "absolute_timestamp_seconds": float(index),
                "source_segment_index": 1,
            }
            for index, _status in enumerate(frame_statuses, start=1)
        ],
        ocr_analyzer=per_frame_ocr,
        motion_analyzer=_motion_unsupported,
    )

    assert result["frame_ocr"]["status"] == expected


def test_cross_frame_ocr_tracks_keep_absolute_time_and_segment_boundaries():
    frames = [
        {"frame_index": 1, "timestamp_seconds": 10.0, "source_segment_index": 1,
         "bbox": {"x": .1, "y": .1, "width": .3, "height": .1}, "text": "SALE",
         "confidence": .9, "analyzer_source": "fixture", "analyzer_version": "1"},
        {"frame_index": 2, "timestamp_seconds": 12.0, "source_segment_index": 1,
         "bbox": {"x": .11, "y": .1, "width": .3, "height": .1}, "text": "SALE",
         "confidence": .8, "analyzer_source": "fixture", "analyzer_version": "1"},
        {"frame_index": 3, "timestamp_seconds": 30.0, "source_segment_index": 2,
         "bbox": {"x": .1, "y": .1, "width": .3, "height": .1}, "text": "SALE",
         "confidence": .7, "analyzer_source": "fixture", "analyzer_version": "1"},
    ]
    tracks = video_evidence_analysis.build_ocr_tracks(frames)

    assert len(tracks) == 2
    assert tracks[0]["start_seconds"] == 10.0
    assert tracks[0]["end_seconds"] == 12.0
    assert tracks[0]["frame_indices"] == [1, 2]
    assert tracks[1]["source_segment_index"] == 2


def test_video_ocr_rejects_low_confidence_and_isolated_short_fragments():
    image = _data_uri(Image.new("RGB", (40, 40), "white"))

    def noisy_ocr(_image):
        texts = [
            ("LANCOME", 0.96),
            ("PARIS", 0.83),
            ("coe", 0.97),
            ("ew", 0.98),
            ("OS", 0.99),
            ("~", 0.99),
            ("4", 0.99),
            ("BLURRED", 0.41),
        ]
        return {
            "status": "analyzed",
            "analyzer": "fixture_ocr",
            "analyzer_version": "1.0",
            "degraded_reason": None,
            "evidence": [
                {
                    "evidence_type": "ocr",
                    "evidence_text": text,
                    "confidence": confidence,
                    "bbox": {"x": 0.1, "y": 0.1 + index * 0.05, "width": 0.3, "height": 0.04},
                }
                for index, (text, confidence) in enumerate(texts)
            ],
        }

    result = video_evidence_analysis.analyze_video_evidence(
        [image],
        [{"index": 1, "absolute_timestamp_seconds": 2.0, "source_segment_index": 1}],
        ocr_analyzer=noisy_ocr,
        motion_analyzer=_motion_unsupported,
        semantic_analyzer=lambda _images, _metadata: video_evidence_analysis._semantic_failure(
            "unsupported", "fixture unavailable"
        ),
    )

    frame_ocr = result["frame_ocr"]
    assert [track["text"] for track in frame_ocr["tracks"]] == ["LANCOME", "PARIS"]
    assert [row["text"] for row in frame_ocr["observations"]] == ["LANCOME", "PARIS"]
    assert frame_ocr["evidence_count"] == 2
    assert frame_ocr["rejected_evidence_count"] == 6


def test_shot_attachment_does_not_promote_vlm_subtitle_claim_to_ocr():
    shots = [{
        "start_seconds": 10, "end_seconds": 12, "source_segment_index": 1,
        "ocr": "VLM claims subtitle", "visual": "product", "evidence_frame_indices": [1, 2],
    }]
    evidence = {
        "frame_ocr": {"tracks": []},
        "motion": {"samples": []},
    }
    attached = video_evidence_analysis.attach_evidence_to_shots(shots, evidence)

    assert attached[0]["ocr"] == ""
    assert attached[0]["vlm_text_description"] == "VLM claims subtitle"
    assert attached[0]["ocr_track_refs"] == []


def test_cv2_motion_classifies_synthetic_pan_zoom_and_static():
    health = video_evidence_analysis.analyzer_health()["motion"]
    assert health["status"] == "available"
    assert video_evidence_analysis.analyzer_health()["camera_motion"]["analyzer"] == (
        "opencv_lk_homography"
    )
    import cv2
    import numpy as np

    rng = np.random.default_rng(20260718)
    base = np.zeros((180, 240, 3), dtype=np.uint8)
    for y in range(15, 170, 25):
        for x in range(15, 230, 25):
            color = tuple(int(value) for value in rng.integers(80, 256, size=3))
            cv2.circle(base, (x, y), 5, color, -1)
            cv2.rectangle(base, (x - 7, y - 7), (x + 7, y + 7), color, 1)
    variants = {
        "pan": cv2.warpAffine(
            base, np.float32([[1, 0, 18], [0, 1, 0]]), (240, 180),
        ),
        "zoom": cv2.warpAffine(
            base, cv2.getRotationMatrix2D((120, 90), 0, 1.12), (240, 180),
        ),
        "static": base.copy(),
    }
    for expected, transformed in variants.items():
        result = video_evidence_analysis.cv2_motion_analysis(
            [Image.fromarray(base), Image.fromarray(transformed)],
            [
                {"frame_index": 1, "timestamp_seconds": 1, "source_segment_index": 1},
                {"frame_index": 2, "timestamp_seconds": 2, "source_segment_index": 1},
            ],
        )
        assert result["status"] == "analyzed"
        assert result["samples"][0]["camera"] == expected


def test_camera_motion_and_http_transition_remain_distinct_timestamped_evidence():
    image = _data_uri(Image.new("RGB", (20, 20), "white"))
    camera_motion = {
        "status": "analyzed",
        "analyzer": "fixture_opencv_motion",
        "analyzer_version": "4.0",
        "samples": [{
            "evidence_id": "motion-fixture-1",
            "start_seconds": 10.0,
            "end_seconds": 12.0,
            "source_segment_index": 1,
            "frame_indices": [1, 2],
            "camera": "pan",
            "camera_confidence": 0.91,
            "camera_scores": {"pan": 0.91, "tilt": 0.02, "zoom": 0.03, "static": 0.04},
            "background_motion_confidence": 0.88,
            "subject_motion_confidence": 0.12,
        }],
        "degraded_reason": None,
    }
    semantic = video_evidence_analysis._semantic_failure(
        "unsupported", "semantic provider unavailable"
    )
    result = video_evidence_analysis.analyze_video_evidence(
        [image, image],
        [
            {"index": 1, "absolute_timestamp_seconds": 10.0, "source_segment_index": 1},
            {"index": 2, "absolute_timestamp_seconds": 12.0, "source_segment_index": 1},
        ],
        ocr_analyzer=_video_ocr_status("unsupported"),
        motion_analyzer=lambda _images, _metadata: camera_motion,
        semantic_analyzer=lambda _images, _metadata: semantic,
    )

    assert result["camera_motion"] == result["motion"]
    assert result["camera_motion"]["analyzer"] == "fixture_opencv_motion"
    assert result["camera_motion"]["samples"][0]["start_seconds"] == 10.0
    assert result["camera_motion"]["samples"][0]["camera_confidence"] == 0.91
    assert result["transition"]["status"] == "unsupported"
    assert result["transition"]["events"] == []

    shot = video_evidence_analysis.attach_evidence_to_shots(
        [{"start_seconds": 10.0, "end_seconds": 12.0, "source_segment_index": 1}],
        result,
    )[0]
    assert shot["camera_motion_evidence_refs"] == ["motion-fixture-1"]
    assert shot["motion_evidence_refs"] == ["motion-fixture-1"]
    assert shot["transition_evidence_refs"] == []
    assert shot["analyzer_status"]["camera_motion"] == "analyzed"
    assert shot["analyzer_status"]["transition"] == "unsupported"


def test_unconfigured_video_semantic_provider_is_truthfully_unsupported(monkeypatch):
    monkeypatch.delenv("VIDEO_EVIDENCE_SEMANTIC_URL", raising=False)
    monkeypatch.setattr(video_evidence_analysis.settings, "video_evidence_semantic_url", "")
    images, frames = _semantic_frames()

    result = video_evidence_analysis.http_semantic_provider(images, frames)

    assert result["contract_version"] == "video-semantic-evidence.v1"
    for capability, evidence_key in {
        "subject_tracking": "tracks",
        "pose": "observations",
        "action": "events",
        "transition": "events",
    }.items():
        assert result[capability]["status"] == "unsupported"
        assert result[capability][evidence_key] == []


def test_video_semantic_provider_uses_loaded_settings_not_late_environment(monkeypatch):
    monkeypatch.setenv(
        "VIDEO_EVIDENCE_SEMANTIC_URL", "https://late-env.example.test/analyze"
    )
    monkeypatch.setenv("VIDEO_EVIDENCE_SEMANTIC_API_KEY", "late-env-key")
    monkeypatch.setenv("VIDEO_EVIDENCE_SEMANTIC_TIMEOUT_SECONDS", "99")
    _configure_semantic_provider(
        monkeypatch,
        url="https://settings-semantic.example.test/analyze",
        api_key="settings-key",
        timeout=37,
    )
    captured = {}

    def provider(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return _semantic_provider_payload()

    monkeypatch.setattr(video_evidence_analysis.gateway, "_request_json", provider)
    images, frames = _semantic_frames()
    result = video_evidence_analysis.http_semantic_provider(images, frames)

    assert result["subject_tracking"]["status"] == "analyzed"
    assert captured["url"] == "https://settings-semantic.example.test/analyze"
    assert captured["headers"] == {"Authorization": "Bearer settings-key"}
    assert captured["timeout"] == 37
    assert captured["trusted_hosts"] == settings.trusted_analyzer_host_list


@pytest.mark.parametrize(
    "field,value",
    [
        ("video_evidence_semantic_timeout_seconds", 0),
        ("video_evidence_semantic_timeout_seconds", 121),
        ("video_evidence_semantic_health_timeout_seconds", 0),
        ("video_evidence_semantic_health_timeout_seconds", 11),
        ("video_evidence_semantic_health_cache_ttl_seconds", 0),
        ("video_evidence_semantic_health_cache_ttl_seconds", 61),
    ],
)
def test_video_semantic_provider_timeout_configuration_is_bounded(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("video_evidence_semantic_api_key", "orphan-secret"),
        (
            "video_evidence_semantic_health_url",
            "https://semantic.example.test/health",
        ),
    ],
)
def test_runtime_rejects_dangling_video_semantic_config(monkeypatch, field, value):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "video_evidence_semantic_url", "")
    monkeypatch.setattr(settings, "video_evidence_semantic_api_key", "")
    monkeypatch.setattr(settings, "video_evidence_semantic_health_url", "")
    monkeypatch.setattr(settings, field, value)

    with pytest.raises(RuntimeError, match="VIDEO_EVIDENCE_SEMANTIC"):
        validate_runtime_config()


@pytest.mark.parametrize(
    "field,url",
    [
        ("video_evidence_semantic_url", "ftp://semantic.example.test/analyze"),
        (
            "video_evidence_semantic_url",
            "https://user:secret@semantic.example.test/analyze",
        ),
        (
            "video_evidence_semantic_health_url",
            "https://semantic.example.test/health#fragment",
        ),
    ],
)
def test_runtime_rejects_invalid_video_semantic_urls(monkeypatch, field, url):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(
        settings,
        "video_evidence_semantic_url",
        "https://semantic.example.test/analyze",
    )
    monkeypatch.setattr(settings, "video_evidence_semantic_api_key", "secret")
    monkeypatch.setattr(settings, "video_evidence_semantic_health_url", "")
    monkeypatch.setattr(settings, field, url)

    with pytest.raises(RuntimeError, match="VIDEO_EVIDENCE_SEMANTIC"):
        validate_runtime_config()


def test_video_semantic_provider_sends_absolute_frame_metadata_and_returns_stable_evidence(
    monkeypatch,
):
    video_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    _configure_semantic_provider(monkeypatch)
    captured = {}

    def provider(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return _semantic_provider_payload()

    monkeypatch.setattr(video_evidence_analysis.gateway, "_request_json", provider)
    images, frames = _semantic_frames()

    result = video_evidence_analysis.http_semantic_provider(images, frames)
    replay = video_evidence_analysis.http_semantic_provider(images, frames)

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/analyze")
    assert captured["headers"] == {"Authorization": "Bearer secret-key"}
    assert captured["timeout"] == 45
    assert captured["retries"] == 0
    assert captured["payload"]["contract_version"] == "video-semantic-evidence.v1"
    assert captured["payload"]["capabilities"] == [
        "subject_tracking", "pose", "action", "transition",
    ]
    assert captured["payload"]["frames"][0]["frame_index"] == 1
    assert captured["payload"]["frames"][0]["absolute_timestamp_seconds"] == 10.0
    assert captured["payload"]["frames"][0]["source_segment_index"] == 1
    assert captured["payload"]["frames"][0]["source_content_hash"] == "a" * 64
    assert captured["payload"]["frames"][0]["image_base64"]
    assert all(result[capability]["status"] == "analyzed" for capability in (
        "subject_tracking", "pose", "action", "transition",
    ))
    subject = result["subject_tracking"]["tracks"][0]
    pose = result["pose"]["observations"][0]
    action = result["action"]["events"][0]
    transition = result["transition"]["events"][0]
    assert subject["evidence_id"].startswith("subject-track-")
    assert subject["provider_evidence_id"] == "provider-subject-1"
    assert pose["subject_evidence_ref"] == subject["evidence_id"]
    assert action["subject_evidence_ref"] == subject["evidence_id"]
    assert transition["evidence_id"].startswith("transition-")
    assert replay["subject_tracking"]["tracks"][0]["evidence_id"] == subject["evidence_id"]
    assert replay["pose"]["observations"][0]["evidence_id"] == pose["evidence_id"]

    attached = video_evidence_analysis.attach_evidence_to_shots(
        [{
            "start_seconds": 10.0,
            "end_seconds": 12.0,
            "source_segment_index": 1,
            "ocr": "VLM subtitle",
            "action": "VLM action prose",
            "transition": "VLM transition prose",
        }],
        {
            "frame_ocr": {"status": "unsupported", "tracks": []},
            "motion": {"status": "unsupported", "samples": []},
            **{
                capability: result[capability]
                for capability in video_evidence_analysis.SEMANTIC_CAPABILITIES
            },
        },
    )[0]
    assert attached["subject_track_refs"] == [subject["evidence_id"]]
    assert attached["pose_evidence_refs"] == [pose["evidence_id"]]
    assert attached["action_evidence_refs"] == [action["evidence_id"]]
    assert attached["transition_evidence_refs"] == [transition["evidence_id"]]
    assert attached["action"] == "VLM action prose"
    assert attached["transition"] == "VLM transition prose"


@pytest.mark.parametrize(
    "malformation,capability",
    [
        ("unsafe_evidence_id", "subject_tracking"),
        ("nonfinite_bbox", "subject_tracking"),
        ("wrong_pose_time", "pose"),
        ("string_confidence", "pose"),
        ("unknown_subject_ref", "pose"),
        ("action_outside_sample_range", "action"),
        ("duplicate_cross_capability_id", "pose"),
        ("reversed_transition_frames", "transition"),
        ("nonfinite_transition_confidence", "transition"),
    ],
)
def test_video_semantic_provider_rejects_malicious_or_malformed_evidence(
    monkeypatch, malformation, capability,
):
    _configure_semantic_provider(monkeypatch)
    payload = deepcopy(_semantic_provider_payload())
    subject = payload["capabilities"]["subject_tracking"]["evidence"][0]
    pose = payload["capabilities"]["pose"]["evidence"][0]
    action = payload["capabilities"]["action"]["evidence"][0]
    transition = payload["capabilities"]["transition"]["evidence"][0]
    if malformation == "unsafe_evidence_id":
        subject["evidence_id"] = "<script>alert(1)</script>"
    elif malformation == "nonfinite_bbox":
        subject["observations"][0]["bbox"]["x"] = math.nan
    elif malformation == "wrong_pose_time":
        pose["absolute_timestamp_seconds"] = 11.0
    elif malformation == "string_confidence":
        pose["confidence"] = "0.92"
    elif malformation == "unknown_subject_ref":
        pose["subject_evidence_id"] = "provider-subject-missing"
    elif malformation == "action_outside_sample_range":
        action["start_seconds"] = 9.0
    elif malformation == "duplicate_cross_capability_id":
        pose["evidence_id"] = "provider-subject-1"
    elif malformation == "reversed_transition_frames":
        transition["before_frame_index"] = 2
        transition["after_frame_index"] = 1
    else:
        transition["confidence"] = math.inf

    monkeypatch.setattr(
        video_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: payload,
    )
    images, frames = _semantic_frames()
    result = video_evidence_analysis.http_semantic_provider(images, frames)

    evidence_key = video_evidence_analysis._SEMANTIC_EVIDENCE_KEYS[capability]
    assert result[capability]["status"] == "degraded"
    assert result[capability][evidence_key] == []
    assert result[capability]["degraded_reason"]


def test_video_semantic_capabilities_degrade_independently_and_empty_evidence_is_partial(
    monkeypatch,
):
    _configure_semantic_provider(monkeypatch)
    payload = deepcopy(_semantic_provider_payload())
    payload["capabilities"]["action"]["evidence"][0]["confidence"] = 5.0
    payload["capabilities"]["transition"]["evidence"] = []
    monkeypatch.setattr(
        video_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: payload,
    )
    images, frames = _semantic_frames()

    result = video_evidence_analysis.http_semantic_provider(images, frames)

    assert result["subject_tracking"]["status"] == "analyzed"
    assert result["pose"]["status"] == "analyzed"
    assert result["action"]["status"] == "degraded"
    assert result["action"]["events"] == []
    assert result["transition"]["status"] == "partial"
    assert result["transition"]["events"] == []
    assert "未返回" in result["transition"]["degraded_reason"]


def test_video_semantic_provider_network_failure_degrades_without_fabricating_evidence(
    monkeypatch,
):
    _configure_semantic_provider(monkeypatch)
    monkeypatch.setattr(
        video_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    images, frames = _semantic_frames()

    result = video_evidence_analysis.http_semantic_provider(images, frames)

    for capability, evidence_key in video_evidence_analysis._SEMANTIC_EVIDENCE_KEYS.items():
        assert result[capability]["status"] == "degraded"
        assert result[capability][evidence_key] == []
        assert "provider unavailable" in result[capability]["degraded_reason"]


def test_video_semantic_health_probe_is_bounded_and_reports_capability_statuses(monkeypatch):
    video_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    clock = [0.0]
    monkeypatch.setattr(video_evidence_analysis.time, "monotonic", lambda: clock[0])
    _configure_semantic_provider(
        monkeypatch,
        api_key="health-secret",
        health_url="https://semantic.example.test/health",
        health_timeout=99,
        health_ttl=2,
    )
    captured = {}

    def healthy(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {
            "contract_version": "video-semantic-evidence.v1",
            "ok": True,
            "analyzer": "fixture_video_semantic",
            "analyzer_version": "2026.07",
            "capability_statuses": {
                "subject_tracking": "available",
                "pose": "available",
                "action": "degraded",
                "transition": "unsupported",
            },
        }

    monkeypatch.setattr(video_evidence_analysis.gateway, "_request_json", healthy)
    health = video_evidence_analysis.analyzer_health()

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/health")
    assert captured["payload"] is None
    assert captured["headers"] == {"Authorization": "Bearer health-secret"}
    assert captured["timeout"] == 10
    assert captured["retries"] == 0
    assert health["semantic_provider"]["status"] == "available"
    assert health["semantic_provider"]["verification_status"] == "health_probe"
    assert health["subject_tracking"]["status"] == "available"
    assert health["pose"]["status"] == "available"
    assert health["action"]["status"] == "degraded"
    assert health["transition"]["status"] == "unsupported"

    monkeypatch.setattr(
        video_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("health unavailable")),
    )
    clock[0] = 3.0
    degraded = video_evidence_analysis.analyzer_health()
    assert degraded["semantic_provider"]["status"] == "degraded"
    assert degraded["semantic_provider"]["verification_status"] == "health_probe_failed"
    assert degraded["semantic_provider"]["last_success_at"]
    assert "health unavailable" in degraded["semantic_provider"]["degraded_reason"]


def test_video_semantic_health_probe_caches_success_and_failure_until_ttl(monkeypatch):
    video_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    clock = [100.0]
    monkeypatch.setattr(video_evidence_analysis.time, "monotonic", lambda: clock[0])
    _configure_semantic_provider(
        monkeypatch,
        health_url="https://semantic.example.test/health",
        health_ttl=5,
    )
    calls: list[float] = []

    def healthy(*_args, **_kwargs):
        calls.append(clock[0])
        return _semantic_health_payload()

    monkeypatch.setattr(video_evidence_analysis.gateway, "_request_json", healthy)

    first = video_evidence_analysis._semantic_provider_health()
    clock[0] = 104.999
    second = video_evidence_analysis._semantic_provider_health()
    clock[0] = 105.001
    third = video_evidence_analysis._semantic_provider_health()

    assert first == second
    assert third["status"] == "available"
    assert calls == [100.0, 105.001]

    video_evidence_analysis._PROVIDER_HEALTH_CACHE.clear()
    calls.clear()
    monkeypatch.setattr(
        video_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: (
            calls.append(clock[0]),
            (_ for _ in ()).throw(RuntimeError("offline")),
        )[1],
    )
    failed = video_evidence_analysis._semantic_provider_health()
    cached_failed = video_evidence_analysis._semantic_provider_health()
    assert failed == cached_failed
    assert failed["verification_status"] == "health_probe_failed"
    assert calls == [105.001]


def test_concurrent_video_semantic_health_requests_share_one_probe(monkeypatch):
    video_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    _configure_semantic_provider(
        monkeypatch, health_url="https://semantic.example.test/health"
    )
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

    monkeypatch.setattr(
        video_evidence_analysis, "_PROVIDER_HEALTH_PROBE_LOCK", ObservedLock()
    )

    def healthy(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return _semantic_health_payload()

    monkeypatch.setattr(video_evidence_analysis.gateway, "_request_json", healthy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(video_evidence_analysis._semantic_provider_health)
        assert entered.wait(timeout=2)
        second = executor.submit(video_evidence_analysis._semantic_provider_health)
        assert second_lock_attempt.wait(timeout=2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert [result["status"] for result in results] == ["available", "available"]
    assert calls == [1]


def test_video_semantic_analysis_invalidates_cached_failed_health(monkeypatch):
    video_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    _configure_semantic_provider(
        monkeypatch, health_url="https://semantic.example.test/health"
    )
    ready = [False]
    calls = {"GET": 0, "POST": 0}

    def provider(method, _url, **_kwargs):
        calls[method] += 1
        if method == "POST":
            ready[0] = True
            return _semantic_provider_payload()
        if not ready[0]:
            raise RuntimeError("not ready")
        return _semantic_health_payload()

    monkeypatch.setattr(video_evidence_analysis.gateway, "_request_json", provider)
    failed = video_evidence_analysis._semantic_provider_health()
    images, frames = _semantic_frames()
    analyzed = video_evidence_analysis.http_semantic_provider(images, frames)
    recovered = video_evidence_analysis._semantic_provider_health()

    assert failed["verification_status"] == "health_probe_failed"
    assert analyzed["subject_tracking"]["status"] == "analyzed"
    assert recovered["verification_status"] == "health_probe"
    assert calls == {"GET": 2, "POST": 1}


@pytest.mark.parametrize(
    "mutation",
    [
        {"contract_version": "wrong.v1"},
        {"analyzer": {"unsafe": True}},
        {"analyzer_version": "bad\nversion"},
        {"capability_statuses": {"subject_tracking": "available"}},
        {"capability_statuses": {
            "subject_tracking": "available",
            "pose": "available",
            "action": "partial",
            "transition": "available",
        }},
    ],
)
def test_video_semantic_health_rejects_invalid_contract_metadata(monkeypatch, mutation):
    video_evidence_analysis._PROVIDER_LAST_SUCCESS.clear()
    _configure_semantic_provider(
        monkeypatch, health_url="https://semantic.example.test/health"
    )
    payload = _semantic_health_payload()
    payload.update(mutation)
    monkeypatch.setattr(
        video_evidence_analysis.gateway,
        "_request_json",
        lambda *_args, **_kwargs: payload,
    )

    result = video_evidence_analysis._semantic_provider_health()

    assert result["status"] == "degraded"
    assert result["verification_status"] == "health_probe_failed"
