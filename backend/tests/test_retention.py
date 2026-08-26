"""Retention math (expiry / days_left / expired), including tz-naive coercion."""
from datetime import datetime, timedelta, timezone

from app.config import Settings, settings
from app.db import SessionLocal
from app.models import (
    AssetReport,
    CreditTransaction,
    GenAsset,
    GenTask,
    ParseRecord,
    ReverseOperation,
    UploadedAsset,
    User,
    UserDraft,
)
from app.services import credits, retention, storage
from app.services.config_store import set_setting


def _clear_active_generation_tasks(db):
    db.query(GenTask).filter(GenTask.status.in_(("queued", "running"))).update(
        {GenTask.status: "failed"},
        synchronize_session=False,
    )
    db.commit()


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


def test_asset_retention_default_is_ten_years():
    assert Settings.model_fields["asset_retention_days"].default == 3650


def test_admin_retention_setting_applies_to_existing_assets(client, make_user, auth):
    uid = make_user("13900000448", balance=1000)
    make_user("13900000449", balance=1000, admin=True)
    headers = auth("13900000449")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        asset = GenAsset(
            user_id=uid,
            type="image",
            preview_url=None,
            moderation_status="active",
            created_at=old,
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id

        response = client.put(
            "/api/admin/settings",
            json={"asset_retention_days": 45},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["asset_retention_days"] == 45
        db.expire_all()
        assert retention.get_retention_days(db) == 45
        assert retention.purge_expired(db, user_id=uid) == 0
        assert db.get(GenAsset, asset_id) is not None

        response = client.put(
            "/api/admin/settings",
            json={"asset_retention_days": 30},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        db.expire_all()
        assert retention.get_retention_days(db) == 30
        assert retention.purge_expired(db, user_id=uid) == 1
        assert db.get(GenAsset, asset_id) is None
    finally:
        db.close()


def test_asset_retention_setting_rejects_out_of_range_values(client, make_user, auth):
    make_user("13900000450", balance=1000, admin=True)
    headers = auth("13900000450")

    for value in (0, 3651):
        response = client.put(
            "/api/admin/settings",
            json={"asset_retention_days": value},
            headers=headers,
        )
        assert response.status_code == 422


def test_asset_retention_falls_back_to_environment_default(client):
    db = SessionLocal()
    try:
        assert retention.get_retention_days(db) == settings.asset_retention_days
        set_setting(db, "asset_retention_days", 60)
        assert retention.get_retention_days(db) == 60
    finally:
        db.close()


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


def test_purge_uploaded_assets_keeps_files_when_commit_fails(client, make_user, monkeypatch):
    uid = make_user("13900000435", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-rollback.png")
    preview_key = storage.save_bytes_named(b"preview", "upload_preview", "retention-rollback.png")
    upload_path = storage.local_path(upload_key)
    preview_path = storage.local_path(preview_key)
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
        db.commit()

        def fail_commit():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(db, "commit", fail_commit)

        try:
            retention.purge_uploaded_assets(db, datetime.now(timezone.utc) - timedelta(days=30))
        except RuntimeError:
            db.rollback()
        else:
            raise AssertionError("purge should surface commit failure")

        assert upload_path.exists()
        assert preview_path.exists()
        assert db.get(UploadedAsset, upload_key) is not None
        assert db.get(UploadedAsset, preview_key) is not None
    finally:
        db.close()
        cleanup = SessionLocal()
        try:
            for key in (upload_key, preview_key):
                row = cleanup.get(UploadedAsset, key)
                if row:
                    cleanup.delete(row)
            cleanup.commit()
        finally:
            cleanup.close()
        upload_path.unlink(missing_ok=True)
        preview_path.unlink(missing_ok=True)


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


def test_purge_uploaded_assets_removes_stale_video_pair(client, make_user):
    uid = make_user("13900000436", balance=1000)
    video_key = storage.save_bytes_named(b"video", "upload_video", "retention-video.mp4")
    poster_key = storage.save_bytes_named(b"poster", "upload_video_preview", "retention-video.jpg")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        db.add(UploadedAsset(
            key=video_key,
            user_id=uid,
            mime="video/mp4",
            bytes=5,
            original_filename="ref.mp4",
            created_at=old,
        ))
        db.add(UploadedAsset(
            key=poster_key,
            user_id=uid,
            mime="image/jpeg",
            bytes=6,
            original_filename="poster:ref.mp4",
            created_at=old,
        ))
        db.commit()

        removed = retention.purge_uploaded_assets(
            db,
            datetime.now(timezone.utc) - timedelta(days=30),
        )

        assert removed == 2
        assert db.get(UploadedAsset, video_key) is None
        assert db.get(UploadedAsset, poster_key) is None
        assert not storage.local_path(video_key).exists()
        assert not storage.local_path(poster_key).exists()
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_recent_video_reference(client, make_user):
    uid = make_user("13900000437", balance=1000)
    video_key = storage.save_bytes_named(b"video", "upload_video", "retention-video-ref.mp4")
    poster_key = storage.save_bytes_named(b"poster", "upload_video_preview", "retention-video-ref.jpg")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(
            key=video_key,
            user_id=uid,
            mime="video/mp4",
            bytes=5,
            original_filename="ref.mp4",
            created_at=old,
        ))
        db.add(UploadedAsset(
            key=poster_key,
            user_id=uid,
            mime="image/jpeg",
            bytes=6,
            original_filename="poster:ref.mp4",
            created_at=old,
        ))
        db.add(GenTask(
            user_id=uid,
            source_asset_url=storage.upload_api_url(video_key),
            category="video",
            stage="preview",
            status="succeeded",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, video_key) is not None
        assert db.get(UploadedAsset, poster_key) is not None
        assert storage.local_path(video_key).exists()
        assert storage.local_path(poster_key).exists()
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_preview_only_reference(client, make_user):
    uid = make_user("13900000444", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-preview-only.png")
    preview_key = storage.save_bytes_named(b"preview", "upload_preview", "retention-preview-only.png")
    model_ref_key = storage.save_bytes_named(b"model-ref", "upload_model_ref", "retention-preview-only.jpg")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for key, mime, name in (
            (upload_key, "image/png", "ref.png"),
            (preview_key, "image/png", "preview:ref.png"),
            (model_ref_key, "image/jpeg", "model-ref:ref.png"),
        ):
            db.add(UploadedAsset(
                key=key,
                user_id=uid,
                mime=mime,
                bytes=7,
                original_filename=name,
                created_at=old,
            ))
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            params={"reference_image_url": storage.upload_api_url(preview_key)},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, upload_key) is not None
        assert db.get(UploadedAsset, preview_key) is not None
        assert db.get(UploadedAsset, model_ref_key) is not None
        assert storage.local_path(upload_key).exists()
        assert storage.local_path(preview_key).exists()
        assert storage.local_path(model_ref_key).exists()
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_video_poster_only_reference(client, make_user):
    uid = make_user("13900000445", balance=1000)
    video_key = storage.save_bytes_named(b"video", "upload_video", "retention-poster-only.mp4")
    poster_key = storage.save_bytes_named(b"poster", "upload_video_preview", "retention-poster-only.jpg")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(
            key=video_key,
            user_id=uid,
            mime="video/mp4",
            bytes=5,
            original_filename="ref.mp4",
            created_at=old,
        ))
        db.add(UploadedAsset(
            key=poster_key,
            user_id=uid,
            mime="image/jpeg",
            bytes=6,
            original_filename="poster:ref.mp4",
            created_at=old,
        ))
        db.add(GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="succeeded",
            params={"first_frame_image": storage.upload_api_url(poster_key)},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, video_key) is not None
        assert db.get(UploadedAsset, poster_key) is not None
        assert storage.local_path(video_key).exists()
        assert storage.local_path(poster_key).exists()
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_old_needs_review_video_reference(client, make_user):
    uid = make_user("13900000440", balance=1000)
    video_key = storage.save_bytes_named(b"video", "upload_video", "retention-review-video.mp4")
    poster_key = storage.save_bytes_named(b"poster", "upload_video_preview", "retention-review-video.jpg")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=60)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(
            key=video_key,
            user_id=uid,
            mime="video/mp4",
            bytes=5,
            original_filename="review.mp4",
            created_at=old,
        ))
        db.add(UploadedAsset(
            key=poster_key,
            user_id=uid,
            mime="image/jpeg",
            bytes=6,
            original_filename="poster:review.mp4",
            created_at=old,
        ))
        db.add(GenTask(
            user_id=uid,
            source_asset_url=storage.upload_api_url(video_key),
            category="video",
            stage="preview",
            status="needs_review",
            created_at=old,
            params={"first_frame_image": storage.upload_api_url(poster_key)},
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, video_key) is not None
        assert db.get(UploadedAsset, poster_key) is not None
        assert storage.local_path(video_key).exists()
        assert storage.local_path(poster_key).exists()
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


def test_purge_all_expires_anonymous_and_tombstones_named_reverse_operations(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13900000456", balance=100)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=91)
    db = SessionLocal()
    operation_ids: list[int] = []
    try:
        operations = [
            ReverseOperation(
                user_id=uid,
                client_request_id=client_request_id,
                request_fingerprint=character * 64,
                target="image",
                asset_url=f"https://cdn.example.com/{index}.jpg",
                status=status,
                result=result,
                error=error,
                created_at=created_at,
                updated_at=created_at,
            )
            for index, (
                status,
                character,
                created_at,
                client_request_id,
                result,
                error,
            ) in enumerate(
                (
                    ("succeeded", "a", old, None, {"final_text": "anonymous"}, None),
                    ("failed", "b", old, None, None, "anonymous failure"),
                    ("running", "c", old, None, None, None),
                    ("succeeded", "d", now, None, {"final_text": "recent"}, None),
                    (
                        "succeeded",
                        "e",
                        old,
                        "reverse-retention-success",
                        {"structured": {"subject": "x"}, "final_text": "named replay"},
                        None,
                    ),
                    (
                        "failed",
                        "f",
                        old,
                        "reverse-retention-failure",
                        {"provider": "sensitive"},
                        "provider secret",
                    ),
                )
            )
        ]
        db.add_all(operations)
        db.commit()
        operation_ids = [operation.id for operation in operations]

        monkeypatch.setattr(
            retention,
            "get_setting",
            lambda _db, key, default=None: 90 if key == "audit_retention_days" else default,
        )

        result = retention.purge_all(db)

        assert result["reverse_operations"] == 2
        assert result["reverse_operation_tombstones"] == 2
        assert db.get(ReverseOperation, operation_ids[0]) is None
        assert db.get(ReverseOperation, operation_ids[1]) is None
        assert db.get(ReverseOperation, operation_ids[2]).status == "running"
        assert db.get(ReverseOperation, operation_ids[3]).status == "succeeded"
        named_success = db.get(ReverseOperation, operation_ids[4])
        assert named_success.status == "succeeded"
        assert named_success.asset_url == ""
        assert named_success.result is None
        assert named_success.error_code == retention.REVERSE_RESULT_EXPIRED_CODE
        named_failure = db.get(ReverseOperation, operation_ids[5])
        assert named_failure.status == "failed"
        assert named_failure.asset_url == ""
        assert named_failure.error is None
        assert named_failure.result is None
        assert named_failure.error_code == retention.REVERSE_RESULT_EXPIRED_CODE

        repeated = retention.purge_all(db)
        assert repeated["reverse_operations"] == 0
        assert repeated["reverse_operation_tombstones"] == 0
    finally:
        db.rollback()
        if operation_ids:
            db.query(ReverseOperation).filter(
                ReverseOperation.id.in_(operation_ids)
            ).delete(synchronize_session=False)
            db.commit()
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


def test_purge_uploaded_assets_keeps_product_reference_group(client, make_user):
    uid = make_user("13900001978", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-product-ref.png")
    preview_key = storage.save_bytes_named(
        b"preview",
        "upload_preview",
        "retention-product-ref.png",
    )
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for key, byte_count in ((upload_key, 6), (preview_key, 7)):
            db.add(UploadedAsset(
                key=key,
                user_id=uid,
                mime="image/png",
                bytes=byte_count,
                original_filename="product.png",
                created_at=old,
            ))
        db.add(GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="succeeded",
            params={"product_reference_image": storage.upload_api_url(upload_key)},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, upload_key) is not None
        assert db.get(UploadedAsset, preview_key) is not None
        assert storage.local_path(upload_key).exists()
        assert storage.local_path(preview_key).exists()
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_recent_mask_reference(client, make_user):
    uid = make_user("13900000463", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-mask-ref.png")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(
            key=upload_key,
            user_id=uid,
            mime="image/png",
            bytes=6,
            original_filename="mask.png",
            created_at=old,
        ))
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            params={"mask_image_url": storage.upload_api_url(upload_key)},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        assert db.get(UploadedAsset, upload_key) is not None
    finally:
        db.close()


def test_referenced_upload_urls_is_candidate_scoped_and_chunked(client, make_user):
    uid = make_user("13900000468", balance=1000)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    candidate_urls = {
        storage.upload_api_url(f"upload/retention-candidate-{index}.png")
        for index in range(1205)
    }
    referenced_candidate = storage.upload_api_url("upload/retention-candidate-1204.png")
    unrelated_url = storage.upload_api_url("upload/retention-unrelated.png")
    db = SessionLocal()
    try:
        db.add_all(
            [
                GenTask(
                    user_id=uid,
                    category="image",
                    stage="preview",
                    status="succeeded",
                    params={"reference_image_url": unrelated_url, "padding": "x" * 1000},
                    created_at=datetime.now(timezone.utc) - timedelta(days=1),
                )
                for _ in range(250)
            ]
        )
        db.add(GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            params={"mask_image_url": referenced_candidate},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()

        refs = retention._referenced_upload_urls(db, cutoff, candidate_urls)
        assert refs == {referenced_candidate}
    finally:
        db.close()


def test_purge_uploaded_assets_keeps_same_owner_studio_draft_upload_group(
    client,
    make_user,
):
    uid = make_user("13900000464", balance=1000)
    stem = "retention-studio-ref"
    upload_key = storage.save_bytes_named(b"upload", "upload", f"{stem}.png")
    preview_key = storage.save_bytes_named(b"preview", "upload_preview", f"{stem}.png")
    model_ref_key = storage.save_bytes_named(b"model-ref", "upload_model_ref", f"{stem}.jpg")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for key in (upload_key, preview_key, model_ref_key):
            db.add(UploadedAsset(key=key, user_id=uid, created_at=old))
        db.add(UserDraft(
            user_id=uid,
            key="studio",
            payload={
                "workspaces": {
                    "image_edit": {
                        "assets": [],
                        "selected": {"url": storage.upload_api_url(preview_key)},
                    }
                }
            },
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 0
        for key in (upload_key, preview_key, model_ref_key):
            assert db.get(UploadedAsset, key) is not None
            assert storage.local_path(key).exists()
    finally:
        db.close()


def test_studio_draft_cannot_pin_another_users_upload(client, make_user):
    owner_id = make_user("13900000465", balance=1000)
    attacker_id = make_user("13900000466", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-cross-owner.png")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(key=upload_key, user_id=owner_id, created_at=old))
        db.add(UserDraft(
            user_id=attacker_id,
            key="studio",
            payload={
                "workspaces": {
                    "image": {
                        "productAsset": {"original_url": storage.upload_api_url(upload_key)},
                    }
                }
            },
        ))
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 1
        assert db.get(UploadedAsset, upload_key) is None
        assert not storage.local_path(upload_key).exists()
    finally:
        db.close()


def test_replaced_studio_draft_reference_no_longer_pins_upload(client, make_user):
    uid = make_user("13900000467", balance=1000)
    upload_key = storage.save_bytes_named(b"upload", "upload", "retention-replaced-draft.png")
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=40)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(UploadedAsset(key=upload_key, user_id=uid, created_at=old))
        draft = UserDraft(
            user_id=uid,
            key="studio",
            payload={
                "workspaces": {
                    "image": {"variationSource": {"thumb": storage.upload_api_url(upload_key)}}
                }
            },
        )
        db.add(draft)
        db.commit()
        draft.payload = {"workspaces": {"image": {"variationSource": None}}}
        db.commit()

        assert retention.purge_uploaded_assets(db, cutoff) == 1
        assert db.get(UploadedAsset, upload_key) is None
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


def test_reap_stuck_parse_records_fails_queued_and_running(client, make_user):
    uid = make_user("13900000442", balance=1000)
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=40)
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        queued = ParseRecord(
            user_id=uid,
            url="https://example.com/queued",
            status="queued",
            created_at=old,
        )
        running = ParseRecord(
            user_id=uid,
            url="https://example.com/running",
            status="running",
            created_at=old,
        )
        recent_running = ParseRecord(
            user_id=uid,
            url="https://example.com/recent",
            status="running",
            created_at=recent,
        )
        done = ParseRecord(
            user_id=uid,
            url="https://example.com/done",
            status="done",
            created_at=old,
        )
        db.add_all([queued, running, recent_running, done])
        db.commit()
        ids = {
            "queued": queued.id,
            "running": running.id,
            "recent_running": recent_running.id,
            "done": done.id,
        }

        assert retention.reap_stuck_parse_records(db, max_minutes=30) == 2

        db.expire_all()
        assert db.get(ParseRecord, ids["queued"]).status == "failed"
        assert db.get(ParseRecord, ids["running"]).status == "failed"
        assert db.get(ParseRecord, ids["recent_running"]).status == "running"
        assert db.get(ParseRecord, ids["done"]).status == "done"
    finally:
        db.close()


def test_reap_stale_reverse_operation_refunds_charge_once(client, make_user):
    uid = make_user("13900000452", balance=100)
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        operation = ReverseOperation(
            user_id=uid,
            client_request_id=None,
            request_fingerprint="a" * 64,
            target="image",
            asset_url="https://cdn.example.com/stale.jpg",
            status="running",
            charged_credits=2,
            created_at=old,
            updated_at=old,
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id
        credits.consume(db, uid, 2, biz_type="reverse", biz_ref=operation_id)

        assert retention.reap_stuck_reverse_operations(db, max_minutes=15) == 1
        assert retention.reap_stuck_reverse_operations(db, max_minutes=15) == 0

        db.expire_all()
        reaped = db.get(ReverseOperation, operation_id)
        assert reaped.status == "failed"
        assert reaped.charged_credits == 0
        assert "超时" in (reaped.error or "")
        assert db.get(User, uid).balance_credits == 100
        refunds = db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse",
            biz_ref=operation_id,
            type="refund",
        ).count()
        assert refunds == 1
    finally:
        db.close()


def test_reap_stale_reverse_operation_does_not_refund_concurrent_success(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13900000453", balance=100)
    db = SessionLocal()
    original_execute = db.execute
    success_committed = {"done": False}
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="reverse-race-success",
            request_fingerprint="b" * 64,
            target="image",
            asset_url="https://cdn.example.com/race.jpg",
            status="running",
            charged_credits=2,
            created_at=old,
            updated_at=old,
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id
        credits.consume(db, uid, 2, biz_type="reverse", biz_ref=operation_id)

        def execute_with_concurrent_success(statement, *args, **kwargs):
            if (
                not success_committed["done"]
                and getattr(statement, "is_update", False)
                and getattr(getattr(statement, "table", None), "name", None) == "reverse_operations"
            ):
                success_committed["done"] = True
                operation.status = "succeeded"
                operation.result = {"structured": {"主体": "x"}, "final_text": "x"}
                operation.updated_at = datetime.now(timezone.utc)
                db.commit()
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", execute_with_concurrent_success)

        assert retention.reap_stuck_reverse_operations(db, max_minutes=15) == 0

        db.expire_all()
        completed = db.get(ReverseOperation, operation_id)
        assert completed.status == "succeeded"
        assert completed.charged_credits == 2
        assert db.get(User, uid).balance_credits == 98
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse",
            biz_ref=operation_id,
            type="refund",
        ).count() == 0
    finally:
        db.close()


def test_reap_stale_uncharged_reverse_operation_fails_without_refund(client, make_user):
    uid = make_user("13900000454", balance=100)
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        operation = ReverseOperation(
            user_id=uid,
            client_request_id=None,
            request_fingerprint="c" * 64,
            target="image",
            asset_url="https://cdn.example.com/uncharged.jpg",
            status="running",
            charged_credits=0,
            created_at=old,
            updated_at=old,
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id

        assert retention.reap_stuck_reverse_operations(db, max_minutes=15) == 1

        db.expire_all()
        failed = db.get(ReverseOperation, operation_id)
        assert failed.status == "failed"
        assert failed.charged_credits == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse",
            biz_ref=operation_id,
            type="refund",
        ).count() == 0
    finally:
        db.close()


def test_reap_reverse_operation_rechecks_freshness_when_claiming(client, make_user, monkeypatch):
    uid = make_user("13900000455", balance=100)
    db = SessionLocal()
    original_execute = db.execute
    refreshed = {"done": False}
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        operation = ReverseOperation(
            user_id=uid,
            client_request_id=None,
            request_fingerprint="d" * 64,
            target="image",
            asset_url="https://cdn.example.com/refreshed.jpg",
            status="running",
            charged_credits=0,
            created_at=old,
            updated_at=old,
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id

        def execute_with_concurrent_refresh(statement, *args, **kwargs):
            if (
                not refreshed["done"]
                and getattr(statement, "is_update", False)
                and getattr(getattr(statement, "table", None), "name", None) == "reverse_operations"
            ):
                refreshed["done"] = True
                operation.updated_at = datetime.now(timezone.utc)
                db.commit()
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", execute_with_concurrent_refresh)

        assert retention.reap_stuck_reverse_operations(db, max_minutes=15) == 0

        db.expire_all()
        assert db.get(ReverseOperation, operation_id).status == "running"
    finally:
        db.close()


def test_reverse_reap_default_window_exceeds_gateway_timeout(monkeypatch):
    monkeypatch.setattr(settings, "reverse_gateway_timeout_seconds", 40 * 60)

    assert retention._reverse_reap_window() > timedelta(minutes=40)


def test_reverse_reap_explicit_window_overrides_gateway_default(client, make_user, monkeypatch):
    uid = make_user("13900000457", balance=100)
    monkeypatch.setattr(settings, "reverse_gateway_timeout_seconds", 2 * 60 * 60)
    db = SessionLocal()
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        operation = ReverseOperation(
            user_id=uid,
            client_request_id=None,
            request_fingerprint="f" * 64,
            target="image",
            asset_url="https://cdn.example.com/override.jpg",
            status="running",
            created_at=old,
            updated_at=old,
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id

        assert retention.reap_stuck_reverse_operations(db, max_minutes=5) == 1
        db.expire_all()
        assert db.get(ReverseOperation, operation_id).status == "failed"
    finally:
        db.close()


def test_reverse_reap_refund_failure_rolls_back_only_that_operation(
    client,
    make_user,
    monkeypatch,
):
    successful_uid = make_user("13900000958", balance=100)
    failing_uid = make_user("13900000959", balance=100)
    db = SessionLocal()
    operation_ids: list[int] = []
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        successful = ReverseOperation(
            user_id=successful_uid,
            client_request_id=None,
            request_fingerprint="1" * 64,
            target="image",
            asset_url="https://cdn.example.com/refund-ok.jpg",
            status="running",
            charged_credits=2,
            created_at=old,
            updated_at=old,
        )
        failing = ReverseOperation(
            user_id=failing_uid,
            client_request_id=None,
            request_fingerprint="2" * 64,
            target="image",
            asset_url="https://cdn.example.com/refund-fail.jpg",
            status="running",
            charged_credits=2,
            created_at=old,
            updated_at=old,
        )
        db.add_all([successful, failing])
        db.commit()
        successful_id = successful.id
        failing_id = failing.id
        operation_ids = [successful_id, failing_id]
        credits.consume(db, successful_uid, 2, biz_type="reverse", biz_ref=successful_id)
        credits.consume(db, failing_uid, 2, biz_type="reverse", biz_ref=failing_id)

        original_refund = retention.credits.refund_consumed

        def refund_with_one_failure(db, user_id, amount, **kwargs):
            if kwargs.get("biz_ref") == failing_id:
                raise RuntimeError("refund unavailable")
            return original_refund(db, user_id, amount, **kwargs)

        monkeypatch.setattr(retention.credits, "refund_consumed", refund_with_one_failure)

        assert retention.reap_stuck_reverse_operations(db, max_minutes=15) == 1

        db.expire_all()
        completed = db.get(ReverseOperation, successful_id)
        unchanged = db.get(ReverseOperation, failing_id)
        assert completed.status == "failed"
        assert completed.charged_credits == 0
        assert db.get(User, successful_uid).balance_credits == 100
        assert unchanged.status == "running"
        assert unchanged.charged_credits == 2
        assert db.get(User, failing_uid).balance_credits == 98
        assert db.query(CreditTransaction).filter_by(
            user_id=failing_uid,
            biz_type="reverse",
            biz_ref=failing_id,
            type="refund",
        ).count() == 0
    finally:
        db.rollback()
        if operation_ids:
            db.query(ReverseOperation).filter(
                ReverseOperation.id.in_(operation_ids)
            ).delete(synchronize_session=False)
            db.commit()
        db.close()


def test_reaper_skips_image_inside_configured_gateway_window(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 600)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 600)
    monkeypatch.setattr(settings, "celery_task_time_limit_seconds", 900)
    uid = make_user("13900000438", balance=1000)
    db = SessionLocal()
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="running",
            cost_frozen=20,
            cost_settled=0,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=19),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)

        assert retention.reap_stuck_tasks(db, max_minutes=5) == 0
        kept = db.get(GenTask, task_id)
        assert kept.status == "running"
    finally:
        db.close()


def test_reaper_skips_queued_image_until_queue_window(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 60)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 60)
    uid = make_user("13900000448", balance=1000)
    db = SessionLocal()
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="queued",
            cost_frozen=20,
            cost_settled=0,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)

        assert retention.reap_stuck_tasks(db, max_minutes=60) == 0
        kept = db.get(GenTask, task_id)
        assert kept.status == "queued"
    finally:
        db.close()


def test_reaper_uses_image_render_started_at_not_task_created_at(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 60)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 60)
    uid = make_user("13900000449", balance=1000)
    db = SessionLocal()
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="running",
            phase="rendering",
            cost_frozen=20,
            cost_settled=0,
            params={
                "_image_render_started_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=30)
                ).isoformat(),
            },
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)

        assert retention.reap_stuck_tasks(db, max_minutes=5) == 0
        kept = db.get(GenTask, task_id)
        assert kept.status == "running"
        assert kept.phase == "rendering"
    finally:
        db.close()


def test_reaper_refunds_image_after_configured_gateway_window(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 60)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 60)
    monkeypatch.setattr(settings, "celery_task_time_limit_seconds", 60)
    uid = make_user("13900000439", balance=1000)
    db = SessionLocal()
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="running",
            cost_frozen=20,
            cost_settled=0,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)

        assert retention.reap_stuck_tasks(db, max_minutes=5) == 1
        reaped = db.get(GenTask, task_id)
        assert reaped.status == "failed"
        assert "任务超时" in reaped.error
    finally:
        db.close()


def test_reaper_skips_image_reconciling_task(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 60)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 60)
    uid = make_user("13900000442", balance=1000)
    db = SessionLocal()
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="running",
            phase="reconciling",
            cost_frozen=20,
            cost_settled=0,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)

        assert retention.reap_stuck_tasks(db, max_minutes=5) == 0
        kept = db.get(GenTask, task_id)
        assert kept.status == "running"
        assert kept.phase == "reconciling"
    finally:
        db.close()


def test_image_reaper_window_ignores_global_celery_lifecycle_limit(monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 60)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 90)
    monkeypatch.setattr(settings, "celery_task_time_limit_seconds", 8100)

    assert retention._image_reap_window() == timedelta(seconds=60 + 90 + 300)


def test_reaper_holds_for_review_when_refund_fails(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 60)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 60)
    monkeypatch.setattr(settings, "celery_task_time_limit_seconds", 60)
    uid = make_user("13900000441", balance=1000)
    db = SessionLocal()
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="running",
            cost_frozen=20,
            cost_settled=0,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)
        user_after_freeze = db.get(User, uid)
        frozen_after_freeze = user_after_freeze.frozen_credits
        balance_after_freeze = user_after_freeze.balance_credits

        def fail_refund(*_args, **_kwargs):
            raise RuntimeError("ledger mismatch")

        monkeypatch.setattr(retention.credits, "refund", fail_refund)

        assert retention.reap_stuck_tasks(db, max_minutes=5) == 1
        held = db.get(GenTask, task_id)
        assert held.status == "needs_review"
        assert held.phase == "reconciling"
        assert "自动退款失败" in held.error
        user = db.get(User, uid)
        assert user.frozen_credits == frozen_after_freeze
        assert user.balance_credits == balance_after_freeze
    finally:
        db.close()


def test_reaper_does_not_reopen_task_canceled_after_snapshot(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "image_gateway_timeout_seconds", 60)
    monkeypatch.setattr(settings, "image_download_timeout_seconds", 60)
    uid = make_user("13900000443", balance=1000)
    db = SessionLocal()
    original_execute = db.execute
    snapshot_seen = {"done": False}
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="running",
            cost_frozen=20,
            cost_settled=0,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)

        def execute_with_concurrent_cancel(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            statement = args[0] if args else None
            if not snapshot_seen["done"] and str(statement).startswith("SELECT gen_tasks.id"):
                rows = result.mappings().all()
                snapshot_seen["done"] = True
                task.status = "canceled"
                db.commit()

                class SnapshotResult:
                    def mappings(self):
                        return rows

                return SnapshotResult()
            return result

        monkeypatch.setattr(db, "execute", execute_with_concurrent_cancel)

        assert retention.reap_stuck_tasks(db, max_minutes=5) == 0
        kept = db.get(GenTask, task_id)
        assert kept.status == "canceled"
    finally:
        db.close()


def test_reaper_uses_video_download_started_at_not_task_created_at(client, make_user, monkeypatch):
    monkeypatch.setattr(retention.settings, "video_download_timeout_seconds", 60)
    monkeypatch.setattr(retention.settings, "video_download_max_attempts", 1)
    uid = make_user("13900000446", balance=1000)
    db = SessionLocal()
    try:
        _clear_active_generation_tasks(db)
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="downloading",
            external_task_id="ext-fresh-download",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(hours=4),
            cost_frozen=20,
            cost_settled=0,
            params={
                "_video_result_url": "https://cdn.example.com/fresh.mp4",
                "_video_download_started_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=30)
                ).isoformat(),
            },
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        credits.freeze(db, uid, 20, task_id)

        assert retention.reap_stuck_tasks(db, max_minutes=5) == 0
        kept = db.get(GenTask, task_id)
        assert kept.status == "running"
        assert kept.phase == "downloading"
    finally:
        db.close()


def test_purge_expired_detaches_asset_reports(client, make_user):
    uid = make_user("13900000447", balance=1000)
    db = SessionLocal()
    try:
        set_setting(db, "asset_retention_days", 30)
        old = datetime.now(timezone.utc) - timedelta(days=60)
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            created_at=old,
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=task.id,
            user_id=uid,
            type="image",
            preview_url="/media/preview/reported.png",
            moderation_status="active",
            created_at=old,
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

        assert retention.purge_expired(db, user_id=uid) == 1
        assert db.get(GenAsset, asset_id) is None
        kept_report = db.get(AssetReport, report_id)
        assert kept_report is not None
        assert kept_report.asset_id is None
    finally:
        db.close()
