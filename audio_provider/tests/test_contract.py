from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from audio_provider.app.config import ProviderSettings
from audio_provider.app.main import create_app


class FakeEngine:
    def __init__(self, *, available: bool = True, fail: bool = False):
        self.available = available
        self.fail = fail
        self.warmed = False
        self.calls: list[dict[str, Any]] = []

    def warmup(self) -> None:
        self.warmed = True

    def health(self) -> dict[str, Any]:
        return {
            "status": "available" if self.available else "degraded",
            "analyzer": "faster-whisper",
            "analyzer_version": "fixture-1.0",
            "degraded_reason": None if self.available else "fixture model unavailable",
        }

    def transcribe(
        self, audio_path: Path, *, language: str | None, prompt: str | None
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "path": audio_path,
                "path_exists": audio_path.exists(),
                "language": language,
                "prompt": prompt,
            }
        )
        if self.fail:
            raise RuntimeError("sensitive inference failure")
        return {
            "text": "你好 世界",
            "language": "zh",
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.25, "text": "你好"},
                {"id": 1, "start": 1.25, "end": 2.5, "text": "世界"},
            ],
        }


def _files() -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("audio.mp3", b"fixture-audio", "audio/mpeg")}


def _data(**extra: str) -> dict[str, str]:
    return {
        "model": "small",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
        **extra,
    }


def test_health_reports_backend_audio_evidence_contract_and_explicit_speaker_gap() -> (
    None
):
    engine = FakeEngine()
    app = create_app(settings=ProviderSettings(eager_load=True), engine=engine)
    with TestClient(app) as client:
        response = client.get("/health")

    assert engine.warmed is True
    assert response.status_code == 200
    assert response.json() == {
        "contract_version": "audio-evidence.v1",
        "status": "ready",
        "ok": True,
        "analyzer": "faster-whisper",
        "analyzer_version": "fixture-1.0",
        "provider_model": "small",
        "capability_statuses": {"asr": "available", "speaker": "unsupported"},
        "degraded_reason": None,
    }


def test_openai_transcription_contract_returns_timestamped_segments_without_speaker_inference() -> (
    None
):
    engine = FakeEngine()
    app = create_app(settings=ProviderSettings(eager_load=False), engine=engine)
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data=_data(language="ZH", prompt="品牌名不要翻译"),
            files=_files(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "text": "你好 世界",
        "language": "zh",
        "segments": [
            {"id": 0, "start": 0.0, "end": 1.25, "text": "你好"},
            {"id": 1, "start": 1.25, "end": 2.5, "text": "世界"},
        ],
        "capability_statuses": {"asr": "available", "speaker": "unsupported"},
    }
    assert engine.calls == [
        {
            "path": engine.calls[0]["path"],
            "path_exists": True,
            "language": "zh",
            "prompt": "品牌名不要翻译",
        }
    ]
    assert not engine.calls[0]["path"].exists()
    assert "speaker_id" not in response.text


def test_bearer_auth_and_model_binding_are_enforced() -> None:
    app = create_app(
        settings=ProviderSettings(api_key="provider-secret", eager_load=False),
        engine=FakeEngine(),
    )
    with TestClient(app) as client:
        unauthorized_health = client.get("/health")
        unauthorized = client.post(
            "/v1/audio/transcriptions", data=_data(), files=_files()
        )
        wrong_model = client.post(
            "/v1/audio/transcriptions",
            data=_data(model="large-v3"),
            files=_files(),
            headers={"Authorization": "Bearer provider-secret"},
        )
        authorized = client.post(
            "/v1/audio/transcriptions",
            data=_data(),
            files=_files(),
            headers={"Authorization": "Bearer provider-secret"},
        )

    assert unauthorized_health.status_code == 401
    assert unauthorized.status_code == 401
    assert wrong_model.status_code == 400
    assert authorized.status_code == 200


def test_model_failure_is_a_sanitized_service_unavailable_response() -> None:
    app = create_app(
        settings=ProviderSettings(eager_load=False), engine=FakeEngine(fail=True)
    )
    with TestClient(app) as client:
        response = client.post("/v1/audio/transcriptions", data=_data(), files=_files())

    assert response.status_code == 503
    assert response.json()["detail"] == "local transcription failed"
    assert "sensitive inference failure" not in response.text
