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

import io  # noqa: E402
import ipaddress  # noqa: E402
import socket  # noqa: E402

import fakeredis  # noqa: E402
from PIL import Image  # noqa: E402

import app.redis_client as rc  # noqa: E402

rc.redis_client = fakeredis.FakeStrictRedis(decode_responses=True)

# DNS shim for the SSRF guard: IP literals resolve for real (so SSRF blocks on
# 127.0.0.1 / 169.254.x still fire), but placeholder hostnames used in tests
# (http://x/..., http://example.com/...) resolve to a public IP offline so the
# guard's happy path passes without network.
_real_getaddrinfo = socket.getaddrinfo
_PUBLIC_RESULT = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _test_getaddrinfo(host, *args, **kwargs):
    try:
        ipaddress.ip_address(str(host))
        return _real_getaddrinfo(host, *args, **kwargs)  # literal -> real
    except ValueError:
        pass
    try:
        return _real_getaddrinfo(host, *args, **kwargs)
    except socket.gaierror:
        return _PUBLIC_RESULT


socket.getaddrinfo = _test_getaddrinfo

from app.celery_app import celery_app  # noqa: E402

celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PhoneWhitelist, User  # noqa: E402
from app.security import hash_password  # noqa: E402


def _test_png_bytes(size=(32, 32), color=(80, 120, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


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


@pytest.fixture()
def client():
    with TestClient(app) as c:  # triggers lifespan -> create_all + seed
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


@pytest.fixture(scope="session")
def tiny_mp4():
    """A real (tiny) mp4 so video content-validation passes. Falls back to a
    sentinel when ffmpeg is absent (validation is skipped without ffprobe)."""
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
