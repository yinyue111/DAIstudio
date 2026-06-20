"""Retention math (expiry / days_left / expired), including tz-naive coercion."""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import GenTask, UploadedAsset
from app.services import retention, storage


def test_expiry_and_days_left():
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=10)
    assert retention.is_expired(created, 30) is False
    assert retention.days_left(created, 30) in (19, 20)  # ~20 days remaining

    old = now - timedelta(days=40)
    assert retention.is_expired(old, 30) is True
    assert retention.days_left(old, 30) == 0


def test_naive_datetime_coerced():
    # SQLite may hand back tz-naive datetimes; must not crash and treat as UTC
    naive_old = datetime.utcnow() - timedelta(days=40)
    assert retention.is_expired(naive_old, 30) is True
    naive_new = datetime.utcnow() - timedelta(days=1)
    assert retention.is_expired(naive_new, 30) is False


def test_none_created_at():
    assert retention.is_expired(None, 30) is False
    assert retention.days_left(None, 30) is None
    assert retention.expiry_of(None, 30) is None


def test_purge_parsed_previews_removes_file_and_row(client, make_user):
    uid = make_user("13900000430", balance=1000)
    key = storage.save_bytes(b"preview", "preview", "png")
    model_ref_key = storage.save_bytes_named(
        b"model-ref",
        "model_ref",
        key.split("/", 1)[1],
    )
    db = SessionLocal()
    try:
        row = UploadedAsset(
            key=key,
            user_id=uid,
            mime="image/png",
            bytes=7,
            original_filename="parsed-preview.png",
        )
        row.created_at = datetime.now(timezone.utc) - timedelta(days=10)
        db.add(row)
        db.add(UploadedAsset(
            key=model_ref_key,
            user_id=uid,
            mime="image/png",
            bytes=9,
            original_filename="parsed-model-ref.png",
            created_at=row.created_at,
        ))
        db.commit()
        path = storage.local_path(key)
        model_ref_path = storage.local_path(model_ref_key)
        assert path.exists()
        assert model_ref_path.exists()

        removed = retention.purge_parsed_previews(
            db,
            datetime.now(timezone.utc) - timedelta(days=7),
        )

        assert removed == 2
        assert db.get(UploadedAsset, key) is None
        assert db.get(UploadedAsset, model_ref_key) is None
        assert not path.exists()
        assert not model_ref_path.exists()
    finally:
        db.close()


def test_purge_uploaded_assets_removes_stale_upload_pair(client, make_user):
    uid = make_user("13900000431", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-pair.png")
    preview_key = storage.save_bytes_named(b"preview", "upload_preview", "retention-pair.png")
    model_ref_key = storage.save_bytes_named(b"model-ref", "upload_model_ref", "retention-pair.png")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        db.add(UploadedAsset(
            key=upload_key,
            user_id=uid,
            mime="image/png",
            bytes=6,
            original_filename="ref.png",
            created_at=old,
        ))
        db.add(UploadedAsset(
            key=preview_key,
            user_id=uid,
            mime="image/png",
            bytes=7,
            original_filename="preview:ref.png",
            created_at=old,
        ))
        db.add(UploadedAsset(
            key=model_ref_key,
            user_id=uid,
            mime="image/png",
            bytes=9,
            original_filename="model-ref:ref.png",
            created_at=old,
        ))
        db.commit()
        upload_path = storage.local_path(upload_key)
        preview_path = storage.local_path(preview_key)
        model_ref_path = storage.local_path(model_ref_key)

        removed = retention.purge_uploaded_assets(
            db,
            datetime.now(timezone.utc) - timedelta(days=30),
        )

        assert removed == 3
        assert db.get(UploadedAsset, upload_key) is None
        assert db.get(UploadedAsset, preview_key) is None
        assert db.get(UploadedAsset, model_ref_key) is None
        assert not upload_path.exists()
        assert not preview_path.exists()
        assert not model_ref_path.exists()
    finally:
        db.close()


def test_purge_uploaded_assets_removes_null_filename_upload(client, make_user):
    uid = make_user("13900000434", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-null-name.png")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        db.add(UploadedAsset(
            key=upload_key,
            user_id=uid,
            mime="image/png",
            bytes=6,
            original_filename=None,
            created_at=old,
        ))
        db.commit()

        removed = retention.purge_uploaded_assets(
            db,
            datetime.now(timezone.utc) - timedelta(days=30),
        )

        assert removed == 1
        assert db.get(UploadedAsset, upload_key) is None
        assert not storage.local_path(upload_key).exists()
    finally:
        db.close()


def test_purge_all_does_not_delete_generation_tasks(client, make_user, monkeypatch):
    uid = make_user("13900000435", balance=1000)
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=60)
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="needs_review",
            cost_frozen=50,
            cost_settled=0,
            created_at=old,
            error="等待人工对账",
        )
        settled = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            cost_frozen=10,
            cost_settled=10,
            created_at=old,
        )
        db.add_all([task, settled])
        db.commit()
        task_id = task.id
        settled_id = settled.id

        result = retention.purge_all(db)

        assert result["tasks"] == 0
        assert db.get(GenTask, task_id) is not None
        assert db.get(GenTask, settled_id) is not None
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_recent_params_reference(client, make_user):
    uid = make_user("13900000433", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-json-ref.png")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(
            key=upload_key,
            user_id=uid,
            mime="image/png",
            bytes=6,
            original_filename="ref.png",
            created_at=old,
        ))
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            params={"reference_image_url": storage.upload_api_url(upload_key)},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, upload_key) is not None
        assert storage.local_path(upload_key).exists()
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_recent_task_reference(client, make_user):
    uid = make_user("13900000432", balance=1000)
    upload_key = storage.save_bytes(b"upload", "upload", "png")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(
            key=upload_key,
            user_id=uid,
            mime="image/png",
            bytes=6,
            original_filename="ref.png",
            created_at=old,
        ))
        db.add(GenTask(
            user_id=uid,
            source_asset_url=storage.upload_api_url(upload_key),
            category="image",
            stage="preview",
            status="succeeded",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, upload_key) is not None
        assert storage.local_path(upload_key).exists()
    finally:
        db.close()
