"""Integration-test harness: SQLite DB, fakeredis, eager Celery, mock gateway.

Env is set BEFORE any app import so settings pick it up. Redis is replaced with
fakeredis so auth/rate-limit/progress/locks work without a server, and Celery
runs tasks inline (eager) so the full generate flow completes in-request.
"""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"  # unused (faked below)
os.environ["DEBUG"] = "true"
os.environ["MOCK_MODE"] = "true"
os.environ["GATEWAY_API_KEY"] = ""  # force effective mock mode
os.environ["JWT_SECRET"] = "test-secret"
os.environ["PAYMENT_MOCK_ENABLED"] = "true"
os.environ["PAYMENT_CONFIG_SECRET"] = "test-payment-config-secret-32-bytes-min"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8000"
os.environ["PAYMENT_FRONTEND_BASE_URL"] = "http://localhost:3000"
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="storage_")
os.environ["PRODUCT_EDITION"] = "full"

import io  # noqa: E402
import ipaddress  # noqa: E402
import socket  # noqa: E402

import fakeredis  # noqa: E402
from PIL import Image  # noqa: E402

import app.redis_client as rc  # noqa: E402

rc.redis_client = fakeredis.FakeStrictRedis(decode_responses=True)
rc.blocking_redis_client = rc.redis_client

# DNS shim for the SSRF guard: IP literals resolve for real (so SSRF blocks on
# 127.0.0.1 / 169.254.x still fire), but placeholder hostnames used in tests
# (http://x/..., http://example.com/...) resolve to a public IP offline so the
# guard's happy path passes without network.
_real_getaddrinfo = socket.getaddrinfo
_PUBLIC_RESULT = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
_SYNTHETIC_DNS_RANGE = ipaddress.ip_network("198.18.0.0/15")


def _test_getaddrinfo(host, *args, **kwargs):
    host_text = str(host).rstrip(".").lower()
    if host_text == "example.com" or host_text.endswith(".example.com"):
        return _PUBLIC_RESULT
    try:
        ipaddress.ip_address(host_text)
        return _real_getaddrinfo(host, *args, **kwargs)  # literal -> real
    except ValueError:
        pass
    try:
        result = _real_getaddrinfo(host, *args, **kwargs)
    except socket.gaierror:
        return _PUBLIC_RESULT
    if any(
        ipaddress.ip_address(info[4][0]) in _SYNTHETIC_DNS_RANGE
        for info in result
    ):
        return _PUBLIC_RESULT
    return result


socket.getaddrinfo = _test_getaddrinfo

from app.celery_app import celery_app  # noqa: E402

celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PhoneWhitelist, User  # noqa: E402
from app.security import hash_password  # noqa: E402


def _test_png_bytes(size=(32, 32), color=(80, 120, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def clear_fake_redis():
    rc.redis_client.flushall()


@pytest.fixture(autouse=True)
def mock_external_image_download(monkeypatch):
    """External user asset refs are localized before reaching the gateway.

    Most integration tests use placeholder example.com image URLs and should
    not hit the network. Tests that exercise download failures can override
    this monkeypatch locally.
    """
    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_args, **_kwargs: _test_png_bytes(),
    )


@pytest.fixture(scope="session", autouse=True)
def create_test_schema():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def reset_test_database(create_test_schema):
    """Keep integration tests independent when the full suite runs in one process."""
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_user():
    def _make(phone, password="pass123456", balance=1000, admin=False, whitelist=True):
        db = SessionLocal()
        try:
            if whitelist and not db.get(PhoneWhitelist, phone):
                db.add(PhoneWhitelist(phone=phone, note="t", department="dev"))
                db.commit()
            u = db.query(User).filter(User.phone == phone).first()
            if not u:
                u = User(phone=phone, password_hash=hash_password(password),
                         status="active", is_admin=admin, balance_credits=balance,
                         department="dev")
                db.add(u)
                db.commit()
                db.refresh(u)
            elif admin and not u.is_admin:
                u.is_admin = True
                db.commit()
                db.refresh(u)
            return u.id
        finally:
            db.close()
    return _make


@pytest.fixture()
def auth(client):
    def _login(phone, password="pass123456"):
        r = client.post("/api/auth/login", json={"phone": phone, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}
    return _login


@pytest.fixture()
def quote_generation(client):
    """Create an explicit server quote for a valid generation request."""
    def _quote(payload, *, headers=None):
        request_payload = dict(payload)
        request_payload.pop("quote_id", None)
        response = client.post("/api/quotes", json=request_payload, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    return _quote


@pytest.fixture()
def quote_and_generate(client, quote_generation):
    """Quote and submit a valid generation request without hiding either API call."""
    def _submit(payload, *, headers=None):
        quote = quote_generation(payload, headers=headers)
        return client.post(
            "/api/generate",
            json={**payload, "quote_id": quote["quote_id"]},
            headers=headers,
        )

    return _submit


@pytest.fixture()
def quote_reverse(client):
    """Create a server-authoritative quote for one modern reverse operation."""
    def _quote(payload, *, headers=None):
        request_payload = dict(payload)
        request_payload.pop("quote_id", None)
        client_request_id = request_payload["client_request_id"]
        response = client.post(
            "/api/quotes",
            json={
                "kind": "reverse",
                "client_request_id": client_request_id,
                "request": request_payload,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _quote


@pytest.fixture()
def quote_and_reverse(client, quote_reverse):
    """Quote and submit one modern reverse operation."""
    def _submit(payload, *, headers=None):
        quote = quote_reverse(payload, headers=headers)
        return client.post(
            "/api/prompt/reverse-operations",
            json={**payload, "quote_id": quote["quote_id"]},
            headers=headers,
        )

    return _submit


@pytest.fixture()
def quote_reverse_batch(client):
    """Create a server-authoritative quote for a reverse batch."""
    def _quote(payload, *, headers=None):
        request_payload = dict(payload)
        request_payload.pop("quote_id", None)
        client_request_id = request_payload["client_request_id"]
        response = client.post(
            "/api/quotes",
            json={
                "kind": "reverse_batch",
                "client_request_id": client_request_id,
                "request": request_payload,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _quote


@pytest.fixture()
def quote_and_reverse_batch(client, quote_reverse_batch):
    """Quote and submit a reverse batch."""
    def _submit(payload, *, headers=None):
        quote = quote_reverse_batch(payload, headers=headers)
        return client.post(
            "/api/prompt/reverse-batches",
            json={**payload, "quote_id": quote["quote_id"]},
            headers=headers,
        )

    return _submit


@pytest.fixture()
def quote_reverse_retry(client):
    """Create the minimal source-bound quote required by reverse retry."""
    def _quote(operation_id, payload, *, headers=None):
        client_request_id = payload["client_request_id"]
        request_payload = {
            "reverse_operation_id": int(operation_id),
            "client_request_id": client_request_id,
        }
        if payload.get("model_config_id") is not None:
            request_payload["model_config_id"] = payload["model_config_id"]
        response = client.post(
            "/api/quotes",
            json={
                "kind": "reverse",
                "client_request_id": client_request_id,
                "request": request_payload,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _quote


@pytest.fixture()
def quote_and_retry_reverse(client, quote_reverse_retry):
    """Quote and submit a reverse retry while preserving its source binding."""
    def _submit(operation_id, payload, *, headers=None):
        quote = quote_reverse_retry(operation_id, payload, headers=headers)
        return client.post(
            f"/api/prompt/reverse-operations/{operation_id}/retry",
            json={**payload, "quote_id": quote["quote_id"]},
            headers=headers,
        )

    return _submit


@pytest.fixture(scope="session")
def tiny_mp4():
    """A real (tiny) mp4 so video content-validation passes."""
    import os  # noqa: E402
    import shutil  # noqa: E402
    import subprocess  # noqa: E402
    import tempfile  # noqa: E402

    ff = shutil.which("ffmpeg")
    if not ff:
        return b"not-a-real-mp4"
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "t.mp4")
        subprocess.run(
            [ff, "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.2",
             "-pix_fmt", "yuv420p", "-y", out],
            capture_output=True,
        )
        with open(out, "rb") as f:
            return f.read()


@pytest.fixture()
def uploaded_video_url(client, tiny_mp4):
    """Upload a valid locally owned video and return its API asset URL."""
    def _upload(headers):
        response = client.post(
            "/api/uploads/video",
            files={"file": ("source.mp4", tiny_mp4, "video/mp4")},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["url"]

    return _upload
