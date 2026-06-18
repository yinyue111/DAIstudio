import io
from urllib.parse import urlparse

from PIL import Image

from app.db import SessionLocal
from app.models import GenTask, UploadedAsset
from app.services.gateway import _mock_image


def _png_bytes(size=(32, 48), color=(20, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_image_returns_reference_asset(client, make_user, auth):
    make_user("13900000100", balance=1000)
    h = auth("13900000100")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    asset = r.json()

    assert asset["type"] == "image"
    assert asset["width"] == 32
    assert asset["height"] == 48
    assert urlparse(asset["url"]).path.startswith("/api/uploads/upload/")
    assert urlparse(asset["thumb"]).path.startswith("/api/uploads/upload_preview/")
    assert client.get(urlparse(asset["url"]).path).status_code == 401
    assert client.get(urlparse(asset["url"]).path, headers=h).status_code == 200
    assert client.get(urlparse(asset["thumb"]).path).status_code == 401
    assert client.get(urlparse(asset["thumb"]).path, headers=h).status_code == 200


def test_upload_image_commits_even_when_audit_fails(client, make_user, auth, monkeypatch):
    make_user("13900000108", balance=1000)
    h = auth("13900000108")
    monkeypatch.setattr("app.routers.uploads.audit.log", lambda *_a, **_k: False)

    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    key = urlparse(r.json()["url"]).path.removeprefix("/api/uploads/")
    db = SessionLocal()
    try:
        assert db.get(UploadedAsset, key) is not None
    finally:
        db.close()


def test_upload_image_rejects_non_image(client, make_user, auth):
    make_user("13900000101", balance=1000)
    h = auth("13900000101")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("not-image.txt", b"hello", "text/plain")},
        headers=h,
    )
    assert r.status_code == 400
    assert "有效图片" in r.text


def test_uploaded_image_can_drive_reference_edit_generation(
    client, make_user, auth, monkeypatch
):
    make_user("13900000102", balance=1000)
    h = auth("13900000102")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen.update({
            "prompt": prompt,
            "n": n,
            "size": size,
            "reference_image_url": reference_image_url,
            "edit_path": edit_path,
        })
        return [_mock_image(prompt, "256x256", i) for i in range(n)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "turn this into a premium product poster",
            "instruction": "turn this into a premium product poster",
        },
        "params": {
            "n": 1,
            "size": "512x1024",
            "reference_width": asset["width"],
            "reference_height": asset["height"],
        },
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["reference_image_url"].startswith("data:image/png;base64,")
    assert seen["edit_path"] == "/v1/images/edits"
    assert seen["size"] == "512x1024"


def test_uploaded_image_requires_owner_for_read_and_generation(client, make_user, auth):
    make_user("13900000103", balance=1000)
    make_user("13900000104", balance=1000)
    owner_h = auth("13900000103")
    other_h = auth("13900000104")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=owner_h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    path = urlparse(asset["url"]).path

    assert client.get(path, headers=other_h).status_code == 404
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "use another user's image"},
        "params": {"n": 1, "size": "512x512"},
    }, headers=other_h)
    assert r.status_code == 404
    assert "上传素材" in r.text


def test_uploaded_image_can_drive_reverse_prompt(client, make_user, auth, monkeypatch):
    make_user("13900000105", balance=1000)
    h = auth("13900000105")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_reverse(refs, model_id, target="image"):
        seen["refs"] = refs
        return {"structured": {"主体": "uploaded"}, "final_text": "uploaded prompt"}

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fake_reverse)
    r = client.post("/api/prompt/reverse", json={
        "asset_url": asset["url"],
        "target": "image",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["refs"][0].startswith("data:image/png;base64,")


def test_uploaded_image_can_drive_video_first_frame(client, make_user, auth, monkeypatch):
    make_user("13900000106", balance=1000, admin=True)
    h = auth("13900000106")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.update(params)
        return "mock-upload-video"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "animate upload"},
        "params": {"duration": 5, "resolution": "720p", "ratio": "9:16"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["first_frame_image"].startswith("data:image/png;base64,")


def test_uploaded_image_final_video_uses_data_uri_first_frame(client, make_user, auth, monkeypatch):
    make_user("13900000107", balance=1000, admin=True)
    h = auth("13900000107")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = []

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.append(dict(params))
        return f"mock-upload-video-{len(seen)}"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)

    preview = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "animate upload"},
        "params": {"duration": 8, "resolution": "720p", "target_resolution": "1080p", "ratio": "9:16"},
    }, headers=h)
    assert preview.status_code == 200, preview.text
    db = SessionLocal()
    try:
        parent = db.get(GenTask, preview.json()["id"])
        assert parent.params["resolution"] == "1080p"
        assert parent.params["duration"] == 8
        assert parent.params["preview_resolution"] == "480p"
        assert parent.params["preview_duration"] == 5
    finally:
        db.close()

    final = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": preview.json()["id"],
        "params": {},
    }, headers=h)
    assert final.status_code == 200, final.text
    assert len(seen) >= 2
    assert seen[-1]["first_frame_image"].startswith("data:image/png;base64,")
