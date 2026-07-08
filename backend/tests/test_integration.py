"""End-to-end API tests over the real app (TestClient + sqlite + fakeredis +
eager Celery + mock gateway)."""
from urllib.parse import urlparse

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (
    AssetReport,
    AuditLog,
    CreditTransaction,
    GenAsset,
    GenTask,
    ModelConfig,
    PhoneWhitelist,
    UserPrompt,
)
from app.services import sms, storage
from app.services.config_store import get_setting, set_setting


def _clear_active_model_tasks(model_use: str) -> None:
    db = SessionLocal()
    try:
        db.query(GenTask).filter(
            GenTask.model_use == model_use,
            GenTask.status.in_(["queued", "running", "needs_review"]),
        ).update({GenTask.status: "failed"}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_health(client):
    r = client.get("/api/health").json()
    assert r["ok"] is True
    assert "components" not in r

    live = client.get("/api/live").json()
    assert live == {"ok": True}

    ready = client.get("/api/ready").json()
    assert ready["ok"] is True
    assert ready["components"]["db"] == "ok"
    assert ready["components"]["redis"] == "ok"

    detail = client.get("/api/health/detail").json()
    assert detail["ok"] is True
    assert detail["components"]["db"] == "ok"
    assert detail["components"]["redis"] == "ok"


def test_register_login_me(client):
    db = SessionLocal()
    set_setting(db, "sms_auth_enabled", False)
    db.commit()
    db.close()

    r = client.post("/api/auth/register",
                    json={"phone": "13700000002", "password": "secret1234",
                          "sms_code": ""})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["phone"] == "13700000002"

    # wrong password
    bad = client.post("/api/auth/login",
                      json={"phone": "13700000002", "password": "nope"})
    assert bad.status_code == 401

    public_ok = client.post("/api/auth/register",
                            json={"phone": "13700000099", "password": "secret1234",
                                  "sms_code": ""})
    assert public_ok.status_code == 200, public_ok.text


def test_production_login_uses_cookie_without_bearer_body(client, make_user, monkeypatch):
    monkeypatch.setattr("app.routers.auth.settings.debug", False)
    monkeypatch.setattr("app.routers.auth.settings.auth_bearer_response_enabled", False)
    make_user("13700000006", password="secret1234", balance=100)

    r = client.post("/api/auth/login", json={"phone": "13700000006", "password": "secret1234"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] is None
    assert "ai_studio_token" in r.cookies

    me = client.get("/api/me", headers={"Cookie": f"ai_studio_token={r.cookies['ai_studio_token']}"})
    assert me.status_code == 200
    assert me.json()["phone"] == "13700000006"


def test_sms_register_switch_requires_code_when_enabled(client):
    db = SessionLocal()
    set_setting(db, "sms_auth_enabled", True)
    db.commit()
    db.close()

    missing = client.post("/api/auth/register",
                          json={"phone": "13700000003", "password": "secret1234"})
    assert missing.status_code == 400
    assert "短信验证码" in missing.text

    code = sms.send_code("13700000003")
    ok = client.post("/api/auth/register",
                     json={"phone": "13700000003", "password": "secret1234",
                           "sms_code": code})
    assert ok.status_code == 200, ok.text

    db = SessionLocal()
    try:
        set_setting(db, "sms_auth_enabled", False)
    finally:
        db.close()


def test_sms_send_returns_disabled_when_switch_is_off(client):
    db = SessionLocal()
    try:
        set_setting(db, "sms_auth_enabled", False)
    finally:
        db.close()

    r = client.post("/api/auth/sms/send", json={"phone": "13700000004"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "disabled": True}


def test_production_registration_allows_public_phone_without_sms(client, monkeypatch):
    monkeypatch.setattr("app.routers.auth.settings.debug", False)
    db = SessionLocal()
    try:
        set_setting(db, "sms_auth_enabled", False)
        db.commit()
    finally:
        db.close()

    features = client.get("/api/auth/features")
    assert features.status_code == 200, features.text
    assert features.json()["registration_enabled"] is True

    r = client.post(
        "/api/auth/register",
        json={"phone": "13700000005", "password": "secret1234"},
    )
    assert r.status_code == 200, r.text


def test_generate_unlock_profile(client, make_user, auth):
    make_user("13900000001", balance=1000)
    h = auth("13900000001")

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/x.png",
        "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image", "stage": "preview",
        "instruction": "a calm cat", "params": {"n": 2, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    # eager Celery already ran the task
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 2
    a0 = t["assets"][0]
    assert a0["unlocked"] is True
    assert a0["watermarked"] is False
    assert a0["unlock_cost"] == 0
    assert a0["hd_url"]
    assert a0["preview_url"]
    assert client.get(urlparse(a0["preview_url"]).path).status_code == 200

    # balance frozen then settled: 1K image cost 15 * n(2) = 30 -> 1000-30 = 970
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 970
    db = SessionLocal()
    try:
        txs = db.query(CreditTransaction).filter(
            CreditTransaction.biz_type == "gen_task",
            CreditTransaction.biz_ref == tid,
        ).order_by(CreditTransaction.id).all()
        assert [(tx.type, tx.balance_delta, tx.frozen_delta, tx.reserved_amount, tx.real_cost) for tx in txs] == [
            ("freeze", -30, 30, None, None),
            ("settle", 0, -30, 30, 30),
        ]
        assert txs[-1].frozen_after == 0
    finally:
        db.close()

    # Generated assets are charged at generation time and download directly.
    assert client.get(f"/api/assets/{a0['id']}/download", headers=h).status_code == 200

    # Old unlock entrypoint remains idempotent/free for generated assets.
    u = client.post(f"/api/assets/{a0['id']}/unlock", headers=h)
    assert u.status_code == 200
    assert u.json()["unlocked"] is True
    assert u.json()["hd_url"]
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 970

    # appears in personal gallery with expiry info
    g = client.get("/api/profile/assets", headers=h).json()
    assert any(x["id"] == a0["id"] for x in g)
    assert g[0]["days_left"] is not None


def test_generate_client_request_id_replays_existing_task(client, make_user, auth):
    make_user("13900000167", balance=1000)
    h = auth("13900000167")
    payload = {
        "client_request_id": "studio-retry-001",
        "source_asset_url": "http://example.com/retry.png",
        "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image",
        "stage": "preview",
        "instruction": "same request should not double charge",
        "params": {"n": 2, "size": "256x256"},
    }

    first = client.post("/api/generate", json=payload, headers=h)
    assert first.status_code == 200, first.text
    second = client.post("/api/generate", json=payload, headers=h)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    db = SessionLocal()
    try:
        tasks = db.query(GenTask).filter(
            GenTask.client_request_id == "studio-retry-001",
        ).all()
        assert len(tasks) == 1
    finally:
        db.close()
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 970


def test_generate_client_request_id_normalizes_default_image_n_before_fingerprint(client, make_user, auth):
    make_user("13900000166", balance=1000)
    h = auth("13900000166")
    payload = {
        "client_request_id": "studio-retry-default-n",
        "source_asset_url": "http://example.com/retry-default.png",
        "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image",
        "stage": "preview",
        "instruction": "same default image count request",
        "params": {"size": "256x256"},
    }

    first = client.post("/api/generate", json=payload, headers=h)
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/generate",
        json={**payload, "params": {"n": 1, "size": "256x256"}},
        headers=h,
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    db = SessionLocal()
    try:
        tasks = db.query(GenTask).filter(
            GenTask.client_request_id == "studio-retry-default-n",
        ).all()
        assert len(tasks) == 1
        assert tasks[0].params["n"] == 1
    finally:
        db.close()
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 985


def test_generate_client_request_id_rejects_different_payload(client, make_user, auth):
    make_user("13900000169", balance=1000)
    h = auth("13900000169")
    payload = {
        "client_request_id": "studio-retry-002",
        "source_asset_url": "http://example.com/retry.png",
        "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image",
        "stage": "preview",
        "instruction": "first request",
        "params": {"n": 1, "size": "256x256"},
    }

    first = client.post("/api/generate", json=payload, headers=h)
    assert first.status_code == 200, first.text
    conflict = client.post(
        "/api/generate",
        json={**payload, "instruction": "different request"},
        headers=h,
    )
    assert conflict.status_code == 409
    assert "client_request_id 已用于不同请求" in conflict.text

    db = SessionLocal()
    try:
        tasks = db.query(GenTask).filter(
            GenTask.client_request_id == "studio-retry-002",
        ).all()
        assert len(tasks) == 1
    finally:
        db.close()
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 985


def test_external_asset_download_streams_from_temp_file(client, make_user, auth, monkeypatch):
    uid = make_user("13900000103", balance=1000)
    h = auth("13900000103")
    db = SessionLocal()
    try:
        asset = GenAsset(
            task_id=0,
            user_id=uid,
            type="video",
            preview_url="https://cdn.example.com/preview.mp4",
            hd_url="https://cdn.example.com/final.mp4",
            unlocked=True,
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    finally:
        db.close()

    calls = {}

    def fake_download_to_path(url, path, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        with open(path, "wb") as f:
            f.write(b"video-bytes")
        return len(b"video-bytes")

    monkeypatch.setattr("app.routers.assets.gateway.download_to_path", fake_download_to_path)
    r = client.get(f"/api/assets/{asset_id}/download", headers=h)
    assert r.status_code == 200, r.text
    assert r.content == b"video-bytes"
    assert r.headers["content-type"].startswith("video/mp4")
    assert "attachment;" in r.headers["content-disposition"]
    assert f"asset-{asset_id}.mp4" in r.headers["content-disposition"]
    assert calls["url"] == "https://cdn.example.com/final.mp4"
    assert calls["kwargs"]["allowed_content_types"] == ("video/", "application/octet-stream")
    assert calls["kwargs"]["max_bytes"] > 100 * 1024 * 1024
    assert calls["kwargs"]["timeout_seconds"] > 60
    db = SessionLocal()
    try:
        log = db.query(AuditLog).filter(
            AuditLog.action == "download_asset",
            AuditLog.biz_id == asset_id,
        ).one()
        assert log.detail["source"] == "external"
        assert "final.mp4" not in str(log.detail)
    finally:
        db.close()


def test_external_image_download_gets_image_filename(client, make_user, auth, monkeypatch):
    uid = make_user("13900000104", balance=1000)
    h = auth("13900000104")
    db = SessionLocal()
    try:
        asset = GenAsset(
            task_id=0,
            user_id=uid,
            type="image",
            preview_url="https://cdn.example.com/preview.jpg",
            hd_url="https://cdn.example.com/final.jpg",
            unlocked=True,
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    finally:
        db.close()

    image_bytes = b"\xff\xd8\xff\xe0" + b"image-bytes"

    calls = {}

    def fake_download_to_path(url, path, **kwargs):
        calls["kwargs"] = kwargs
        with open(path, "wb") as f:
            f.write(image_bytes)
        return len(image_bytes)

    monkeypatch.setattr("app.routers.assets.gateway.download_to_path", fake_download_to_path)
    r = client.get(f"/api/assets/{asset_id}/download", headers=h)
    assert r.status_code == 200, r.text
    assert r.content == image_bytes
    assert r.headers["content-type"].startswith("image/jpeg")
    assert "attachment;" in r.headers["content-disposition"]
    assert f"asset-{asset_id}.jpg" in r.headers["content-disposition"]
    assert calls["kwargs"]["allowed_content_types"] == ("image/",)
    assert calls["kwargs"]["max_bytes"] < 100 * 1024 * 1024
    assert calls["kwargs"]["timeout_seconds"] <= 600


def test_video_preview_settles_preview_cost(client, make_user, auth):
    make_user("13900000002", balance=1000, admin=True)
    h = auth("13900000002")

    r = client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
        "extra": {"preview_cost": 5},
    }, headers=h)
    assert r.status_code == 200, r.text

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/cover.png",
        "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "video",
        "stage": "preview",
        "instruction": "slow product spin",
        "params": {"duration": 2, "resolution": "480p"},
    }, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert t["cost_frozen"] == 15
    assert t["cost_settled"] == 15
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 985


def test_video_final_can_generate_directly_without_preview_parent(client, make_user, auth):
    make_user("13900001002", balance=1000, admin=True)
    h = auth("13900001002")

    r = client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
        "extra": {},
    }, headers=h)
    assert r.status_code == 200, r.text

    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "instruction": "direct final clip, product reveal",
        "params": {"duration": 2, "resolution": "480p", "target_resolution": "480p"},
    }, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "final"
    assert body["parent_task_id"] is None

    t = client.get(f"/api/tasks/{body['id']}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert t["stage"] == "final"
    assert t["cost_frozen"] == 16
    assert t["cost_settled"] == 16
    assert t["assets"][0]["type"] == "video"
    assert t["assets"][0]["unlocked"] is True
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 984


def test_video_preview_adapts_ratio_and_cover_from_reference(
    client, make_user, auth, monkeypatch
):
    make_user("13900000016", balance=1000, admin=True)
    h = auth("13900000016")
    monkeypatch.setattr("app.config.settings.mock_mode", False)
    monkeypatch.setattr("app.config.settings.gateway_base_url", "https://gateway.test")
    monkeypatch.setattr("app.config.settings.gateway_api_key", "sk-test")

    r = client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
        "extra": {"preview_cost": 5},
    }, headers=h)
    assert r.status_code == 200, r.text

    seen = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.update(params)
        return "mock-ratio"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/source-video.mp4",
        "source_type": "video",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "video",
        "stage": "preview",
        "instruction": "slow product spin",
        "params": {
            "duration": 8,
            "resolution": "720p",
            "reference_width": 720,
            "reference_height": 1280,
            "reference_image_url": "http://example.com/cover.jpg",
        },
    }, headers=h)
    assert r.status_code == 200, r.text

    assert seen["duration"] == 5
    assert seen["resolution"] == "480p"
    assert seen["ratio"] == "9:16"
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")


def test_video_final_uses_selected_quality_and_reference_poster(
    client, make_user, auth, monkeypatch, tiny_mp4
):
    make_user("13900000018", balance=1000, admin=True)
    h = auth("13900000018")
    monkeypatch.setattr("app.config.settings.mock_mode", False)
    monkeypatch.setattr("app.config.settings.gateway_base_url", "https://gateway.test")
    monkeypatch.setattr("app.config.settings.gateway_api_key", "sk-test")

    r = client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
        "extra": {"preview_cost": 5},
    }, headers=h)
    assert r.status_code == 200, r.text

    seen = {}
    submit_count = {"n": 0}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submit_count["n"] += 1
        if submit_count["n"] >= 2:
            seen.update(params)
        return f"external-{submit_count['n']}"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    monkeypatch.setattr("app.services.gateway.poll_video",
                        lambda *_args, **_kwargs: {
                            "status": "succeeded",
                            "url": "http://example.com/result.mp4",
                        })

    def fake_download(url):
        if url.endswith(".jpg"):
            from io import BytesIO

            from PIL import Image

            buf = BytesIO()
            Image.new("RGB", (64, 96), "white").save(buf, format="JPEG")
            return buf.getvalue()
        return tiny_mp4

    monkeypatch.setattr("app.services.gateway.download_bytes", fake_download)

    def fake_download_to_storage(url, subdir, ext, **_kwargs):
        return storage.save_bytes(tiny_mp4, subdir, ext)

    monkeypatch.setattr("app.services.gateway.download_to_storage", fake_download_to_storage)

    preview = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/source-video.mp4",
        "source_type": "video",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "video",
        "stage": "preview",
        "instruction": "slow product spin",
        "params": {
            "duration": 8,
            "resolution": "720p",
            "target_resolution": "1080p",
            "reference_width": 720,
            "reference_height": 1280,
            "reference_image_url": "http://example.com/cover.jpg",
        },
    }, headers=h)
    assert preview.status_code == 200, preview.text
    parent_id = preview.json()["id"]

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/source-video.mp4",
        "source_type": "video",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "video",
        "stage": "final",
        "parent_task_id": parent_id,
        "instruction": "slow product spin",
        "params": {
            "duration": 8,
            "resolution": "720p",
            "target_resolution": "1080p",
            "reference_width": 720,
            "reference_height": 1280,
            "reference_image_url": "http://example.com/cover.jpg",
        },
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["resolution"] == "1080p"
    assert seen["target_resolution"] == "1080p"
    assert seen["duration"] == 8
    assert seen["target_duration"] == 8
    assert seen["ratio"] == "9:16"
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert urlparse(task["assets"][0]["preview_url"]).path.startswith("/media/preview/")
    assert task["assets"][0]["unlocked"] is True
    assert task["assets"][0]["unlock_cost"] == 0
    assert task["assets"][0]["hd_url"]


def test_video_final_is_idempotent_for_same_preview(client, make_user, auth, monkeypatch, tiny_mp4):
    make_user("13900000019", balance=1000, admin=True)
    h = auth("13900000019")

    r = client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
        "extra": {"preview_cost": 5},
    }, headers=h)
    assert r.status_code == 200, r.text

    preview = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/source-video.mp4",
        "source_type": "video",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "video",
        "stage": "preview",
        "instruction": "slow product spin",
        "params": {
            "duration": 8,
            "resolution": "720p",
            "target_resolution": "1080p",
            "reference_width": 720,
            "reference_height": 1280,
            "reference_image_url": "http://example.com/cover.jpg",
        },
    }, headers=h)
    assert preview.status_code == 200, preview.text
    parent_id = preview.json()["id"]

    monkeypatch.setattr("app.services.gateway.submit_video", lambda *_a, **_k: "external-final")
    monkeypatch.setattr("app.services.gateway.poll_video",
                        lambda *_args, **_kwargs: {
                            "status": "succeeded",
                            "url": "http://example.com/result.mp4",
                        })

    def fake_download(url):
        if url.endswith(".jpg"):
            from io import BytesIO

            from PIL import Image

            buf = BytesIO()
            Image.new("RGB", (64, 96), "white").save(buf, format="JPEG")
            return buf.getvalue()
        return tiny_mp4

    monkeypatch.setattr("app.services.gateway.download_bytes", fake_download)
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda _url, subdir, ext, **_kwargs: storage.save_bytes(tiny_mp4, subdir, ext),
    )

    payload = {
        "category": "video",
        "stage": "final",
        "parent_task_id": parent_id,
        "params": {},
    }
    first = client.post("/api/generate", json=payload, headers=h)
    assert first.status_code == 200, first.text
    second = client.post("/api/generate", json=payload, headers=h)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["progress"] == 100
    assert second.json()["assets"]
    assert second.json()["assets"][0]["type"] == "video"

    db = SessionLocal()
    try:
        finals = db.query(GenTask).filter(
            GenTask.parent_task_id == parent_id,
            GenTask.stage == "final",
        ).all()
        assert len(finals) == 1
    finally:
        db.close()
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 761


def test_db_rejects_duplicate_active_final_for_same_preview(client, make_user):
    uid = make_user("13900000142", balance=1000)
    db = SessionLocal()
    try:
        preview = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "preview"},
            params={},
        )
        db.add(preview)
        db.commit()
        first = GenTask(
            user_id=uid,
            category="video",
            stage="final",
            parent_task_id=preview.id,
            status="queued",
            prompt={"final_text": "final"},
            params={},
        )
        second = GenTask(
            user_id=uid,
            category="video",
            stage="final",
            parent_task_id=preview.id,
            status="needs_review",
            prompt={"final_text": "final"},
            params={},
        )
        db.add_all([first, second])
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_video_final_integrity_error_replays_active_final(client, make_user, auth, monkeypatch):
    make_user("13900000143", balance=1000, admin=True)
    h = auth("13900000143")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
        "extra": {"preview_cost": 5},
    }, headers=h).status_code == 200

    preview = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/source-video.mp4",
        "source_type": "video",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "video",
        "stage": "preview",
        "instruction": "preview for active final replay",
        "params": {
            "duration": 2,
            "reference_image_url": "http://example.com/cover.jpg",
        },
    }, headers=h)
    assert preview.status_code == 200, preview.text
    parent_id = preview.json()["id"]

    db = SessionLocal()
    try:
        final = GenTask(
            user_id=make_user("13900000143"),
            category="video",
            stage="final",
            parent_task_id=parent_id,
            status="queued",
            prompt={"final_text": "existing final"},
            params={},
            cost_frozen=50,
        )
        db.add(final)
        db.commit()
        final_id = final.id
    finally:
        db.close()

    from sqlalchemy.orm import Session

    import app.routers.generate as generate_router

    real_flush = Session.flush
    real_existing_active_final = generate_router._existing_active_final
    tripped = {"value": False}
    active_lookup_count = {"value": 0}

    def flush_once_then_fail(self, *args, **kwargs):
        if not tripped["value"]:
            tripped["value"] = True
            raise IntegrityError("duplicate active final", {}, Exception("unique"))
        return real_flush(self, *args, **kwargs)

    def delayed_existing_active_final(*args, **kwargs):
        active_lookup_count["value"] += 1
        if active_lookup_count["value"] <= 2:
            return None
        return real_existing_active_final(*args, **kwargs)

    monkeypatch.setattr(Session, "flush", flush_once_then_fail)
    monkeypatch.setattr(generate_router, "_existing_active_final", delayed_existing_active_final)

    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": parent_id,
        "params": {},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == final_id


def test_video_final_uses_preview_model_snapshot_price(client, make_user, auth, monkeypatch, tiny_mp4):
    make_user("13900000147", balance=1000, admin=True)
    h = auth("13900000147")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video-old",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    preview = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "snapshot video"},
        "params": {"duration": 2},
    }, headers=h)
    assert preview.status_code == 200, preview.text

    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video-new",
        "cost_credits": 200,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 20},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    seen = {}

    def fake_submit(_prompt, model_id, _params, extra=None):
        seen["model_id"] = model_id
        seen["extra"] = extra
        return "external-snapshot-final"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "url": "http://example.com/final.mp4",
        },
    )
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda _url, subdir, ext, **_kwargs: storage.save_bytes(tiny_mp4, subdir, ext),
    )

    final = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": preview.json()["id"],
        "params": {},
    }, headers=h)
    assert final.status_code == 200, final.text
    task = client.get(f"/api/tasks/{final.json()['id']}", headers=h).json()
    assert task["cost_frozen"] == 32
    assert task["cost_settled"] == 32
    assert seen["model_id"] == "mock-video-old"


def test_video_final_rejects_stale_preview_gateway_key_snapshot(client, make_user, auth):
    make_user("13900000178", balance=1000, admin=True)
    h = auth("13900000178")
    _clear_active_model_tasks("video")
    body = {
        "use": "video",
        "provider": "custom_openai",
        "base_url": "https://video-gateway.example.com/v1",
        "gateway_format": "openai",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }
    first = client.put("/api/admin/models", json={**body, "api_key": "old-key"}, headers=h)
    assert first.status_code == 200, first.text

    preview = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "snapshot drift"},
        "params": {"duration": 2},
    }, headers=h)
    assert preview.status_code == 200, preview.text

    changed = client.put("/api/admin/models", json={**body, "api_key": "new-key"}, headers=h)
    assert changed.status_code == 200, changed.text

    final = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": preview.json()["id"],
        "params": {},
    }, headers=h)
    assert final.status_code == 409
    assert "模型网关配置已变更" in final.text


def test_video_final_can_regenerate_after_succeeded_task_loses_asset(
    client, make_user, auth, monkeypatch, tiny_mp4
):
    make_user("13877777164", balance=1000, admin=True)
    h = auth("13877777164")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200
    preview = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "rebuild final"},
        "params": {"duration": 2},
    }, headers=h)
    assert preview.status_code == 200, preview.text

    monkeypatch.setattr("app.services.gateway.submit_video", lambda *_args, **_kwargs: "external-rebuild")
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "url": "http://example.com/final.mp4",
        },
    )
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda _url, subdir, ext, **_kwargs: storage.save_bytes(tiny_mp4, subdir, ext),
    )

    first = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": preview.json()["id"],
        "params": {},
    }, headers=h)
    assert first.status_code == 200, first.text
    first_task = client.get(f"/api/tasks/{first.json()['id']}", headers=h).json()
    asset_id = first_task["assets"][0]["id"]
    assert client.delete(f"/api/assets/{asset_id}", headers=h).status_code == 200

    second = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": preview.json()["id"],
        "params": {},
    }, headers=h)
    assert second.status_code == 200, second.text
    assert second.json()["id"] != first.json()["id"]
    assert client.get(f"/api/tasks/{second.json()['id']}", headers=h).json()["assets"]


def test_image_hd_extension_matches_jpeg_response(client, make_user, auth, monkeypatch):
    from io import BytesIO

    from PIL import Image

    make_user("13900000075", balance=1000)
    h = auth("13900000075")

    buf = BytesIO()
    Image.new("RGB", (32, 32), "white").save(buf, format="JPEG")
    monkeypatch.setattr("app.services.gateway.gen_image",
                        lambda *_a, **_k: [buf.getvalue()])
    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "jpeg"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    asset = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()["assets"][0]
    unlocked = client.post(f"/api/assets/{asset['id']}/unlock", headers=h).json()
    assert urlparse(unlocked["hd_url"]).path.endswith(".jpg")


def test_generated_hd_reference_requires_owner_and_unlock(client, make_user, auth, monkeypatch):
    make_user("13900000082", balance=1000)
    make_user("13900000083", balance=1000)
    owner_h = auth("13900000082")
    other_h = auth("13900000083")

    from app.services.gateway import _mock_image

    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda prompt, _model, n=1, size="256x256", **_k: [_mock_image(prompt, size, 0)],
    )
    created = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "private reference"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=owner_h)
    assert created.status_code == 200, created.text
    asset = client.get(f"/api/tasks/{created.json()['id']}", headers=owner_h).json()["assets"][0]
    unlocked = client.post(f"/api/assets/{asset['id']}/unlock", headers=owner_h).json()
    hd_url = unlocked["hd_url"]

    other = client.post("/api/generate", json={
        "source_asset_url": hd_url,
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "instruction": "steal reference",
        "params": {"n": 1, "size": "256x256"},
    }, headers=other_h)
    assert other.status_code == 404
    assert "生成素材" in other.text

    owner = client.post("/api/generate", json={
        "source_asset_url": hd_url,
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "instruction": "reuse own unlocked reference",
        "params": {"n": 1, "size": "256x256"},
    }, headers=owner_h)
    assert owner.status_code == 200, owner.text


def test_image_generation_adapts_size_from_reference(client, make_user, auth, monkeypatch):
    make_user("13900000017", balance=1000, admin=True)
    h = auth("13900000017")
    client.put("/api/admin/settings", json={"image_size": "1024x1024", "admin_password": "pass123456"}, headers=h)

    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["size"] = size
        from app.services.gateway import _mock_image
        return [_mock_image(prompt, size, 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/reference.jpg",
        "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image",
        "stage": "preview",
        "instruction": "same style",
        "params": {"n": 1, "reference_width": 720, "reference_height": 1280},
    }, headers=h)
    assert r.status_code == 200, r.text

    assert seen["size"] == "720x1280"


def test_image_generation_adapts_size_from_reference_at_2k_default(client, make_user, auth, monkeypatch):
    make_user("13900000079", balance=1000, admin=True)
    h = auth("13900000079")
    client.put("/api/admin/settings", json={"image_size": "2048x2048", "admin_password": "pass123456"}, headers=h)

    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["size"] = size
        from app.services.gateway import _mock_image
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/reference.jpg",
        "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image",
        "stage": "preview",
        "instruction": "same style",
        "params": {"n": 1, "reference_width": 720, "reference_height": 1280},
    }, headers=h)
    assert r.status_code == 200, r.text

    assert seen["size"] == "1152x2048"


def test_insufficient_credits(client, make_user, auth):
    make_user("13900000003", balance=1)  # below image cost (5)
    h = auth("13900000003")
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image", "stage": "preview", "instruction": "x",
    }, headers=h)
    assert r.status_code == 400
    assert "额度不足" in r.text


def test_generate_rejects_invalid_stage(client, make_user, auth):
    make_user("13900000005", balance=1000)
    h = auth("13900000005")
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png",
        "source_type": "image",
        "category": "image",
        "stage": "oops",
        "instruction": "x",
    }, headers=h)
    assert r.status_code == 422


def test_video_final_requires_succeeded_preview_parent(client, make_user, auth):
    uid = make_user("13900000006", balance=1000, admin=True)
    h = auth("13900000006")
    db = SessionLocal()
    try:
        parent = GenTask(
            user_id=uid,
            source_asset_url="http://example.com/source.mp4",
            source_type="video",
            category="video",
            stage="preview",
            prompt={"instruction": "x"},
            model_use="video",
            params={"duration": 5, "resolution": "480p"},
            status="failed",
            cost_frozen=5,
            cost_settled=0,
        )
        db.add(parent)
        db.commit()
        db.refresh(parent)
        parent_id = parent.id
    finally:
        db.close()

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/source.mp4",
        "source_type": "video",
        "category": "video",
        "stage": "final",
        "parent_task_id": parent_id,
        "instruction": "x",
        "params": {"duration": 5, "target_resolution": "720p"},
    }, headers=h)
    assert r.status_code == 400
    assert "已成功的视频预览任务" in r.text


def test_admin_guard(client, make_user, auth):
    make_user("13900000004", balance=10)
    h = auth("13900000004")
    assert client.get("/api/admin/users", headers=h).status_code == 403


def test_admin_users_support_search_and_pagination(client, make_user, auth):
    make_user("13900001001", admin=True)
    make_user("13900001002", balance=10)
    make_user("13900001003", balance=20)
    ah = auth("13900001001")

    all_rows = client.get("/api/admin/users?limit=1&offset=0", headers=ah)
    assert all_rows.status_code == 200, all_rows.text
    assert len(all_rows.json()) == 1

    found = client.get("/api/admin/users?q=13900001002&limit=20", headers=ah)
    assert found.status_code == 200, found.text
    assert [u["phone"] for u in found.json()] == ["13900001002"]

    admins = client.get("/api/admin/users?is_admin=true", headers=ah)
    assert admins.status_code == 200, admins.text
    assert any(u["phone"] == "13900001001" for u in admins.json())


def test_admin_bulk_grant_updates_balances(client, make_user, auth):
    make_user("13900001004", admin=True)
    uid1 = make_user("13900001005", balance=10)
    uid2 = make_user("13900001006", balance=20)
    ah = auth("13900001004")

    r = client.post("/api/admin/quota/bulk-grant", headers=ah, json={
        "idempotency_key": "bulk-grant-test-001",
        "items": [
            {"user_id": uid1, "amount": 7, "note": "批量测试"},
            {"user_id": uid2, "amount": 9, "note": "批量测试"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["granted"]) == 2
    assert body["failed"] == []

    h1 = auth("13900001005")
    h2 = auth("13900001006")
    assert client.get("/api/me", headers=h1).json()["balance_credits"] == 17
    assert client.get("/api/me", headers=h2).json()["balance_credits"] == 29

    replay = client.post("/api/admin/quota/bulk-grant", headers=ah, json={
        "idempotency_key": "bulk-grant-test-001",
        "items": [
            {"user_id": uid1, "amount": 7, "note": "批量测试"},
            {"user_id": uid2, "amount": 9, "note": "批量测试"},
        ],
    })
    assert replay.status_code == 200, replay.text
    assert len(replay.json()["granted"]) == 2
    assert client.get("/api/me", headers=h1).json()["balance_credits"] == 17
    assert client.get("/api/me", headers=h2).json()["balance_credits"] == 29

    conflict = client.post("/api/admin/quota/bulk-grant", headers=ah, json={
        "idempotency_key": "bulk-grant-test-001",
        "items": [
            {"user_id": uid1, "amount": 8, "note": "批量测试"},
        ],
    })
    assert conflict.status_code == 409


def _gen(client, h, n=2):
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image", "stage": "preview", "instruction": "x",
        "params": {"n": n, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_change_password_invalidates_token(client, make_user, auth):
    make_user("13900000010", password="old123456")
    h = auth("13900000010", "old123456")
    assert client.get("/api/me", headers=h).status_code == 200

    r = client.post("/api/me/password",
                    json={"old_password": "old123456", "new_password": "new1234567"}, headers=h)
    assert r.status_code == 200
    # old token now revoked
    assert client.get("/api/me", headers=h).status_code == 401
    # new password works
    h2 = auth("13900000010", "new1234567")
    assert client.get("/api/me", headers=h2).status_code == 200


def test_logout_invalidates_token(client, make_user, auth):
    make_user("13900000011")
    h = auth("13900000011")
    assert client.get("/api/me", headers=h).status_code == 200
    client.post("/api/me/logout", headers=h)
    assert client.get("/api/me", headers=h).status_code == 401


def test_admin_reset_password(client, make_user, auth):
    make_user("13900000012", admin=True)
    target = make_user("13900000013", password="orig123456")
    ah = auth("13900000012")
    r = client.post(f"/api/admin/users/{target}/reset_password",
                    json={"password": "reset12345", "admin_password": "pass123456"}, headers=ah)
    assert r.status_code == 200
    th = auth("13900000013", "reset12345")
    assert client.get("/api/me", headers=th).status_code == 200


def test_favorite_and_delete(client, make_user, auth):
    make_user("13900000014", balance=1000)
    h = auth("13900000014")
    tid = _gen(client, h)
    a = client.get(f"/api/tasks/{tid}", headers=h).json()["assets"][0]
    preview_key = storage.key_from_url(a["preview_url"])
    model_ref_path = storage.local_path(preview_key.replace("preview/", "model_ref/", 1).rsplit(".", 1)[0] + ".jpg")
    assert model_ref_path.exists()

    f = client.post(f"/api/assets/{a['id']}/favorite", headers=h)
    assert f.status_code == 200 and f.json()["favorite"] is True
    fav = client.get("/api/profile/assets?favorite=true", headers=h).json()
    assert any(x["id"] == a["id"] for x in fav)

    d = client.delete(f"/api/assets/{a['id']}", headers=h)
    assert d.status_code == 200
    assert not model_ref_path.exists()
    after = client.get("/api/profile/assets", headers=h).json()
    assert all(x["id"] != a["id"] for x in after)


def test_profile_assets_filters_and_batch_operations(client, make_user, auth):
    make_user("13900001007", balance=1000)
    h = auth("13900001007")
    tid = _gen(client, h, n=2)
    task = client.get(f"/api/tasks/{tid}", headers=h).json()
    asset_ids = [a["id"] for a in task["assets"]]
    for asset_id in asset_ids:
        assert client.post(f"/api/assets/{asset_id}/unlock", headers=h).status_code == 200

    square = client.get("/api/profile/assets?type=image&model_use=image&size=square&min_width=1", headers=h)
    assert square.status_code == 200, square.text
    assert {a["id"] for a in square.json()} >= set(asset_ids)

    dl = client.post("/api/assets/batch/download", headers=h, json={"asset_ids": asset_ids})
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"].startswith("application/zip")

    deleted = client.post("/api/assets/batch/delete", headers=h, json={"asset_ids": asset_ids})
    assert deleted.status_code == 200, deleted.text
    assert set(deleted.json()["deleted"]) == set(asset_ids)
    after = client.get("/api/profile/assets", headers=h).json()
    assert all(a["id"] not in asset_ids for a in after)


def test_delete_asset_keeps_shared_underlying_file(client, make_user, auth):
    uid = make_user("13900001018", balance=1000)
    h = auth("13900001018")
    key = storage.save_bytes(b"\x89PNG\r\n\x1a\nshared", "hd", "png")
    path = storage.local_path(key)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
        )
        db.add(task)
        db.flush()
        first = GenAsset(
            task_id=task.id,
            user_id=uid,
            type="image",
            preview_url=storage.public_url(key),
            hd_url=storage.public_url(key),
            unlocked=True,
            moderation_status="active",
        )
        second = GenAsset(
            task_id=task.id,
            user_id=uid,
            type="image",
            preview_url=storage.public_url(key),
            hd_url=storage.public_url(key),
            unlocked=True,
            moderation_status="active",
        )
        db.add_all([first, second])
        db.commit()
        first_id = first.id
        second_id = second.id
    finally:
        db.close()

    deleted = client.delete(f"/api/assets/{first_id}", headers=h)

    assert deleted.status_code == 200, deleted.text
    assert path.exists()
    download = client.get(f"/api/assets/{second_id}/download", headers=h)
    assert download.status_code == 200, download.text


def test_delete_reported_asset_detaches_report_history(client, make_user, auth):
    uid = make_user("13900001019", balance=1000)
    h = auth("13900001019")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=task.id,
            user_id=uid,
            type="image",
            preview_url="/media/preview/reported-delete.png",
            moderation_status="active",
        )
        db.add(asset)
        db.flush()
        report = AssetReport(
            asset_id=asset.id,
            reporter_user_id=uid,
            owner_user_id=uid,
            reason="other",
            status="open",
        )
        db.add(report)
        db.commit()
        asset_id = asset.id
        report_id = report.id
    finally:
        db.close()

    deleted = client.delete(f"/api/assets/{asset_id}", headers=h)

    assert deleted.status_code == 200, deleted.text
    db = SessionLocal()
    try:
        assert db.get(GenAsset, asset_id) is None
        kept_report = db.get(AssetReport, report_id)
        assert kept_report is not None
        assert kept_report.asset_id is None
    finally:
        db.close()


def test_cancel_queued_task_refunds_frozen_credits(client, make_user, auth):
    uid = make_user("13900001008", balance=100)
    h = auth("13900001008")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            prompt={"instruction": "queued"},
            model_use="image",
            params={"n": 1, "size": "256x256"},
            status="queued",
            cost_frozen=15,
            cost_settled=0,
        )
        db.add(task)
        db.flush()
        from app.services import credits

        credits.freeze(db, uid, 15, biz_ref=task.id, commit=False)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    before = client.get("/api/me", headers=h).json()
    assert before["balance_credits"] == 85
    assert before["frozen_credits"] == 15
    r = client.post(f"/api/tasks/{task_id}/cancel", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"
    after = client.get("/api/me", headers=h).json()
    assert after["balance_credits"] == 100
    assert after["frozen_credits"] == 0


def test_cancel_running_task_marks_cancel_requested(client, make_user, auth):
    uid = make_user("13900001018", balance=100)
    h = auth("13900001018")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            prompt={"instruction": "running"},
            model_use="image",
            params={"n": 1, "size": "256x256"},
            status="running",
            cost_frozen=15,
            cost_settled=0,
        )
        db.add(task)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    r = client.post(f"/api/tasks/{task_id}/cancel", headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    db = SessionLocal()
    try:
        saved = db.get(GenTask, task_id)
        assert saved.params["_cancel_requested"] is True
        assert saved.error == "取消请求已提交，系统将在安全阶段停止任务"
    finally:
        db.close()


def test_cancel_running_submitted_video_is_rejected(client, make_user, auth):
    uid = make_user("13900001019", balance=100)
    h = auth("13900001019")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            prompt={"instruction": "running video"},
            model_use="video",
            params={"duration": 5},
            status="running",
            phase="polling",
            external_task_id="external-123",
            cost_frozen=15,
            cost_settled=0,
        )
        db.add(task)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    r = client.post(f"/api/tasks/{task_id}/cancel", headers=h)

    assert r.status_code == 409
    assert "外部网关" in r.text


def test_prompt_history_crud_and_auto_record(client, make_user, auth):
    make_user("13900001009", balance=1000)
    h = auth("13900001009")

    created = client.post("/api/prompts/history", headers=h, json={
        "title": "产品图",
        "prompt": "高端产品摄影，柔和布光",
        "category": "image",
        "source": "manual",
        "favorite": True,
    })
    assert created.status_code == 200, created.text
    prompt_id = created.json()["id"]

    listed = client.get("/api/prompts/history?favorite=true&q=产品", headers=h)
    assert listed.status_code == 200, listed.text
    assert any(row["id"] == prompt_id for row in listed.json())

    patched = client.patch(f"/api/prompts/history/{prompt_id}", headers=h, json={"increment_usage": True})
    assert patched.status_code == 200, patched.text
    assert patched.json()["usage_count"] == 1

    tid = _gen(client, h, n=1)
    assert client.get(f"/api/tasks/{tid}", headers=h).json()["status"] == "succeeded"
    rows = client.get("/api/prompts/history?source=generate", headers=h).json()
    assert any("生成提示词" in row["title"] for row in rows)

    deleted = client.delete(f"/api/prompts/history/{prompt_id}", headers=h)
    assert deleted.status_code == 200, deleted.text
    db = SessionLocal()
    try:
        assert db.get(UserPrompt, prompt_id) is None
    finally:
        db.close()


def test_retry_requires_failed(client, make_user, auth):
    make_user("13900000015", balance=1000)
    h = auth("13900000015")
    tid = _gen(client, h)  # eager -> succeeded
    r = client.post(f"/api/tasks/{tid}/retry", headers=h)
    assert r.status_code == 400  # only failed tasks can be retried


def test_retry_reprices_failed_image_task(client, make_user, auth):
    uid = make_user("13900000144", balance=1000)
    h = auth("13900000144")
    db = SessionLocal()
    original_image_n = None
    try:
        original_image_n = get_setting(db, "image_n", 1)
        set_setting(db, "image_n", 4)
        db.commit()
    finally:
        db.close()
    try:
        _clear_active_model_tasks("image")
        admin_phone = "13877777165"
        make_user(admin_phone, balance=1000, admin=True)
        admin_h = auth(admin_phone)
        assert client.put("/api/admin/models", json={
            "use": "image",
            "model_id": "mock-image",
            "cost_credits": 5,
            "unlock_cost": 5,
            "enabled": True,
            "provider": None,
            "base_url": None,
            "gateway_format": "openai",
            "api_key_clear": True,
            "admin_password": "pass123456",
        }, headers=admin_h).status_code == 200
        db = SessionLocal()
        try:
            t = GenTask(
                user_id=uid,
                category="image",
                stage="preview",
                prompt={"final_text": "retry reprices"},
                model_use="image",
                params={"size": "256x256"},
                status="failed",
                cost_frozen=5,
                cost_settled=0,
            )
            db.add(t)
            db.commit()
            tid = t.id
        finally:
            db.close()

        r = client.post(f"/api/tasks/{tid}/retry", headers=h)
        assert r.status_code == 200, r.text
        task = client.get(f"/api/tasks/{tid}", headers=h).json()
        assert task["cost_frozen"] == 60
        assert len(task["assets"]) == 4
        db = SessionLocal()
        try:
            assert db.get(GenTask, tid).params["n"] == 4
        finally:
            db.close()
    finally:
        db = SessionLocal()
        try:
            set_setting(db, "image_n", original_image_n if original_image_n is not None else 1)
        finally:
            db.close()


def test_retry_preserves_existing_model_snapshot_price(client, make_user, auth):
    uid = make_user("13900000166", balance=1000)
    h = auth("13900000166")
    original_model = None
    db = SessionLocal()
    try:
        model = db.query(ModelConfig).filter(ModelConfig.use == "image").one()
        original_model = {
            "model_id": model.model_id,
            "cost_credits": model.cost_credits,
            "unlock_cost": model.unlock_cost,
            "enabled": model.enabled,
            "extra": model.extra,
        }
        model.model_id = "new-mock-image"
        model.cost_credits = 99
        model.unlock_cost = 99
        model.enabled = True
        t = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            prompt={"final_text": "retry old snapshot"},
            model_use="image",
            params={
                "n": 2,
                "size": "256x256",
                "_model_snapshot": {
                    "model_id": "old-mock-image",
                    "cost_credits": 5,
                    "unlock_cost": 7,
                    "extra": {},
                },
            },
            status="failed",
            cost_frozen=10,
            cost_settled=0,
        )
        db.add(t)
        db.commit()
        tid = t.id
    finally:
        db.close()

    try:
        r = client.post(f"/api/tasks/{tid}/retry", headers=h)
        assert r.status_code == 200, r.text
        task = client.get(f"/api/tasks/{tid}", headers=h).json()
        assert task["cost_frozen"] == 10
        assert task["cost_settled"] == 10
        db = SessionLocal()
        try:
            params = db.get(GenTask, tid).params
            assert params["_model_snapshot"]["model_id"] == "old-mock-image"
            assert params["_model_snapshot"]["cost_credits"] == 5
        finally:
            db.close()
    finally:
        if original_model:
            db = SessionLocal()
            try:
                model = db.query(ModelConfig).filter(ModelConfig.use == "image").one()
                for key, value in original_model.items():
                    setattr(model, key, value)
                db.commit()
            finally:
                db.close()


def test_retry_rejects_stale_gateway_key_snapshot(client, make_user, auth):
    make_user("13877777179", balance=1000, admin=True)
    h = auth("13877777179")
    _clear_active_model_tasks("image")
    body = {
        "use": "image",
        "provider": "custom_openai",
        "base_url": "https://image-gateway.example.com/v1",
        "gateway_format": "openai",
        "model_id": "mock-image",
        "cost_credits": 5,
        "unlock_cost": 5,
        "enabled": True,
        "admin_password": "pass123456",
    }
    first = client.put("/api/admin/models", json={**body, "api_key": "old-key"}, headers=h)
    assert first.status_code == 200, first.text
    task = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "retry drift"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert task.status_code == 200, task.text
    tid = task.json()["id"]

    db = SessionLocal()
    try:
        db_task = db.get(GenTask, tid)
        db_task.status = "failed"
        db_task.error = "force retry"
        db.commit()
    finally:
        db.close()

    changed = client.put("/api/admin/models", json={**body, "api_key": "new-key"}, headers=h)
    assert changed.status_code == 200, changed.text
    retry = client.post(f"/api/tasks/{tid}/retry", headers=h)
    assert retry.status_code == 409
    assert "模型网关配置已变更" in retry.text
