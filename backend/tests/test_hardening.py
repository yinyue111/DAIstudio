"""Hardening: input validation on generate params + server-side reverse switch."""

from app.db import SessionLocal
from app.models import GenTask, ParseRecord
from app.services import gateway, generation
from app.services.progress import set_progress


def test_text_to_image_without_reference(client, make_user, auth):
    # 即梦-style pure text-to-image: no source_asset_url, just a prompt
    make_user("13900000040", balance=1000)
    h = auth("13900000040")
    r = client.post("/api/generate", json={
        "category": "image", "stage": "preview",
        "prompt": {"final_text": "a calm cat on a windowsill, soft light"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 1


def test_generate_rejects_unknown_video_params(client, make_user, auth):
    make_user("13900000140", balance=1000)
    h = auth("13900000140")
    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "x"},
        "params": {
            "duration": 5,
            "resolution": "480p",
            "callback_url": "http://169.254.169.254/latest/meta-data",
        },
    }, headers=h)
    assert r.status_code == 400
    assert "不支持的生成参数" in r.text


def test_generate_rejects_oversized_prompt(client, make_user, auth, monkeypatch):
    make_user("13900000141", balance=1000)
    h = auth("13900000141")
    monkeypatch.setattr("app.routers.generate.settings.max_prompt_chars", 32)
    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "x" * 128},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 400
    assert "prompt 过长" in r.text


def test_image_partial_success_settles_actual_count(client, make_user, auth, monkeypatch):
    make_user("13900000045", balance=1000)
    h = auth("13900000045")

    def two_images(prompt, model_id, n=4, size="1024x1024", **_kwargs):
        assert n == 4
        return [
            gateway._mock_image(prompt, "256x256", 0),
            gateway._mock_image(prompt, "256x256", 1),
        ]

    monkeypatch.setattr("app.services.gateway.gen_image", two_images)
    r = client.post("/api/generate", json={
        "category": "image", "stage": "preview",
        "prompt": {"final_text": "partial"}, "params": {"n": 4, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded", task
    assert len(task["assets"]) == 2
    assert task["partial"] is True
    assert task["requested_count"] == 4
    assert task["saved_count"] == 2
    assert task["skipped_count"] == 2
    assert task["cost_frozen"] == 20
    assert task["cost_settled"] == 10
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 990


def test_generated_image_over_pixel_limit_is_rejected(client, make_user, auth, monkeypatch):
    make_user("13900000046", balance=1000)
    h = auth("13900000046")
    monkeypatch.setattr("app.services.generation.settings.generated_image_max_pixels", 1)
    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda *_a, **_k: [gateway._mock_image("huge", "256x256", 0)],
    )

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "too many pixels"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)

    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "failed"
    assert task["error"] == "图片生成失败，已退回冻结积分，请稍后重试"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000


def test_partial_image_generation_exposes_skip_reason(client, make_user, auth, monkeypatch):
    make_user("13900000048", balance=1000)
    h = auth("13900000048")
    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda *_a, **_k: [gateway._mock_image("ok", "256x256", 0), b"not-an-image"],
    )

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "partial"},
        "params": {"n": 2, "size": "256x256"},
    }, headers=h)

    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded"
    assert task["partial"] is True
    assert task["requested_count"] == 2
    assert task["saved_count"] == 1
    assert task["skipped_count"] == 1
    assert task["partial_errors"]
    assert len(task["assets"]) == 1


def test_needs_review_image_task_is_not_reprocessed(client, make_user, monkeypatch):
    uid = make_user("13900000047", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="needs_review",
            cost_frozen=10,
            prompt={"final_text": "held"},
            params={"n": 1, "size": "256x256"},
        )
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()

    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("needs_review reran")),
    )

    generation.run_image_task(tid)
    db = SessionLocal()
    try:
        assert db.get(GenTask, tid).status == "needs_review"
    finally:
        db.close()


def test_task_list_batched_keeps_assets_per_task(client, make_user, auth):
    # guards the batched (no-N+1) task-list builder against cross-task asset mixups
    make_user("13900000041", balance=1000)
    h = auth("13900000041")
    ids = []
    for _ in range(2):
        r = client.post("/api/generate", json={
            "category": "image", "stage": "preview",
            "prompt": {"final_text": "x"}, "params": {"n": 2, "size": "256x256"},
        }, headers=h)
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])

    lst = client.get("/api/tasks", headers=h).json()
    got = {t["id"]: t for t in lst}
    for tid in ids:
        assert tid in got
        assert len(got[tid]["assets"]) == 2
        assert all(a["task_id"] == tid for a in got[tid]["assets"])


def test_websocket_progress_uses_db_terminal_status(client, make_user):
    uid = make_user("13900000042", balance=1000)
    login = client.post("/api/auth/login", json={
        "phone": "13900000042",
        "password": "pass123456",
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            prompt={"final_text": "x"},
            model_use="image",
            params={"n": 1, "size": "256x256"},
            status="succeeded",
            cost_frozen=5,
            cost_settled=5,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    # Simulate stale Redis progress from a best-effort progress write that did
    # not get cleaned up after the DB task already committed as terminal.
    set_progress(task_id, 70, "running")

    ticket = client.post(f"/api/tasks/{task_id}/ws-ticket", headers=headers).json()["ticket"]
    with client.websocket_connect(f"/ws/tasks/{task_id}?ticket={ticket}") as ws:
        first = ws.receive_json()
        second = ws.receive_json()

    assert first["status"] == "running"
    assert second["status"] == "succeeded"
    assert second["percent"] == 100


def test_websocket_ticket_is_owner_scoped_and_one_time(client, make_user):
    uid = make_user("13900000043", balance=1000)
    make_user("13900000044", balance=1000)
    login1 = client.post("/api/auth/login", json={
        "phone": "13900000043",
        "password": "pass123456",
    })
    login2 = client.post("/api/auth/login", json={
        "phone": "13900000044",
        "password": "pass123456",
    })
    h1 = {"Authorization": f"Bearer {login1.json()['access_token']}"}
    h2 = {"Authorization": f"Bearer {login2.json()['access_token']}"}

    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            prompt={"final_text": "x"},
            model_use="image",
            params={"n": 1, "size": "256x256"},
            status="succeeded",
            cost_frozen=5,
            cost_settled=5,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    assert client.post(f"/api/tasks/{task_id}/ws-ticket", headers=h2).status_code == 404

    ticket = client.post(f"/api/tasks/{task_id}/ws-ticket", headers=h1).json()["ticket"]
    with client.websocket_connect(f"/ws/tasks/{task_id}?ticket={ticket}") as ws:
        assert ws.receive_json()["status"] == "succeeded"

    try:
        with client.websocket_connect(f"/ws/tasks/{task_id}?ticket={ticket}") as ws:
            ws.receive_json()
        reused = True
    except Exception:
        reused = False
    assert not reused


def test_generate_rejects_oversized_n(client, make_user, auth):
    make_user("13900000020", balance=1000)
    h = auth("13900000020")
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "category": "image", "stage": "preview", "instruction": "x",
        "params": {"n": 99999, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 400
    assert "出图数量" in r.text


def test_generate_rejects_bad_size(client, make_user, auth):
    make_user("13900000021", balance=1000)
    h = auth("13900000021")
    for bad in ("99999x99999", "abc", "1024X", "1024*1024"):
        r = client.post("/api/generate", json={
            "source_asset_url": "http://x/y.png", "source_type": "image",
            "category": "image", "stage": "preview", "instruction": "x",
            "params": {"n": 1, "size": bad},
        }, headers=h)
        assert r.status_code == 400, f"size={bad} should be rejected"


def test_video_reference_rejects_external_video_without_cover(client, make_user, auth):
    make_user("13900000393", balance=1000, admin=True)
    h = auth("13900000393")
    client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
        "extra": {"preview_cost": 5},
    }, headers=h)

    r = client.post("/api/generate", json={
        "source_asset_url": "https://cdn.example.com/source.mp4",
        "source_type": "video",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "looks like the source video"},
        "params": {"duration": 5, "resolution": "480p"},
    }, headers=h)
    assert r.status_code == 400
    assert "视频参考缺少可用封面" in r.text


def test_generate_accepts_4k_size(client, make_user, auth, monkeypatch):
    make_user("13900000029", balance=1000)
    h = auth("13900000029")

    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None):
        seen["size"] = size
        from app.services.gateway import _mock_image
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "source_asset_meta": {"user_confirmed_rights": True},
        "category": "image", "stage": "preview", "instruction": "x",
        "params": {"n": 1, "size": "4096x4096"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["size"] == "4096x4096"


def test_generate_rejects_oversized_duration(client, make_user, auth):
    make_user("13900000022", balance=1000, admin=True)
    h = auth("13900000022")
    client.put("/api/admin/models", json={
        "use": "video", "model_id": "mock-video", "cost_credits": 50,
        "unlock_cost": 0, "enabled": True, "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h)
    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png", "source_type": "image",
        "category": "video", "stage": "preview", "instruction": "x",
        "params": {"duration": 9999, "resolution": "480p"},
    }, headers=h)
    assert r.status_code == 400
    assert "时长" in r.text


def test_generate_accepts_max_15_min_video_duration(client, make_user, auth):
    make_user("13900000028", balance=1000, admin=True)
    h = auth("13900000028")
    client.put("/api/admin/models", json={
        "use": "video", "model_id": "mock-video", "cost_credits": 50,
        "unlock_cost": 0, "enabled": True, "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h)
    r = client.post("/api/generate", json={
        "source_type": "image",
        "category": "video", "stage": "preview", "instruction": "x",
        "params": {"duration": 900, "resolution": "480p", "ratio": "9:16"},
    }, headers=h)
    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded"


def test_admin_rejects_negative_model_cost(client, make_user, auth):
    make_user("13900000024", balance=1000, admin=True)
    h = auth("13900000024")
    r = client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": -1,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": 5},
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 422

    r2 = client.put("/api/admin/models", json={
        "use": "video",
        "model_id": "mock-video",
        "cost_credits": 50,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {"preview_cost": -1},
        "admin_password": "pass123456",
    }, headers=h)
    assert r2.status_code == 422


def test_admin_rejects_zero_or_extreme_model_cost(client, make_user, auth):
    make_user("13900000026", balance=1000, admin=True)
    h = auth("13900000026")
    base = {
        "use": "video",
        "model_id": "mock-video",
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }

    zero = client.put("/api/admin/models", json={**base, "cost_credits": 0}, headers=h)
    assert zero.status_code == 422

    huge = client.put(
        "/api/admin/models",
        json={**base, "cost_credits": 1_000_001, "extra": {"preview_cost": 1}},
        headers=h,
    )
    assert huge.status_code == 422

    huge_preview = client.put(
        "/api/admin/models",
        json={**base, "cost_credits": 50, "extra": {"preview_cost": 1_000_001}},
        headers=h,
    )
    assert huge_preview.status_code == 422


def test_admin_settings_rejects_oversized_default_image_n(client, make_user, auth):
    make_user("13900000025", balance=1000, admin=True)
    h = auth("13900000025")
    r = client.put("/api/admin/settings", json={"image_n": 999, "admin_password": "pass123456"}, headers=h)
    assert r.status_code == 422


def test_reverse_switch_enforced_server_side(client, make_user, auth):
    make_user("13900000023", balance=1000, admin=True)
    h = auth("13900000023")

    # reverse on by default -> works (mock vision model)
    r = client.post("/api/prompt/reverse",
                    json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r.status_code == 200, r.text

    # admin turns it off -> endpoint must refuse, not just the UI
    s = client.put("/api/admin/settings",
                   json={"reverse_prompt_enabled": False,
                         "admin_password": "pass123456"}, headers=h)
    assert s.status_code == 200
    r2 = client.post("/api/prompt/reverse",
                     json={"asset_url": "http://x/y.png", "target": "image"}, headers=h)
    assert r2.status_code == 403
    assert "反推" in r2.text

    # restore so this global setting doesn't leak into other tests (shared DB)
    client.put("/api/admin/settings", json={
        "reverse_prompt_enabled": True,
        "admin_password": "pass123456",
    }, headers=h)


def test_content_safety_blocks_configured_prompt_terms(client, make_user, auth):
    make_user("13900000196", balance=1000, admin=True)
    h = auth("13900000196")
    s = client.put(
        "/api/admin/settings",
        json={
            "content_safety_enabled": True,
            "content_safety_banned_terms": "forbidden-word\nanother-term",
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert s.status_code == 200, s.text
    assert s.json()["content_safety_enabled"] is True

    blocked = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "a calm forbidden-word poster"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert blocked.status_code == 400
    assert "内容安全拦截" in blocked.text

    # Restore the global default for the shared integration-test DB.
    client.put(
        "/api/admin/settings",
        json={
            "content_safety_enabled": False,
            "content_safety_banned_terms": "",
            "admin_password": "pass123456",
        },
        headers=h,
    )


def test_content_safety_disabled_by_default(client, make_user, auth):
    make_user("13900000197", balance=1000)
    h = auth("13900000197")
    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "forbidden-word is allowed while safety is off"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text


def test_parse_record_is_atomically_claimed(client, make_user, monkeypatch):
    uid = make_user("13900000199", balance=1000)
    db = SessionLocal()
    try:
        rec = ParseRecord(user_id=uid, url="https://example.com/xhs/1", status="queued")
        db.add(rec)
        db.commit()
        parse_id = rec.id
    finally:
        db.close()

    calls = {"n": 0}

    def fake_parse_url(_url):
        calls["n"] += 1
        return [{"type": "image", "url": "https://cdn.example.com/a.png"}]

    monkeypatch.setattr("app.routers.parse.parse_url", fake_parse_url)
    from app.routers.parse import run_parse_record

    run_parse_record(parse_id)
    run_parse_record(parse_id)

    assert calls["n"] == 1
    db = SessionLocal()
    try:
        rec = db.get(ParseRecord, parse_id)
        assert rec.status == "done"
    finally:
        db.close()
