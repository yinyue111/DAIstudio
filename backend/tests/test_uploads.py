import asyncio
import base64
import io
import os
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import AuditLog, GatewayCall, GenAsset, GenTask, ModelConfig, UploadedAsset
from app.services import generation_media, storage
from app.services.gateway import _mock_image
from app.services.watermark import make_model_reference


def _png_bytes(size=(32, 48), color=(20, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _white_bg_product_png_bytes(size=(300, 200)):
    img = Image.new("RGB", size, (255, 255, 255))
    px = img.load()
    left = int(size[0] * 0.10)
    right = int(size[0] * 0.37)
    top = int(size[1] * 0.30)
    bottom = int(size[1] * 0.80)
    for y in range(top, bottom):
        for x in range(left, right):
            px[x, y] = (190, 40, 36)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _transparent_product_png_bytes(size=(160, 120)):
    img = Image.new("RGBA", size, (255, 255, 255, 0))
    px = img.load()
    for y in range(30, 95):
        for x in range(45, 120):
            px[x, y] = (40, 110, 210, 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _repainted_product_scene_bytes(size=(256, 256)):
    img = Image.new("RGB", size, (18, 36, 28))
    px = img.load()
    for y in range(92, 164):
        for x in range(80, 176):
            px[x, y] = (40, 90, 210)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _mask_alpha_at(mask_data_uri, x, y):
    mask_img = Image.open(io.BytesIO(base64.b64decode(mask_data_uri.split(",", 1)[1])))
    return mask_img.getchannel("A").getpixel((x, y))


def _jpeg_with_exif_bytes(size=(32, 48), color=(20, 120, 200)):
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x010F] = "PrivateCamera"
    exif[0x0132] = "2026:06:21 12:34:56"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _mp4_bytes(tmp_path):
    out = tmp_path / "ref.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x48:d=0.2",
            "-frames:v",
            "2",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return out.read_bytes()


class _PartialWriteFailure:
    def __init__(self, file_obj):
        self._file_obj = file_obj

    def __enter__(self):
        self._file_obj.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._file_obj.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name):
        return getattr(self._file_obj, name)

    def write(self, data):
        partial_size = max(1, len(data) // 2)
        self._file_obj.write(data[:partial_size])
        self._file_obj.flush()
        raise OSError("simulated partial write failure")


class _TrackingSemaphore:
    def __init__(self, value=1):
        self._semaphore = threading.BoundedSemaphore(value)
        self._lock = threading.Lock()
        self.active = 0
        self.acquire_started = threading.Event()

    def acquire(self, blocking=True, timeout=None):
        self.acquire_started.set()
        if timeout is None:
            acquired = self._semaphore.acquire(blocking)
        else:
            acquired = self._semaphore.acquire(blocking, timeout)
        if acquired:
            with self._lock:
                self.active += 1
        return acquired

    def release(self):
        with self._lock:
            self.active -= 1
        self._semaphore.release()

    def is_active(self):
        with self._lock:
            return self.active > 0


def _fail_storage_writes_after_partial_write(monkeypatch, directory):
    real_open = open
    real_named_temporary_file = storage.tempfile.NamedTemporaryFile
    resolved_directory = directory.resolve()

    def failing_open(file, mode="r", *args, **kwargs):
        file_obj = real_open(file, mode, *args, **kwargs)
        path = Path(file)
        if any(flag in mode for flag in ("w", "x", "a")) and path.parent.resolve() == resolved_directory:
            return _PartialWriteFailure(file_obj)
        return file_obj

    def failing_named_temporary_file(*args, **kwargs):
        file_obj = real_named_temporary_file(*args, **kwargs)
        temp_directory = kwargs.get("dir")
        if temp_directory is not None and Path(temp_directory).resolve() == resolved_directory:
            return _PartialWriteFailure(file_obj)
        return file_obj

    monkeypatch.setattr(storage, "open", failing_open, raising=False)
    monkeypatch.setattr(storage.tempfile, "NamedTemporaryFile", failing_named_temporary_file)


def _configure_local_storage(monkeypatch, tmp_path):
    root = tmp_path / "storage"
    monkeypatch.setattr(storage, "ROOT", root)
    monkeypatch.setattr(storage.settings, "storage_backend", "local")
    return root


def test_save_bytes_new_file_uses_process_umask_permissions(monkeypatch, tmp_path):
    root = _configure_local_storage(monkeypatch, tmp_path)
    root.mkdir()
    control = root / "control.bin"
    previous_umask = os.umask(0o027)
    try:
        with open(control, "wb") as file_obj:
            file_obj.write(b"control")
        key = storage.save_bytes(b"payload", "upload", "bin")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE((root / key).stat().st_mode) == stat.S_IMODE(control.stat().st_mode)


def test_save_bytes_named_overwrite_preserves_permission_bits(monkeypatch, tmp_path):
    root = _configure_local_storage(monkeypatch, tmp_path)
    destination = root / "upload"
    destination.mkdir(parents=True)
    target = destination / "stable.png"
    target.write_bytes(b"old payload")
    target.chmod(0o640)

    storage.save_bytes_named(b"new payload", "upload", target.name)

    assert target.read_bytes() == b"new payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_save_bytes_named_overwrite_preserves_setid_permission_bits(monkeypatch, tmp_path):
    root = _configure_local_storage(monkeypatch, tmp_path)
    destination = root / "upload"
    destination.mkdir(parents=True)
    target = destination / "stable-setid.png"
    target.write_bytes(b"old payload")
    target.chmod(0o6755)
    assert stat.S_IMODE(target.stat().st_mode) == 0o6755

    storage.save_bytes_named(b"new payload", "upload", target.name)

    assert target.read_bytes() == b"new payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o6755


def test_save_bytes_named_supports_name_max_filename(monkeypatch, tmp_path):
    root = _configure_local_storage(monkeypatch, tmp_path)
    destination = root / "upload"
    destination.mkdir(parents=True)
    try:
        name_max = os.pathconf(destination, "PC_NAME_MAX")
    except (OSError, ValueError):
        pytest.skip("filesystem does not expose PC_NAME_MAX")
    suffix = ".png"
    filename = "a" * (name_max - len(suffix)) + suffix
    assert len(os.fsencode(filename)) == name_max
    target = destination / filename
    target.write_bytes(b"control")
    target.unlink()

    key = storage.save_bytes_named(b"payload", "upload", filename)

    assert key == f"upload/{filename}"
    assert target.read_bytes() == b"payload"


def test_save_bytes_named_replace_failure_preserves_target(monkeypatch, tmp_path):
    class ReplaceFailure(OSError):
        pass

    root = _configure_local_storage(monkeypatch, tmp_path)
    destination = root / "upload"
    destination.mkdir(parents=True)
    target = destination / "stable.png"
    target.write_bytes(b"old payload")
    target.chmod(0o640)

    def fail_replace(_source, _target):
        raise ReplaceFailure("simulated replace failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(ReplaceFailure, match="simulated replace failure"):
        storage.save_bytes_named(b"new payload", "upload", target.name)

    assert target.read_bytes() == b"old payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(destination.iterdir()) == [target]


def test_cleanup_failure_does_not_mask_replace_failure(monkeypatch, tmp_path):
    class ReplaceFailure(OSError):
        pass

    class CleanupFailure(OSError):
        pass

    root = _configure_local_storage(monkeypatch, tmp_path)
    target = root / "upload" / "stable.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old payload")
    real_unlink = Path.unlink
    cleanup_failed = False

    def fail_replace(_source, _target):
        raise ReplaceFailure("simulated replace failure")

    def fail_first_temp_cleanup(path, *args, **kwargs):
        nonlocal cleanup_failed
        if path.suffix == ".tmp" and not cleanup_failed:
            cleanup_failed = True
            raise CleanupFailure("simulated cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_first_temp_cleanup)

    with pytest.raises(ReplaceFailure, match="simulated replace failure") as exc_info:
        storage.save_bytes_named(b"new payload", "upload", target.name)

    assert isinstance(exc_info.value.__cause__, CleanupFailure)
    assert target.read_bytes() == b"old payload"


def test_save_bytes_partial_write_does_not_publish_target(monkeypatch, tmp_path):
    root = tmp_path / "storage"
    destination = root / "upload"
    monkeypatch.setattr(storage, "ROOT", root)
    monkeypatch.setattr(storage.settings, "storage_backend", "local")
    monkeypatch.setattr(
        storage.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="11111111111111111111111111111111"),
    )
    _fail_storage_writes_after_partial_write(monkeypatch, destination)

    with pytest.raises(OSError, match="simulated partial write failure"):
        storage.save_bytes(b"complete payload", "upload", "bin")

    target = destination / "11111111111111111111111111111111.bin"
    assert not target.exists()
    assert list(destination.iterdir()) == []


def test_save_bytes_named_partial_write_preserves_existing_target(monkeypatch, tmp_path):
    root = tmp_path / "storage"
    destination = root / "upload"
    destination.mkdir(parents=True)
    target = destination / "stable.png"
    target.write_bytes(b"previous complete payload")
    monkeypatch.setattr(storage, "ROOT", root)
    monkeypatch.setattr(storage.settings, "storage_backend", "local")
    _fail_storage_writes_after_partial_write(monkeypatch, destination)

    with pytest.raises(OSError, match="simulated partial write failure"):
        storage.save_bytes_named(b"replacement payload", "upload", target.name)

    assert target.read_bytes() == b"previous complete payload"
    assert list(destination.iterdir()) == [target]


def test_save_file_partial_write_does_not_publish_target(monkeypatch, tmp_path):
    root = tmp_path / "storage"
    destination = root / "upload_video"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"complete video payload")
    monkeypatch.setattr(storage, "ROOT", root)
    monkeypatch.setattr(storage.settings, "storage_backend", "local")
    monkeypatch.setattr(
        storage.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="22222222222222222222222222222222"),
    )
    _fail_storage_writes_after_partial_write(monkeypatch, destination)

    with pytest.raises(OSError, match="simulated partial write failure"):
        storage.save_file(source, "upload_video", "mp4")

    target = destination / "22222222222222222222222222222222.mp4"
    assert not target.exists()
    assert list(destination.iterdir()) == []


def _enable_multi_image_edit():
    db = SessionLocal()
    try:
        model = db.query(ModelConfig).filter(ModelConfig.use == "image").one()
        model.extra = {
            **(model.extra or {}),
            "edit_path": "/v1/images/edits",
            "multi_image_edit_enabled": True,
        }
        db.commit()
    finally:
        db.close()


def test_upload_image_returns_reference_asset(client, make_user, auth):
    uid = make_user("13900000100", balance=1000)
    h = auth("13900000100")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    asset = r.json()

    assert asset["type"] == "image"
    assert asset["width"] == 32
    assert asset["height"] == 48
    url_key = urlparse(asset["url"]).path.removeprefix("/api/uploads/")
    thumb_key = urlparse(asset["thumb"]).path.removeprefix("/api/uploads/")
    assert url_key.startswith("upload/")
    assert thumb_key.startswith("upload_preview/")
    assert url_key.split("/", 1)[1].rsplit(".", 1)[0] == thumb_key.split("/", 1)[1].rsplit(".", 1)[0]
    model_ref_key = "upload_model_ref/" + url_key.split("/", 1)[1].rsplit(".", 1)[0] + ".jpg"
    db = SessionLocal()
    try:
        assert db.get(UploadedAsset, model_ref_key) is not None
        log = db.query(AuditLog).filter(
            AuditLog.user_id == uid,
            AuditLog.action == "upload_image",
        ).order_by(AuditLog.id.desc()).first()
        assert "rights_confirmed" not in log.detail
        assert "rights_confirmation" not in log.detail
    finally:
        db.close()
    anon = TestClient(app)
    assert anon.get(urlparse(asset["url"]).path).status_code == 401
    assert client.get(urlparse(asset["url"]).path, headers=h).status_code == 200
    assert anon.get(urlparse(asset["thumb"]).path).status_code == 401
    assert client.get(urlparse(asset["thumb"]).path, headers=h).status_code == 200
    assert client.get(f"/api/uploads/{model_ref_key}", headers=h).status_code == 404


def test_upload_image_runs_cpu_image_processing_off_event_loop(client, make_user, auth, monkeypatch):
    make_user("13900000253", balance=1000)
    h = auth("13900000253")
    calls = []
    cleanup_callbacks = []

    async def fake_run_upload_thread(func, *args, on_cancel_result=None, **kwargs):
        calls.append(getattr(func, "__name__", str(func)))
        if on_cancel_result is not None:
            cleanup_callbacks.append((getattr(func, "__name__", str(func)), on_cancel_result))
        return func(*args, **kwargs)

    from app.routers import uploads

    monkeypatch.setattr(uploads, "_run_upload_thread", fake_run_upload_thread)

    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )

    assert r.status_code == 200, r.text
    assert "_normalize_image_upload" in calls
    assert "make_image_preview" in calls
    assert "make_model_reference" in calls
    assert calls.count("save_bytes_named") == 3
    assert [name for name, _callback in cleanup_callbacks] == [
        "save_bytes_named",
        "save_bytes_named",
        "save_bytes_named",
    ]


def test_upload_processing_slot_cancelled_while_waiting_does_not_leak_permit(monkeypatch):
    from app.routers import uploads

    semaphore = _TrackingSemaphore(1)
    assert semaphore.acquire(False)
    semaphore.acquire_started.clear()
    monkeypatch.setattr(uploads, "_UPLOAD_PROCESSING_SEMAPHORE", semaphore)

    async def scenario():
        entered = False

        async def waiter():
            nonlocal entered
            async with uploads._upload_processing_slot():
                entered = True

        task = asyncio.create_task(waiter())
        assert await asyncio.to_thread(semaphore.acquire_started.wait, 2)
        task.cancel()
        semaphore.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert entered is False

    asyncio.run(scenario())
    assert semaphore.acquire(False)
    assert not semaphore.acquire(False)
    semaphore.release()


def test_upload_processing_slot_keeps_permit_until_cancelled_thread_finishes(monkeypatch):
    from app.routers import uploads

    semaphore = _TrackingSemaphore(1)
    worker_started = threading.Event()
    allow_worker_exit = threading.Event()
    cancellation_cleanup_started = asyncio.Event()
    monkeypatch.setattr(uploads, "_UPLOAD_PROCESSING_SEMAPHORE", semaphore)
    real_finish_after_cancel = uploads._finish_thread_task_after_cancel

    async def tracked_finish_after_cancel(*args, **kwargs):
        cancellation_cleanup_started.set()
        return await real_finish_after_cancel(*args, **kwargs)

    monkeypatch.setattr(uploads, "_finish_thread_task_after_cancel", tracked_finish_after_cancel)

    def blocking_worker():
        worker_started.set()
        assert allow_worker_exit.wait(timeout=5)

    async def scenario():
        async def run_worker():
            async with uploads._upload_processing_slot():
                await uploads._run_upload_thread(blocking_worker)

        task = asyncio.create_task(run_worker())
        assert await asyncio.to_thread(worker_started.wait, 2)
        task.cancel()
        await cancellation_cleanup_started.wait()
        assert not semaphore.acquire(False)
        allow_worker_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert semaphore.acquire(False)
    semaphore.release()


def test_upload_processing_slot_returns_503_when_acquire_times_out(monkeypatch):
    from app.routers import uploads

    class TimedOutSemaphore:
        def __init__(self):
            self.release_called = False

        def acquire(self, blocking=True, timeout=None):
            assert blocking is True
            assert timeout == uploads.settings.upload_processing_acquire_timeout_seconds
            return False

        def release(self):
            self.release_called = True

    semaphore = TimedOutSemaphore()
    monkeypatch.setattr(uploads, "_UPLOAD_PROCESSING_SEMAPHORE", semaphore)

    async def scenario():
        with pytest.raises(HTTPException) as exc_info:
            async with uploads._upload_processing_slot():
                raise AssertionError("timed-out slot must not enter its body")
        assert exc_info.value.status_code == 503
        assert "媒体处理繁忙" in str(exc_info.value.detail)

    asyncio.run(scenario())
    assert semaphore.release_called is False


def test_cancelled_upload_stream_removes_partial_raw_temp_file(monkeypatch, tmp_path):
    from app.routers import uploads

    raw_path = tmp_path / "partial.mp4"
    second_read_started = asyncio.Event()

    class FakeTempFile:
        name = str(raw_path)

        def close(self):
            pass

    class BlockingUpload:
        def __init__(self):
            self.read_count = 0

        async def read(self, _size):
            self.read_count += 1
            if self.read_count == 1:
                return b"partial"
            second_read_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(uploads.tempfile, "NamedTemporaryFile", lambda **_kwargs: FakeTempFile())

    async def scenario():
        task = asyncio.create_task(
            uploads._save_upload_stream_to_temp(BlockingUpload(), "mp4", limit=1024)
        )
        await second_read_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not raw_path.exists()


def test_cancelled_video_sanitize_removes_completed_result_temp(monkeypatch, tmp_path):
    from app.routers import uploads

    sanitizer_started = threading.Event()
    allow_sanitizer_return = threading.Event()
    sanitized_path = tmp_path / "cancelled-sanitized.mp4"
    raw_path = tmp_path / "raw.mp4"
    raw_path.write_bytes(b"raw")

    def sanitizer(_path):
        sanitizer_started.set()
        assert allow_sanitizer_return.wait(timeout=5)
        sanitized_path.write_bytes(b"sanitized")
        return sanitized_path, 32, 48, 1.0, None

    async def scenario():
        task = asyncio.create_task(
            uploads._run_upload_thread(
                sanitizer,
                raw_path,
                on_cancel_result=lambda result: result[0].unlink(missing_ok=True),
            )
        )
        assert await asyncio.to_thread(sanitizer_started.wait, 2)
        task.cancel()
        allow_sanitizer_return.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not sanitized_path.exists()


def test_cancelled_video_storage_removes_completed_persisted_key(monkeypatch, tmp_path):
    from app.routers import uploads

    save_started = threading.Event()
    allow_save_return = threading.Event()
    stored_path = tmp_path / "upload_video" / "cancelled.mp4"
    key = "upload_video/cancelled.mp4"

    def save_file():
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(b"persisted")
        save_started.set()
        assert allow_save_return.wait(timeout=5)
        return key

    monkeypatch.setattr(uploads.storage, "local_path", lambda value: tmp_path / value)

    async def scenario():
        task = asyncio.create_task(
            uploads._run_upload_thread(
                save_file,
                on_cancel_result=lambda result: uploads._cleanup_storage_keys(result),
            )
        )
        assert await asyncio.to_thread(save_started.wait, 2)
        task.cancel()
        allow_save_return.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not stored_path.exists()


def test_upload_image_holds_processing_slot_through_storage_and_commit(
    client, make_user, auth, monkeypatch
):
    make_user("13900000255", balance=1000)
    headers = auth("13900000255")
    from app.routers import uploads

    semaphore = _TrackingSemaphore(1)
    monkeypatch.setattr(uploads, "_UPLOAD_PROCESSING_SEMAPHORE", semaphore)
    real_save = uploads.storage.save_bytes_named
    real_commit = uploads.Session.commit

    def checked_save(*args, **kwargs):
        assert semaphore.is_active()
        return real_save(*args, **kwargs)

    def checked_commit(session, *args, **kwargs):
        if any(isinstance(row, UploadedAsset) for row in session.new):
            assert semaphore.is_active()
        return real_commit(session, *args, **kwargs)

    monkeypatch.setattr(uploads.storage, "save_bytes_named", checked_save)
    monkeypatch.setattr(uploads.Session, "commit", checked_commit)

    response = client.post(
        "/api/uploads/image",
        files={"file": ("gated.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_upload_video_holds_processing_slot_through_temp_storage_poster_and_commit(
    client, make_user, auth, monkeypatch, tmp_path
):
    make_user("13900000256", balance=1000)
    headers = auth("13900000256")
    from app.routers import uploads

    semaphore = _TrackingSemaphore(1)
    monkeypatch.setattr(uploads, "_UPLOAD_PROCESSING_SEMAPHORE", semaphore)
    real_stream = uploads._save_upload_stream_to_temp
    real_save_file = uploads.storage.save_file
    real_save_bytes = uploads.storage.save_bytes_named
    real_commit = uploads.Session.commit

    async def checked_stream(*args, **kwargs):
        assert semaphore.is_active()
        return await real_stream(*args, **kwargs)

    def fake_sanitize(_raw_path):
        assert semaphore.is_active()
        sanitized = tmp_path / "sanitized.mp4"
        sanitized.write_bytes(b"sanitized-video")
        return sanitized, 32, 48, 1.0, b"poster"

    def checked_save_file(*args, **kwargs):
        assert semaphore.is_active()
        return real_save_file(*args, **kwargs)

    def checked_save_bytes(*args, **kwargs):
        assert semaphore.is_active()
        return real_save_bytes(*args, **kwargs)

    def checked_commit(session, *args, **kwargs):
        if any(isinstance(row, UploadedAsset) for row in session.new):
            assert semaphore.is_active()
        return real_commit(session, *args, **kwargs)

    monkeypatch.setattr(uploads, "_save_upload_stream_to_temp", checked_stream)
    monkeypatch.setattr(uploads, "_sanitize_video_and_poster", fake_sanitize)
    monkeypatch.setattr(uploads.storage, "save_file", checked_save_file)
    monkeypatch.setattr(uploads.storage, "save_bytes_named", checked_save_bytes)
    monkeypatch.setattr(uploads.Session, "commit", checked_commit)

    response = client.post(
        "/api/uploads/video",
        files={"file": ("gated.mp4", b"raw-video", "video/mp4")},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_upload_image_processing_respects_global_parallelism(client, make_user, auth, monkeypatch):
    make_user("13900000254", balance=1000)
    headers = auth("13900000254")
    active = 0
    peak = 0
    state_lock = threading.Lock()
    two_entered = threading.Event()
    third_entered = threading.Event()
    third_acquire_started = threading.Event()
    release_processing = threading.Event()

    from app.routers import uploads

    class CountingSemaphore(_TrackingSemaphore):
        def __init__(self):
            super().__init__(2)
            self._acquire_count = 0
            self._count_lock = threading.Lock()

        def acquire(self, blocking=True, timeout=None):
            with self._count_lock:
                self._acquire_count += 1
                if self._acquire_count >= 3:
                    third_acquire_started.set()
            return super().acquire(blocking, timeout)

    monkeypatch.setattr(uploads, "_UPLOAD_PROCESSING_SEMAPHORE", CountingSemaphore())
    real_normalize = uploads._normalize_image_upload

    def _blocking_normalize(data):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                two_entered.set()
            if active >= 3:
                third_entered.set()
        try:
            assert release_processing.wait(timeout=5)
            return real_normalize(data)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(uploads, "_normalize_image_upload", _blocking_normalize)

    def _upload(index):
        return client.post(
            "/api/uploads/image",
            files={"file": (f"ref-{index}.png", _png_bytes(), "image/png")},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_upload, index) for index in range(3)]
        assert two_entered.wait(timeout=5)
        assert third_acquire_started.wait(timeout=5)
        assert not third_entered.is_set()
        release_processing.set()
        responses = [future.result(timeout=10) for future in futures]

    assert all(response.status_code == 200 for response in responses)
    assert peak == 2


def test_upload_image_sanitizes_long_original_filename(client, make_user, auth):
    make_user("13900000109", balance=1000)
    h = auth("13900000109")
    long_name = "../" + ("产品资料" * 120) + ".png"

    r = client.post(
        "/api/uploads/image",
        files={"file": (long_name, _png_bytes(), "image/png")},
        headers=h,
    )
    assert r.status_code == 200, r.text

    key = urlparse(r.json()["url"]).path.removeprefix("/api/uploads/")
    stem = key.split("/", 1)[1].rsplit(".", 1)[0]
    keys = [
        key,
        f"upload_preview/{stem}.png",
        f"upload_model_ref/{stem}.jpg",
    ]
    db = SessionLocal()
    try:
        filenames = [db.get(UploadedAsset, asset_key).original_filename for asset_key in keys]
        assert all(filename and len(filename) <= 255 for filename in filenames)
        assert all("/" not in filename and "\\" not in filename for filename in filenames)
    finally:
        db.close()


def test_upload_image_commits_even_when_audit_fails(client, make_user, auth, monkeypatch):
    make_user("13900000108", balance=1000)
    h = auth("13900000108")
    monkeypatch.setattr("app.routers.uploads.audit.log", lambda *_a, **_k: False)

    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    key = urlparse(r.json()["url"]).path.removeprefix("/api/uploads/")
    db = SessionLocal()
    try:
        assert db.get(UploadedAsset, key) is not None
    finally:
        db.close()


def test_upload_image_strips_exif_and_stores_normalized_png(client, make_user, auth):
    make_user("13900001903", balance=1000)
    h = auth("13900001903")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("private.jpg", _jpeg_with_exif_bytes(), "image/jpeg")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    key = urlparse(r.json()["url"]).path.removeprefix("/api/uploads/")
    assert key.startswith("upload/")
    assert key.endswith(".png")

    resp = client.get(urlparse(r.json()["url"]).path, headers=h)
    assert resp.status_code == 200
    stored = Image.open(io.BytesIO(resp.content))
    assert stored.format == "PNG"
    assert not stored.getexif()
    db = SessionLocal()
    try:
        row = db.get(UploadedAsset, key)
        assert row is not None
        assert row.mime == "image/png"
    finally:
        db.close()


def test_upload_image_rejects_non_image(client, make_user, auth):
    make_user("13900000101", balance=1000)
    h = auth("13900000101")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("not-image.txt", b"hello", "text/plain")},
        headers=h,
    )
    assert r.status_code == 400
    assert "有效图片" in r.text


def test_upload_image_allows_upload_without_rights_confirmation(client, make_user, auth):
    make_user("13900001904", balance=1000)
    h = auth("13900001904")

    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )

    assert r.status_code == 200, r.text


def test_upload_image_rejects_oversized_content_length(client, make_user, auth, monkeypatch):
    make_user("13900000109", balance=1000)
    h = auth("13900000109")
    monkeypatch.setattr("app.routers.uploads.settings.max_upload_image_bytes", 16)
    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    assert r.status_code == 413


def test_upload_image_rejects_user_storage_quota(client, make_user, auth, monkeypatch):
    uid = make_user("13900001901", balance=1000)
    h = auth("13900001901")
    monkeypatch.setattr("app.routers.uploads.settings.user_upload_storage_quota_bytes", 512)
    db = SessionLocal()
    try:
        db.add(UploadedAsset(
            key="upload/quota-existing.png",
            user_id=uid,
            mime="image/png",
            width=1,
            height=1,
            bytes=500,
            original_filename="existing.png",
        ))
        db.commit()
    finally:
        db.close()

    before = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}
    r = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
        headers=h,
    )
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 413
    assert "上传空间不足" in r.text
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(
            UploadedAsset.user_id == uid,
            UploadedAsset.key != "upload/quota-existing.png",
        ).count() == 0
    finally:
        db.close()


def test_uploaded_image_can_drive_reference_edit_generation(
    client, make_user, auth, monkeypatch
):
    make_user("13900000102", balance=1000)
    h = auth("13900000102")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen.update({
            "prompt": prompt,
            "n": n,
            "size": size,
            "reference_image_url": reference_image_url,
            "edit_path": edit_path,
        })
        return [_mock_image(prompt, "256x256", i) for i in range(n)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "turn this into a premium product poster",
            "instruction": "turn this into a premium product poster",
        },
        "params": {
            "n": 1,
            "size": "512x1024",
            "reference_width": asset["width"],
            "reference_height": asset["height"],
        },
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["reference_image_url"].startswith("data:image/jpeg;base64,")
    ref_bytes = base64.b64decode(seen["reference_image_url"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert ref_img.size == (40, 80)
    assert seen["edit_path"] == "/v1/images/edits"
    assert seen["size"] == "512x1024"


def test_structured_portrait_reference_without_instruction_uses_image_edit(
    client, make_user, auth, monkeypatch
):
    make_user("13900001970", balance=1000)
    h = auth("13900001970")

    source = client.post(
        "/api/uploads/image",
        files={
            "file": (
                "source-portrait.png",
                _png_bytes(size=(1200, 2000), color=(20, 120, 200)),
                "image/png",
            )
        },
        headers=h,
    )
    assert source.status_code == 200, source.text
    explicit_reference = client.post(
        "/api/uploads/image",
        files={
            "file": (
                "explicit-landscape.png",
                _png_bytes(size=(2000, 1200), color=(180, 80, 40)),
                "image/png",
            )
        },
        headers=h,
    )
    assert explicit_reference.status_code == 200, explicit_reference.text
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["reference_image_url"] = reference_image_url
        seen["edit_path"] = edit_path
        seen["extra_payload"] = extra_payload
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": source.json()["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": (
                "保持参考人物斜向后仰坐姿、低机位仰拍与近距离透视，前景自然放大；"
                "服装为非婚纱的冰晶有机雕塑结构；左上大面积柔光，暗部保留冷蓝层次；"
                "低对比柔雾与宽泛光晕，抬升黑位，低锐化、非 HDR。"
            ),
            "图像类型": "人物图",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "reference_image_url": explicit_reference.json()["url"],
            "negative_prompt": "用户自定义负向词",
        },
    }, headers=h)

    assert r.status_code == 200, r.text
    task = r.json()
    assert task["cost_frozen"] == 20
    assert seen["reference_image_url"].startswith("data:image/jpeg;base64,")
    ref_bytes = base64.b64decode(seen["reference_image_url"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert max(ref_img.size) == 1536
    assert ref_img.width > ref_img.height
    assert seen["edit_path"] == "/v1/images/edits"
    assert "用户自定义负向词" in seen["extra_payload"]["negative_prompt"]
    assert "主动瘦身" in seen["extra_payload"]["negative_prompt"]
    assert "HDR" in seen["extra_payload"]["negative_prompt"]

    db = SessionLocal()
    try:
        call = db.query(GatewayCall).filter(
            GatewayCall.task_id == task["id"],
            GatewayCall.kind == "image",
            GatewayCall.status == "ok",
        ).order_by(GatewayCall.id.desc()).first()
        assert call is not None
        assert call.detail["mode"] == "edit"
    finally:
        db.close()


def test_plain_text_portrait_without_reference_does_not_claim_uploaded_identity(
    client, make_user, auth, monkeypatch
):
    make_user("13900001973", balance=1000)
    h = auth("13900001973")
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["prompt"] = prompt
        seen["reference_image_url"] = reference_image_url
        seen["edit_path"] = edit_path
        seen["negative_prompt"] = extra_payload["negative_prompt"]
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": (
                "成年职业人像，人物直立居中，穿白色婚纱，使用硬质影棚主光和清晰锐利的 HDR 质感，"
                "保持自然五官、完整服装与干净背景。"
            ),
            "图像类型": "人物图",
        },
        "params": {"n": 1, "size": "1024x1024", "subject_mode": "portrait"},
    }, headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["cost_frozen"] == 15
    assert seen["reference_image_url"] is None
    assert seen["edit_path"] == "/v1/images/edits"
    assert "上传人像照片是唯一人物身份来源" not in seen["prompt"]
    assert seen["negative_prompt"] is None


def test_portrait_reference_fails_instead_of_falling_back_without_edit_endpoint(
    client, make_user, auth, monkeypatch
):
    make_user("13900001974", balance=1000)
    h = auth("13900001974")
    source = client.post(
        "/api/uploads/image",
        files={"file": ("portrait.png", _png_bytes(size=(1200, 1600)), "image/png")},
        headers=h,
    )
    assert source.status_code == 200, source.text
    called = {"value": False}

    def fake_gen_image(*_args, **_kwargs):
        called["value"] = True
        return [_mock_image("unexpected", "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)
    monkeypatch.setattr("app.services.generation_image_flow.settings.image_edit_path", "")

    r = client.post("/api/generate", json={
        "source_asset_url": source.json()["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "保持参考人物的低机位后仰坐姿和柔雾光影",
            "instruction": "保持参考人物的低机位后仰坐姿和柔雾光影",
            "图像类型": "人物图",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "reference_image_url": source.json()["url"],
        },
    }, headers=h)

    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "failed"
    assert task["error"] == "图片生成失败，已退回冻结积分，请稍后重试"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000
    assert called["value"] is False


@pytest.mark.parametrize(
    "reference_value",
    [123, {"url": "https://example.com/reference.png"}],
)
def test_image_reference_url_rejects_non_string_value(
    client, make_user, auth, reference_value
):
    make_user("13900001971", balance=1000)
    h = auth("13900001971")

    r = client.post("/api/generate", json={
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "保持参考人物、姿态、机位和光影关系",
            "图像类型": "人物图",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "reference_image_url": reference_value,
        },
    }, headers=h)

    assert r.status_code == 400, r.text
    assert "reference_image_url 非法" in r.text


def test_uploaded_large_image_edit_uses_high_resolution_original(
    client, make_user, auth, monkeypatch
):
    make_user("13900001905", balance=1000)
    h = auth("13900001905")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _png_bytes(size=(1600, 900)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["reference_image_url"] = reference_image_url
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "preserve the product and move it into a studio ad",
            "instruction": "preserve the product and move it into a studio ad",
        },
        "params": {"n": 1, "size": "1024x576"},
    }, headers=h)
    assert r.status_code == 200, r.text

    ref_bytes = base64.b64decode(seen["reference_image_url"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert ref_img.size == (1024, 576)


def test_product_image_edit_uses_larger_reference_and_server_fidelity_guard(
    client, make_user, auth, monkeypatch
):
    make_user("13900001966", balance=1000)
    h = auth("13900001966")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _white_bg_product_png_bytes(size=(2000, 1200)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["prompt"] = prompt
        seen["reference_image_url"] = reference_image_url
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "move this product into a premium studio ad",
            "instruction": "move this product into a premium studio ad",
        },
        "params": {"n": 1, "size": "1024x1024"},
    }, headers=h)
    assert r.status_code == 200, r.text

    assert "产品高保真硬约束" in seen["prompt"]
    assert "包装文字" in seen["prompt"]
    assert "逐字逐形保持原图" in seen["prompt"]
    ref_bytes = base64.b64decode(seen["reference_image_url"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert max(ref_img.size) == 1536


def test_product_negative_prompt_wins_when_metadata_also_marks_portrait(
    client, make_user, auth, monkeypatch
):
    make_user("13900001972", balance=1000)
    h = auth("13900001972")
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["prompt"] = prompt
        seen["negative_prompt"] = extra_payload["negative_prompt"]
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": "https://example.com/product.png",
        "source_type": "image",
        "source_asset_meta": {
            "product_generation_mode": True,
            "portrait_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "move this product into a premium studio ad",
            "instruction": "move this product into a premium studio ad",
            "图像类型": "人物+产品混合图",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "subject_mode": "product",
            "edit_mask_mode": "off",
            "negative_prompt": "用户自定义负向词",
        },
    }, headers=h)

    assert r.status_code == 200, r.text
    assert "用户自定义负向词" in seen["negative_prompt"]
    assert "产品残缺" in seen["negative_prompt"]
    assert "主动瘦身" not in seen["negative_prompt"]
    assert "产品高保真硬约束" in seen["prompt"]
    assert "人像高保真硬约束" not in seen["prompt"]
    assert "上传人像照片作为唯一人物身份" not in seen["prompt"]
    assert "品牌 Lookbook" not in seen["prompt"]
    assert "不得主动瘦身" not in seen["prompt"]


def test_product_image_edit_auto_generates_mask_for_inpaint(
    client, make_user, auth, monkeypatch
):
    make_user("13900001968", balance=1000)
    h = auth("13900001968")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _white_bg_product_png_bytes(size=(2000, 1200)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}
    component_sizes = []
    original_largest_component_mask = generation_media._largest_component_mask

    def recording_largest_component_mask(mask):
        component_sizes.append(mask.size)
        return original_largest_component_mask(mask)

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["extra_payload"] = extra_payload or {}
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)
    monkeypatch.setattr(generation_media, "_largest_component_mask", recording_largest_component_mask)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "put this product in a clean studio scene",
            "instruction": "put this product in a clean studio scene",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "edit_mask_mode": "protect_subject",
        },
    }, headers=h)
    assert r.status_code == 200, r.text

    mask = seen["extra_payload"].get("mask")
    assert mask.startswith("data:image/png;base64,")
    mask_img = Image.open(io.BytesIO(base64.b64decode(mask.split(",", 1)[1])))
    assert mask_img.mode == "RGBA"
    assert max(mask_img.size) == 1536
    assert component_sizes
    assert max(max(size) for size in component_sizes) <= generation_media.EDIT_MASK_ANALYSIS_MAX_SIDE


def test_product_image_edit_rgb_mask_protects_detected_subject_not_center_background(
    client, make_user, auth, monkeypatch
):
    make_user("13900001970", balance=1000)
    h = auth("13900001970")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _white_bg_product_png_bytes(), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["extra_payload"] = extra_payload or {}
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "put this product in a bright lifestyle scene",
            "instruction": "put this product in a bright lifestyle scene",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "edit_mask_mode": "protect_subject",
        },
    }, headers=h)
    assert r.status_code == 200, r.text

    mask = seen["extra_payload"].get("mask")
    assert mask.startswith("data:image/png;base64,")
    assert _mask_alpha_at(mask, 70, 110) > 200
    assert _mask_alpha_at(mask, 180, 100) < 20


def test_auto_subject_mask_detects_white_product_on_natural_background():
    img = Image.new("RGB", (512, 768), (42, 68, 38))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 511, 230), fill=(176, 174, 154))
    draw.ellipse((410, 40, 500, 130), fill=(245, 235, 200))
    for x in range(0, 512, 18):
        draw.line((x, 260, x - 80, 767), fill=(34, 78, 32), width=5)
        draw.line((x + 8, 300, x + 120, 767), fill=(82, 102, 45), width=3)
    draw.rounded_rectangle((86, 336, 446, 520), radius=10, fill=(244, 244, 239), outline=(230, 230, 225), width=4)
    draw.rectangle((96, 494, 436, 515), fill=(26, 26, 24))
    draw.rectangle((205, 405, 328, 455), outline=(20, 20, 20), width=3)
    draw.text((222, 420), "DAMAH", fill=(20, 20, 20))

    result = generation_media._auto_subject_mask(img.convert("RGBA"))

    assert result.mode == "auto_subject"
    assert result.confidence >= generation_media.EDIT_MASK_SEND_CONFIDENCE
    assert result.bbox is not None
    left, top, right, bottom = result.bbox
    assert left > 40
    assert top > 250
    assert right < 490
    assert bottom < 590


def test_auto_subject_mask_recovers_white_product_body_on_white_background():
    img = Image.new("RGB", (800, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    body = [(78, 365), (210, 284), (642, 300), (748, 420), (704, 592), (132, 514)]
    draw.polygon(body, fill=(247, 247, 244), outline=(226, 226, 222))
    draw.polygon([(210, 284), (385, 210), (690, 262), (642, 300)], fill=(250, 250, 247), outline=(226, 226, 222))
    draw.polygon([(642, 300), (748, 420), (704, 592), (640, 525)], fill=(243, 243, 240), outline=(222, 222, 218))
    draw.rectangle((282, 360, 480, 430), outline=(35, 35, 35), width=3)
    draw.text((320, 382), "DAMAH", fill=(25, 25, 25))
    draw.rectangle((180, 503, 676, 534), fill=(28, 28, 26))
    draw.rectangle((322, 225, 438, 255), fill=(24, 24, 24))
    draw.text((348, 232), "DAMAH", fill=(240, 240, 240))
    for y in range(335, 500, 10):
        draw.line((120, y, 700, y + 16), fill=(232, 232, 228), width=1)

    result = generation_media._auto_subject_mask(img.convert("RGBA"))

    assert result.mode == "auto_subject"
    assert result.confidence >= generation_media.EDIT_MASK_SEND_CONFIDENCE
    assert result.bbox is not None
    left, top, right, bottom = result.bbox
    assert left < 95
    assert top < 250
    assert right > 705
    assert bottom > 565


def test_auto_subject_mask_handles_common_white_product_shapes():
    def bbox_area(bbox):
        left, top, right, bottom = bbox
        return max(0, right - left + 1) * max(0, bottom - top + 1)

    def bbox_iou(a, b):
        intersection = (
            max(a[0], b[0]),
            max(a[1], b[1]),
            min(a[2], b[2]),
            min(a[3], b[3]),
        )
        intersection_area = bbox_area(intersection)
        return intersection_area / max(1, bbox_area(a) + bbox_area(b) - intersection_area)

    cases = []

    bottle = Image.new("RGB", (800, 800), (255, 255, 255))
    draw = ImageDraw.Draw(bottle)
    draw.rounded_rectangle((330, 235, 485, 640), radius=46, fill=(248, 248, 246), outline=(222, 222, 218), width=4)
    draw.rectangle((372, 168, 443, 245), fill=(246, 246, 244), outline=(224, 224, 220), width=3)
    draw.rectangle((390, 124, 480, 166), fill=(245, 245, 242), outline=(224, 224, 220), width=3)
    draw.rectangle((478, 139, 558, 154), fill=(245, 245, 242), outline=(224, 224, 220), width=3)
    draw.rectangle((356, 390, 460, 468), outline=(42, 42, 42), width=3)
    draw.text((374, 420), "MILK", fill=(35, 35, 35))
    for x in range(345, 470, 14):
        draw.line((x, 250, x + 18, 630), fill=(235, 235, 232), width=1)
    cases.append(("bottle", bottle, (320, 115, 565, 650)))

    tube = Image.new("RGB", (800, 800), (255, 255, 255))
    draw = ImageDraw.Draw(tube)
    draw.polygon([(238, 190), (525, 250), (470, 650), (165, 594)], fill=(248, 248, 245), outline=(222, 222, 218))
    draw.polygon([(170, 594), (470, 650), (454, 710), (152, 654)], fill=(238, 238, 234), outline=(215, 215, 210))
    draw.line((242, 218, 508, 273), fill=(230, 230, 226), width=3)
    draw.rectangle((292, 354, 425, 416), outline=(35, 35, 35), width=3)
    draw.text((315, 378), "PURE", fill=(30, 30, 30))
    for i in range(9):
        draw.line((255 + i * 20, 230 + i * 4, 205 + i * 20, 590 + i * 4), fill=(235, 235, 231), width=1)
    cases.append(("tube", tube, (145, 180, 535, 720)))

    pouch = Image.new("RGB", (800, 800), (255, 255, 255))
    draw = ImageDraw.Draw(pouch)
    draw.rounded_rectangle((220, 170, 585, 635), radius=70, fill=(248, 248, 245), outline=(222, 222, 218), width=4)
    draw.polygon([(245, 625), (565, 625), (610, 700), (198, 700)], fill=(238, 238, 234), outline=(216, 216, 212))
    draw.arc((260, 170, 545, 255), start=0, end=180, fill=(228, 228, 224), width=3)
    draw.rectangle((303, 340, 505, 430), outline=(30, 30, 30), width=3)
    draw.text((355, 375), "SOFT", fill=(30, 30, 30))
    for y in range(470, 600, 14):
        draw.line((255, y, 550, y), fill=(233, 233, 230), width=1)
    cases.append(("pouch", pouch, (190, 160, 615, 710)))

    jar = Image.new("RGB", (800, 800), (255, 255, 255))
    draw = ImageDraw.Draw(jar)
    draw.ellipse((240, 215, 570, 340), fill=(250, 250, 247), outline=(222, 222, 218), width=4)
    draw.rectangle((242, 277, 568, 555), fill=(247, 247, 244), outline=(222, 222, 218), width=4)
    draw.ellipse((242, 490, 568, 620), fill=(242, 242, 238), outline=(218, 218, 214), width=4)
    draw.rectangle((340, 376, 475, 445), outline=(30, 30, 30), width=3)
    draw.text((370, 402), "JAR", fill=(30, 30, 30))
    for y in range(300, 530, 16):
        draw.line((260, y, 550, y + 4), fill=(235, 235, 232), width=1)
    cases.append(("jar", jar, (230, 205, 580, 625)))

    for name, image, expected in cases:
        result = generation_media._auto_subject_mask(image.convert("RGBA"))
        assert result.mode == "auto_subject", name
        assert result.confidence >= generation_media.EDIT_MASK_SEND_CONFIDENCE, name
        assert result.bbox is not None, name
        assert bbox_iou(result.bbox, expected) >= 0.82, (name, result.bbox, expected)


def test_subject_protection_preview_reports_alpha_mask_and_requires_owner(client, make_user, auth):
    make_user("13900001976", balance=1000)
    make_user("13900001977", balance=1000)
    owner_h = auth("13900001976")
    other_h = auth("13900001977")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _transparent_product_png_bytes(), "image/png")},
        headers=owner_h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()

    preview = client.post(
        "/api/subject-protection/preview",
        json={"asset_url": asset["url"], "edit_mask_mode": "protect_subject"},
        headers=owner_h,
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["mode"] == "alpha_subject"
    assert data["risk_level"] == "low"
    assert data["will_send_mask"] is True
    assert data["pixel_lock_recommended"] is True
    assert data["mask_data_uri"].startswith("data:image/png;base64,")

    other_preview = client.post(
        "/api/subject-protection/preview",
        json={"asset_url": asset["url"], "edit_mask_mode": "protect_subject"},
        headers=other_h,
    )
    assert other_preview.status_code == 404


def test_subject_protection_preview_distinguishes_auto_center_and_off(client, make_user, auth):
    make_user("13900001978", balance=1000)
    h = auth("13900001978")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _white_bg_product_png_bytes(), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()

    auto_preview = client.post(
        "/api/subject-protection/preview",
        json={"asset_url": asset["url"], "edit_mask_mode": "protect_subject"},
        headers=h,
    )
    assert auto_preview.status_code == 200, auto_preview.text
    auto_data = auto_preview.json()
    assert auto_data["mode"] == "auto_subject"
    assert auto_data["will_send_mask"] is True
    assert auto_data["risk_level"] in {"low", "medium"}

    center_preview = client.post(
        "/api/subject-protection/preview",
        json={"asset_url": asset["url"], "edit_mask_mode": "center_box"},
        headers=h,
    )
    assert center_preview.status_code == 200, center_preview.text
    center_data = center_preview.json()
    assert center_data["mode"] == "center_box"
    assert center_data["risk_level"] == "medium"
    assert center_data["will_send_mask"] is True
    assert "不等于真正识别产品主体" in center_data["message"]

    off_preview = client.post(
        "/api/subject-protection/preview",
        json={"asset_url": asset["url"], "edit_mask_mode": "off"},
        headers=h,
    )
    assert off_preview.status_code == 200, off_preview.text
    off_data = off_preview.json()
    assert off_data["mode"] == "none"
    assert off_data["risk_level"] == "high"
    assert off_data["will_send_mask"] is False


def test_product_image_edit_strict_lock_composites_original_subject_pixels(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13900001973", balance=1000)
    h = auth("13900001973")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _white_bg_product_png_bytes(), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        return [_repainted_product_scene_bytes()]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "put this product into a forest scene",
            "instruction": "put this product into a forest scene",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "edit_mask_mode": "protect_subject",
            "product_pixel_lock": "strict",
        },
    }, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        task = (
            db.query(GenTask)
            .filter(GenTask.user_id == uid)
            .order_by(GenTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.params["_product_pixel_lock"] == "strict"
        assert task.params["_product_composite_applied_count"] == 1
        asset_row = db.query(GenAsset).filter(GenAsset.task_id == task.id).first()
        assert asset_row is not None
        hd_key = asset_row.hd_url.rsplit("/media/", 1)[1]
        hd = Image.open(Path(settings.storage_dir) / hd_key)
        r_px, g_px, b_px = hd.convert("RGB").getpixel((128, 128))
        assert r_px > 140
        assert g_px < 110
        assert b_px < 110
    finally:
        db.close()


def test_product_image_edit_rgb_auto_mask_composites_high_confidence_subject_by_default(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13900001974", balance=1000)
    h = auth("13900001974")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _white_bg_product_png_bytes(), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        return [_repainted_product_scene_bytes()]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "put this product into a forest scene",
            "instruction": "put this product into a forest scene",
        },
        "params": {"n": 1, "size": "1024x1024", "edit_mask_mode": "protect_subject"},
    }, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        task = (
            db.query(GenTask)
            .filter(GenTask.user_id == uid)
            .order_by(GenTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.params["_edit_mask_sent"] is True
        assert task.params["_product_pixel_lock"] == "auto_subject"
        assert task.params["_product_composite_applied_count"] == 1
    finally:
        db.close()


def test_product_image_edit_alpha_mask_records_high_confidence_subject_protection(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13900001971", balance=1000)
    h = auth("13900001971")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("cutout.png", _transparent_product_png_bytes(), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["extra_payload"] = extra_payload or {}
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "put this product in a premium campaign scene",
            "instruction": "put this product in a premium campaign scene",
        },
        "params": {"n": 1, "size": "1024x1024", "edit_mask_mode": "protect_subject"},
    }, headers=h)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        task = (
            db.query(GenTask)
            .filter(GenTask.user_id == uid)
            .order_by(GenTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.params["_edit_mask_mode"] == "alpha_subject"
        assert task.params["_edit_mask_confidence"] >= 0.9
        assert task.params["_edit_mask_bbox"] == [45, 30, 119, 94]
        assert task.params["_edit_mask_requested_mode"] == "protect_subject"
        assert task.params["_edit_mask_sent"] is True
    finally:
        db.close()


def test_product_image_edit_auto_mask_failure_does_not_send_center_box_fallback(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13900001975", balance=1000)
    h = auth("13900001975")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("flat.png", _png_bytes(size=(300, 200)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["extra_payload"] = extra_payload or {}
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "change the scene but keep the product",
            "instruction": "change the scene but keep the product",
        },
        "params": {"n": 1, "size": "1024x1024", "edit_mask_mode": "protect_subject"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert "mask" not in seen["extra_payload"]

    db = SessionLocal()
    try:
        task = (
            db.query(GenTask)
            .filter(GenTask.user_id == uid)
            .order_by(GenTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.params["_edit_mask_mode"] == "none"
        assert task.params["_edit_mask_sent"] is False
    finally:
        db.close()


def test_product_image_edit_explicit_center_box_mask_is_sent(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13900001972", balance=1000)
    h = auth("13900001972")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("flat.png", _png_bytes(size=(300, 200)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["extra_payload"] = extra_payload or {}
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "change the outer scene but keep center product",
            "instruction": "change the outer scene but keep center product",
        },
        "params": {"n": 1, "size": "1024x1024", "edit_mask_mode": "center_box"},
    }, headers=h)
    assert r.status_code == 200, r.text

    mask = seen["extra_payload"].get("mask")
    assert mask.startswith("data:image/png;base64,")
    assert _mask_alpha_at(mask, 150, 100) > 200
    assert _mask_alpha_at(mask, 10, 10) < 20
    db = SessionLocal()
    try:
        task = (
            db.query(GenTask)
            .filter(GenTask.user_id == uid)
            .order_by(GenTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.params["_edit_mask_mode"] == "center_box"
        assert task.params["_edit_mask_requested_mode"] == "center_box"
        assert task.params["_edit_mask_sent"] is True
    finally:
        db.close()


def test_product_image_edit_can_disable_auto_mask(
    client, make_user, auth, monkeypatch
):
    make_user("13900001969", balance=1000)
    h = auth("13900001969")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _png_bytes(size=(900, 600)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["extra_payload"] = extra_payload or {}
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "product_generation_mode": True,
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "put this product in a clean studio scene",
            "instruction": "put this product in a clean studio scene",
        },
        "params": {"n": 1, "size": "1024x1024", "edit_mask_mode": "off"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert "mask" not in seen["extra_payload"]


def test_portrait_image_edit_accepts_character_reference_and_server_fidelity_guard(
    client, make_user, auth, monkeypatch
):
    make_user("13900001967", balance=1000)
    h = auth("13900001967")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("portrait.png", _png_bytes(size=(1200, 1600)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["prompt"] = prompt
        seen["reference_image_url"] = reference_image_url
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "selected_type": "image",
            "mode": "image_edit",
            "portrait_generation_mode": True,
            "subject_mode": "portrait",
        },
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "turn this portrait into a premium xiaohongshu cover",
            "instruction": "turn this portrait into a premium xiaohongshu cover",
        },
        "params": {
            "n": 1,
            "size": "1024x1024",
            "subject_mode": "portrait",
            "character_reference_image": asset["url"],
        },
    }, headers=h)
    assert r.status_code == 200, r.text

    assert "人像高保真硬约束" in seen["prompt"]
    assert "上传人像照片是唯一人物身份来源" in seen["prompt"]
    assert seen["reference_image_url"].startswith("data:image/jpeg;base64,")


def test_image_edit_can_send_product_and_style_refs_when_enabled(
    client, make_user, auth, monkeypatch
):
    make_user("13900001907", balance=1000, admin=True)
    h = auth("13900001907")
    _enable_multi_image_edit()

    product = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _png_bytes(size=(1600, 900), color=(220, 20, 20)), "image/png")},
        headers=h,
    ).json()
    style = client.post(
        "/api/uploads/image",
        files={"file": ("style.png", _png_bytes(size=(500, 800), color=(20, 220, 20)), "image/png")},
        headers=h,
    ).json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, reference_image_urls=None,
                       edit_path=None, extra_payload=None):
        seen["reference_image_url"] = reference_image_url
        seen["reference_image_urls"] = reference_image_urls
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": product["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "keep product identity and transfer style",
            "instruction": "keep product identity and transfer style",
        },
        "params": {"n": 1, "size": "1024x576", "style_reference_image": style["url"]},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert len(seen["reference_image_urls"]) == 2
    assert seen["reference_image_urls"][0] == seen["reference_image_url"]
    product_ref = Image.open(io.BytesIO(base64.b64decode(seen["reference_image_urls"][0].split(",", 1)[1])))
    style_ref = Image.open(io.BytesIO(base64.b64decode(seen["reference_image_urls"][1].split(",", 1)[1])))
    assert max(product_ref.size) == 1024
    assert max(style_ref.size) == 384


def test_image_edit_skips_invalid_optional_style_ref_when_multi_image_enabled(
    client, make_user, auth, monkeypatch
):
    make_user("13900001908", balance=1000, admin=True)
    h = auth("13900001908")
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    _enable_multi_image_edit()
    product = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _png_bytes(size=(1600, 900)), "image/png")},
        headers=h,
    ).json()
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, reference_image_urls=None,
                       edit_path=None, extra_payload=None):
        seen["reference_image_urls"] = reference_image_urls
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)
    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_a, **_k: b"not-an-image",
    )

    r = client.post("/api/generate", json={
        "source_asset_url": product["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {
            "final_text": "keep product identity and transfer style",
            "instruction": "keep product identity and transfer style",
        },
        "params": {
            "n": 1,
            "size": "1024x576",
            "style_reference_image": "https://example.com/style.jpg",
        },
    }, headers=h)
    assert r.status_code == 200, r.text
    assert len(seen["reference_image_urls"]) == 1


def test_uploaded_image_requires_owner_for_read_and_generation(client, make_user, auth):
    make_user("13900000103", balance=1000)
    make_user("13900000104", balance=1000)
    owner_h = auth("13900000103")
    other_h = auth("13900000104")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=owner_h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    path = urlparse(asset["url"]).path

    assert client.get(path, headers=other_h).status_code == 404
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "use another user's image"},
        "params": {"n": 1, "size": "512x512"},
    }, headers=other_h)
    assert r.status_code == 404
    assert "上传素材" in r.text


def test_uploaded_image_can_drive_reverse_prompt(client, make_user, auth, monkeypatch):
    make_user("13900000105", balance=1000)
    h = auth("13900000105")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_reverse(refs, model_id, target="image"):
        seen["refs"] = refs
        return {"structured": {"主体": "uploaded"}, "final_text": "uploaded prompt"}

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fake_reverse)
    r = client.post("/api/prompt/reverse", json={
        "asset_url": asset["url"],
        "target": "image",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["refs"][0].startswith("data:image/jpeg;base64,")


def test_uploaded_large_image_reverse_prompt_uses_higher_quality_reference(client, make_user, auth, monkeypatch):
    make_user("13900001909", balance=1000)
    h = auth("13900001909")

    up = client.post(
        "/api/uploads/image",
        files={"file": ("large-ref.png", _png_bytes(size=(1600, 900)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_reverse(refs, model_id, target="image"):
        seen["refs"] = refs
        return {"structured": {"主体": "large uploaded"}, "final_text": "large uploaded prompt"}

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fake_reverse)
    r = client.post("/api/prompt/reverse", json={
        "asset_url": asset["url"],
        "target": "image",
    }, headers=h)
    assert r.status_code == 200, r.text
    ref_bytes = base64.b64decode(seen["refs"][0].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert ref_img.size == (1024, 576)


def test_model_reference_does_not_explode_extreme_aspect_ratio():
    ref_bytes, _, _ = make_model_reference(
        _png_bytes(size=(768, 1)),
        min_side=300,
        max_side=768,
    )
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert max(ref_img.size) <= 768
    assert ref_img.size[0] == 768
    assert ref_img.size[1] == 1


def test_external_image_reverse_prompt_uses_high_quality_localized_reference(
    client, make_user, auth, monkeypatch
):
    make_user("13900001910", balance=1000)
    h = auth("13900001910")
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_args, **_kwargs: _png_bytes(size=(1800, 1200)),
    )
    seen = {}

    def fake_reverse(refs, model_id, target="image"):
        seen["refs"] = refs
        return {"structured": {"主体": "external"}, "final_text": "external prompt"}

    monkeypatch.setattr("app.services.gateway.reverse_prompt", fake_reverse)
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "https://cdn.example.com/social-product.png",
        "target": "image",
    }, headers=h)
    assert r.status_code == 200, r.text
    ref = seen["refs"][0]
    assert ref.startswith("data:image/jpeg;base64,")
    assert "cdn.example.com" not in ref
    ref_bytes = base64.b64decode(ref.split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert ref_img.size[0] == 1024
    assert 680 <= ref_img.size[1] <= 683


def test_uploaded_image_can_drive_video_first_frame(client, make_user, auth, monkeypatch):
    make_user("13900000106", balance=1000, admin=True)
    h = auth("13900000106")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.update(params)
        return "mock-upload-video"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "animate upload"},
        "params": {"duration": 5, "resolution": "720p", "ratio": "9:16"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")
    assert seen["last_frame_image"] == seen["first_frame_image"]
    assert seen["_product_locked"] is True
    ref_bytes = base64.b64decode(seen["first_frame_image"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert min(ref_img.size) >= 300


def test_uploaded_image_video_free_motion_does_not_auto_lock_last_frame(client, make_user, auth, monkeypatch):
    make_user("13900001970", balance=1000, admin=True)
    h = auth("13900001970")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.update(params)
        return "mock-upload-video-free"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "animate upload with dynamic movement"},
        "params": {
            "duration": 5,
            "resolution": "720p",
            "ratio": "9:16",
            "subject_mode": "product",
            "product_lock_mode": "free",
        },
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")
    assert "last_frame_image" not in seen
    assert "_product_locked" not in seen


def test_uploaded_product_video_uses_high_fidelity_reference_and_negative_prompt(
    client, make_user, auth, monkeypatch
):
    make_user("13900001971", balance=1000, admin=True)
    h = auth("13900001971")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _png_bytes(size=(2000, 1200)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.update(params)
        return "mock-product-video-fidelity"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "生成产品广告视频"},
        "params": {
            "duration": 5,
            "resolution": "720p",
            "ratio": "16:9",
            "subject_mode": "product",
        },
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["last_frame_image"] == seen["first_frame_image"]
    assert seen["_product_locked"] is True
    assert "包装文字乱码" in seen["negative_prompt"]
    assert "Logo扭曲" in seen["negative_prompt"]
    ref_bytes = base64.b64decode(seen["first_frame_image"].split(",", 1)[1])
    ref_img = Image.open(io.BytesIO(ref_bytes))
    assert max(ref_img.size) == 1280


def test_uploaded_portrait_image_can_drive_video_character_reference(client, make_user, auth, monkeypatch):
    make_user("13900000179", balance=1000, admin=True)
    h = auth("13900000179")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/image",
        files={"file": ("portrait.png", _png_bytes(size=(1200, 1600)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.update(params)
        return "mock-portrait-video"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "source_asset_meta": {
            "mode": "video_edit",
            "portrait_generation_mode": True,
            "subject_mode": "portrait",
        },
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "rebuild target video style with the uploaded person"},
        "params": {
            "duration": 5,
            "resolution": "720p",
            "ratio": "9:16",
            "subject_mode": "portrait",
            "character_reference_image": asset["url"],
        },
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["character_reference_image"].startswith("data:image/jpeg;base64,")
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")
    assert seen["last_frame_image"] == seen["first_frame_image"]
    assert seen["_portrait_locked"] is True


def test_uploaded_image_final_video_uses_data_uri_first_frame(client, make_user, auth, monkeypatch):
    make_user("13900000107", balance=1000, admin=True)
    h = auth("13900000107")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/image",
        files={"file": ("ref.png", _png_bytes(size=(40, 80)), "image/png")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = []

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.append(dict(params))
        return f"mock-upload-video-{len(seen)}"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)

    preview = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "image",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "animate upload"},
        "params": {"duration": 8, "resolution": "720p", "target_resolution": "1080p", "ratio": "9:16"},
    }, headers=h)
    assert preview.status_code == 200, preview.text
    db = SessionLocal()
    try:
        parent = db.get(GenTask, preview.json()["id"])
        assert parent.params["resolution"] == "1080p"
        assert parent.params["duration"] == 8
        assert parent.params["preview_resolution"] == "480p"
        assert parent.params["preview_duration"] == 5
    finally:
        db.close()

    final = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": preview.json()["id"],
        "params": {},
    }, headers=h)
    assert final.status_code == 200, final.text
    assert len(seen) >= 2
    assert seen[-1]["first_frame_image"].startswith("data:image/jpeg;base64,")
    assert seen[-1]["last_frame_image"] == seen[-1]["first_frame_image"]


def test_upload_video_returns_reference_asset(client, make_user, auth, tmp_path):
    uid = make_user("13900000110", balance=1000)
    h = auth("13900000110")

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    asset = r.json()

    assert asset["type"] == "video"
    assert asset["width"] == 32
    assert asset["height"] == 48
    url_key = urlparse(asset["url"]).path.removeprefix("/api/uploads/")
    assert url_key.startswith("upload_video/")
    assert url_key.endswith(".mp4")
    anon = TestClient(app)
    assert anon.get(urlparse(asset["url"]).path).status_code == 401
    assert client.get(urlparse(asset["url"]).path, headers=h).status_code == 200
    if asset.get("thumb"):
        thumb_key = urlparse(asset["thumb"]).path.removeprefix("/api/uploads/")
        assert thumb_key.startswith("upload_video_preview/")
        assert client.get(urlparse(asset["thumb"]).path, headers=h).status_code == 200
    db = SessionLocal()
    try:
        log = db.query(AuditLog).filter(
            AuditLog.user_id == uid,
            AuditLog.action == "upload_video",
        ).order_by(AuditLog.id.desc()).first()
        row = db.get(UploadedAsset, url_key)
        assert row.mime == "video/mp4"
        assert row.bytes > 0
        assert "rights_confirmed" not in log.detail
        assert "rights_confirmation" not in log.detail
        assert log.detail["sanitized"] is True
        assert log.detail["raw_bytes"] > 0
    finally:
        db.close()


def test_upload_video_runs_probe_off_event_loop(client, make_user, auth, monkeypatch, tmp_path):
    make_user("13900001909", balance=1000)
    h = auth("13900001909")
    calls = []
    cleanup_callbacks = []

    async def fake_run_upload_thread(func, *args, on_cancel_result=None, **kwargs):
        calls.append(getattr(func, "__name__", str(func)))
        if on_cancel_result is not None:
            cleanup_callbacks.append((getattr(func, "__name__", str(func)), on_cancel_result))
        return func(*args, **kwargs)

    from app.routers import uploads

    monkeypatch.setattr(uploads, "_run_upload_thread", fake_run_upload_thread)

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )

    assert r.status_code == 200, r.text
    assert "_sanitize_video_and_poster" in calls
    assert {name for name, _callback in cleanup_callbacks} == {
        "_sanitize_video_and_poster",
        "save_file",
        "save_bytes_named",
    }


def test_upload_video_rejects_when_ffprobe_missing(client, make_user, auth, monkeypatch, tmp_path):
    make_user("13900000115", balance=1000)
    h = auth("13900000115")
    monkeypatch.setattr("app.services.video_frames.FFPROBE", None)

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )

    assert r.status_code == 400
    assert "ffprobe" in r.text


def test_upload_video_transcode_disables_network_protocols_and_caps_output(monkeypatch, tmp_path):
    from app.routers import uploads

    src = tmp_path / "src.mp4"
    src.write_bytes(b"fake")
    seen = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"sanitized")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.services.video_frames.FFMPEG", "ffmpeg")
    monkeypatch.setattr(uploads.subprocess, "run", fake_run)
    monkeypatch.setattr("app.routers.uploads.settings.max_upload_video_bytes", 12345)

    out = uploads._transcode_sanitized_video(src, duration=1)

    try:
        cmd = seen["cmd"]
        assert "-protocol_whitelist" in cmd
        assert cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"
        assert "-fs" in cmd
        assert cmd[cmd.index("-fs") + 1] == "12345"
        assert "http" not in " ".join(cmd).lower()
    finally:
        out.unlink(missing_ok=True)


def test_upload_video_rejects_sanitized_output_over_limit(monkeypatch, tmp_path):
    from app.routers import uploads

    src = tmp_path / "src.mp4"
    src.write_bytes(b"fake")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"x" * 20)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.services.video_frames.FFMPEG", "ffmpeg")
    monkeypatch.setattr(uploads.subprocess, "run", fake_run)
    monkeypatch.setattr("app.routers.uploads.settings.max_upload_video_bytes", 10)

    try:
        uploads._transcode_sanitized_video(src, duration=1)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 413
        assert "视频净化后仍超过" in str(getattr(exc, "detail", ""))
    else:
        raise AssertionError("oversized sanitized video should be rejected")


def test_upload_video_allows_upload_without_rights_confirmation(client, make_user, auth, tmp_path):
    make_user("13900001905", balance=1000)
    h = auth("13900001905")

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )

    assert r.status_code == 200, r.text


def test_upload_video_rejects_over_duration_limit(client, make_user, auth, monkeypatch, tmp_path):
    make_user("13900000116", balance=1000)
    h = auth("13900000116")
    monkeypatch.setattr("app.routers.uploads.settings.max_video_seconds", 0)

    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )

    assert r.status_code == 400
    assert "视频时长不能超过" in r.text


def test_upload_video_rejects_user_storage_quota_and_cleans_file(
    client, make_user, auth, monkeypatch, tmp_path
):
    uid = make_user("13900001902", balance=1000)
    h = auth("13900001902")
    mp4 = _mp4_bytes(tmp_path)
    monkeypatch.setattr("app.routers.uploads.settings.user_upload_storage_quota_bytes", max(1, len(mp4) - 1))

    before = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}
    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", mp4, "video/mp4")},
        headers=h,
    )
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 413
    assert "上传空间不足" in r.text
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(UploadedAsset.user_id == uid).count() == 0
    finally:
        db.close()


def test_upload_video_quota_preflight_rejects_before_video_probe(
    client, make_user, auth, monkeypatch, tmp_path
):
    uid = make_user("13900001906", balance=1000)
    h = auth("13900001906")
    mp4 = _mp4_bytes(tmp_path)
    monkeypatch.setattr("app.routers.uploads.settings.user_upload_storage_quota_bytes", 1)

    def fail_probe(_path):
        raise AssertionError("video probe should not run after quota preflight rejection")

    monkeypatch.setattr("app.routers.uploads._sanitize_video_and_poster", fail_probe)
    before = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}
    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", mp4, "video/mp4")},
        headers={**h, "Content-Length": str(len(mp4) + 2 * 1024 * 1024)},
    )
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 413
    assert "上传空间不足" in r.text
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(UploadedAsset.user_id == uid).count() == 0
    finally:
        db.close()


def test_upload_video_missing_content_length_rejects_quota_before_video_probe(
    client, make_user, auth, monkeypatch, tmp_path
):
    uid = make_user("13900001910", balance=1000)
    h = auth("13900001910")
    mp4 = _mp4_bytes(tmp_path)
    monkeypatch.setattr("app.routers.uploads.settings.user_upload_storage_quota_bytes", max(1, len(mp4) - 1))

    def fail_probe(_path):
        raise AssertionError("video probe should not run after streamed quota rejection")

    monkeypatch.setattr("app.routers.uploads._sanitize_video_and_poster", fail_probe)
    before = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}
    r = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", mp4, "video/mp4")},
        headers=h,
    )
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 413
    assert "上传空间不足" in r.text
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(UploadedAsset.user_id == uid).count() == 0
    finally:
        db.close()


def test_uploaded_video_requires_owner_for_read_and_generation(client, make_user, auth, tmp_path):
    make_user("13900000111", balance=1000)
    make_user("13900000112", balance=1000)
    owner_h = auth("13900000111")
    other_h = auth("13900000112")

    up = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=owner_h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()

    assert client.get(urlparse(asset["url"]).path, headers=other_h).status_code == 404
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "video",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "edit another user's video"},
        "params": {"duration": 5, "resolution": "720p", "ratio": "9:16"},
    }, headers=other_h)
    assert r.status_code == 404
    assert "上传素材" in r.text


def test_uploaded_video_can_drive_video_first_frame(client, make_user, auth, monkeypatch, tmp_path):
    make_user("13900000113", balance=1000, admin=True)
    h = auth("13900000113")
    assert client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    up = client.post(
        "/api/uploads/video",
        files={"file": ("ref.mp4", _mp4_bytes(tmp_path), "video/mp4")},
        headers=h,
    )
    assert up.status_code == 200, up.text
    asset = up.json()
    seen = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        seen.update(params)
        return "mock-upload-video"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    r = client.post("/api/generate", json={
        "source_asset_url": asset["url"],
        "source_type": "video",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "edit uploaded video into a polished ad"},
        "params": {"duration": 5, "resolution": "720p", "ratio": "9:16"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["first_frame_image"].startswith("data:image/jpeg;base64,")
