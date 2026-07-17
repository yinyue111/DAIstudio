from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, GenAsset, GenTask, UploadedAsset
from app.services import storage, user_assets


def _generated_asset(
    db,
    user_id: int,
    *,
    asset_type: str = "image",
    created_at: datetime,
    preview_url: str | None = None,
    hd_url: str | None = None,
    retained: bool = False,
    favorite: bool = False,
) -> GenAsset:
    task = GenTask(
        user_id=user_id,
        category=asset_type,
        stage="preview",
        status="succeeded",
        cost_frozen=0,
        cost_settled=0,
        created_at=created_at,
    )
    db.add(task)
    db.flush()
    asset = GenAsset(
        task_id=task.id,
        user_id=user_id,
        type=asset_type,
        preview_url=preview_url,
        hd_url=hd_url,
        watermarked=False,
        unlocked=True,
        favorite=favorite,
        moderation_status="active",
        retained_at=created_at if retained else None,
        created_at=created_at,
    )
    db.add(asset)
    db.flush()
    return asset


def _upload_group(
    db,
    user_id: int,
    stem: str,
    *,
    created_at: datetime,
    favorite: bool = False,
) -> tuple[str, str, str]:
    root = storage.save_bytes_named(b"root", "upload", f"{stem}.png")
    preview = storage.save_bytes_named(b"preview", "upload_preview", f"{stem}.png")
    model_ref = storage.save_bytes_named(b"model", "upload_model_ref", f"{stem}.jpg")
    for key, size, name in (
        (root, 4, f"{stem}.png"),
        (preview, 7, f"preview:{stem}.png"),
        (model_ref, 5, f"model-ref:{stem}.jpg"),
    ):
        db.add(UploadedAsset(
            key=key,
            user_id=user_id,
            mime="image/png",
            bytes=size,
            original_filename=name,
            favorite=favorite,
            created_at=created_at,
        ))
    return root, preview, model_ref


def test_unified_asset_list_filters_roots_retention_and_cursor(client, make_user, auth):
    user_id = make_user("13900000501")
    other_id = make_user("13900000502")
    headers = auth("13900000501")
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        recent_preview_key = storage.save_bytes_named(b"gen", "preview", "assets-list.png")
        recent = _generated_asset(
            db,
            user_id,
            created_at=now,
            preview_url=storage.public_url(recent_preview_key),
            favorite=True,
        )
        retained_old = _generated_asset(
            db,
            user_id,
            asset_type="video",
            created_at=now - timedelta(days=60),
            preview_url="https://cdn.example.com/retained.mp4",
            retained=True,
        )
        _generated_asset(
            db,
            user_id,
            created_at=now - timedelta(days=60),
            preview_url="https://cdn.example.com/expired.png",
        )
        upload_key, preview_key, _ = _upload_group(
            db,
            user_id,
            "assets-list-upload",
            created_at=now,
            favorite=True,
        )
        _upload_group(
            db,
            other_id,
            "assets-list-other",
            created_at=now + timedelta(seconds=1),
        )
        db.commit()
        recent_ref = user_assets.generated_asset_ref(recent.id)
        retained_ref = user_assets.generated_asset_ref(retained_old.id)
        upload_ref = user_assets.uploaded_asset_ref(upload_key)
    finally:
        db.close()

    response = client.get("/api/me/assets?favorite=&limit=10", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert body["stats"] == {
        "generated": 2,
        "uploaded": 1,
        "images": 2,
        "videos": 1,
        "favorites": 2,
        "retained": 1,
    }
    assert [item["asset_ref"] for item in body["items"]] == [
        recent_ref,
        upload_ref,
        retained_ref,
    ]
    assert all(not item["asset_ref"].startswith("u.") or item["url"].endswith(upload_key)
               for item in body["items"])
    upload_item = next(item for item in body["items"] if item["asset_ref"] == upload_ref)
    assert upload_item["preview_url"].endswith(preview_key)
    assert upload_item["bytes"] == 16
    assert upload_item["unlocked"] is True
    assert upload_item["unlock_cost"] == 0
    retained_item = next(item for item in body["items"] if item["asset_ref"] == retained_ref)
    assert retained_item["expires_at"] is None
    assert retained_item["days_left"] is None

    favorite_uploads = client.get(
        "/api/me/assets?origin=uploaded&type=image&favorite=true&retention=expiring",
        headers=headers,
    )
    assert favorite_uploads.status_code == 200
    assert [item["asset_ref"] for item in favorite_uploads.json()["items"]] == [upload_ref]

    first = client.get("/api/me/assets?limit=1", headers=headers).json()
    assert first["next_cursor"]
    second = client.get(
        "/api/me/assets",
        params={"limit": 1, "cursor": first["next_cursor"], "offset": 1},
        headers=headers,
    ).json()
    assert second["items"][0]["asset_ref"] == upload_ref
    assert second["total"] == 3
    assert client.get("/api/me/assets?cursor=", headers=headers).status_code == 400
    assert client.get("/api/me/assets?cursor=not-a-cursor", headers=headers).status_code == 400


def test_asset_metadata_updates_upload_group_and_backfills_generated_bytes(
    client,
    make_user,
    auth,
):
    user_id = make_user("13900000503")
    make_user("13900000504")
    headers = auth("13900000503")
    other_headers = auth("13900000504")
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        root, preview, model_ref = _upload_group(
            db,
            user_id,
            "assets-metadata",
            created_at=now,
        )
        gen_preview = storage.save_bytes_named(b"abc", "preview", "assets-metadata.png")
        gen_hd = storage.save_bytes_named(b"defg", "hd", "assets-metadata.png")
        generated = _generated_asset(
            db,
            user_id,
            created_at=now,
            preview_url=storage.public_url(gen_preview),
            hd_url=storage.public_url(gen_hd),
        )
        db.commit()
        upload_ref = user_assets.uploaded_asset_ref(root)
        generated_ref = user_assets.generated_asset_ref(generated.id)
        generated_id = generated.id
    finally:
        db.close()
    denied = client.post(
        "/api/me/assets/metadata",
        headers=other_headers,
        json={"asset_refs": [upload_ref], "favorite": True},
    )
    assert denied.status_code == 404

    updated = client.post(
        "/api/me/assets/metadata",
        headers=headers,
        json={
            "asset_refs": [upload_ref, generated_ref],
            "favorite": True,
            "retained": True,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["asset_refs"] == [upload_ref, generated_ref]

    db = SessionLocal()
    try:
        for key in (root, preview, model_ref):
            row = db.get(UploadedAsset, key)
            assert row.favorite is True
            assert row.retained_at is not None
        generated = db.get(GenAsset, generated_id)
        assert generated.favorite is True
        assert generated.retained_at is not None
        assert generated.bytes == 7
        assert db.query(AuditLog).filter(
            AuditLog.user_id == user_id,
            AuditLog.action == "update_user_asset_metadata",
        ).count() == 1
    finally:
        db.close()


def test_retaining_generated_assets_enforces_user_media_quota_atomically(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13900000508")
    headers = auth("13900000508")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "user_upload_storage_quota_bytes", 6)

    db = SessionLocal()
    try:
        preview_key = storage.save_bytes_named(b"abc", "preview", "assets-quota.png")
        hd_key = storage.save_bytes_named(b"defg", "hd", "assets-quota.png")
        generated = _generated_asset(
            db,
            user_id,
            created_at=now,
            preview_url=storage.public_url(preview_key),
            hd_url=storage.public_url(hd_key),
        )
        db.commit()
        generated_id = generated.id
        generated_ref = user_assets.generated_asset_ref(generated.id)
    finally:
        db.close()

    denied = client.post(
        "/api/me/assets/metadata",
        headers=headers,
        json={"asset_refs": [generated_ref], "favorite": True, "retained": True},
    )
    assert denied.status_code == 413, denied.text

    db = SessionLocal()
    try:
        generated = db.get(GenAsset, generated_id)
        assert generated.retained_at is None
        assert generated.favorite is False
        assert generated.bytes is None
    finally:
        db.close()


def test_batch_delete_is_atomic_on_reference_and_removes_groups_and_shared_files(
    client,
    make_user,
    auth,
):
    user_id = make_user("13900000505")
    headers = auth("13900000505")
    old = datetime.now(timezone.utc) - timedelta(minutes=1)

    db = SessionLocal()
    try:
        root, preview, model_ref = _upload_group(
            db,
            user_id,
            "assets-delete",
            created_at=old,
        )
        shared_key = storage.save_bytes_named(b"shared", "preview", "assets-delete.png")
        shared_url = storage.public_url(shared_key)
        first = _generated_asset(db, user_id, created_at=old, preview_url=shared_url)
        second = _generated_asset(db, user_id, created_at=old, preview_url=shared_url)
        blocker = GenTask(
            user_id=user_id,
            category="video",
            stage="preview",
            status="queued",
            params={"product_reference_image": storage.upload_api_url(preview)},
            created_at=old,
        )
        db.add(blocker)
        db.commit()
        refs = [
            user_assets.uploaded_asset_ref(root),
            user_assets.generated_asset_ref(first.id),
            user_assets.generated_asset_ref(second.id),
        ]
        first_id = first.id
        second_id = second.id
        blocker_id = blocker.id
    finally:
        db.close()

    conflict = client.post(
        "/api/me/assets/batch-delete",
        headers=headers,
        json={"asset_refs": refs},
    )
    assert conflict.status_code == 409, conflict.text
    db = SessionLocal()
    try:
        assert db.get(UploadedAsset, root) is not None
        assert db.get(GenAsset, first_id) is not None
        blocker = db.get(GenTask, blocker_id)
        blocker.status = "failed"
        db.commit()
    finally:
        db.close()
    assert storage.local_path(shared_key).exists()

    deleted = client.post(
        "/api/me/assets/batch-delete",
        headers=headers,
        json={"asset_refs": refs},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["asset_refs"] == refs
    db = SessionLocal()
    try:
        assert db.get(UploadedAsset, root) is None
        assert db.get(UploadedAsset, preview) is None
        assert db.get(UploadedAsset, model_ref) is None
        assert db.get(GenAsset, first_id) is None
        assert db.get(GenAsset, second_id) is None
    finally:
        db.close()
    assert not storage.local_path(root).exists()
    assert not storage.local_path(shared_key).exists()


def test_batch_delete_ignores_another_users_legacy_task_reference(
    client,
    make_user,
    auth,
):
    user_id = make_user("13900000509")
    other_id = make_user("13900000510")
    headers = auth("13900000509")
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        root, _, _ = _upload_group(
            db,
            user_id,
            "assets-owner-isolation",
            created_at=now,
        )
        db.add(GenTask(
            user_id=other_id,
            category="video",
            stage="preview",
            status="queued",
            params={"product_reference_image": storage.upload_api_url(root)},
            created_at=now,
        ))
        db.commit()
        upload_ref = user_assets.uploaded_asset_ref(root)
    finally:
        db.close()

    deleted = client.post(
        "/api/me/assets/batch-delete",
        headers=headers,
        json={"asset_refs": [upload_ref]},
    )
    assert deleted.status_code == 200, deleted.text

    db = SessionLocal()
    try:
        assert db.get(UploadedAsset, root) is None
    finally:
        db.close()


def test_unified_download_enforces_owner_and_existing_generated_rules(client, make_user, auth):
    user_id = make_user("13900000506")
    make_user("13900000507")
    headers = auth("13900000506")
    other_headers = auth("13900000507")
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        root = storage.save_bytes_named(b"uploaded-original", "upload", "assets-download.png")
        db.add(UploadedAsset(
            key=root,
            user_id=user_id,
            mime="image/png",
            bytes=17,
            original_filename="product.png",
            created_at=now,
        ))
        generated_key = storage.save_bytes_named(b"generated-original", "hd", "assets-download.png")
        generated = _generated_asset(
            db,
            user_id,
            created_at=now,
            preview_url=storage.public_url(generated_key),
            hd_url=storage.public_url(generated_key),
        )
        db.commit()
        upload_ref = user_assets.uploaded_asset_ref(root)
        generated_ref = user_assets.generated_asset_ref(generated.id)
    finally:
        db.close()

    uploaded = client.get(
        "/api/me/assets/download",
        params={"asset_ref": upload_ref},
        headers=headers,
    )
    assert uploaded.status_code == 200
    assert uploaded.content == b"uploaded-original"
    assert client.get(
        "/api/me/assets/download",
        params={"asset_ref": upload_ref},
        headers=other_headers,
    ).status_code == 404

    generated = client.get(
        "/api/me/assets/download",
        params={"asset_ref": generated_ref},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text
    assert generated.content == b"generated-original"
