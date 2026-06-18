"""End-to-end API tests over the real app (TestClient + sqlite + fakeredis +
eager Celery + mock gateway)."""
from urllib.parse import urlparse

from app.db import SessionLocal
from app.models import GenAsset, GenTask, PhoneWhitelist
from app.services import sms, storage


def test_health(client):
    r = client.get("/api/health").json()
    assert r["ok"] is True
    assert "components" not in r

    detail = client.get("/api/health/detail").json()
    assert detail["ok"] is True
    assert detail["components"]["db"] == "ok"
    assert detail["components"]["redis"] == "ok"


def test_register_login_me(client):
    db = SessionLocal()
    db.add(PhoneWhitelist(phone="13700000002", note="x", department="dev"))
    db.commit()
    db.close()

    code = sms.send_code("13700000002")
    r = client.post("/api/auth/register",
                    json={"phone": "13700000002", "password": "secret1234",
                          "sms_code": code})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["phone"] == "13700000002"

    # wrong password
    bad = client.post("/api/auth/login",
                      json={"phone": "13700000002", "password": "nope"})
    assert bad.status_code == 401

    # not whitelisted
    no = client.post("/api/auth/register",
                     json={"phone": "13700000099", "password": "secret1234",
                           "sms_code": "000000"})
    assert no.status_code == 403


def test_generate_unlock_profile(client, make_user, auth):
    make_user("13900000001", balance=1000)
    h = auth("13900000001")

    r = client.post("/api/generate", json={
        "source_asset_url": "http://example.com/x.png",
        "source_type": "image", "category": "image", "stage": "preview",
        "instruction": "a calm cat", "params": {"n": 2, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    # eager Celery already ran the task
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 2
    a0 = t["assets"][0]
    assert a0["hd_url"] is None          # HD hidden before unlock
    assert a0["preview_url"]
    assert client.get(urlparse(a0["preview_url"]).path).status_code == 200

    # balance frozen then settled: image cost 5 * n(2) = 10 -> 1000-10 = 990
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 990

    # unlock -> charges unlock_cost (5) and reveals HD -> 985
    u = client.post(f"/api/assets/{a0['id']}/unlock", headers=h)
    assert u.status_code == 200
    assert u.json()["unlocked"] is True
    assert u.json()["hd_url"]
    assert client.get(urlparse(u.json()["hd_url"]).path).status_code == 404
    assert client.get(f"/api/assets/{a0['id']}/download", headers=h).status_code == 200
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 985

    # appears in personal gallery with expiry info
    g = client.get("/api/profile/assets", headers=h).json()
    assert any(x["id"] == a0["id"] for x in g)
    assert g[0]["days_left"] is not None


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

    def fake_download_to_path(url, path, **_kwargs):
        calls["url"] = url
        with open(path, "wb") as f:
            f.write(b"video-bytes")
        return len(b"video-bytes")

    monkeypatch.setattr("app.routers.assets.gateway.download_to_path", fake_download_to_path)
    r = client.get(f"/api/assets/{asset_id}/download", headers=h)
    assert r.status_code == 200, r.text
    assert r.content == b"video-bytes"
    assert calls["url"] == "https://cdn.example.com/final.mp4"


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
        "category": "video",
        "stage": "preview",
        "instruction": "slow product spin",
        "params": {"duration": 2, "resolution": "480p"},
    }, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert t["cost_frozen"] == 5
    assert t["cost_settled"] == 5
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 995


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
    assert seen["first_frame_image"].startswith("data:image/png;base64,")


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
    assert seen["first_frame_image"].startswith("data:image/png;base64,")
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert urlparse(task["assets"][0]["preview_url"]).path.startswith("/media/preview/")
    assert task["assets"][0]["unlocked"] is False


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

    db = SessionLocal()
    try:
        finals = db.query(GenTask).filter(
            GenTask.parent_task_id == parent_id,
            GenTask.stage == "final",
        ).all()
        assert len(finals) == 1
    finally:
        db.close()
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 945


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
    make_user("13900000017", balance=1000)
    h = auth("13900000017")

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
        "category": "image",
        "stage": "preview",
        "instruction": "same style",
        "params": {"n": 1, "reference_width": 720, "reference_height": 1280},
    }, headers=h)
    assert r.status_code == 200, r.text

    assert seen["size"] == "720x1280"


def test_image_generation_adapts_size_from_reference_at_4k_default(client, make_user, auth, monkeypatch):
    make_user("13900000079", balance=1000, admin=True)
    h = auth("13900000079")
    client.put("/api/admin/settings", json={"image_size": "4096x4096", "admin_password": "pass123456"}, headers=h)

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
        "category": "image",
        "stage": "preview",
        "instruction": "same style",
        "params": {"n": 1, "reference_width": 720, "reference_height": 1280},
    }, headers=h)
    assert r.status_code == 200, r.text

    assert seen["size"] == "2304x4096"


def test_insufficient_credits(client, make_user, auth):
    make_user("13900000003", balance=1)  # below image cost (5)
    h = auth("13900000003")
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
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


def _gen(client, h, n=2):
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
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

    f = client.post(f"/api/assets/{a['id']}/favorite", headers=h)
    assert f.status_code == 200 and f.json()["favorite"] is True
    fav = client.get("/api/profile/assets?favorite=true", headers=h).json()
    assert any(x["id"] == a["id"] for x in fav)

    d = client.delete(f"/api/assets/{a['id']}", headers=h)
    assert d.status_code == 200
    after = client.get("/api/profile/assets", headers=h).json()
    assert all(x["id"] != a["id"] for x in after)


def test_retry_requires_failed(client, make_user, auth):
    make_user("13900000015", balance=1000)
    h = auth("13900000015")
    tid = _gen(client, h)  # eager -> succeeded
    r = client.post(f"/api/tasks/{tid}/retry", headers=h)
    assert r.status_code == 400  # only failed tasks can be retried


def test_retry_reprices_failed_image_task(client, make_user, auth):
    uid = make_user("13900000082", balance=1000, admin=True)
    h = auth("13900000082")
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            prompt={"final_text": "retry reprices"},
            model_use="image",
            params={"n": 2, "size": "256x256"},
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
    assert task["cost_frozen"] == 10
