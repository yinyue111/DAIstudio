from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db import SessionLocal
from app.models import GenAsset, GenTask, UploadedAsset
from app.services import storage
from app.services.media_sidecars import storage_bytes_for_asset_urls
from app.services.upload_quota import user_media_usage_bytes


def test_retained_generated_assets_are_added_once_to_user_media_usage(client, make_user):
    user_id = make_user("13900001940", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            status="succeeded",
        )
        db.add(task)
        db.flush()
        db.add_all(
            [
                UploadedAsset(
                    key=f"upload/{uuid4().hex}.png",
                    user_id=user_id,
                    bytes=100,
                    retained_at=datetime.now(timezone.utc),
                ),
                UploadedAsset(
                    key=f"upload_preview/{uuid4().hex}.png",
                    user_id=user_id,
                    bytes=20,
                    retained_at=datetime.now(timezone.utc),
                ),
                GenAsset(
                    task_id=task.id,
                    user_id=user_id,
                    type="image",
                    bytes=300,
                    retained_at=datetime.now(timezone.utc),
                ),
                GenAsset(
                    task_id=task.id,
                    user_id=user_id,
                    type="image",
                    bytes=900,
                    retained_at=None,
                ),
            ]
        )
        db.commit()

        assert user_media_usage_bytes(db, user_id) == 420
    finally:
        db.close()


def test_generated_asset_byte_count_includes_preview_sidecar():
    stem = uuid4().hex
    preview_key = storage.save_bytes_named(b"preview", "preview", f"{stem}.png")
    storage.save_bytes_named(b"model-ref", "model_ref", f"{stem}.jpg")
    hd_key = storage.save_bytes_named(b"high-definition", "hd", f"{stem}.png")

    assert storage_bytes_for_asset_urls(
        storage.public_url(preview_key),
        storage.public_url(hd_key),
    ) == len(b"preview") + len(b"model-ref") + len(b"high-definition")


def test_legacy_profile_keeps_retained_expired_generated_asset(client, make_user, auth):
    user_id = make_user("13900001941", balance=1000)
    headers = auth("13900001941")
    old = datetime.now(timezone.utc) - timedelta(days=60)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            status="succeeded",
            created_at=old,
        )
        db.add(task)
        db.flush()
        retained = GenAsset(
            task_id=task.id,
            user_id=user_id,
            type="image",
            preview_url=storage.public_url(f"preview/{uuid4().hex}.png"),
            retained_at=datetime.now(timezone.utc),
            created_at=old,
        )
        expired = GenAsset(
            task_id=task.id,
            user_id=user_id,
            type="image",
            preview_url=storage.public_url(f"preview/{uuid4().hex}.png"),
            created_at=old,
        )
        db.add_all([retained, expired])
        db.commit()
        retained_id = retained.id
        expired_id = expired.id
    finally:
        db.close()

    gallery = client.get("/api/profile/assets", headers=headers)
    assert gallery.status_code == 200, gallery.text
    assert [item["id"] for item in gallery.json()] == [retained_id]
    assert gallery.json()[0]["expires_at"] is None
    assert gallery.json()[0]["days_left"] is None
    assert client.get("/api/profile", headers=headers).json()["stats"]["images"] == 1

    db = SessionLocal()
    try:
        assert db.get(GenAsset, retained_id) is not None
        assert db.get(GenAsset, expired_id) is None
    finally:
        db.close()
