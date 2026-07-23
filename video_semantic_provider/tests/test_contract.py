from __future__ import annotations

import base64
import io
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from video_semantic_provider.app.config import ProviderSettings
from video_semantic_provider.app.main import create_app


def _frame_base64(*, offset: int, color: str = "white") -> str:
    image = Image.new("RGB", (96, 64), color)
    ImageDraw.Draw(image).rectangle((offset, 20, offset + 18, 40), fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _frame(
    index: int, timestamp: float, *, offset: int, color: str = "white"
) -> dict[str, Any]:
    return {
        "frame_index": index,
        "absolute_timestamp_seconds": timestamp,
        "source_segment_index": 1,
        "source_content_hash": f"{index:064x}",
        "width": 96,
        "height": 64,
        "image_media_type": "image/png",
        "image_base64": _frame_base64(offset=offset, color=color),
    }


def _body(frames: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contract_version": "video-semantic-evidence.v1",
        "capabilities": ["subject_tracking", "pose", "action", "transition"],
        "frames": frames,
    }


def test_health_and_analysis_follow_video_semantic_contract() -> None:
    app = create_app(settings=ProviderSettings())
    with TestClient(app) as client:
        health = client.get("/health")
        response = client.post(
            "/v1/video-semantic/analyze",
            json=_body(
                [
                    _frame(1, 0.0, offset=10),
                    _frame(2, 1.0, offset=24),
                    _frame(3, 2.0, offset=38),
                    _frame(4, 3.0, offset=52, color="navy"),
                ]
            ),
        )

    assert health.status_code == 200
    assert health.json()["contract_version"] == "video-semantic-evidence.v1"
    assert health.json()["capability_statuses"] == {
        "subject_tracking": "available",
        "pose": "unsupported",
        "action": "unsupported",
        "transition": "available",
    }
    payload = response.json()
    assert payload["contract_version"] == "video-semantic-evidence.v1"
    assert payload["capabilities"]["pose"] == {
        "status": "unsupported",
        "evidence": [],
        "degraded_reason": "This provider does not include a pose estimation model",
    }
    assert payload["capabilities"]["action"]["status"] == "unsupported"
    assert payload["capabilities"]["subject_tracking"]["status"] == "analyzed"
    tracks = payload["capabilities"]["subject_tracking"]["evidence"]
    assert tracks
    for track in tracks:
        assert track["label"] == "moving_subject"
        assert len(track["observations"]) >= 2
    assert payload["capabilities"]["transition"]["status"] == "analyzed"
    assert (
        payload["capabilities"]["transition"]["evidence"][0]["label"]
        == "visual_discontinuity"
    )


def test_bearer_auth_and_bad_contract_are_rejected() -> None:
    app = create_app(settings=ProviderSettings(api_key="provider-secret"))
    with TestClient(app) as client:
        denied = client.post(
            "/v1/video-semantic/analyze", json=_body([_frame(1, 0, offset=1)])
        )
        bad_contract = client.post(
            "/v1/video-semantic/analyze",
            json={**_body([_frame(1, 0, offset=1)]), "contract_version": "wrong.v1"},
            headers={"Authorization": "Bearer provider-secret"},
        )
        accepted = client.get(
            "/health", headers={"Authorization": "Bearer provider-secret"}
        )

    assert denied.status_code == 401
    assert bad_contract.status_code == 422
    assert accepted.status_code == 200


def test_rejects_noncanonical_capabilities_and_mismatched_dimensions() -> None:
    app = create_app(settings=ProviderSettings())
    bad_dimensions = _frame(1, 0, offset=1)
    bad_dimensions["width"] = 97
    with TestClient(app) as client:
        capabilities = client.post(
            "/v1/video-semantic/analyze",
            json={**_body([_frame(1, 0, offset=1)]), "capabilities": ["transition"]},
        )
        dimensions = client.post(
            "/v1/video-semantic/analyze", json=_body([bad_dimensions])
        )

    assert capabilities.status_code == 422
    assert dimensions.status_code == 422
