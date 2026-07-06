"""Real-cost accounting (gateway_calls) + video keyframe graceful fallback."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import AuditLog, CreditTransaction, GatewayCall, GenTask, User
from app.services import audit, credits, gateway, usage, video_analysis, video_frames


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

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 998
    db = SessionLocal()
    try:
        tx = db.query(CreditTransaction).filter(
            CreditTransaction.user_id == uid,
            CreditTransaction.type == "consume",
            CreditTransaction.biz_type == "reverse",
        ).one()
        assert tx.change == -2
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
    make_user("13900009040", balance=1000, admin=True)
    h = auth("13900009040")

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
    assert row["spend_credits"] == 2


def test_usage_report_does_not_count_refunds_as_negative_spend(client, make_user, auth):
    phone = "13900009041"
    uid = make_user(phone, balance=1000, admin=True)
    h = auth(phone)
    db = SessionLocal()
    try:
        credits.consume(db, uid, 10, biz_type="reverse", biz_ref=1, note="reverse")
        credits.refund_consumed(db, uid, 10, biz_type="reverse", biz_ref=1, note="manual refund")
    finally:
        db.close()

    report = client.get("/api/admin/usage/report", headers=h).json()
    row = next(x for x in report["per_user"] if x["phone"] == phone)
    assert row["spend_credits"] == 10


def test_usage_report_attributes_generation_spend_to_finished_date(client, make_user, auth):
    uid = make_user("13900000139", balance=1000, admin=True)
    h = auth("13900000139")
    freeze_dt = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    finish_dt = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        user.balance_credits = 990
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "x"},
            params={"n": 1},
            cost_frozen=10,
            cost_settled=6,
            finished_at=finish_dt,
        ))
        db.add(CreditTransaction(
            user_id=uid,
            type="freeze",
            change=-10,
            balance_after=990,
            biz_type="gen_task",
            biz_ref=999,
            created_at=freeze_dt,
        ))
        db.add(CreditTransaction(
            user_id=uid,
            type="settle",
            change=4,
            balance_after=994,
            biz_type="gen_task",
            biz_ref=999,
            created_at=finish_dt,
        ))
        db.commit()
    finally:
        db.close()

    jan1 = client.get(
        "/api/admin/usage/report?start=2026-01-01T00:00:00%2B00:00&end=2026-01-01T23:59:59%2B00:00",
        headers=h,
    ).json()
    jan2 = client.get(
        "/api/admin/usage/report?start=2026-01-02T00:00:00%2B00:00&end=2026-01-02T23:59:59%2B00:00",
        headers=h,
    ).json()

    row1 = next(x for x in jan1["per_user"] if x["phone"] == "13900000139")
    row2 = next(x for x in jan2["per_user"] if x["phone"] == "13900000139")
    assert row1["spend_credits"] == 0
    assert row2["spend_credits"] == 6
    assert jan1["daily"] == []
    assert jan2["daily"] == [{"date": "2026-01-02", "spend_credits": 6}]


def test_usage_report_end_date_includes_full_day(client, make_user, auth):
    uid = make_user("13900000149", balance=1000, admin=True)
    h = auth("13900000149")
    finish_dt = datetime(2026, 6, 19, 18, 30, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "x"},
            params={"n": 1},
            cost_frozen=10,
            cost_settled=6,
            finished_at=finish_dt,
        ))
        db.commit()
    finally:
        db.close()

    report = client.get(
        "/api/admin/usage/report?start=2026-06-19&end=2026-06-19",
        headers=h,
    ).json()

    row = next(x for x in report["per_user"] if x["phone"] == "13900000149")
    assert row["spend_credits"] == 6
    daily = next(x for x in report["daily"] if x["date"] == "2026-06-19")
    assert daily["spend_credits"] >= 6


def test_image_generation_logs_gateway_call(client, make_user, auth):
    make_user("13900000031", balance=1000)
    h = auth("13900000031")
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
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


def test_usage_and_audit_logging_do_not_commit_caller_transaction(client, make_user):
    uid = make_user("13900000143", balance=1000)
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        user.nickname = "dirty-but-uncommitted"
        usage.record_call(
            db,
            kind="image",
            model_id="mock-image",
            user_id=uid,
            status="ok",
            detail={"case": "isolation"},
        )
        audit.log(
            db,
            user_id=uid,
            action="transaction_isolation_check",
            detail={"case": "isolation"},
        )
        db.rollback()
    finally:
        db.close()

    check = SessionLocal()
    try:
        assert check.get(User, uid).nickname is None
        assert check.query(GatewayCall).filter(
            GatewayCall.user_id == uid,
            GatewayCall.detail == {"case": "isolation"},
        ).count() == 1
        assert check.query(AuditLog).filter(
            AuditLog.user_id == uid,
            AuditLog.action == "transaction_isolation_check",
        ).count() == 1
    finally:
        check.close()


def test_failed_image_generation_logs_gateway_call(client, make_user, auth, monkeypatch):
    make_user("13900000034", balance=1000)
    h = auth("13900000034")

    def fail(*_args, **_kwargs):
        raise gateway.GatewayError("bad image request")

    monkeypatch.setattr("app.services.gateway.gen_image", fail)
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
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
    make_user("13900009042", balance=1000, admin=True)
    h = auth("13900009042")
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
    seen = {}

    def fake_sample(*_a, **kwargs):
        seen["n"] = kwargs.get("n")
        seen["preset"] = kwargs.get("preset")
        return [b"a", b"b", b"c"]

    monkeypatch.setattr(video_frames, "sample_keyframes", fake_sample)
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
    assert seen["n"] == 24
    assert seen["preset"] == "standard"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 982


def test_video_analysis_presets_match_product_frame_ranges():
    assert video_analysis.frame_count_for_duration(10, "fast") == 4
    assert video_analysis.frame_count_for_duration(10, "standard") == 7
    assert video_analysis.frame_count_for_duration(10, "fine") == 11
    assert video_analysis.frame_count_for_duration(120, "fast") == 8
    assert video_analysis.frame_count_for_duration(120, "standard") == 16
    assert video_analysis.frame_count_for_duration(120, "fine") == 24
    assert video_analysis.frame_count_for_duration(300, "fast") == 12
    assert video_analysis.frame_count_for_duration(300, "standard") == 24
    assert video_analysis.frame_count_for_duration(300, "fine") == 36


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


def test_video_sample_download_rejects_compressed_response(monkeypatch):
    class FakeResponse:
        is_redirect = False
        headers = {
            "content-type": "video/mp4",
            "content-encoding": "gzip",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            raise AssertionError("compressed responses should be rejected before status/body handling")

        def iter_bytes(self):
            yield b"\x00\x00\x00\x18ftypmp42"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://example.com/clip.mp4"
            return FakeResponse()

    seen = {}

    def fake_pinned_client(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return FakeClient()

    monkeypatch.setattr(video_frames, "assert_safe_url", lambda _url: None)
    monkeypatch.setattr(video_frames, "pinned_client", fake_pinned_client)

    assert video_frames._download_capped("https://example.com/clip.mp4") is None
    assert seen["headers"]["Accept-Encoding"] == "identity"


def test_keyframe_sampling_rejects_non_video_bytes(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"<html>not video</html>")
    assert video_frames.sample_keyframes("http://x/not-video.mp4", n=2) == []


def test_keyframe_sampling_uses_full_duration_for_long_video(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "_duration_seconds", lambda _path: 900.0)
    stamps = []

    def fake_grab(_src, ts, dst):
        stamps.append(ts)
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    assert video_frames.sample_keyframes("http://x/long.mp4", n=3) == [b"jpg", b"jpg", b"jpg"]
    assert stamps == [0.0, 300.0, 600.0]


def test_keyframe_sampling_preset_uses_downloaded_duration(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "_duration_seconds", lambda _path: 10.0)
    monkeypatch.setattr(video_frames, "_scene_change_timestamps", lambda *_a, **_k: [])
    stamps = []

    def fake_grab(_src, ts, dst):
        stamps.append(ts)
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    assert video_frames.sample_keyframes("http://x/short.mp4", n=36, preset="fine") == [b"jpg"] * 11
    assert len(stamps) == 11


def test_keyframe_sampling_prefers_scene_changes_then_uniform_fill(monkeypatch):
    monkeypatch.setattr(video_frames, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(video_frames, "_download_capped", lambda *_a, **_k: b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_frames, "_duration_seconds", lambda _path: 20.0)
    monkeypatch.setattr(video_frames, "_scene_change_timestamps", lambda *_a, **_k: [2.0, 9.0])
    stamps = []

    def fake_grab(_src, ts, dst):
        stamps.append(ts)
        with open(dst, "wb") as f:
            f.write(b"jpg")
        return True

    monkeypatch.setattr(video_frames, "_grab_frame", fake_grab)

    assert video_frames.sample_keyframes("http://x/cuts.mp4", n=4) == [b"jpg"] * 4
    assert stamps == [0.0, 2.0, 5.0, 9.0]


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
