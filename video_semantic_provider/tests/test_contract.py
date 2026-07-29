from __future__ import annotations

import base64
import io
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from video_semantic_provider.app.config import ProviderSettings
from video_semantic_provider.app.inference import (
    MediaPipePoseAdapter,
    OpenCvSemanticEngine,
)
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


class FakePoseAdapter:
    """Derives one keypoint from the dark subject pixels, or a fixed miss."""

    def __init__(self, *, status: str = "available", off_subject: bool = False):
        self.status = status
        self.off_subject = off_subject
        self.calls = 0

    def health(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "analyzer": "fixture-pose",
            "analyzer_version": "fixture-1.0",
            "degraded_reason": None
            if self.status == "available"
            else "fixture pose model unavailable",
        }

    def estimate(self, image: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if self.off_subject:
            return [{"name": "nose", "x": 0.99, "y": 0.02, "confidence": 0.9}]
        dark = (image.sum(axis=2) < 120).nonzero()
        if not dark[0].size:
            return []
        y = float(dark[0].mean()) / image.shape[0]
        x = float(dark[1].mean()) / image.shape[1]
        return [{"name": "nose", "x": round(x, 6), "y": round(y, 6), "confidence": 0.9}]


def _pose_frames() -> list[dict[str, Any]]:
    return [
        _frame(1, 0.0, offset=10),
        _frame(2, 1.0, offset=24),
        _frame(3, 2.0, offset=38),
    ]


def test_enabled_pose_adapter_attaches_keypoints_to_subject_tracks() -> None:
    settings = ProviderSettings(pose_enabled=True)
    adapter = FakePoseAdapter()
    app = create_app(
        settings=settings,
        engine=OpenCvSemanticEngine(settings, pose_adapter=adapter),
    )
    with TestClient(app) as client:
        health = client.get("/health")
        response = client.post("/v1/video-semantic/analyze", json=_body(_pose_frames()))

    assert health.json()["capability_statuses"]["pose"] == "available"
    payload = response.json()
    tracks = payload["capabilities"]["subject_tracking"]["evidence"]
    assert tracks
    pose = payload["capabilities"]["pose"]
    assert pose["status"] == "analyzed"
    assert pose["degraded_reason"] is None
    assert adapter.calls >= 1
    track_ids = {track["evidence_id"] for track in tracks}
    for row in pose["evidence"]:
        assert row["subject_evidence_id"] in track_ids
        assert row["keypoints"] == [
            {
                "name": "nose",
                "x": row["keypoints"][0]["x"],
                "y": row["keypoints"][0]["y"],
                "confidence": 0.9,
            }
        ]
        assert 0 <= row["keypoints"][0]["x"] <= 1
        assert row["evidence_id"].startswith("pose-")


def test_pose_that_does_not_cover_a_track_is_not_attached() -> None:
    settings = ProviderSettings(pose_enabled=True)
    engine = OpenCvSemanticEngine(
        settings, pose_adapter=FakePoseAdapter(off_subject=True)
    )
    app = create_app(settings=settings, engine=engine)
    with TestClient(app) as client:
        response = client.post("/v1/video-semantic/analyze", json=_body(_pose_frames()))

    pose = response.json()["capabilities"]["pose"]
    assert pose["status"] == "partial"
    assert pose["evidence"] == []
    assert "No pose matching a tracked subject" in pose["degraded_reason"]


def test_degraded_pose_adapter_reports_degraded_without_evidence() -> None:
    settings = ProviderSettings(pose_enabled=True)
    engine = OpenCvSemanticEngine(
        settings, pose_adapter=FakePoseAdapter(status="degraded")
    )
    app = create_app(settings=settings, engine=engine)
    with TestClient(app) as client:
        health = client.get("/health")
        response = client.post("/v1/video-semantic/analyze", json=_body(_pose_frames()))

    assert health.json()["capability_statuses"]["pose"] == "degraded"
    pose = response.json()["capabilities"]["pose"]
    assert pose["status"] == "degraded"
    assert pose["evidence"] == []
    assert pose["degraded_reason"] == "fixture pose model unavailable"


def test_mediapipe_pose_adapter_degrades_honestly_without_dependency() -> None:
    # 本仓库测试环境未安装 mediapipe：适配层必须降级而不是伪造关键点。
    adapter = MediaPipePoseAdapter(ProviderSettings(pose_enabled=True))
    health = adapter.health()
    if health["status"] == "available":  # pragma: no cover - dependency present
        return
    assert health["status"] == "degraded"
    assert health["degraded_reason"] == "mediapipe is not installed"


def test_engine_failure_with_pose_enabled_reports_pose_degraded() -> None:
    class ExplodingEngine:
        def health(self) -> dict[str, Any]:
            return {"status": "available"}

        def analyze(self, frames: list[dict[str, Any]]) -> dict[str, Any]:
            raise RuntimeError("sensitive engine failure")

    app = create_app(
        settings=ProviderSettings(pose_enabled=True), engine=ExplodingEngine()
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/video-semantic/analyze", json=_body([_frame(1, 0, offset=1)])
        )

    payload = response.json()
    assert payload["capabilities"]["pose"]["status"] == "degraded"
    assert payload["capabilities"]["action"]["status"] == "unsupported"
    assert "sensitive engine failure" not in response.text


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
