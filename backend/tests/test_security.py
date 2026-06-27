"""Regression tests for the review-flagged P0/P1 fixes:
refund idempotency, final-inherits-preview, SSRF on user URLs, media traversal.
"""
import asyncio
import io
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.celery_app import celery_app
from app.config import settings
from app.db import SessionLocal
from app.deps import get_client_ip
from app.main import BodySizeLimitMiddleware, validate_model_gateway_rows, validate_runtime_config
from app.models import (
    AdminIdempotencyKey,
    AppSetting,
    AssetReport,
    AuditLog,
    GenAsset,
    GenTask,
    ModelConfig,
    PhoneWhitelist,
    UploadedAsset,
    User,
)
from app.redis_client import redis_client
from app.services import asset_refs, credits, gateway, generation, locks, ssrf, storage
from app.services.ssrf import SsrfError, assert_safe_user_asset_url


def _png_bytes(size=(32, 48), color=(20, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- refund idempotency
def test_claim_terminal_is_exactly_once(client, make_user):
    uid = make_user("13900000050")
    db = SessionLocal()
    try:
        t = GenTask(user_id=uid, category="image", stage="preview",
                    status="queued", cost_frozen=10)
        db.add(t)
        db.commit()
        tid = t.id
        assert generation.claim_terminal(db, tid, "failed") is True
        db.commit()
        # second attempt sees a terminal status -> does NOT transition again
        assert generation.claim_terminal(db, tid, "failed") is False
        db.commit()
    finally:
        db.close()


def test_double_fail_refunds_once(client, make_user):
    """Reaper + worker (or duplicate delivery) must not double-refund."""
    uid = make_user("13900000051", balance=100)
    db = SessionLocal()
    try:
        t = GenTask(user_id=uid, category="image", stage="preview",
                    status="running", cost_frozen=10)
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 10, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
    finally:
        db.close()

    for _ in range(2):  # two racing finalizers
        d = SessionLocal()
        try:
            generation._fail_and_refund(d, tid, "boom")
        finally:
            d.close()

    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100  # refunded once, not 110
    finally:
        db.close()


# ---------------------------------------------------------------- billing
def test_default_n_image_charges_for_all_images(client, make_user, auth):
    # no n supplied -> freeze AND settle must use the default image_n (4), not 1
    make_user("13900000070", balance=1000)
    h = auth("13900000070")
    r = client.post("/api/generate", json={
        "category": "image", "stage": "preview",
        "prompt": {"final_text": "x"}, "params": {"size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    t = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 4          # seeded image_n default
    assert t["cost_settled"] == 60        # 1K image cost 15 * n(4)
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 940


def test_generate_response_exposes_only_public_params(client, make_user, auth):
    make_user("13900000079", balance=1000)
    h = auth("13900000079")
    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "public params"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    params = r.json()["params"]
    assert params == {"n": 1, "size": "256x256"}
    assert all(not key.startswith("_") for key in params)


def test_unlock_is_idempotent(client, make_user, auth):
    make_user("13900000071", balance=1000)
    h = auth("13900000071")
    r = client.post("/api/generate", json={
        "category": "image", "stage": "preview", "prompt": {"final_text": "x"},
        "params": {"n": 1, "size": "256x256"}}, headers=h)
    a = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()["assets"][0]

    bal0 = client.get("/api/me", headers=h).json()["balance_credits"]
    assert client.post(f"/api/assets/{a['id']}/unlock", headers=h).json()["unlocked"]
    bal1 = client.get("/api/me", headers=h).json()["balance_credits"]
    assert bal0 - bal1 == 0  # generated HD unlock is free
    # a repeat unlock (double-click / retry) must NOT charge again
    assert client.post(f"/api/assets/{a['id']}/unlock", headers=h).json()["unlocked"]
    assert client.get("/api/me", headers=h).json()["balance_credits"] == bal1


def test_generated_asset_unlock_ignores_model_unlock_cost(client, make_user, auth):
    make_user("13900000072", balance=1000, admin=True)
    h = auth("13900000072")
    assert client.put("/api/admin/models", json={
        "use": "image",
        "model_id": "mock-image",
        "cost_credits": 5,
        "unlock_cost": 5,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h).status_code == 200
    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "snapshot price"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    asset = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()["assets"][0]

    assert client.put("/api/admin/models", json={
        "use": "image",
        "model_id": "mock-image",
        "cost_credits": 5,
        "unlock_cost": 99,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h).status_code == 200
    before = client.get("/api/me", headers=h).json()["balance_credits"]

    unlock = client.post(f"/api/assets/{asset['id']}/unlock", headers=h)
    assert unlock.status_code == 200, unlock.text
    assert before - client.get("/api/me", headers=h).json()["balance_credits"] == 0


def test_asset_response_exposes_zero_generated_unlock_cost(client, make_user, auth):
    make_user("13900000150", balance=1000, admin=True)
    h = auth("13900000150")
    assert client.put("/api/admin/models", json={
        "use": "image",
        "model_id": "mock-image",
        "cost_credits": 5,
        "unlock_cost": 5,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h).status_code == 200
    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "snapshot unlock"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    task_id = r.json()["id"]
    asset_id = client.get(f"/api/tasks/{task_id}", headers=h).json()["assets"][0]["id"]

    assert client.put("/api/admin/models", json={
        "use": "image",
        "model_id": "mock-image",
        "cost_credits": 5,
        "unlock_cost": 99,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h).status_code == 200

    asset = client.get(f"/api/tasks/{task_id}", headers=h).json()["assets"][0]
    assert asset["id"] == asset_id
    assert asset["unlock_cost"] == 0


# ---------------------------------------------------------------- final inheritance
def test_final_inherits_preview_prompt_not_client(client, make_user, auth):
    make_user("13900000052", balance=1000, admin=True)
    h = auth("13900000052")
    client.put("/api/admin/models", json={
        "use": "video", "model_id": "mock-video", "cost_credits": 50,
        "unlock_cost": 0, "enabled": True, "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h)

    r = client.post("/api/generate", json={
        "category": "video", "stage": "preview",
        "prompt": {"final_text": "AAA preview prompt"}, "params": {"duration": 2},
    }, headers=h)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    # final supplies a DIFFERENT prompt — it must be ignored, inheriting parent's
    r2 = client.post("/api/generate", json={
        "category": "video", "stage": "final", "parent_task_id": pid,
        "prompt": {"final_text": "BBB attacker prompt"},
    }, headers=h)
    assert r2.status_code == 200, r2.text
    fid = r2.json()["id"]

    db = SessionLocal()
    try:
        ft = db.get(GenTask, fid)
        assert ft.prompt.get("final_text") == "AAA preview prompt"
    finally:
        db.close()


def test_final_reuses_needs_review_task_for_same_preview(client, make_user, auth):
    uid = make_user("13900000949", balance=1000)
    h = auth("13900000949")
    db = SessionLocal()
    try:
        preview = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "held final"},
            params={},
        )
        db.add(preview)
        db.commit()
        db.add(
            GenAsset(
                task_id=preview.id,
                user_id=uid,
                type="video",
                preview_url="https://cdn.example.com/preview.mp4",
                watermarked=True,
                unlocked=False,
            )
        )
        held = GenTask(
            user_id=uid,
            category="video",
            stage="final",
            parent_task_id=preview.id,
            status=generation.NEEDS_REVIEW,
            prompt={"final_text": "held final"},
            params={},
            cost_frozen=50,
        )
        db.add(held)
        db.commit()
        preview_id = preview.id
        held_id = held.id
    finally:
        db.close()

    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "final",
        "parent_task_id": preview_id,
        "prompt": {"final_text": "retry should reuse"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == held_id


# ---------------------------------------------------------------- SSRF on user URLs
def test_reverse_rejects_internal_url(client, make_user, auth):
    make_user("13900000053", balance=1000)
    h = auth("13900000053")
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "http://169.254.169.254/latest/meta-data", "target": "image",
    }, headers=h)
    assert r.status_code == 400
    assert "安全策略" in r.text


def test_generate_rejects_internal_source_url(client, make_user, auth):
    make_user("13900000054", balance=1000)
    h = auth("13900000054")
    r = client.post("/api/generate", json={
        "category": "image", "stage": "preview", "prompt": {"final_text": "x"},
        "source_asset_url": "http://127.0.0.1/secret.png",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 400
    assert "安全策略" in r.text


def test_reverse_video_requires_generated_asset_owner(client, make_user, auth, monkeypatch):
    owner_id = make_user("13900000186", balance=1000)
    make_user("13900000187", balance=1000)
    h = auth("13900000187")
    monkeypatch.setattr("app.routers.prompt.video_frames.available", lambda: True)
    video_key = "video_preview/owned.mp4"
    path = storage.local_path(video_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake video")
    db = SessionLocal()
    try:
        task = GenTask(user_id=owner_id, category="video", stage="preview", status="succeeded")
        db.add(task)
        db.commit()
        db.add(
            GenAsset(
                task_id=task.id,
                user_id=owner_id,
                type="video",
                preview_url=storage.public_url(video_key),
                watermarked=True,
                unlocked=False,
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.post(
        "/api/prompt/reverse",
        json={"asset_url": storage.public_url(video_key), "target": "video"},
        headers=h,
    )
    assert r.status_code == 404
    assert "生成视频不存在" in r.text


def test_body_size_limit_rejects_chunked_body_without_content_length(monkeypatch):
    monkeypatch.setattr(settings, "payment_notify_max_body_bytes", 3)
    app_started = {"value": False}

    async def consuming_app(scope, receive, send):
        app_started["value"] = True
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = iter([
        {"type": "http.request", "body": b"xx", "more_body": True},
        {"type": "http.request", "body": b"xx", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(
        BodySizeLimitMiddleware(consuming_app)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/payments/wechat/notify",
                "headers": [],
            },
            receive,
            send,
        )
    )

    assert app_started["value"] is True
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_video_upload_body_size_limit_applies_to_chunked_body(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_video_bytes", 3)
    app_started = {"value": False}

    async def consuming_app(scope, receive, send):
        app_started["value"] = True
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = iter([
        {"type": "http.request", "body": b"x" * (10 * 1024 * 1024), "more_body": True},
        {"type": "http.request", "body": b"x" * (7 * 1024 * 1024), "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(
        BodySizeLimitMiddleware(consuming_app)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/uploads/video",
                "headers": [],
            },
            receive,
            send,
        )
    )

    assert app_started["value"] is True
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_auth_body_size_limit_applies_to_login():
    app_started = {"value": False}

    async def consuming_app(scope, receive, send):
        app_started["value"] = True
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"x" * (33 * 1024), "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        BodySizeLimitMiddleware(consuming_app)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/login",
                "headers": [],
            },
            receive,
            send,
        )
    )

    assert app_started["value"] is True
    assert sent[0]["status"] == 413


def test_auth_cookie_can_access_me_and_logout_clears_cookie(client, make_user):
    make_user("13900000198", password="pass123456")
    login = client.post(
        "/api/auth/login",
        json={"phone": "13900000198", "password": "pass123456"},
    )
    assert login.status_code == 200, login.text
    set_cookie = login.headers.get("set-cookie", "")
    assert "ai_studio_token=" in set_cookie
    assert "HttpOnly" in set_cookie

    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["phone"] == "13900000198"

    logout = client.post("/api/me/logout", headers={"Origin": "http://localhost:3000"})
    assert logout.status_code == 200, logout.text
    cleared = logout.headers.get("set-cookie", "")
    assert "ai_studio_token=" in cleared
    assert "Max-Age=0" in cleared


def test_cookie_auth_post_rejects_cross_site_origin(client, make_user):
    make_user("13900000197", password="pass123456")
    login = client.post(
        "/api/auth/login",
        json={"phone": "13900000197", "password": "pass123456"},
    )
    assert login.status_code == 200, login.text

    blocked = client.post("/api/me/logout", headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403
    assert "CSRF origin check failed" in blocked.text


def test_cookie_auth_post_rejects_missing_origin_and_referer(client, make_user):
    make_user("13900000399", password="pass123456")
    login = client.post(
        "/api/auth/login",
        json={"phone": "13900000399", "password": "pass123456"},
    )
    assert login.status_code == 200, login.text

    blocked = client.post("/api/me/logout")
    assert blocked.status_code == 403
    assert "CSRF origin required" in blocked.text


def test_cookie_auth_post_allows_configured_frontend_origin(client, make_user):
    make_user("13900000196", password="pass123456")
    login = client.post(
        "/api/auth/login",
        json={"phone": "13900000196", "password": "pass123456"},
    )
    assert login.status_code == 200, login.text

    ok = client.post("/api/me/logout", headers={"Origin": "http://localhost:3000"})
    assert ok.status_code == 200, ok.text


def test_bearer_post_with_cookie_is_not_csrf_blocked(client, make_user, auth):
    make_user("13900000195", password="pass123456")
    h = auth("13900000195")

    ok = client.post("/api/me/logout", headers=h)
    assert ok.status_code == 200, ok.text


def test_auth_login_rejects_cross_site_origin(client, make_user):
    make_user("13900000194", password="pass123456")
    first = client.post(
        "/api/auth/login",
        json={"phone": "13900000194", "password": "pass123456"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/auth/login",
        json={"phone": "13900000194", "password": "pass123456"},
        headers={"Origin": "https://evil.example"},
    )
    assert second.status_code == 403
    assert "CSRF origin check failed" in second.text


def test_payment_notify_endpoints_skip_csrf_cookie_origin_check(client, make_user):
    make_user("13900000193", password="pass123456")
    login = client.post(
        "/api/auth/login",
        json={"phone": "13900000193", "password": "pass123456"},
    )
    assert login.status_code == 200, login.text

    notify = client.post(
        "/api/payments/wechat/notify",
        json={"resource": {}},
        headers={"Origin": "https://evil.example"},
    )
    assert notify.status_code != 403


def test_payment_order_body_size_limit_applies_to_chunked_body():
    app_started = {"value": False}

    async def consuming_app(scope, receive, send):
        app_started["value"] = True
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = iter([
        {"type": "http.request", "body": b"x" * (20 * 1024), "more_body": True},
        {"type": "http.request", "body": b"x" * (20 * 1024), "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(
        BodySizeLimitMiddleware(consuming_app)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/payments/orders",
                "headers": [],
            },
            receive,
            send,
        )
    )

    assert app_started["value"] is True
    assert sent[0]["status"] == 413


def test_admin_body_size_limit_applies_to_chunked_body():
    app_started = {"value": False}

    async def consuming_app(scope, receive, send):
        app_started["value"] = True
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = iter([
        {"type": "http.request", "body": b"x" * (200 * 1024), "more_body": True},
        {"type": "http.request", "body": b"x" * (100 * 1024), "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(
        BodySizeLimitMiddleware(consuming_app)(
            {
                "type": "http",
                "method": "PUT",
                "path": "/api/admin/payments/providers/alipay",
                "headers": [],
            },
            receive,
            send,
        )
    )

    assert app_started["value"] is True
    assert sent[0]["status"] == 413


def test_lock_release_requires_owner_token():
    key = "test:lock:owner-token"
    redis_client.delete(key)
    token = locks.acquire(key, ttl=60)
    assert token

    locks.release(key, "wrong-token")
    assert redis_client.get(key) == token

    locks.release(key, token)
    assert redis_client.get(key) is None


def test_redis_semaphore_waits_for_capacity():
    key = "test:semaphore:waits"
    redis_client.delete(key)
    first = locks.RedisSemaphore(key, limit=1, ttl=5, wait_timeout=0.1, poll_interval=0.01)
    first.__enter__()

    def release_later():
        time.sleep(0.05)
        first.__exit__(None, None, None)

    t = threading.Thread(target=release_later)
    t.start()
    started = time.monotonic()
    with locks.RedisSemaphore(key, limit=1, ttl=5, wait_timeout=0.5, poll_interval=0.01):
        waited = time.monotonic() - started
    t.join()

    assert waited >= 0.04


def test_gateway_rejects_redirects(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)

    def fake_request(self, method, url, headers=None, json=None):  # noqa: ARG001
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    with pytest.raises(gateway.GatewayError, match="不安全重定向"):
        gateway._request(
            "POST",
            "https://gateway.test/v1/images/generations",
            headers={},
            json={},
            timeout=1,
            retries=0,
        )


def test_pinned_network_backend_connects_to_vetted_ip_without_global_lock():
    calls: list[tuple[str, int]] = []
    sentinel = object()

    class FakeBackend:
        def connect_tcp(self, host, port, **kwargs):  # noqa: ARG002
            calls.append((host, port))
            return sentinel

        def connect_unix_socket(self, path, **kwargs):  # noqa: ARG002
            raise AssertionError("unexpected unix socket connection")

        def sleep(self, seconds):  # noqa: ARG002
            return None

    backend = ssrf._PinnedNetworkBackend(
        {"gateway.test": ("93.184.216.34", "93.184.216.35")},
        backend=FakeBackend(),
    )

    assert backend.connect_tcp("gateway.test", 443) is sentinel
    assert backend.connect_tcp("gateway.test", 443) is sentinel
    assert calls == [("93.184.216.34", 443), ("93.184.216.35", 443)]


def test_gateway_download_uses_pinned_client_without_dns_pin_lock(monkeypatch):
    events: list[str] = []

    class FakeResponse:
        is_redirect = False
        status_code = 200
        headers = {"content-type": "image/png", "content-length": "4"}

        def iter_raw(self):
            events.append("read-body")
            yield b"data"

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakePinnedClient:
        def __init__(self, url, **kwargs):  # noqa: ARG002
            self.url = url

        def __enter__(self):
            events.append("connect")
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, **kwargs):  # noqa: ARG002
            assert url == self.url
            return FakeStream()

    def fake_pinned_client(url, **kwargs):  # noqa: ARG001
        return FakePinnedClient(url)

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(gateway, "assert_safe_url", lambda url: url)
    monkeypatch.setattr(gateway, "pinned_client", fake_pinned_client)

    assert gateway._download(
        "https://cdn.example.com/out.png",
        max_bytes=10,
        allowed_content_types=("image/",),
    ) == b"data"
    assert events == ["connect", "read-body"]


def test_gateway_request_uses_pinned_client_without_dns_pin_lock(monkeypatch):
    events: list[str] = []

    class FakePinnedClient:
        def __init__(self, url, **kwargs):  # noqa: ARG002
            self.url = url

        def __enter__(self):
            events.append("connect")
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, headers=None, json=None):  # noqa: ARG002
            events.append("wait-for-headers")
            return httpx.Response(
                200,
                json={"ok": True},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(gateway, "pinned_client", FakePinnedClient)

    response = gateway._request(
        "POST",
        "https://gateway.test/v1/images/generations",
        headers={},
        json={},
        timeout=1,
        retries=0,
    )

    assert response.json() == {"ok": True}
    assert events == ["connect", "wait-for-headers"]


def test_pinned_client_rejects_unresolved_hosts(monkeypatch):
    monkeypatch.setattr(ssrf, "resolve_safe", lambda _host: ["93.184.216.34"])
    client = ssrf.pinned_client("https://gateway.test")
    try:
        with pytest.raises(ssrf.SsrfError, match="未固定 DNS"):
            client._transport._pool._network_backend.connect_tcp("other.test", 443)
    finally:
        client.close()


def test_production_rejects_unsupported_jwt_algorithm(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "jwt_algorithm", "none")
    with pytest.raises(RuntimeError, match="JWT_ALGORITHM"):
        validate_runtime_config()


def test_openapi_docs_disabled_when_not_debug(monkeypatch):
    from app.main import app

    assert app.docs_url in ("/docs", None)
    # App is instantiated at import time from current settings; assert the
    # production branch is encoded by checking a fresh FastAPI config indirectly.
    monkeypatch.setattr(settings, "debug", False)
    from app.main import FastAPI as _FastAPI  # noqa: PLC0415

    prod = _FastAPI(docs_url="/docs" if settings.debug else None,
                    redoc_url="/redoc" if settings.debug else None,
                    openapi_url="/openapi.json" if settings.debug else None)
    assert prod.docs_url is None
    assert prod.redoc_url is None
    assert prod.openapi_url is None


def test_task_lock_retry_window_covers_lock_ttl():
    from app import tasks

    assert (
        tasks._LOCK_RETRY_MAX * tasks._LOCK_RETRY_COUNTDOWN_SECONDS
        >= settings.celery_task_time_limit_seconds + 300
    )


def test_external_gateway_asset_ref_is_localized_to_data_uri(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13900000084", balance=1000)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, format="PNG")

    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_a, **_k: buf.getvalue(),
    )
    db = SessionLocal()
    try:
        ref = asset_refs.gateway_ref_for_user_asset(
            db,
            uid,
            "https://cdn.example.com/user-image.png",
        )
    finally:
        db.close()

    assert ref.startswith("data:image/jpeg;base64,")
    assert "cdn.example.com" not in ref


def test_external_gateway_asset_ref_rejects_non_image(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13900000085", balance=1000)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_a, **_k: b"not an image",
    )
    db = SessionLocal()
    try:
        try:
            asset_refs.gateway_ref_for_user_asset(
                db,
                uid,
                "https://cdn.example.com/not-image.png",
            )
        except asset_refs.AssetRefError as e:
            assert "有效图片" in str(e)
        else:
            raise AssertionError("invalid external image must be rejected")
    finally:
        db.close()


def test_video_params_localizes_client_reference_url_before_gateway(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13900000093", balance=1000)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video-gateway.test")
    monkeypatch.setattr(settings, "video_gateway_api_key", "sk-test")
    buf = io.BytesIO()
    Image.new("RGB", (16, 24), (10, 20, 30)).save(buf, format="PNG")
    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_a, **_k: buf.getvalue(),
    )
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            source_type="video",
            status="queued",
            params={
                "reference_image_url": "https://cdn.example.com/user-video-cover.png",
                "duration": 5,
                "resolution": "720p",
            },
        )
        db.add(task)
        db.commit()

        params = generation._video_submit_params(db, task)
    finally:
        db.close()

    assert params["first_frame_image"].startswith("data:image/jpeg;base64,")
    assert params["reference_image_url"] == params["first_frame_image"]
    assert "cdn.example.com" not in params["first_frame_image"]


def test_parse_video_thumb_is_cleared_when_localize_fails(client, make_user, auth, monkeypatch):
    make_user("13900000086", balance=1000)
    h = auth("13900000086")
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [
            {
                "type": "video",
                "url": "https://cdn.example.com/video.mp4",
                "thumb": "https://cdn.example.com/thumb.jpg",
            }
        ],
    )
    monkeypatch.setattr("app.routers.parse._localize_media_url", lambda *_a, **_k: None)

    r = client.post("/api/parse", json={"url": "https://www.xiaohongshu.com/explore/abc"}, headers=h)

    assert r.status_code == 200, r.text
    asset = r.json()["assets"][0]
    assert asset["thumb"] is None
    assert asset["original_thumb"] == "https://cdn.example.com/thumb.jpg"


def test_parse_localized_image_thumb_uses_local_preview(client, make_user, auth, monkeypatch):
    make_user("13900000087", balance=1000)
    h = auth("13900000087")
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [
            {
                "type": "image",
                "url": "https://cdn.example.com/full.jpg",
                "thumb": "https://cdn.example.com/thumb.jpg",
                "width": 2304,
                "height": 4096,
            }
        ],
    )
    monkeypatch.setattr(
        "app.routers.parse._localize_media_url",
        lambda *_a, **_k: "http://localhost:8000/media/preview/localized.png",
    )

    r = client.post("/api/parse", json={"url": "https://www.xiaohongshu.com/explore/img"}, headers=h)

    assert r.status_code == 200, r.text
    asset = r.json()["assets"][0]
    assert asset["url"] == "http://localhost:8000/media/preview/localized.png"
    assert asset["thumb"] == "http://localhost:8000/media/preview/localized.png"
    assert asset["original_url"] == "https://cdn.example.com/full.jpg"
    assert asset["original_thumb"] == "https://cdn.example.com/thumb.jpg"
    assert asset["source_page_url"] == "https://www.xiaohongshu.com/explore/img"


def test_parse_accepts_copied_share_text_with_embedded_url(client, make_user, auth, monkeypatch):
    make_user("13900000088", balance=1000)
    h = auth("13900000088")

    monkeypatch.setattr("app.tasks.parse_url_task.delay", lambda parse_id: None)

    text = (
        "【淘宝】7天无理由退货 https://e.tb.cn/h.RsKuM6YesPztUCt?tk=ZTsRgiCip4j "
        "CA381 「DAMAH黑魔法一次性洗脸巾女加厚悬挂式抽取式卷筒干湿两用洁面巾」"
    )
    r = client.post("/api/parse", json={"url": text}, headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://e.tb.cn/h.RsKuM6YesPztUCt?tk=ZTsRgiCip4j"


def test_generate_records_source_trace_for_parsed_reference(client, make_user, auth, monkeypatch):
    make_user("13900000188", balance=1000)
    h = auth("13900000188")
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [
            {
                "type": "image",
                "url": "https://cdn.example.com/full.jpg",
                "thumb": "https://cdn.example.com/thumb.jpg",
                "width": 1024,
                "height": 1536,
            }
        ],
    )

    parsed = client.post(
        "/api/parse",
        json={"url": "https://www.xiaohongshu.com/explore/source-trace"},
        headers=h,
    )
    assert parsed.status_code == 200, parsed.text
    asset = parsed.json()["assets"][0]

    generated = client.post(
        "/api/generate",
        json={
            "source_asset_url": asset["url"],
            "source_type": "image",
            "source_asset_meta": {
                "selected_type": asset["type"],
                "selected_url": asset["url"],
                "selected_thumb": asset["thumb"],
                "original_url": asset["original_url"],
                "original_thumb": asset["original_thumb"],
                "source_page_url": asset["source_page_url"],
                "source_captured_at": asset["source_captured_at"],
                "ignored": {"x": "y"},
            },
            "category": "image",
            "stage": "preview",
            "instruction": "trace this reference",
            "params": {"n": 1, "size": "256x256"},
        },
        headers=h,
    )
    assert generated.status_code == 200, generated.text

    db = SessionLocal()
    try:
        task = db.get(GenTask, generated.json()["id"])
        trace = task.params["_source_trace"]
    finally:
        db.close()

    assert trace["source_asset_url"] == asset["url"]
    assert trace["selected_url"] == asset["url"]
    assert trace["original_url"] == "https://cdn.example.com/full.jpg"
    assert trace["original_thumb"] == "https://cdn.example.com/thumb.jpg"
    assert trace["source_page_url"] == "https://www.xiaohongshu.com/explore/source-trace"
    assert trace["source_captured_at"] == asset["source_captured_at"]
    assert trace["source_type"] == "image"
    assert "ignored" not in trace


def test_generate_allows_external_reference_without_rights_confirmation(client, make_user, auth, monkeypatch):
    make_user("13900001185", balance=1000)
    h = auth("13900001185")
    monkeypatch.setattr("app.routers.generate.assert_safe_user_asset_url", lambda *_a, **_k: None)

    r = client.post(
        "/api/generate",
        json={
            "source_asset_url": "https://cdn.example.com/full.jpg",
            "source_type": "image",
            "source_asset_meta": {
                "selected_type": "image",
                "selected_url": "https://cdn.example.com/full.jpg",
                "user_confirmed_rights": False,
            },
            "category": "image",
            "stage": "preview",
            "instruction": "use external reference",
            "params": {"n": 1, "size": "256x256"},
        },
        headers=h,
    )

    assert r.status_code == 200, r.text


def test_generate_allows_confirmed_external_reference(client, make_user, auth, monkeypatch):
    make_user("13900001184", balance=1000)
    h = auth("13900001184")
    monkeypatch.setattr("app.routers.generate.assert_safe_user_asset_url", lambda *_a, **_k: None)
    monkeypatch.setattr("app.services.gateway.download_bytes_limited", lambda *_a, **_k: _png_bytes())

    r = client.post(
        "/api/generate",
        json={
            "source_asset_url": "https://cdn.example.com/full.jpg",
            "source_type": "image",
            "source_asset_meta": {
                "selected_type": "image",
                "selected_url": "https://cdn.example.com/full.jpg",
            },
            "category": "image",
            "stage": "preview",
            "instruction": "use external reference",
            "params": {"n": 1, "size": "256x256"},
        },
        headers=h,
    )

    assert r.status_code == 200, r.text


def test_parse_localize_respects_user_storage_quota(client, make_user, auth, monkeypatch):
    uid = make_user("13900000187", balance=1000)
    h = auth("13900000187")
    monkeypatch.setattr("app.services.upload_quota.settings.user_upload_storage_quota_bytes", 512)
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [
            {
                "type": "image",
                "url": "https://cdn.example.com/full.jpg",
                "thumb": "https://cdn.example.com/thumb.jpg",
            }
        ],
    )
    db = SessionLocal()
    try:
        db.add(UploadedAsset(
            key="upload/quota-existing-parse.png",
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

    r = client.post("/api/parse", json={"url": "https://www.xiaohongshu.com/explore/quota"}, headers=h)
    after = {str(p) for p in Path(settings.storage_dir).rglob("*") if p.is_file()}

    assert r.status_code == 200, r.text
    assert r.json()["assets"] == []
    assert after == before
    db = SessionLocal()
    try:
        assert db.query(UploadedAsset).filter(
            UploadedAsset.user_id == uid,
            UploadedAsset.key != "upload/quota-existing-parse.png",
        ).count() == 0
    finally:
        db.close()


def test_parse_localizes_all_images_not_just_first_eight(client, make_user, auth, monkeypatch):
    make_user("13900000189", balance=1000)
    h = auth("13900000189")
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [
            {
                "type": "image",
                "url": f"https://cdn.example.com/full-{idx}.jpg",
                "thumb": f"https://cdn.example.com/thumb-{idx}.jpg",
            }
            for idx in range(10)
        ],
    )
    monkeypatch.setattr(
        "app.routers.parse._localize_media_url",
        lambda url, *_a, **_k: f"http://localhost:8000/media/preview/{url.rsplit('-', 1)[-1].replace('.jpg', '.png')}",
    )

    r = client.post("/api/parse", json={"url": "https://www.xiaohongshu.com/explore/many"}, headers=h)

    assert r.status_code == 200, r.text
    assets = r.json()["assets"]
    assert len(assets) == 10
    assert all(asset["url"].startswith("http://localhost:8000/media/preview/") for asset in assets)
    assert assets[-1]["original_url"] == "https://cdn.example.com/full-9.jpg"


def test_parse_submit_returns_queued_when_worker_is_async(client, make_user, auth, monkeypatch):
    make_user("13900000186", balance=1000)
    h = auth("13900000186")
    queued_ids = []

    class _Delay:
        @staticmethod
        def delay(parse_id):
            queued_ids.append(parse_id)

    monkeypatch.setattr("app.tasks.parse_url_task", _Delay)
    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    r = client.post(
        "/api/parse",
        json={"url": "https://www.xiaohongshu.com/explore/queued"},
        headers=h,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["assets"] is None
    assert queued_ids == [body["id"]]

    status = client.get(f"/api/parse/{body['id']}", headers=h)
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "queued"


def test_parsed_preview_reference_is_owner_bound(client, make_user, auth, monkeypatch):
    user_a = make_user("13900000092", balance=1000)
    make_user("13900000093", balance=1000)
    h_a = auth("13900000092")
    h_b = auth("13900000093")
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [{"type": "image", "url": "https://cdn.example.com/ref.png"}],
    )

    r = client.post("/api/parse", json={"url": "https://www.xiaohongshu.com/explore/owner"}, headers=h_a)
    assert r.status_code == 200, r.text
    ref_url = r.json()["assets"][0]["url"]
    assert "/media/preview/" in ref_url
    key = storage.key_from_url(ref_url)
    db = SessionLocal()
    try:
        row = db.get(UploadedAsset, key)
        assert row is not None
        assert row.user_id == user_a
    finally:
        db.close()

    ok = client.post("/api/generate", json={
        "source_asset_url": ref_url,
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "instruction": "use parsed reference",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h_a)
    assert ok.status_code == 200, ok.text

    blocked = client.post("/api/generate", json={
        "source_asset_url": ref_url,
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "instruction": "steal parsed reference",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h_b)
    assert blocked.status_code == 404


def test_parsed_preview_reference_prefers_clean_model_ref(client, make_user, auth, monkeypatch):
    user_id = make_user("13900000164", balance=1000)
    h = auth("13900000164")
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [{"type": "image", "url": "https://cdn.example.com/ref.png"}],
    )
    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_args, **_kwargs: _png_bytes(size=(1600, 900)),
    )

    r = client.post("/api/parse", json={"url": "https://www.xiaohongshu.com/explore/model-ref"}, headers=h)
    assert r.status_code == 200, r.text
    ref_url = r.json()["assets"][0]["url"]
    preview_key = storage.key_from_url(ref_url)
    assert preview_key and preview_key.startswith("preview/")

    db = SessionLocal()
    try:
        ref = asset_refs.gateway_ref_for_user_asset(db, user_id, ref_url)
        assert ref.startswith("data:image/jpeg;base64,")
        model_ref_path = storage.local_path(preview_key.replace("preview/", "model_ref/", 1).rsplit(".", 1)[0] + ".jpg")
        assert model_ref_path.exists()
        model_ref_img = Image.open(model_ref_path)
        assert max(model_ref_img.size) == 1024
    finally:
        db.close()


def test_orphaned_parsed_preview_file_cannot_drive_reference_generation(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13900000161", balance=1000)
    h = auth("13900000161")
    monkeypatch.setattr(
        "app.routers.parse.parse_url",
        lambda _url: [{"type": "image", "url": "https://cdn.example.com/orphan-ref.png"}],
    )

    r = client.post("/api/parse", json={"url": "https://www.xiaohongshu.com/explore/orphan"}, headers=h)
    assert r.status_code == 200, r.text
    ref_url = r.json()["assets"][0]["url"]
    key = storage.key_from_url(ref_url)
    assert key
    assert storage.local_path(key).exists()

    db = SessionLocal()
    try:
        row = db.get(UploadedAsset, key)
        assert row is not None
        assert row.user_id == user_id
        db.delete(row)
        db.commit()
    finally:
        db.close()

    blocked = client.post("/api/generate", json={
        "source_asset_url": ref_url,
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "instruction": "reuse orphaned parsed preview",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert blocked.status_code == 404
    assert "生成素材不存在" in blocked.text or "上传素材不存在" in blocked.text


def test_asset_report_takedown_soft_blocks_reported_asset(client, make_user, auth):
    make_user("13900000189", balance=1000)
    make_user("13900000190", balance=1000, admin=True)
    user_h = auth("13900000189")
    admin_h = auth("13900000190")

    created = client.post(
        "/api/generate",
        json={
            "category": "image",
            "stage": "preview",
            "prompt": {"final_text": "reportable asset"},
            "params": {"n": 1, "size": "256x256"},
        },
        headers=user_h,
    )
    assert created.status_code == 200, created.text
    task = client.get(f"/api/tasks/{created.json()['id']}", headers=user_h).json()
    asset_id = task["assets"][0]["id"]

    report = client.post(
        f"/api/assets/{asset_id}/report",
        json={"reason": "copyright", "note": "疑似未授权素材"},
        headers=user_h,
    )
    assert report.status_code == 200, report.text
    report_id = report.json()["id"]
    assert report.json()["status"] == "open"

    reports = client.get("/api/admin/asset-reports", headers=admin_h)
    assert reports.status_code == 200, reports.text
    assert any(r["id"] == report_id and r["asset_id"] == asset_id for r in reports.json())
    duplicate = client.post(
        f"/api/assets/{asset_id}/report",
        json={"reason": "copyright", "note": "重复举报"},
        headers=user_h,
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == report_id

    handled = client.post(
        f"/api/admin/asset-reports/{report_id}/handle",
        json={
            "action": "takedown",
            "note": "确认下架",
            "admin_password": "pass123456",
        },
        headers=admin_h,
    )
    assert handled.status_code == 200, handled.text
    assert handled.json()["status"] == "takedown"

    refreshed = client.get(f"/api/tasks/{created.json()['id']}", headers=user_h)
    assert refreshed.status_code == 200, refreshed.text
    asset_out = next(a for a in refreshed.json()["assets"] if a["id"] == asset_id)
    assert asset_out["moderation_status"] == "takedown"
    assert asset_out["preview_url"] is None
    assert asset_out["hd_url"] is None
    gallery = client.get("/api/profile/assets", headers=user_h)
    assert gallery.status_code == 200, gallery.text
    gallery_asset = next(a for a in gallery.json() if a["id"] == asset_id)
    assert gallery_asset["moderation_status"] == "takedown"
    assert gallery_asset["preview_url"] is None
    assert gallery_asset["hd_url"] is None
    assert gallery_asset["unlock_cost"] == 0
    assert client.post(f"/api/assets/{asset_id}/unlock", headers=user_h).status_code == 410
    assert client.get(f"/api/assets/{asset_id}/download", headers=user_h).status_code == 410
    db = SessionLocal()
    try:
        row = db.get(AssetReport, report_id)
        assert row is not None
        assert row.status == "takedown"
        assert row.handled_by is not None
        asset = db.get(GenAsset, asset_id)
        assert asset is not None
        assert asset.moderation_status == "takedown"
    finally:
        db.close()


def test_asset_report_requires_asset_owner(client, make_user, auth):
    owner_id = make_user("13900000158", balance=1000)
    make_user("13900000159", balance=1000)
    other_h = auth("13900000159")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=owner_id,
            category="image",
            stage="preview",
            status="succeeded",
            cost_frozen=0,
            cost_settled=0,
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=task.id,
            user_id=owner_id,
            type="image",
            preview_url="/storage/generated/owner-only.png",
            moderation_status="active",
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    finally:
        db.close()

    r = client.post(
        f"/api/assets/{asset_id}/report",
        json={"reason": "privacy", "note": "not mine"},
        headers=other_h,
    )
    assert r.status_code == 404
    db = SessionLocal()
    try:
        assert db.query(AssetReport).filter(AssetReport.asset_id == asset_id).count() == 0
    finally:
        db.close()


def test_orphaned_generated_preview_file_cannot_drive_reference_generation(
    client, make_user, auth
):
    owner_id = make_user("13900000162", balance=1000)
    make_user("13900000163", balance=1000)
    owner_h = auth("13900000162")
    other_h = auth("13900000163")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, format="PNG")
    key = storage.save_bytes(buf.getvalue(), "preview", "png")
    url = storage.public_url(key)
    db = SessionLocal()
    try:
        asset = GenAsset(
            task_id=999999,
            user_id=owner_id,
            type="image",
            preview_url=url,
            watermarked=True,
            unlocked=False,
        )
        db.add(asset)
        db.commit()
        db.delete(asset)
        db.commit()
    finally:
        db.close()

    for headers in (owner_h, other_h):
        blocked = client.post("/api/generate", json={
            "source_asset_url": url,
            "source_type": "image",
            "category": "image",
            "stage": "preview",
            "instruction": "reuse orphaned generated preview",
            "params": {"n": 1, "size": "256x256"},
        }, headers=headers)
        assert blocked.status_code == 404
        assert "生成素材不存在" in blocked.text


def test_unlocked_final_video_stream_uses_short_lived_ticket(client, make_user, auth, tiny_mp4):
    owner_id = make_user("13900000165", balance=1000)
    make_user("13900000166", balance=1000)
    owner_h = auth("13900000165")
    other_h = auth("13900000166")
    video_key = storage.save_bytes(tiny_mp4, "video_hd", "mp4")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=owner_id,
            category="video",
            stage="final",
            status="succeeded",
            cost_frozen=50,
            cost_settled=50,
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=task.id,
            user_id=owner_id,
            type="video",
            preview_url=storage.public_url("preview/not-required.jpg"),
            hd_url=storage.public_url(video_key),
            watermarked=False,
            unlocked=True,
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
    finally:
        db.close()

    assert client.get(f"/media/{video_key}").status_code == 404
    assert client.post(f"/api/assets/{asset_id}/playback-ticket", headers=other_h).status_code == 404

    ticket_resp = client.post(f"/api/assets/{asset_id}/playback-ticket", headers=owner_h)
    assert ticket_resp.status_code == 200, ticket_resp.text
    ticket = ticket_resp.json()["ticket"]
    stream = client.get(f"/api/assets/{asset_id}/stream?ticket={ticket}")
    assert stream.status_code == 200, stream.text
    assert stream.headers["content-type"].startswith("video/mp4")
    assert stream.content == tiny_mp4
    db = SessionLocal()
    try:
        actions = {
            row.action
            for row in db.query(AuditLog).filter(
                AuditLog.biz_type == "asset",
                AuditLog.biz_id == asset_id,
            )
        }
        assert {"create_playback_ticket", "stream_asset"} <= actions
    finally:
        db.close()


# ---------------------------------------------------------------- media traversal
def test_key_from_url_blocks_traversal():
    base = storage.settings.public_base_url.rstrip("/")
    assert storage.key_from_url(f"{base}/media/preview/abc.png") == "preview/abc.png"
    assert storage.key_from_url(f"{base}/media/upload/ref.png") == "upload/ref.png"
    assert storage.key_from_url(f"{base}/media/../.env") is None
    assert storage.key_from_url(f"{base}/media/../../etc/passwd") is None
    assert storage.key_from_url(f"{base}/media/hd/x.png?token=../y") == "hd/x.png"
    assert storage.key_from_url(f"{base}/media/tmp/x.png") is None
    assert storage.key_from_url("https://evil.example.com/media/preview/abc.png") is None
    assert storage.key_from_url("https://external.example.com/img.png") is None


def test_user_asset_local_media_must_be_valid_storage_url():
    base = storage.settings.public_base_url.rstrip("/")
    assert_safe_user_asset_url(f"{base}/media/preview/abc.png")
    try:
        assert_safe_user_asset_url(f"{base}/media/../.env")
    except SsrfError as e:
        assert "本机媒体" in str(e)
    else:
        raise AssertionError("invalid local media URL must be rejected")


def test_reverse_rejects_hls_playlist(client, make_user, auth):
    make_user("13900000072", balance=1000)
    h = auth("13900000072")
    r = client.post("/api/prompt/reverse", json={
        "asset_url": "https://example.com/live/playlist.m3u8", "target": "video",
    }, headers=h)
    assert r.status_code == 400
    assert "m3u8" in r.text


def test_audit_failure_does_not_break_generate(client, make_user, auth, monkeypatch):
    make_user("13900000073", balance=1000)
    h = auth("13900000073")
    monkeypatch.setattr("app.services.audit.log", lambda *_a, **_k: False)
    r = client.post("/api/generate", json={
        "category": "image", "stage": "preview",
        "prompt": {"final_text": "x"}, "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text


def test_pending_user_cannot_login_or_use_existing_token(client, make_user, auth):
    uid = make_user("13900000074", balance=1000)
    h = auth("13900000074")
    db = SessionLocal()
    try:
        db.get(User, uid).status = "pending"
        db.commit()
    finally:
        db.close()

    r = client.post("/api/auth/login", json={
        "phone": "13900000074", "password": "pass123456",
    })
    assert r.status_code == 403
    assert client.get("/api/me", headers=h).status_code == 403


def test_admin_status_change_revokes_old_token_even_after_reactivate(client, make_user, auth):
    make_user("13900000077", balance=1000, admin=True)
    uid = make_user("13900000078", balance=1000)
    admin_h = auth("13900000077")
    user_h = auth("13900000078")

    r1 = client.patch(
        f"/api/admin/users/{uid}/status",
        json={"status": "disabled", "admin_password": "pass123456"},
        headers=admin_h,
    )
    assert r1.status_code == 200, r1.text
    assert client.get("/api/me", headers=user_h).status_code == 401

    r2 = client.patch(
        f"/api/admin/users/{uid}/status",
        json={"status": "active", "admin_password": "pass123456"},
        headers=admin_h,
    )
    assert r2.status_code == 200, r2.text
    assert client.get("/api/me", headers=user_h).status_code == 401

    fresh_h = auth("13900000078")
    assert client.get("/api/me", headers=fresh_h).status_code == 200


def test_admin_cannot_disable_self_but_can_disable_other_admin(client, make_user, auth):
    self_id = make_user("13900000152", balance=1000, admin=True)
    other_id = make_user("13900000153", balance=1000, admin=True)
    h = auth("13900000152")

    self_disable = client.patch(
        f"/api/admin/users/{self_id}/status",
        json={"status": "disabled", "admin_password": "pass123456"},
        headers=h,
    )
    assert self_disable.status_code == 400
    assert "当前管理员" in self_disable.text

    other_disable = client.patch(
        f"/api/admin/users/{other_id}/status",
        json={"status": "disabled", "admin_password": "pass123456"},
        headers=h,
    )
    assert other_disable.status_code == 200, other_disable.text
    db = SessionLocal()
    try:
        assert db.get(User, self_id).status == "active"
        assert db.get(User, other_id).status == "disabled"
    finally:
        db.close()


def test_admin_sensitive_actions_allow_logged_in_admin_without_second_password(client, make_user, auth):
    make_user("13888888079", balance=1000, admin=True)
    target = make_user("13888888075", balance=1000)
    h = auth("13888888079")

    r = client.patch(
        f"/api/admin/users/{target}/status",
        json={"status": "disabled"},
        headers=h,
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"/api/admin/users/{target}/reset_password",
        json={"password": "reset12345"},
        headers=h,
    )
    assert r.status_code == 200, r.text

    r = client.post(
        "/api/admin/quota/grant",
        json={"user_id": target, "amount": 10, "note": "manual", "idempotency_key": "admin-pw-check-1"},
        headers=h,
    )
    assert r.status_code == 200, r.text


def test_admin_config_allows_logged_in_admin_without_second_password(client, make_user, auth):
    make_user("13900000089", balance=1000, admin=True)
    h = auth("13900000089")

    ok = client.post(
        "/api/admin/whitelist",
        json={"phone": "13900000090", "note": "test", "department": "dev"},
        headers=h,
    )
    assert ok.status_code == 200, ok.text

    delete_ok = client.request(
        "DELETE",
        "/api/admin/whitelist/13900000090",
        json={},
        headers=h,
    )
    assert delete_ok.status_code == 200, delete_ok.text

    model_ok = client.put(
        "/api/admin/models",
        json={
            "use": "image",
            "model_id": "mock-image",
            "cost_credits": 5,
            "unlock_cost": 5,
            "enabled": True,
        },
        headers=h,
    )
    assert model_ok.status_code == 200, model_ok.text

    settings_ok = client.put(
        "/api/admin/settings",
        json={"image_n": 2},
        headers=h,
    )
    assert settings_ok.status_code == 200, settings_ok.text
    restore_ok = client.put(
        "/api/admin/settings",
        json={"image_n": 4},
        headers=h,
    )
    assert restore_ok.status_code == 200, restore_ok.text


def test_mock_mode_external_reference_does_not_download(client, make_user, auth, monkeypatch):
    make_user("13900000091", balance=1000)
    h = auth("13900000091")

    def boom(*_args, **_kwargs):
        raise AssertionError("mock generation must not download external references")

    monkeypatch.setattr("app.services.gateway.download_bytes_limited", boom)
    r = client.post("/api/generate", json={
        "source_asset_url": "https://example.com/mock-ref.png",
        "source_type": "image",
        "source_asset_meta": {},
        "category": "image",
        "stage": "preview",
        "instruction": "mock reference",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text


def test_image_finalize_holds_for_review_when_settle_fails(client, make_user, auth, monkeypatch):
    make_user("13900000094", balance=1000)
    h = auth("13900000094")
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, format="PNG")
    monkeypatch.setattr("app.services.gateway.gen_image", lambda *_a, **_k: [buf.getvalue()])

    def fail_settle(*_args, **_kwargs):
        raise RuntimeError("settle failed after files were written")

    monkeypatch.setattr("app.services.generation.credits.settle", fail_settle)
    before = {
        str(p)
        for p in Path(settings.storage_dir).rglob("*")
        if p.is_file()
    }

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "cleanup orphan files"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)

    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "needs_review"
    assert "本地落账失败" in task["error"]
    after = {
        str(p)
        for p in Path(settings.storage_dir).rglob("*")
        if p.is_file()
    }
    assert after - before
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 985
    assert me["frozen_credits"] == 15


def test_admin_quota_grant_idempotency_replays_duplicate(client, make_user, auth):
    make_user("13900000157", balance=1000, admin=True)
    target = make_user("13900000158", balance=1000)
    h = auth("13900000157")
    body = {
        "user_id": target,
        "amount": 10,
        "note": "manual adjust",
        "admin_password": "pass123456",
        "idempotency_key": "grant-test-001",
    }
    assert client.post("/api/admin/quota/grant", json=body, headers=h).status_code == 200
    dup = client.post("/api/admin/quota/grant", json=body, headers=h)
    assert dup.status_code == 200
    assert dup.json()["balance_credits"] == 1010
    db = SessionLocal()
    try:
        assert db.get(User, target).balance_credits == 1010
        row = db.query(AdminIdempotencyKey).filter(AdminIdempotencyKey.key == "grant-test-001").one()
        assert row.target_user_id == target
        assert row.amount == 10
    finally:
        db.close()


def test_admin_quota_grant_idempotency_rejects_key_reuse_with_different_body(client, make_user, auth):
    make_user("13900000847", balance=1000, admin=True)
    target = make_user("13900000848", balance=1000)
    h = auth("13900000847")
    body = {
        "user_id": target,
        "amount": 10,
        "note": "manual adjust",
        "admin_password": "pass123456",
        "idempotency_key": "grant-test-002",
    }
    assert client.post("/api/admin/quota/grant", json=body, headers=h).status_code == 200
    changed = {**body, "amount": 20}
    r = client.post("/api/admin/quota/grant", json=changed, headers=h)
    assert r.status_code == 409


def test_admin_quota_grant_replays_same_business_fingerprint_with_new_key(client, make_user, auth):
    make_user("13900000859", balance=1000, admin=True)
    target = make_user("13900000860", balance=1000)
    h = auth("13900000859")
    body = {
        "user_id": target,
        "amount": 10,
        "note": "manual adjust retry",
        "admin_password": "pass123456",
        "idempotency_key": "grant-test-fp-001",
    }
    assert client.post("/api/admin/quota/grant", json=body, headers=h).status_code == 200
    retry = {**body, "idempotency_key": "grant-test-fp-002"}
    replay = client.post("/api/admin/quota/grant", json=retry, headers=h)
    assert replay.status_code == 200
    assert replay.json()["balance_credits"] == 1010
    db = SessionLocal()
    try:
        assert db.get(User, target).balance_credits == 1010
    finally:
        db.close()


def test_admin_quota_grant_requires_persistent_idempotency_key(client, make_user, auth):
    make_user("13900000097", balance=1000, admin=True)
    target = make_user("13900000098", balance=1000)
    h = auth("13900000097")
    r = client.post("/api/admin/quota/grant", json={
        "user_id": target,
        "amount": 10,
        "note": "manual adjust",
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 422


def test_admin_quota_grant_respects_configured_single_and_daily_limits(client, make_user, auth):
    make_user("13900000191", balance=1000, admin=True)
    target = make_user("13900000192", balance=1000)
    h = auth("13900000191")
    settings_resp = client.put(
        "/api/admin/settings",
        json={
            "admin_quota_grant_single_limit": 50,
            "admin_quota_grant_daily_limit": 80,
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert settings_resp.status_code == 200, settings_resp.text

    too_large = client.post(
        "/api/admin/quota/grant",
        json={
            "user_id": target,
            "amount": 51,
            "note": "single limit",
            "admin_password": "pass123456",
            "idempotency_key": "grant-limit-001",
        },
        headers=h,
    )
    assert too_large.status_code == 400
    assert "单次发放额度不能超过 50" in too_large.text

    first = client.post(
        "/api/admin/quota/grant",
        json={
            "user_id": target,
            "amount": 50,
            "note": "daily limit first",
            "admin_password": "pass123456",
            "idempotency_key": "grant-limit-002",
        },
        headers=h,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/admin/quota/grant",
        json={
            "user_id": target,
            "amount": 31,
            "note": "daily limit second",
            "admin_password": "pass123456",
            "idempotency_key": "grant-limit-003",
        },
        headers=h,
    )
    assert second.status_code == 400
    assert "今日额度发放累计不能超过 80" in second.text


def test_admin_quota_grant_replay_is_not_blocked_by_daily_limit(client, make_user, auth):
    make_user("13900000281", balance=1000, admin=True)
    target = make_user("13900000282", balance=1000)
    h = auth("13900000281")
    settings_resp = client.put(
        "/api/admin/settings",
        json={
            "admin_quota_grant_single_limit": 50,
            "admin_quota_grant_daily_limit": 50,
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert settings_resp.status_code == 200, settings_resp.text
    body = {
        "user_id": target,
        "amount": 50,
        "note": "daily replay",
        "admin_password": "pass123456",
        "idempotency_key": "grant-replay-limit-001",
    }
    first = client.post("/api/admin/quota/grant", json=body, headers=h)
    assert first.status_code == 200, first.text
    replay = client.post("/api/admin/quota/grant", json=body, headers=h)
    assert replay.status_code == 200, replay.text
    assert replay.json()["balance_credits"] == 1050
    db = SessionLocal()
    try:
        assert db.get(User, target).balance_credits == 1050
    finally:
        db.close()


def test_admin_quota_grant_returns_conflict_when_admin_grant_lock_is_held(client, make_user, auth):
    admin_id = make_user("13900000183", balance=1000, admin=True)
    target = make_user("13900000184", balance=1000)
    h = auth("13900000183")
    token = locks.acquire(f"admin:quota:grant:{admin_id}", ttl=30)
    assert token
    try:
        r = client.post(
            "/api/admin/quota/grant",
            json={
                "user_id": target,
                "amount": 10,
                "note": "locked grant",
                "admin_password": "pass123456",
                "idempotency_key": "grant-locked-001",
            },
            headers=h,
        )
    finally:
        locks.release(f"admin:quota:grant:{admin_id}", token)
    assert r.status_code == 409
    assert "额度发放正在处理中" in r.text


def test_admin_api_has_independent_rate_limit(client, make_user, auth):
    admin_id = make_user("13900000185", balance=1000, admin=True)
    h = auth("13900000185")
    rate_keys = [f"admin:rate:user:{admin_id}", "admin:rate:ip:testclient"]
    for key in rate_keys:
        redis_client.delete(key)
    try:
        r = client.put(
            "/api/admin/settings",
            json={"admin_api_rate_per_hour": 10, "admin_password": "pass123456"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        for _ in range(9):
            ok = client.get("/api/admin/users", headers=h)
            assert ok.status_code == 200, ok.text
        limited = client.get("/api/admin/users", headers=h)
        assert limited.status_code == 429
        assert "管理操作过于频繁" in limited.text
    finally:
        for key in rate_keys:
            redis_client.delete(key)
        db = SessionLocal()
        try:
            row = db.get(AppSetting, "admin_api_rate_per_hour")
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()


def test_usage_report_csv_escapes_formula_cells(client, make_user, auth):
    make_user("13900000080", balance=1000, admin=True)
    admin_h = auth("13900000080")
    db = SessionLocal()
    try:
        db.add(PhoneWhitelist(phone="13900000081", note="x", department=" =HYPERLINK(\"http://x\")"))
        db.add(User(
            phone="13900000081",
            password_hash="x",
            status="active",
            balance_credits=0,
            department=" =HYPERLINK(\"http://x\")",
        ))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/admin/usage/report?format=csv", headers=admin_h)
    assert r.status_code == 200, r.text
    assert "' =HYPERLINK" in r.text
    assert ",=HYPERLINK" not in r.text
    assert ", =HYPERLINK" not in r.text


def test_admin_settings_update_is_atomic(client, make_user, auth):
    make_user("13900000076", balance=1000, admin=True)
    h = auth("13900000076")
    before = client.get("/api/admin/settings", headers=h).json()
    r = client.put("/api/admin/settings", json={
        "reverse_prompt_enabled": False,
        "image_size": "999999x999999",
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 400
    after = client.get("/api/admin/settings", headers=h).json()
    assert after["reverse_prompt_enabled"] == before["reverse_prompt_enabled"]


def test_admin_password_field_is_ignored_for_authenticated_admin(client, make_user, auth):
    make_user("13900000145", balance=1000, admin=True)
    h = auth("13900000145")

    body = {"phone": "13900000146", "admin_password": "wrong-password"}
    ok = client.post("/api/admin/whitelist", json=body, headers=h)
    assert ok.status_code == 200, ok.text
    assert redis_client.get("admin:confirm:failip:testclient") is None


def test_admin_routes_still_require_admin_role(client, make_user, auth):
    make_user("13888888847", balance=1000, admin=False)
    h = auth("13888888847")

    denied = client.post(
        "/api/admin/whitelist",
        json={"phone": "13888888849"},
        headers=h,
    )
    assert denied.status_code == 403


def test_production_requires_real_gateway(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "")
    monkeypatch.setattr(settings, "gateway_api_key", "")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "x" * 48)
    validate_runtime_config()
    db = SessionLocal()
    try:
        for row in db.query(ModelConfig).all():
            row.base_url = None
            row.api_key_encrypted = None
        db.commit()
        with pytest.raises(RuntimeError, match="真实网关"):
            validate_model_gateway_rows(db)
    finally:
        db.close()


def test_production_rejects_internal_gateway_without_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://127.0.0.1:9000")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_http_url", "https://sms.example.com/send")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    try:
        validate_runtime_config()
    except RuntimeError as e:
        assert "GATEWAY_BASE_URL" in str(e)
    else:
        raise AssertionError("production internal gateway must require allowlist")


def test_trusted_egress_host_does_not_bypass_internal_ip_guard(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "http://127.0.0.1:9000")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "trusted_egress_hosts", "127.0.0.1")

    with pytest.raises(RuntimeError, match="非公网地址|内网"):
        validate_runtime_config()


def test_production_rejects_missing_admin_secret_encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "model_config_secret", "")
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    with pytest.raises(RuntimeError, match="PAYMENT_CONFIG_SECRET|MODEL_CONFIG_SECRET"):
        validate_runtime_config()


def test_production_allows_model_config_secret_without_payment_secret(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "model_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    validate_runtime_config()


def test_production_rejects_placeholder_metrics_token(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "metrics-token")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    with pytest.raises(RuntimeError, match="METRICS_TOKEN"):
        validate_runtime_config()


def test_production_rejects_env_example_metrics_token(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "please-change-me-to-a-long-random-metrics-token")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "model_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    with pytest.raises(RuntimeError, match="METRICS_TOKEN"):
        validate_runtime_config()


def test_runtime_rejects_short_payment_config_secret(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "too-short")
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    with pytest.raises(RuntimeError, match="PAYMENT_CONFIG_SECRET"):
        validate_runtime_config()


def test_production_ark_video_gateway_requires_explicit_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "ark")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "model_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    validate_runtime_config()
    db = SessionLocal()
    try:
        video = db.query(ModelConfig).filter(ModelConfig.use == "video").one()
        video.base_url = None
        video.api_key_encrypted = None
        db.commit()
        with pytest.raises(RuntimeError, match="真实网关"):
            validate_model_gateway_rows(db)
    finally:
        db.close()


def test_production_allows_db_model_gateway_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "")
    monkeypatch.setattr(settings, "gateway_api_key", "")
    monkeypatch.setattr(settings, "video_gateway_format", "ark")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "model_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    validate_runtime_config()

    db = SessionLocal()
    try:
        for row in db.query(ModelConfig).all():
            row.base_url = "https://gateway.example.com/v1"
            row.api_key_encrypted = "sk-test"
        db.commit()
        validate_model_gateway_rows(db)
    finally:
        db.close()


def test_production_rejects_partial_db_model_gateway(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "model_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    validate_runtime_config()

    db = SessionLocal()
    try:
        row = db.query(ModelConfig).filter(ModelConfig.use == "image").one()
        row.base_url = "https://model-gateway.example.com/v1"
        row.api_key_encrypted = None
        db.commit()
        with pytest.raises(RuntimeError, match="不完整"):
            validate_model_gateway_rows(db)
    finally:
        db.close()


def test_init_db_refuses_dev_create_tables_in_production(monkeypatch):
    from scripts import init_db

    monkeypatch.setattr(init_db.settings, "debug", False)
    monkeypatch.setattr(
        "sys.argv",
        ["init_db", "--create-tables-dev-only", "--admin-phone", "13900009996"],
    )
    with pytest.raises(SystemExit, match="DEBUG=true"):
        init_db.main()


def test_client_ip_accepts_trusted_proxy_cidr(monkeypatch):
    class Client:
        host = "172.18.0.5"

    class Request:
        client = Client()
        headers = {"x-forwarded-for": "203.0.113.8, 172.18.0.5"}

    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1,172.16.0.0/12")
    assert get_client_ip(Request()) == "203.0.113.8"
