from __future__ import annotations

import base64
import io
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image

from evidence_provider.app.config import ProviderSettings
from evidence_provider.app.main import create_app


class FakeEngine:
    def __init__(self, *, fail: bool = False, available: bool = True):
        self.fail = fail
        self.available = available
        self.warmed = False

    def warmup(self) -> None:
        self.warmed = True

    def health(self, capability: str) -> dict[str, Any]:
        return {
            "status": "available" if self.available else "degraded",
            "analyzer": f"fixture_{capability}",
            "analyzer_version": "1.0.0",
            "degraded_reason": None if self.available else "fixture unavailable",
        }

    def analyze(self, image: Image.Image, capability: str) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("sensitive fixture detail")
        assert image.mode == "RGB"
        if capability == "segmenter":
            return [
                {
                    "label": "person",
                    "text": "person",
                    "confidence": 0.91,
                    "polygon": [
                        {"x": 0.1, "y": 0.1},
                        {"x": 0.8, "y": 0.1},
                        {"x": 0.8, "y": 0.8},
                        {"x": 0.1, "y": 0.8},
                    ],
                }
            ]
        return [
            {
                "label": "person",
                "text": "person",
                "confidence": 0.93,
                "bbox": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.6},
            }
        ]


def _image_base64() -> str:
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "white").save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _body(capability: str = "detector") -> dict[str, Any]:
    return {
        "contract_version": "region-analyzer.v1",
        "capability": capability,
        "image_base64": _image_base64(),
    }


def test_health_and_analysis_return_region_analyzer_contract() -> None:
    engine = FakeEngine()
    app = create_app(settings=ProviderSettings(eager_load=True), engine=engine)
    with TestClient(app) as client:
        health = client.get("/health/detector")
        response = client.post("/v1/region/analyze", json=_body())

    assert engine.warmed is True
    assert health.status_code == 200
    assert health.json() == {
        "contract_version": "region-analyzer.v1",
        "capability": "detector",
        "status": "ready",
        "ok": True,
        "analyzer": "fixture_detector",
        "analyzer_version": "1.0.0",
        "degraded_reason": None,
    }
    assert response.status_code == 200
    assert response.json()["status"] == "analyzed"
    assert response.json()["evidence"][0]["bbox"]["width"] == 0.5


def test_bearer_auth_protects_health_and_analysis() -> None:
    app = create_app(
        settings=ProviderSettings(api_key="provider-secret", eager_load=False),
        engine=FakeEngine(),
    )
    with TestClient(app) as client:
        unauthorized = client.post("/v1/region/analyze", json=_body())
        authorized = client.post(
            "/v1/region/analyze",
            json=_body("segmenter"),
            headers={"Authorization": "Bearer provider-secret"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["evidence"][0]["polygon"][2] == {"x": 0.8, "y": 0.8}


def test_invalid_image_and_contract_are_rejected_before_inference() -> None:
    app = create_app(settings=ProviderSettings(eager_load=False), engine=FakeEngine())
    with TestClient(app) as client:
        invalid_image = client.post(
            "/v1/region/analyze",
            json={**_body(), "image_base64": "not-base64"},
        )
        invalid_contract = client.post(
            "/v1/region/analyze",
            json={**_body(), "contract_version": "region-analyzer.v0"},
        )

    assert invalid_image.status_code == 422
    assert invalid_contract.status_code == 422


def test_inference_failure_degrades_without_leaking_exception_details() -> None:
    app = create_app(
        settings=ProviderSettings(eager_load=False),
        engine=FakeEngine(fail=True),
    )
    with TestClient(app) as client:
        response = client.post("/v1/region/analyze", json=_body())

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["evidence"] == []
    assert "sensitive fixture detail" not in response.text


def test_degraded_model_health_is_not_reported_ready() -> None:
    app = create_app(
        settings=ProviderSettings(eager_load=False),
        engine=FakeEngine(available=False),
    )
    with TestClient(app) as client:
        response = client.get("/health/segmenter")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["ok"] is False
