from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image

from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, GenAsset, GenTask, UploadedAsset, UserAssetMetadata
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
    origin: str = "uploaded",
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
            origin=origin,
            favorite=favorite,
            created_at=created_at,
        ))
    return root, preview, model_ref


def _png_bytes(*, accent: int = 0) -> bytes:
    image = Image.new("RGB", (24, 24), "white")
    for x in range(4, 20):
        for y in range(5, 19):
            if x + y + accent > 20:
                image.putpixel((x, y), (20, 40, 80))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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
        "fetched": 0,
        "images": 2,
        "videos": 1,
        "audios": 0,
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


def test_asset_list_separates_fetched_materials_from_manual_uploads(client, make_user, auth):
    user_id = make_user("13900000503")
    headers = auth("13900000503")
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        uploaded_key, _, _ = _upload_group(
            db,
            user_id,
            "manual-material",
            created_at=now,
        )
        fetched_key, _, _ = _upload_group(
            db,
            user_id,
            "fetched-material",
            created_at=now - timedelta(seconds=1),
            origin="fetched",
        )
        db.commit()
    finally:
        db.close()

    uploaded = client.get("/api/me/assets?origin=uploaded", headers=headers)
    assert uploaded.status_code == 200, uploaded.text
    assert [item["asset_ref"] for item in uploaded.json()["items"]] == [
        user_assets.uploaded_asset_ref(uploaded_key),
    ]
    assert uploaded.json()["items"][0]["origin"] == "uploaded"

    fetched = client.get("/api/me/assets?origin=fetched", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert [item["asset_ref"] for item in fetched.json()["items"]] == [
        user_assets.uploaded_asset_ref(fetched_key),
    ]
    assert fetched.json()["items"][0]["origin"] == "fetched"

    all_assets = client.get("/api/me/assets", headers=headers).json()
    assert all_assets["stats"]["uploaded"] == 1
    assert all_assets["stats"]["fetched"] == 1


def test_asset_discovery_search_tags_and_folder_ownership(client, make_user, auth):
    user_id = make_user("13900000511")
    make_user("13900000512")
    headers = auth("13900000511")
    other_headers = auth("13900000512")
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        upload_key, _, _ = _upload_group(
            db,
            user_id,
            "hero-product",
            created_at=now,
        )
        generated = _generated_asset(
            db,
            user_id,
            created_at=now - timedelta(seconds=1),
            preview_url="https://cdn.example.com/generated-bottle.png",
        )
        db.get(GenTask, generated.task_id).prompt = {
            "final_text": "cinematic blue bottle campaign",
        }
        db.commit()
        upload_ref = user_assets.uploaded_asset_ref(upload_key)
        generated_ref = user_assets.generated_asset_ref(generated.id)
    finally:
        db.close()

    folder = client.post(
        "/api/asset-folders",
        headers=headers,
        json={"name": "商品主图"},
    )
    assert folder.status_code == 201, folder.text
    folder_id = folder.json()["id"]
    other_folder = client.post(
        "/api/asset-folders",
        headers=other_headers,
        json={"name": "他人文件夹"},
    )
    assert other_folder.status_code == 201, other_folder.text

    tagged = client.put(
        f"/api/me/assets/{upload_ref}/tags",
        headers=headers,
        json={"tags": [" 商品 ", "#主图", "商品"]},
    )
    assert tagged.status_code == 200, tagged.text
    assert tagged.json()["tags"] == ["商品", "主图"]
    assert client.put(
        f"/api/me/assets/{upload_ref}/tags",
        headers=other_headers,
        json={"tags": ["越权"]},
    ).status_code == 404

    moved = client.post(
        f"/api/asset-folders/{folder_id}/assets",
        headers=headers,
        json={"asset_refs": [upload_ref]},
    )
    assert moved.status_code == 200, moved.text

    by_filename = client.get("/api/me/assets", params={"q": "hero-product"}, headers=headers)
    assert by_filename.status_code == 200, by_filename.text
    assert [item["asset_ref"] for item in by_filename.json()["items"]] == [upload_ref]

    by_prompt = client.get("/api/me/assets", params={"q": "cinematic blue"}, headers=headers)
    assert by_prompt.status_code == 200, by_prompt.text
    assert [item["asset_ref"] for item in by_prompt.json()["items"]] == [generated_ref]

    by_tag = client.get("/api/me/assets", params={"tag": "商品"}, headers=headers)
    assert by_tag.status_code == 200, by_tag.text
    assert by_tag.json()["items"][0]["tags"] == ["商品", "主图"]

    by_folder = client.get("/api/me/assets", params={"folder": folder_id}, headers=headers)
    assert by_folder.status_code == 200, by_folder.text
    assert [item["asset_ref"] for item in by_folder.json()["items"]] == [upload_ref]
    assert by_folder.json()["items"][0]["folder_name"] == "商品主图"

    unfiled = client.get("/api/me/assets", params={"folder": "unfiled"}, headers=headers)
    assert unfiled.status_code == 200, unfiled.text
    assert [item["asset_ref"] for item in unfiled.json()["items"]] == [generated_ref]
    assert client.get(
        "/api/me/assets",
        params={"folder": other_folder.json()["id"]},
        headers=headers,
    ).status_code == 404

    db = SessionLocal()
    try:
        metadata = db.query(UserAssetMetadata).filter_by(
            user_id=user_id,
            asset_ref=upload_ref,
        ).one()
        assert metadata.tags == ["商品", "主图"]
        assert db.query(AuditLog).filter(
            AuditLog.user_id == user_id,
            AuditLog.action == "update_user_asset_tags",
        ).count() == 1
    finally:
        db.close()


def test_full_library_similarity_is_owner_scoped_and_audited(client, make_user, auth):
    user_id = make_user("13900000513")
    other_id = make_user("13900000514")
    headers = auth("13900000513")
    now = datetime.now(timezone.utc)
    content = _png_bytes()

    db = SessionLocal()
    try:
        keys = [
            storage.save_bytes_named(content, "upload", "similarity-query.png"),
            storage.save_bytes_named(content, "upload", "similarity-match.png"),
            storage.save_bytes_named(content, "upload", "similarity-other.png"),
        ]
        for index, (key, owner) in enumerate(
            [(keys[0], user_id), (keys[1], user_id), (keys[2], other_id)]
        ):
            db.add(UploadedAsset(
                key=key,
                user_id=owner,
                mime="image/png",
                bytes=len(content),
                original_filename=f"similarity-{index}.png",
                created_at=now - timedelta(seconds=index),
            ))
        db.commit()
        query_ref = user_assets.uploaded_asset_ref(keys[0])
        match_ref = user_assets.uploaded_asset_ref(keys[1])
        other_ref = user_assets.uploaded_asset_ref(keys[2])
    finally:
        db.close()

    analyzed = client.post(
        f"/api/me/assets/{query_ref}/similar",
        params={"max_distance": 8},
        headers=headers,
    )
    assert analyzed.status_code == 200, analyzed.text
    payload = analyzed.json()
    assert payload["status"] == "ready"
    assert payload["exact_available"] is True
    assert payload["perceptual_available"] is True
    assert [row["asset_ref"] for row in payload["matches"]] == [match_ref]
    assert payload["matches"][0]["match_type"] == "exact"
    assert payload["matches"][0]["asset"]["asset_ref"] == match_ref
    assert other_ref not in {row["asset_ref"] for row in payload["matches"]}

    db = SessionLocal()
    try:
        assert db.query(UserAssetMetadata).filter_by(user_id=other_id).count() == 0
        assert db.query(AuditLog).filter(
            AuditLog.user_id == user_id,
            AuditLog.action == "analyze_user_asset_similarity",
        ).count() == 1
    finally:
        db.close()


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


def _lavfi_video_bytes(tmp_path, name, *, source="testsrc2=size=64x64:rate=12:duration=0.6", scale=None):
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    workdir = Path(tempfile.mkdtemp(prefix="phash-video-", dir=str(tmp_path)))
    out = workdir / name
    cmd = [
        str(shutil.which("ffmpeg")), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", source,
    ]
    if scale:
        cmd.extend(["-vf", f"scale={scale}"])
    cmd.extend(["-pix_fmt", "yuv420p", "-c:v", "libx264", str(out)])
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return out.read_bytes()


def _uploaded_video_asset(db, user_id, payload, stem):
    key = storage.save_bytes_named(payload, "upload_video", f"{stem}.mp4")
    db.add(UploadedAsset(
        key=key,
        user_id=user_id,
        mime="video/mp4",
        bytes=len(payload),
        original_filename=f"{stem}.mp4",
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return user_assets.uploaded_asset_ref(key)


def test_video_similarity_uses_frame_level_perceptual_fingerprint(
    client, make_user, auth, tmp_path
):
    import shutil

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        import pytest

        pytest.skip("ffmpeg/ffprobe required")

    user_id = make_user("13900000521")
    db = SessionLocal()
    try:
        # 同源:同一画面内容,不同分辨率/码率的转码副本(sha256 不同)
        query_ref = _uploaded_video_asset(
            db, user_id, _lavfi_video_bytes(tmp_path, "query.mp4"), "phash-query"
        )
        transcoded_ref = _uploaded_video_asset(
            db,
            user_id,
            _lavfi_video_bytes(tmp_path, "transcoded.mp4", scale="48:48"),
            "phash-transcoded",
        )
        # 非同源:纯色画面,dhash 梯度全 0,与 testsrc2 距离远
        unrelated_ref = _uploaded_video_asset(
            db,
            user_id,
            _lavfi_video_bytes(
                tmp_path, "unrelated.mp4", source="color=c=blue:size=64x64:rate=12:duration=0.6"
            ),
            "phash-unrelated",
        )

        result = user_assets.find_similar_assets(
            db,
            user_id,
            query_ref,
            [transcoded_ref, unrelated_ref],
            max_distance=10,
        )
        assert result["exact_available"] is True
        assert result["perceptual_available"] is True, result
        assert result["query"]["perceptual_hash_algorithm"] == "video-dhash64x4"
        assert len(result["query"]["perceptual_hash"]) == 64
        matched = {row["asset_ref"]: row for row in result["matches"]}
        assert transcoded_ref in matched, result
        assert matched[transcoded_ref]["match_type"] == "similar"
        assert 0 <= matched[transcoded_ref]["hamming_distance"] <= 10
        assert unrelated_ref not in matched
        # 感知指纹按素材缓存,重复查询不再重新抽帧
        db2 = SessionLocal()
        try:
            row = db2.query(UserAssetMetadata).filter_by(
                user_id=user_id, asset_ref=query_ref
            ).one()
            assert row.analysis_status == "ready"
            assert row.perceptual_hash
        finally:
            db2.close()
    finally:
        db.close()


def test_video_similarity_degrades_to_exact_when_fingerprint_unavailable(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13900000522")
    payload = b"fake-video-bytes-identical"
    db = SessionLocal()
    try:
        query_ref = _uploaded_video_asset(db, user_id, payload, "degraded-query")
        exact_ref = _uploaded_video_asset(db, user_id, payload, "degraded-copy")
        # 指纹计算失败(如 ffmpeg 不可用/文件损坏)时保留 sha256 精确匹配
        result = user_assets.find_similar_assets(db, user_id, query_ref, [exact_ref])
        assert result["exact_available"] is True
        assert result["perceptual_available"] is False
        assert result["status"] == "degraded"
        assert "视频感知指纹不可用" in (result["message"] or "")
        matched = {row["asset_ref"]: row for row in result["matches"]}
        assert exact_ref in matched
        assert matched[exact_ref]["match_type"] == "exact"
    finally:
        db.close()


def _uploaded_audio_asset(db, user_id, payload, stem):
    key = storage.save_bytes_named(payload, "upload_audio", f"{stem}.m4a")
    db.add(UploadedAsset(
        key=key,
        user_id=user_id,
        mime="audio/mp4",
        bytes=len(payload),
        original_filename=f"{stem}.m4a",
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return user_assets.uploaded_asset_ref(key)


def test_uploaded_audio_asset_enters_unified_library(client, make_user):
    user_id = make_user("13900000523")
    db = SessionLocal()
    try:
        ref = _uploaded_audio_asset(db, user_id, b"fake-m4a", "bgm-meta")
        resolved = user_assets.resolve_asset_ref(db, user_id, ref)
        assert resolved.origin == "uploaded"
        assert user_assets._media_type(resolved) == "audio"
        # 音频进入统一素材列表,type=audio
        listing = user_assets.list_user_assets(
            db,
            user_id,
            origin="all",
            asset_type="all",
            favorite=None,
            retention_filter="all",
            limit=100,
            offset=0,
            cursor=None,
        )
        listed = {item["asset_ref"]: item for item in listing["items"]}
        assert ref in listed
        assert listed[ref]["type"] == "audio"
        assert listed[ref]["origin"] == "uploaded"
        assert listing["stats"]["audios"] == 1
        # 批量视图同样产出音频条目
        views = user_assets.asset_items_for_refs(db, user_id, [ref])
        assert ref in views
        assert views[ref]["type"] == "audio"
    finally:
        db.close()


def test_audio_tags_and_exact_similarity_supported(client, make_user):
    user_id = make_user("13900000524")
    payload = b"fake-m4a-identical-bytes"
    db = SessionLocal()
    try:
        query_ref = _uploaded_audio_asset(db, user_id, payload, "bgm-query")
        exact_ref = _uploaded_audio_asset(db, user_id, payload, "bgm-copy")
        other_ref = _uploaded_audio_asset(db, user_id, b"different-audio-bytes", "bgm-other")
        # 标签写入不再被业务错误拦截(CHECK 约束已放开 audio)
        metadata_row = user_assets.update_asset_tags(db, user_id, query_ref, ["bgm", "轻快"])
        assert set(metadata_row.tags) == {"bgm", "轻快"}
        assert metadata_row.media_type == "audio"
        # 相似分析:音频只支持 SHA-256 精确匹配,感知指纹不可用
        result = user_assets.find_similar_assets(
            db, user_id, query_ref, [exact_ref, other_ref]
        )
        assert result["exact_available"] is True
        assert result["perceptual_available"] is False
        assert result["status"] == "degraded"
        assert "音频素材仅支持 SHA-256 精确重复检测" in (result["message"] or "")
        matched = {row["asset_ref"]: row for row in result["matches"]}
        assert exact_ref in matched
        assert matched[exact_ref]["match_type"] == "exact"
        assert other_ref not in matched
        assert result["query"]["media_type"] == "audio"
        assert result["query"]["analysis_status"] == "ready"
    finally:
        db.close()


def test_me_assets_endpoint_filters_by_audio_type(client, make_user, auth):
    """/me/assets 的 type 查询参数必须放行 audio（此前 pattern 只允许
    all|image|video，音频虽已进入统一素材库却无法单独筛选）。"""
    user_id = make_user("13900000525")
    headers = auth("13900000525")
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        audio_ref = _uploaded_audio_asset(db, user_id, b"fake-m4a-endpoint", "bgm-endpoint")
        image_root, _, _ = _upload_group(db, user_id, "pic-endpoint", created_at=now)
        db.commit()
        image_ref = user_assets.uploaded_asset_ref(image_root)
    finally:
        db.close()

    audio_only = client.get("/api/me/assets?type=audio", headers=headers)
    assert audio_only.status_code == 200, audio_only.text
    audio_items = audio_only.json()["items"]
    assert [item["asset_ref"] for item in audio_items] == [audio_ref]
    assert audio_items[0]["type"] == "audio"

    image_only = client.get("/api/me/assets?type=image", headers=headers)
    assert image_only.status_code == 200, image_only.text
    image_refs = [item["asset_ref"] for item in image_only.json()["items"]]
    assert image_ref in image_refs
    assert audio_ref not in image_refs

    unfiltered = client.get("/api/me/assets?type=all", headers=headers)
    assert unfiltered.status_code == 200
    all_refs = [item["asset_ref"] for item in unfiltered.json()["items"]]
    assert audio_ref in all_refs and image_ref in all_refs

    # 仍拒绝未知类型
    assert client.get("/api/me/assets?type=bogus", headers=headers).status_code == 422
