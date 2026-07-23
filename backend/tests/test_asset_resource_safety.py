import io
from pathlib import Path

from PIL import Image

from app.config import settings
from app.redis_client import redis_client
from app.routers.assets import _download_media_info
from app.services import locks, storage


def test_download_media_info_reads_only_the_signature(monkeypatch, tmp_path):
    path = tmp_path / "large.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1024)

    def fail_full_read(_path):
        raise AssertionError("media sniffing must not read the complete asset")

    monkeypatch.setattr(Path, "read_bytes", fail_full_read)

    media_type, ext = _download_media_info(type("Asset", (), {"type": "image"})(), path)

    assert (media_type, ext) == ("image/png", "png")


def test_image_result_save_failure_removes_files_written_for_that_result(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    make_user("13900001998", balance=1000)
    headers = auth("13900001998")
    original_save_bytes = storage.save_bytes
    written_keys: list[str] = []

    def fail_after_hd_write(data, subdir, ext):
        if subdir == "preview":
            raise RuntimeError("preview storage unavailable")
        key = original_save_bytes(data, subdir, ext)
        written_keys.append(key)
        return key

    monkeypatch.setattr("app.services.generation_image_flow.storage.save_bytes", fail_after_hd_write)

    response = quote_and_generate(
        headers=headers,
        payload={
            "source_asset_url": "http://x/y.png",
            "source_type": "image",
            "source_asset_meta": {"user_confirmed_rights": True},
            "category": "image",
            "stage": "preview",
            "instruction": "resource cleanup regression",
            "params": {"n": 1, "size": "256x256"},
        },
    )

    assert response.status_code == 200, response.text
    task = client.get(f"/api/tasks/{response.json()['id']}", headers=headers)
    assert task.status_code == 200, task.text
    assert task.json()["status"] == "failed"
    assert written_keys
    assert all(not (Path(settings.storage_dir) / key).exists() for key in written_keys)


def test_batch_download_rate_limit_blocks_repeated_archives(client, make_user, auth):
    user_id = make_user("13900001997", balance=1000)
    headers = auth("13900001997")
    redis_client.set(f"asset:batch-download-rate:{user_id}", 12, ex=3600)

    response = client.post("/api/assets/batch/download", headers=headers, json={"asset_ids": [999999]})

    assert response.status_code == 429
    assert "打包下载过于频繁" in response.text


def test_batch_download_global_concurrency_cap_fails_fast(client, make_user, auth):
    make_user("13900001996", balance=1000)
    headers = auth("13900001996")
    first = locks.RedisSemaphore("semaphore:asset-batch-download", limit=2, ttl=900, wait_timeout=0)
    second = locks.RedisSemaphore("semaphore:asset-batch-download", limit=2, ttl=900, wait_timeout=0)

    with first, second:
        response = client.post(
            "/api/assets/batch/download",
            headers=headers,
            json={"asset_ids": [999999]},
        )

    assert response.status_code == 429
    assert "打包下载任务繁忙" in response.text


def test_subject_protection_global_concurrency_cap_fails_fast(client, make_user, auth):
    make_user("13900001995", balance=1000)
    headers = auth("13900001995")
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(image, format="PNG")
    upload = client.post(
        "/api/uploads/image",
        headers=headers,
        files={"file": ("product.png", image.getvalue(), "image/png")},
    )
    assert upload.status_code == 200, upload.text

    first = locks.RedisSemaphore("semaphore:subject-protection", limit=2, ttl=120, wait_timeout=0)
    second = locks.RedisSemaphore("semaphore:subject-protection", limit=2, ttl=120, wait_timeout=0)
    with first, second:
        response = client.post(
            "/api/subject-protection/preview",
            headers=headers,
            json={"asset_url": upload.json()["url"], "edit_mask_mode": "protect_subject"},
        )

    assert response.status_code == 429
    assert "主体保护处理繁忙" in response.text
