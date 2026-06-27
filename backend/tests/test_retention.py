"""Retention math (expiry / days_left / expired), including tz-naive coercion."""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import SessionLocal
from app.models import GenTask, ParseRecord, UploadedAsset, User
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
