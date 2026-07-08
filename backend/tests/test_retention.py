"""Retention math (expiry / days_left / expired), including tz-naive coercion."""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import SessionLocal
from app.models import AssetReport, GenAsset, GenTask, ParseRecord, UploadedAsset, User
from app.services import credits, retention, storage


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
