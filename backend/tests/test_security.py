"""Regression tests for the review-flagged P0/P1 fixes:
refund idempotency, final-inherits-preview, SSRF on user URLs, media traversal.
"""
import io

from PIL import Image

from app.config import settings
from app.db import SessionLocal
from app.deps import get_client_ip
from app.main import validate_runtime_config
from app.models import GenTask, PhoneWhitelist, UploadedAsset, User
from app.redis_client import redis_client
from app.services import asset_refs, generation, storage
from app.services.ssrf import SsrfError, assert_safe_user_asset_url


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
        u = db.get(User, uid)
        u.balance_credits = 90  # simulate the freeze having moved 10 -> frozen
        u.frozen_credits = 10
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
    assert t["cost_settled"] == 20        # cost_credits(5) * n(4)
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 980


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
    assert bal0 - bal1 == 5  # unlock_cost charged once
    # a repeat unlock (double-click / retry) must NOT charge again
    assert client.post(f"/api/assets/{a['id']}/unlock", headers=h).json()["unlocked"]
    assert client.get("/api/me", headers=h).json()["balance_credits"] == bal1


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

    assert ref.startswith("data:image/png;base64,")
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


def test_admin_dangerous_actions_require_admin_password(client, make_user, auth):
    make_user("13900000079", balance=1000, admin=True)
    target = make_user("13900000075", balance=1000)
    h = auth("13900000079")

    r = client.patch(
        f"/api/admin/users/{target}/status",
        json={"status": "disabled"},
        headers=h,
    )
    assert r.status_code == 403

    r = client.post(
        f"/api/admin/users/{target}/reset_password",
        json={"password": "reset12345"},
        headers=h,
    )
    assert r.status_code == 403

    r = client.post(
        "/api/admin/quota/grant",
        json={"user_id": target, "amount": 10, "note": "manual"},
        headers=h,
    )
    assert r.status_code == 403


def test_admin_config_and_whitelist_require_admin_password(client, make_user, auth):
    make_user("13900000089", balance=1000, admin=True)
    h = auth("13900000089")

    no_pw = client.post(
        "/api/admin/whitelist",
        json={"phone": "13900000090", "note": "test", "department": "dev"},
        headers=h,
    )
    assert no_pw.status_code == 403

    ok = client.post(
        "/api/admin/whitelist",
        json={
            "phone": "13900000090",
            "note": "test",
            "department": "dev",
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert ok.status_code == 200, ok.text

    bad_delete = client.request(
        "DELETE",
        "/api/admin/whitelist/13900000090",
        json={},
        headers=h,
    )
    assert bad_delete.status_code == 403

    no_model_pw = client.put(
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
    assert no_model_pw.status_code == 403

    model_ok = client.put(
        "/api/admin/models",
        json={
            "use": "image",
            "model_id": "mock-image",
            "cost_credits": 5,
            "unlock_cost": 5,
            "enabled": True,
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert model_ok.status_code == 200, model_ok.text

    no_settings_pw = client.put(
        "/api/admin/settings",
        json={"image_n": 2},
        headers=h,
    )
    assert no_settings_pw.status_code == 403

    settings_ok = client.put(
        "/api/admin/settings",
        json={"image_n": 2, "admin_password": "pass123456"},
        headers=h,
    )
    assert settings_ok.status_code == 200, settings_ok.text


def test_mock_mode_external_reference_does_not_download(client, make_user, auth, monkeypatch):
    make_user("13900000091", balance=1000)
    h = auth("13900000091")

    def boom(*_args, **_kwargs):
        raise AssertionError("mock generation must not download external references")

    monkeypatch.setattr("app.services.gateway.download_bytes_limited", boom)
    r = client.post("/api/generate", json={
        "source_asset_url": "https://example.com/mock-ref.png",
        "source_type": "image",
        "category": "image",
        "stage": "preview",
        "instruction": "mock reference",
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text


def test_admin_quota_grant_idempotency_blocks_duplicate(client, make_user, auth):
    make_user("13900000087", balance=1000, admin=True)
    target = make_user("13900000088", balance=1000)
    h = auth("13900000087")
    body = {
        "user_id": target,
        "amount": 10,
        "note": "manual adjust",
        "admin_password": "pass123456",
        "idempotency_key": "grant-test-001",
    }
    assert client.post("/api/admin/quota/grant", json=body, headers=h).status_code == 200
    dup = client.post("/api/admin/quota/grant", json=body, headers=h)
    assert dup.status_code == 409


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


def test_admin_password_confirmation_is_rate_limited(client, make_user, auth, monkeypatch):
    make_user("13900000142", balance=1000, admin=True)
    h = auth("13900000142")
    monkeypatch.setattr("app.routers.admin._ADMIN_CONFIRM_FAIL_LIMIT", 2)

    body = {"phone": "13900000143", "admin_password": "wrong-password"}
    assert client.post("/api/admin/whitelist", json=body, headers=h).status_code == 403
    assert client.post("/api/admin/whitelist", json=body, headers=h).status_code == 403
    limited = client.post("/api/admin/whitelist", json=body, headers=h)
    assert limited.status_code == 429

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.phone == "13900000142").first()
        redis_client.delete(f"admin:confirm:fail:{admin.id}")
    finally:
        db.close()
    ok = client.post(
        "/api/admin/whitelist",
        json={"phone": "13900000143", "admin_password": "pass123456"},
        headers=h,
    )
    assert ok.status_code == 200, ok.text


def test_production_requires_real_gateway(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "gateway_base_url", "")
    monkeypatch.setattr(settings, "gateway_api_key", "")
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "metrics-token")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "x" * 48)
    try:
        validate_runtime_config()
    except RuntimeError as e:
        assert "GATEWAY_BASE_URL" in str(e)
    else:
        raise AssertionError("production without real gateway must fail")


def test_client_ip_accepts_trusted_proxy_cidr(monkeypatch):
    class Client:
        host = "172.18.0.5"

    class Request:
        client = Client()
        headers = {"x-forwarded-for": "203.0.113.8, 172.18.0.5"}

    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1,172.16.0.0/12")
    assert get_client_ip(Request()) == "203.0.113.8"
