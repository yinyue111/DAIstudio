from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from app.db import SessionLocal
from app.models import UploadedAsset
from app.services import storage
from scripts.media_snapshot import create_snapshot, restore_snapshot, verify_snapshot
from scripts.migrate_media_storage import iter_local_objects, migrate


class MissingObject(Exception):
    def __init__(self):
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.lifecycle = None

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Key] = bytes(Body)

    def upload_file(self, filename, bucket, key):
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        if key not in self.objects:
            raise MissingObject()
        self.downloads.append(key)
        Path(filename).write_bytes(self.objects[key])

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise MissingObject()
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise MissingObject()
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, *, Bucket, ContinuationToken=None):
        return {
            "Contents": [
                {"Key": key, "Size": len(value)}
                for key, value in sorted(self.objects.items())
            ],
            "IsTruncated": False,
        }

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?expires={ExpiresIn}"

    def put_bucket_lifecycle_configuration(self, *, Bucket, LifecycleConfiguration):
        self.lifecycle = LifecycleConfiguration


@pytest.fixture()
def fake_s3(monkeypatch, tmp_path):
    client = FakeS3()
    monkeypatch.setattr(storage, "ROOT", tmp_path)
    monkeypatch.setattr(storage.settings, "storage_backend", "s3")
    monkeypatch.setattr(storage.settings, "storage_mirror_local", False)
    monkeypatch.setattr(storage.settings, "storage_s3_bucket", "test-bucket")
    monkeypatch.setattr(storage.settings, "storage_s3_public_base_url", "https://media.example")
    monkeypatch.setattr(storage, "_s3_client", lambda: client)
    return client


def test_s3_round_trip_cache_visibility_and_lifecycle(fake_s3):
    key = storage.save_bytes(b"preview", "preview", "png")
    assert fake_s3.objects[key] == b"preview"
    assert storage.exists(key)
    assert storage.object_size(key) == 7
    assert storage.object_sha256(key) == hashlib.sha256(b"preview").hexdigest()

    path = storage.local_path(key)
    assert path.read_bytes() == b"preview"
    assert storage.materialized_path_matches_key(path, key)
    assert storage.public_url(key) == f"https://media.example/{key}"

    hd_key = storage.save_bytes(b"private", "hd", "png")
    assert storage.public_url(hd_key).endswith(f"/media/{hd_key}")
    assert storage.presigned_download_url(hd_key, expires=42).endswith("?expires=42")

    storage.configure_lifecycle(abort_incomplete_days=3)
    rule = fake_s3.lifecycle["Rules"][0]
    assert rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 3

    storage.delete(key)
    assert key not in fake_s3.objects
    assert not path.exists()


def test_upload_existing_preserves_key_and_rejects_size_conflict(fake_s3, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"stable")
    assert storage.upload_existing(source, "upload/stable.bin") is True
    assert storage.upload_existing(source, "upload/stable.bin") is False

    source.write_bytes(b"different-length")
    with pytest.raises(storage.StorageUnavailable, match="大小不一致"):
        storage.upload_existing(source, "upload/stable.bin")


def test_migration_is_dry_run_by_default_and_verifies_execution(fake_s3, tmp_path):
    local = tmp_path / "upload" / "legacy.png"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"legacy")
    assert list(iter_local_objects(tmp_path)) == [("upload/legacy.png", local)]

    dry_run = migrate(execute=False, verify_only=False, verify="sha256", overwrite=False)
    assert dry_run.discovered == 1
    assert not fake_s3.objects

    executed = migrate(execute=True, verify_only=False, verify="sha256", overwrite=False)
    assert executed.uploaded == 1
    assert executed.verified == 1
    assert executed.failed == 0
    assert fake_s3.objects["upload/legacy.png"] == b"legacy"


def test_authenticated_upload_route_accepts_s3_cache_path(
    fake_s3,
    client,
    make_user,
    auth,
):
    owner_id = make_user("13900003001")
    make_user("13900003002")
    owner_headers = auth("13900003001")
    other_headers = auth("13900003002")
    key = "upload/cache-route.png"
    fake_s3.objects[key] = b"image-bytes"
    db = SessionLocal()
    try:
        db.add(UploadedAsset(
            key=key,
            user_id=owner_id,
            mime="image/png",
            bytes=len(fake_s3.objects[key]),
            original_filename="cache-route.png",
        ))
        db.commit()
    finally:
        db.close()

    denied = client.get(f"/api/uploads/{key}", headers=other_headers)
    assert denied.status_code == 404
    assert fake_s3.downloads == []

    response = client.get(f"/api/uploads/{key}", headers=owner_headers)
    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert fake_s3.downloads == [key]


def test_storage_write_entrypoints_reject_unmanaged_paths(fake_s3, tmp_path):
    with pytest.raises(ValueError, match="非法存储子目录"):
        storage.save_bytes(b"secret", "../outside", "bin")
    with pytest.raises(ValueError, match="非法文件名"):
        storage.save_bytes_named(b"secret", "upload", "nested/secret.bin")
    with pytest.raises(ValueError, match="非法存储子目录"):
        storage.save_file(tmp_path / "missing.bin", "arbitrary", "bin")
    with pytest.raises(ValueError, match="非法文件扩展名"):
        storage.save_bytes(b"secret", "upload", "../../secret")
    assert fake_s3.objects == {}


def test_migration_does_not_follow_symlinks(fake_s3, tmp_path):
    outside = tmp_path.parent / "migration-secret.txt"
    outside.write_bytes(b"must-not-upload")
    upload = tmp_path / "upload"
    upload.mkdir()
    (upload / "linked-secret.txt").symlink_to(outside)

    assert list(iter_local_objects(tmp_path)) == []


def test_s3_snapshot_is_complete_without_local_mirror_and_restores(fake_s3, tmp_path):
    fake_s3.objects.update(
        {
            "preview/a.png": b"preview-bytes",
            "hd/a.png": b"original-bytes",
            "upload/video.mp4": b"video-bytes",
        }
    )
    archive = tmp_path / "media.tar.gz"
    created = create_snapshot(archive)
    assert created.objects == 3
    assert created.bytes == sum(map(len, fake_s3.objects.values()))
    assert verify_snapshot(archive) == created

    fake_s3.objects.clear()
    dry_run = restore_snapshot(archive)
    assert dry_run.objects == 3
    assert fake_s3.objects == {}

    restored = restore_snapshot(archive, execute=True)
    assert restored.restored == 3
    assert fake_s3.objects == {
        "preview/a.png": b"preview-bytes",
        "hd/a.png": b"original-bytes",
        "upload/video.mp4": b"video-bytes",
    }


def test_snapshot_restore_preflights_all_conflicts_before_writing(fake_s3, tmp_path):
    fake_s3.objects.update(
        {
            "preview/a.png": b"snapshot-preview",
            "hd/a.png": b"snapshot-original",
        }
    )
    archive = tmp_path / "conflict-media.tar.gz"
    create_snapshot(archive)
    fake_s3.objects = {"hd/a.png": b"newer-original"}

    with pytest.raises(RuntimeError, match="内容不同"):
        restore_snapshot(archive, execute=True)

    assert fake_s3.objects == {"hd/a.png": b"newer-original"}


def test_local_snapshot_restore_is_owner_of_only_managed_media(monkeypatch, tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "upload").mkdir(parents=True)
    (source / "hd").mkdir(parents=True)
    (source / "upload" / "input.png").write_bytes(b"input")
    (source / "hd" / "result.png").write_bytes(b"result")
    (source / "unmanaged.txt").write_bytes(b"exclude")
    monkeypatch.setattr(storage.settings, "storage_backend", "local")
    monkeypatch.setattr(storage, "ROOT", source)

    archive = tmp_path / "local-media.tar.gz"
    created = create_snapshot(archive)
    assert created.objects == 2

    monkeypatch.setattr(storage, "ROOT", target)
    restored = restore_snapshot(archive, execute=True)
    assert restored.restored == 2
    assert (target / "upload" / "input.png").read_bytes() == b"input"
    assert (target / "hd" / "result.png").read_bytes() == b"result"
    assert not (target / "unmanaged.txt").exists()
