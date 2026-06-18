"""Real-cost accounting (gateway_calls) + video keyframe graceful fallback."""
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import CreditTransaction, GatewayCall
from app.services import gateway, video_frames


def test_reverse_logs_gateway_call(client, make_user, auth):
    make_user("13900000030", balance=1000)
    h = auth("13900000030")
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        rows = db.query(GatewayCall).filter(GatewayCall.kind == "reverse").all()
        assert rows, "a reverse call must be logged for real-cost accounting"
    finally:
        db.close()


def test_reverse_charges_configured_vision_cost(client, make_user, auth):
    uid = make_user("13900000035", balance=1000, admin=True)
    h = auth("13900000035")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text

    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 200, r.text

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 993
    db = SessionLocal()
    try:
        tx = db.query(CreditTransaction).filter(
            CreditTransaction.user_id == uid,
            CreditTransaction.type == "consume",
            CreditTransaction.biz_type == "reverse",
        ).one()
        assert tx.change == -7
    finally:
        db.close()


def test_reverse_refunds_configured_cost_on_gateway_failure(client, make_user, auth, monkeypatch):
    make_user("13900000036", balance=1000, admin=True)
    h = auth("13900000036")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text

    def fail(*_args, **_kwargs):
        raise gateway.GatewayError("vision failed")

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fail)
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 502
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000


def test_reverse_refunds_configured_cost_on_unexpected_gateway_error(client, make_user, auth, monkeypatch):
    make_user("13900000038", balance=1000, admin=True)
    h = auth("13900000038")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text

    def fail(*_args, **_kwargs):
        raise RuntimeError("bad response shape")

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fail)
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 502
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000


def test_admin_report_includes_reverse_model_call_spend(client, make_user, auth):
    make_user("13900000037", balance=1000, admin=True)
    h = auth("13900000037")

    r = client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 200, r.text

    report = client.get("/api/admin/usage/report", headers=h).json()
    row = next(x for x in report["per_user"] if x["phone"] == "13900000037")
    assert row["spend_credits"] == 7


def test_image_generation_logs_gateway_call(client, make_user, auth):
    make_user("13900000031", balance=1000)
    h = auth("13900000031")
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "category": "image", "stage": "preview", "instruction": "x",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        rows = db.query(GatewayCall).filter(GatewayCall.kind == "image").all()
        assert rows, "an image generation must be logged"
    finally:
        db.close()


def test_failed_image_generation_logs_gateway_call(client, make_user, auth, monkeypatch):
    make_user("13900000034", balance=1000)
    h = auth("13900000034")

    def fail(*_args, **_kwargs):
        raise gateway.GatewayError("bad image request")

    monkeypatch.setattr("app.services.gateway.gen_image", fail)
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "category": "image", "stage": "preview", "instruction": "x",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        rows = db.query(GatewayCall).filter(
            GatewayCall.kind == "image",
            GatewayCall.status == "failed",
        ).all()
        assert rows
        assert "bad image request" in rows[-1].detail["error"]
    finally:
        db.close()


def test_video_reverse_uses_fallback_in_mock(client, make_user, auth):
    make_user("13900000032", balance=1000)
    h = auth("13900000032")
    # mock mode: keyframe sampling is skipped; the cover (fallback_image) is used
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "http://x/clip.mp4", "target": "video",
        "fallback_image": "http://x/cover.jpg",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert "主体" in r.json()["structured"]


def test_video_reverse_charges_per_reference_frame(client, make_user, auth, monkeypatch):
    make_user("13900000039", balance=1000, admin=True)
    h = auth("13900000039")
    assert client.put("/api/admin/models", json={
        "use": "vision",
        "model_id": "mock-vision",
        "cost_credits": 7,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    monkeypatch.setattr("app.config.settings.mock_mode", False)
    monkeypatch.setattr("app.config.settings.gateway_base_url", "https://gateway.test")
    monkeypatch.setattr("app.config.settings.gateway_api_key", "sk-test")
    monkeypatch.setattr(video_frames, "available", lambda: True)
    monkeypatch.setattr(video_frames, "sample_keyframes", lambda *_a, **_k: [b"a", b"b", b"c"])
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda refs, *_a, **_k: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": None,
            "latency_ms": 1,
            "ref_count": len(refs),
        },
    )
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "http://x/clip.mp4", "target": "video",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 979


def test_video_reverse_rejects_video_url_for_image_target(client, make_user, auth):
    make_user("13900000033", balance=1000)
    h = auth("13900000033")
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "http://x/clip.mp4", "target": "image",
    }, headers=h)
    assert r.status_code == 400
    assert "反推只支持图片素材" in r.text


def test_keyframe_sampling_blocked_url_returns_empty():
    # SSRF-blocked / unreachable source must degrade gracefully, never raise
    assert video_frames.sample_keyframes("http://127.0.0.1/x.mp4", n=2) == []


def test_keyframe_sampling_busy_returns_empty(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames.settings, "reverse_video_acquire_timeout_seconds", 0)
    assert video_frames._SAMPLE_SEMAPHORE.acquire(blocking=False)
    try:
        monkeypatch.setattr(
            video_frames,
            "_download_capped",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not download")),
        )
        assert video_frames.sample_keyframes("http://x/clip.mp4", n=2) == []
    finally:
        video_frames._SAMPLE_SEMAPHORE.release()


def test_keyframe_duration_uses_ffprobe(monkeypatch):
    monkeypatch.setattr(video_frames, "FFPROBE", "/usr/bin/ffprobe")

    def fake_run(args, **_kwargs):
        assert "-show_entries" in args
        return SimpleNamespace(stdout="8.5\n")

    monkeypatch.setattr(video_frames.subprocess, "run", fake_run)
    assert video_frames._duration_seconds("/tmp/clip.mp4") == 8.5
