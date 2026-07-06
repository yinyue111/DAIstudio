from pathlib import Path

from app.services import storage


def test_v2_health(client):
    response = client.get("/api/v2/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": "v2"}


def test_v2_manifest_replaces_placeholder_schema(client):
    response = client.get("/api/v2/manifest")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "v2"
    assert body["status"] == "experimental"
    assert any(item["path"] == "/api/v2/health" for item in body["endpoints"])

    old = client.get("/api/v2/tasks/schema/placeholder")
    assert old.status_code == 404


def test_storage_download_helpers_local_mode(monkeypatch):
    monkeypatch.setattr(storage.settings, "storage_backend", "local")
    key = storage.save_bytes(b"hello", "preview", "txt")

    local = storage.download_to_local_temp(key)

    assert isinstance(local, Path)
    assert local == storage.local_path(key)
    assert local.read_bytes() == b"hello"
    assert storage.presigned_download_url(key) == storage.public_url(key)
