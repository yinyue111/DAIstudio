import base64
import io
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import AuditLog, GenTask, UploadedAsset
from app.services.gateway import _mock_image


def _png_bytes(size=(32, 48), color=(20, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif_bytes(size=(32, 48), color=(20, 120, 200)):
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x010F] = "PrivateCamera"
    exif[0x0132] = "2026:06:21 12:34:56"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _mp4_bytes(tmp_path):
    out = tmp_path / "ref.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x48:d=0.2",
            "-frames:v",
            "2",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return out.read_bytes()


def test_upload_image_returns_reference_asset(client, make_user, auth):
    uid = make_user("13900000100", balance=1000)
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
    url_key = urlparse(asset["url"]).path.removeprefix("/api/uploads/")
    thumb_key = urlparse(asset["thumb"]).path.removeprefix("/api/uploads/")
    assert url_key.startswith("upload/")
    assert thumb_key.startswith("upload_preview/")
    assert url_key.split("/", 1)[1].rsplit(".", 1)[0] == thumb_key.split("/", 1)[1].rsplit(".", 1)[0]
    model_ref_key = "upload_model_ref/" + url_key.split("/", 1)[1].rsplit(".", 1)[0] + ".jpg"
    db = SessionLocal()
    try:
        assert db.get(UploadedAsset, model_ref_key) is not None
        log = db.query(AuditLog).filter(
            AuditLog.user_id == uid,
            AuditLog.action == "upload_image",
        ).order_by(AuditLog.id.desc()).first()
        assert "rights_confirmed" not in log.detail
        assert "rights_confirmation" not in log.detail
    finally:
        db.close()
    anon = TestClient(app)
    assert anon.get(urlparse(asset["url"]).path).status_code == 401
    assert client.get(urlparse(asset["url"]).path, headers=h).status_code == 200
    assert anon.get(urlparse(asset["thumb"]).path).status_code == 401
    assert client.get(urlparse(asset["thumb"]).path, headers=h).status_code == 200
    assert client.get(f"/api/uploads/{model_ref_key}", headers=h).status_code == 404


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


def test_upload_image_strips_exif_and_stores_normalized_png(client, make_user, auth):
    make_user("13900001903", balance=1000)
    h = auth("13900001903")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("private.jpg", _jpeg_with_exif_bytes(), "image/jpeg")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    key = urlparse(r.json()["url"]).path.removeprefix("/api/uploads/")
    assert key.startswith("upload/")
    assert key.endswith(".png")

    resp = client.get(urlparse(r.json()["url"]).path, headers=h)
    assert resp.status_code == 200
    stored = Image.open(io.BytesIO(resp.content))
    assert stored.format == "PNG"
    assert not stored.getexif()
    db = SessionLocal()
    try:
        row = db.get(UploadedAsset, key)
        assert row is not None
        assert row.mime == "image/png"
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


def test_upload_image_allows_upload_without_rights_confirmation(client, make_user, auth):
    make_user("13900001904", balance=1000)
    h = auth("13900001904")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )

    assert r.status_code == 200, r.text


def test_upload_image_rejects_oversized_content_length(client, make_user, auth, monkeypatch):
    make_user("13900000109", balance=1000)
    h = auth("13900000109")
    monkeypatch.setattr("app.routers.uploads.settings.max_upload_image_bytes", 16)
    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    assert r.status_code == 413


def test_upload_image_rejects_user_storage_quota(client, make_user, auth, monkeypatch):
    uid = make_user("13900001901", balance=1000)
    h = auth("13900001901")
    monkeypatch.setattr("app.routers.uploads.settings.user_upload_storage_quota_bytes", 512)
    db = SessionLocal()
    try:
        db.add(UploadedAsset(
            key="upload/quota-existing.png",
            user_id=uid,
            mime="image/png",
            width=1,
            height=1,
            bytes=500,
            original_filename="existing.png",
        ))
        db.commit()
    finally:
        db.close()

    before = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}
    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 413
    assert "上传空间不足" in r.text
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(
            UploadedAsset.user_id == uid,
            UploadedAsset.key != "upload/quota-existing.png",
        ).count() == 0
    finally:
        db.close()


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
    assert seen["reference_image_url"].startswith("data:image/jpeg;base64,")
    ref_bytes = base64.b64decode(seen["reference_image_url"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert ref_img.size == (40, 80)
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
    assert seen["refs"][0].startswith("data:image/jpeg;base64,")


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
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")
    ref_bytes = base64.b64decode(seen["first_frame_image"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert min(ref_img.size) >= 300


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
    assert seen[-1]["first_frame_image"].startswith("data:image/jpeg;base64,")


def test_upload_video_returns_reference_asset(client, make_user, auth, tmp_path):
    uid = make_user("13900000110", balance=1000)
    h = auth("13900000110")

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    asset = r.json()

    assert asset["type"] == "video"
    assert asset["width"] == 32
    assert asset["height"] == 48
    url_key = urlparse(asset["url"]).path.removeprefix("/api/uploads/")
    assert url_key.startswith("upload_video/")
    anon = TestClient(app)
    assert anon.get(urlparse(asset["url"]).path).status_code == 401
    assert client.get(urlparse(asset["url"]).path, headers=h).status_code == 200
    if asset.get("thumb"):
        thumb_key = urlparse(asset["thumb"]).path.removeprefix("/api/uploads/")
        assert thumb_key.startswith("upload_video_preview/")
        assert client.get(urlparse(asset["thumb"]).path, headers=h).status_code == 200
    db = SessionLocal()
    try:
        log = db.query(AuditLog).filter(
            AuditLog.user_id == uid,
            AuditLog.action == "upload_video",
        ).order_by(AuditLog.id.desc()).first()
        assert "rights_confirmed" not in log.detail
        assert "rights_confirmation" not in log.detail
    finally:
        db.close()


def test_upload_video_rejects_when_ffprobe_missing(client, make_user, auth, monkeypatch, tmp_path):
    make_user("13900000115", balance=1000)
    h = auth("13900000115")
    monkeypatch.setattr("app.services.video_frames.FFPROBE", None)

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )

    assert r.status_code == 400
    assert "ffprobe" in r.text


def test_upload_video_allows_upload_without_rights_confirmation(client, make_user, auth, tmp_path):
    make_user("13900001905", balance=1000)
    h = auth("13900001905")

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )

    assert r.status_code == 200, r.text


def test_upload_video_rejects_over_duration_limit(client, make_user, auth, monkeypatch, tmp_path):
    make_user("13900000116", balance=1000)
    h = auth("13900000116")
    monkeypatch.setattr("app.routers.uploads.settings.max_video_seconds", 0)

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )

    assert r.status_code == 400
    assert "视频时长不能超过" in r.text


def test_upload_video_rejects_user_storage_quota_and_cleans_file(
    client, make_user, auth, monkeypatch, tmp_path
):
    uid = make_user("13900001902", balance=1000)
    h = auth("13900001902")
    mp4 = _mp4_bytes(tmp_path)
    monkeypatch.setattr("app.routers.uploads.settings.user_upload_storage_quota_bytes", max(1, len(mp4) - 1))

    before = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}
    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", mp4, "video/mp4")},
        headers=h,
    )
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 413
    assert "上传空间不足" in r.text
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(UploadedAsset.user_id == uid).count() == 0
    finally:
        db.close()


def test_upload_video_quota_preflight_rejects_before_video_probe(
    client, make_user, auth, monkeypatch, tmp_path
):
    uid = make_user("13900001906", balance=1000)
    h = auth("13900001906")
    mp4 = _mp4_bytes(tmp_path)
    monkeypatch.setattr("app.routers.uploads.settings.user_upload_storage_quota_bytes", 1)

    def fail_probe(_path):
        raise AssertionError("video probe should not run after quota preflight rejection")

    monkeypatch.setattr("app.routers.uploads._inspect_video_and_poster", fail_probe)
    before = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}
    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", mp4, "video/mp4")},
        headers={**h, "Content-Length": str(len(mp4) + 2 * 1024 * 1024)},
    )
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 413
    assert "上传空间不足" in r.text
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(UploadedAsset.user_id == uid).count() == 0
    finally:
        db.close()


def test_uploaded_video_requires_owner_for_read_and_generation(client, make_user, auth, tmp_path):
    make_user("13900000111", balance=1000)
    make_user("13900000112", balance=1000)
    owner_h = auth("13900000111")
    other_h = auth("13900000112")

    up = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=owner_h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()

    assert client.get(urlparse(asset["url"]).path, headers=other_h).status_code == 404
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "video",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "edit another user's video"},
        "params": {"duration": 5, "resolution": "720p", "ratio": "9:16"},
    }, headers=other_h)
    assert r.status_code == 404
    assert "上传素材" in r.text


def test_uploaded_video_can_drive_video_first_frame(client, make_user, auth, monkeypatch, tmp_path):
    make_user("13900000113", balance=1000, admin=True)
    h = auth("13900000113")
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
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
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
        "source_type": "video",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "edit uploaded video into a polished ad"},
        "params": {"duration": 5, "resolution": "720p", "ratio": "9:16"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")
