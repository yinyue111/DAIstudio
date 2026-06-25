"""Recoverable video lifecycle: non-blocking submit + self-polling + crash recovery."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models import GatewayCall, GenAsset, GenTask, User
from app.services import (
    credits,
    gateway,
    generation,
    generation_video_flow,
    retention,
    video_frames,
)
from app.services.config_store import set_setting


def _config_video(client, h, extra=None):
    payload_extra = {"preview_cost": 5}
    payload_extra.update(extra or {})
    r = client.put("/api/admin/models", json={
        "use": "video", "model_id": "mock-video", "cost_credits": 50,
        "unlock_cost": 0, "enabled": True, "extra": payload_extra,
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text


def _stranded_video(uid, ext_id):
    db = SessionLocal()
    try:
        t = GenTask(user_id=uid, category="video", stage="preview", status="running",
                    phase="polling", external_task_id=ext_id,
                    external_submitted_at=datetime.now(timezone.utc),
                    cost_frozen=5, params={"duration": 2})
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        return t.id
    finally:
        db.close()


def test_video_preview_completes_via_poll(client, make_user, auth):
    make_user("13900000061", balance=1000, admin=True)
    h = auth("13900000061")
    _config_video(client, h)

    r = client.post("/api/generate", json={
        "category": "video", "stage": "preview",
        "prompt": {"final_text": "spin"}, "params": {"duration": 2},
    }, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 1
    # the state machine persisted the external submission for DB-side recovery
    db = SessionLocal()
    try:
        gt = db.get(GenTask, tid)
        assert gt.external_task_id is not None
        assert gt.external_submitted_at is not None
    finally:
        db.close()


def test_resume_recovers_stranded_video(client, make_user, auth):
    uid = make_user("13900000062", balance=1000, admin=True)
    h = auth("13900000062")
    _config_video(client, h)
    # simulate a worker crash: submitted, left running, no live poll chain
    tid = _stranded_video(uid, "mock-stranded")

    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) >= 1  # re-attach + (eager) finish
    finally:
        db.close()

    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 1


def test_video_resubmit_skipped_when_external_id_present(client, make_user, auth, monkeypatch):
    # acks_late redelivery: a task that already has an external_task_id must NOT
    # be submitted again (no duplicate external render).
    uid = make_user("13900000066", balance=1000, admin=True)
    h = auth("13900000066")
    _config_video(client, h)
    calls = {"n": 0}

    def counting_submit(*_a, **_k):
        calls["n"] += 1
        return "ext-new"

    monkeypatch.setattr("app.services.gateway.submit_video", counting_submit)
    tid = _stranded_video(uid, "mock-existing")  # already submitted (running + ext id)
    generation.start_video_task(tid)             # simulate a Celery redelivery
    assert calls["n"] == 0                        # resumed polling, never re-submitted
    assert client.get(f"/api/tasks/{tid}", headers=h).json()["status"] == "succeeded"


def test_video_rejects_non_video_content(client, make_user, auth, monkeypatch):
    # gateway "succeeds" with a URL but the bytes aren't a video -> fail + refund
    if not video_frames.FFPROBE:
        pytest.skip("ffprobe unavailable")
    make_user("13900000067", balance=1000, admin=True)
    h = auth("13900000067")
    _config_video(client, h)
    monkeypatch.setattr("app.services.gateway.submit_video", lambda *_a, **_k: "ext-bad")
    monkeypatch.setattr("app.services.gateway.poll_video",
                        lambda *_a, **_k: {"status": "succeeded", "url": "http://example.com/x.mp4"})
    monkeypatch.setattr("app.services.gateway.download_to_storage", lambda *_a, **_k: "video_preview/bad.mp4")

    r = client.post("/api/generate", json={
        "category": "video", "stage": "preview",
        "prompt": {"final_text": "x"}, "params": {"duration": 2},
    }, headers=h)
    assert r.status_code == 200, r.text
    t = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert t["status"] == "failed"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000  # refunded


def test_video_submit_unknown_state_holds_for_review_without_refund(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13900000065", balance=1000, admin=True)
    h = auth("13900000065")
    _config_video(client, h)

    def fail_submit(*_a, **_k):
        raise RuntimeError("submit accepted state unknown")

    monkeypatch.setattr("app.services.gateway.submit_video", fail_submit)
    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "request id"},
        "params": {"duration": 2},
    }, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        task = db.get(GenTask, r.json()["id"])
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert (task.params or {}).get("_video_request_id", "").startswith(f"video-{task.id}-")
        assert (task.params or {}).get("_video_submit_state_unknown") is True
        assert "视频提交状态未知" in (task.error or "")
        user = db.get(User, task.user_id)
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
    finally:
        db.close()


def test_video_unknown_submit_recovers_by_request_id(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13900000995", balance=1000, admin=True)
    h = auth("13900000995")
    _config_video(
        client,
        h,
        extra={
            "request_query_path": "/v1/videos/by-request/{request_id}",
            "request_query_id_field": "id",
            "request_query_status_field": "status",
        },
    )

    def fail_submit(*_a, **_k):
        raise gateway.GatewayError("read timed out", transient=True)

    monkeypatch.setattr("app.services.gateway.submit_video", fail_submit)
    monkeypatch.setattr(
        "app.services.gateway.find_video_by_request_id",
        lambda request_id, *_args, **_kwargs: {
            "external_task_id": f"ext-{request_id}",
            "status": "running",
        },
    )
    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "recover by request id"},
        "params": {"duration": 2},
    }, headers=h)
    assert r.status_code == 200, r.text

    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded", task

    db = SessionLocal()
    try:
        row = db.get(GenTask, r.json()["id"])
        assert row.external_task_id == f"ext-{row.params['_video_request_id']}"
        calls = [
            call
            for call in db.query(GatewayCall).filter(
                GatewayCall.task_id == row.id,
                GatewayCall.kind == "video_submit",
                GatewayCall.status == "ok",
            ).all()
            if (call.detail or {}).get("reconcile") is True
        ]
        assert len(calls) == 1
    finally:
        db.close()


def test_video_unknown_submit_request_id_miss_holds_for_review(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13900000994", balance=1000, admin=True)
    h = auth("13900000994")
    _config_video(
        client,
        h,
        extra={
            "request_query_path": "/v1/videos/by-request/{request_id}",
            "request_query_id_field": "id",
            "request_query_status_field": "status",
        },
    )

    def fail_submit(*_a, **_k):
        raise gateway.GatewayError("read timed out", transient=True)

    monkeypatch.setattr("app.services.gateway.submit_video", fail_submit)
    monkeypatch.setattr("app.services.gateway.find_video_by_request_id", lambda *_args, **_kwargs: None)

    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "recover miss by request id"},
        "params": {"duration": 2},
    }, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        row = db.get(GenTask, r.json()["id"])
        assert row.status == "needs_review"
        assert row.phase == "reconciling"
        assert (row.params or {}).get("_video_submit_state_unknown") is True
        assert "视频提交状态未知" in (row.error or "")
        call = db.query(GatewayCall).filter(
            GatewayCall.task_id == row.id,
            GatewayCall.kind == "video_submit",
            GatewayCall.status == "failed",
        ).order_by(GatewayCall.id.desc()).first()
        assert call is not None
        assert (call.detail or {}).get("reconcile") is True
        assert (call.detail or {}).get("result") == "miss"
    finally:
        db.close()


def test_video_submit_account_pool_error_fails_and_refunds(client, make_user, auth, monkeypatch):
    make_user("13900000973", balance=1000, admin=True)
    h = auth("13900000973")
    _config_video(client, h)

    def fail_submit(*_a, **_k):
        raise gateway.GatewayError(
            "网关调用失败: No available compatible accounts",
            status_code=503,
            transient=True,
            submit_state_unknown=False,
        )

    monkeypatch.setattr("app.services.gateway.submit_video", fail_submit)
    r = client.post("/api/generate", json={
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": "account pool exhausted"},
        "params": {"duration": 2},
    }, headers=h)
    assert r.status_code == 200, r.text

    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "failed"
    assert task["cost_settled"] == 0
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 1000


def test_admin_settle_needs_review_video_with_result_url(
    client,
    make_user,
    auth,
    monkeypatch,
    tiny_mp4,
):
    uid = make_user("13900000975", balance=1000, admin=True)
    h = auth("13900000975")
    _config_video(client, h)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="needs_review",
            phase="submitting",
            cost_frozen=5,
            cost_settled=0,
            params={"duration": 2, "_video_request_id": "video-review-1"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
    finally:
        db.close()

    def fake_download_to_storage(_url, subdir, ext, **_kwargs):
        from app.services import storage
        return storage.save_bytes(tiny_mp4, subdir, ext)

    monkeypatch.setattr("app.services.gateway.download_to_storage", fake_download_to_storage)
    r = client.post(
        f"/api/admin/tasks/{tid}/settle_review",
        json={
            "result_url": "https://cdn.example.com/review-ok.mp4",
            "external_task_id": "ext-review-ok",
            "admin_password": "pass123456",
            "note": "provider finished",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["cost_settled"] == 5
    assert len(body["assets"]) == 1
    assert body["assets"][0]["preview_url"]
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 995

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.external_task_id == "ext-review-ok"
        assert task.params["_video_result_url"] == "https://cdn.example.com/review-ok.mp4"
        assert db.query(GenAsset).filter(GenAsset.task_id == tid).count() == 1
    finally:
        db.close()


def test_admin_settle_download_failure_keeps_task_in_review(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13900000978", balance=1000, admin=True)
    h = auth("13900000978")
    _config_video(client, h)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="needs_review",
            phase="submitting",
            cost_frozen=5,
            cost_settled=0,
            params={"duration": 2, "_video_request_id": "video-review-fail"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
    finally:
        db.close()

    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdn timeout")),
    )
    r = client.post(
        f"/api/admin/tasks/{tid}/settle_review",
        json={
            "result_url": "https://cdn.example.com/review-timeout.mp4",
            "external_task_id": "ext-review-timeout",
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert r.status_code == 400
    assert "补结果结算失败" in r.text

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert task.cost_settled == 0
        assert task.finished_at is None
        assert "cdn timeout" in (task.error or "")
        assert db.query(GenAsset).filter(GenAsset.task_id == tid).count() == 0
    finally:
        db.close()
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 995
    assert me["frozen_credits"] == 5
    tasks = client.get("/api/admin/tasks/review", headers=h).json()
    assert any(task["id"] == tid and task["status"] == "needs_review" for task in tasks)


def test_image_gateway_timeout_fails_and_refunds(client, make_user, auth, monkeypatch):
    make_user("13900000984", balance=1000, admin=True)
    h = auth("13900000984")
    monkeypatch.setattr(
        "app.services.generation._gen_image_with_model_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            gateway.GatewayError("read timed out", transient=True)
        ),
    )

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "slow image"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    body = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert body["status"] == "failed"
    assert "图片生成等待超时" in body["error"]
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 1000
    assert me["frozen_credits"] == 0
    db = SessionLocal()
    try:
        assert db.query(GenAsset).filter(GenAsset.task_id == tid).count() == 0
    finally:
        db.close()


def test_admin_settle_needs_review_image_from_result_url(client, make_user, auth, monkeypatch):
    uid = make_user("13900000985", balance=1000, admin=True)
    h = auth("13900000985")
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="needs_review",
            phase="submitting",
            cost_frozen=5,
            cost_settled=0,
            prompt={"final_text": "manual image"},
            params={"n": 1, "size": "256x256"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
    finally:
        db.close()

    from tests.conftest import _test_png_bytes

    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_args, **_kwargs: _test_png_bytes(),
    )
    r = client.post(
        f"/api/admin/tasks/{tid}/settle_review",
        json={
            "result_url": "https://cdn.example.com/review-ok.png",
            "admin_password": "pass123456",
            "note": "provider finished",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["cost_settled"] == 5
    assert len(body["assets"]) == 1
    assert body["assets"][0]["type"] == "image"
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 995
    assert me["frozen_credits"] == 0


def test_admin_can_list_needs_review_tasks(client, make_user, auth):
    uid = make_user("13900000976", balance=1000, admin=True)
    h = auth("13900000976")
    db = SessionLocal()
    try:
        set_setting(db, "review_task_sla_minutes", 10)
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="needs_review",
            phase="submitting",
            cost_frozen=5,
            cost_settled=0,
            error="上游状态未知",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=15),
            params={"duration": 2, "_video_request_id": "video-review-list"},
        )
        db.add(t)
        db.commit()
        tid = t.id
    finally:
        db.close()

    r = client.get("/api/admin/tasks/review", headers=h)
    assert r.status_code == 200, r.text
    tasks = r.json()
    listed = next(task for task in tasks if task["id"] == tid)
    assert listed["status"] == "needs_review"
    assert listed["age_minutes"] >= 10
    assert listed["review_sla_minutes"] == 10
    assert listed["review_overdue"] is True


def test_preview_task_includes_final_summary_outside_current_page(client, make_user, auth):
    uid = make_user("13900000977", balance=1000, admin=True)
    h = auth("13900000977")
    db = SessionLocal()
    try:
        preview = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="succeeded",
            cost_frozen=5,
            cost_settled=5,
            params={"duration": 2},
        )
        db.add(preview)
        db.flush()
        final = GenTask(
            user_id=uid,
            category="video",
            stage="final",
            parent_task_id=preview.id,
            status="succeeded",
            cost_frozen=50,
            cost_settled=50,
            params={"duration": 2},
        )
        db.add(final)
        db.flush()
        db.add(
            GenAsset(
                task_id=final.id,
                user_id=uid,
                type="video",
                preview_url="http://localhost:8000/media/preview/final.png",
                hd_url="http://localhost:8000/media/video_hd/final.mp4",
                watermarked=True,
                unlocked=False,
            )
        )
        db.commit()
        preview_id = preview.id
        final_id = final.id
    finally:
        db.close()

    task = client.get(f"/api/tasks/{preview_id}", headers=h).json()
    assert task["final_task_id"] == final_id
    assert task["final_status"] == "succeeded"
    assert task["final_asset_count"] == 1


def test_video_download_failure_retries_without_repolling(client, make_user, auth, monkeypatch, tiny_mp4):
    uid = make_user("13900000068", balance=1000, admin=True)
    h = auth("13900000068")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-done")
    calls = {"poll": 0, "download": 0}
    download_kwargs = []

    queued_downloads = []
    monkeypatch.setattr(generation, "_enqueue_video_download", lambda task_id, **_kw: queued_downloads.append(task_id))
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: calls.__setitem__("poll", calls["poll"] + 1)
        or {"status": "succeeded", "url": "https://cdn.example.com/ok.mp4"},
    )

    def flaky_download(url, subdir, ext, **_kwargs):
        calls["download"] += 1
        download_kwargs.append(dict(_kwargs))
        if calls["download"] == 1:
            raise generation.gateway.GatewayError("cdn timeout", transient=True)
        from app.services import storage
        return storage.save_bytes(tiny_mp4, subdir, ext)

    monkeypatch.setattr("app.services.gateway.download_to_storage", flaky_download)

    generation.poll_video_once(tid)
    assert queued_downloads == [tid]
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.params["_video_result_url"] == "https://cdn.example.com/ok.mp4"
    finally:
        db.close()
    assert calls == {"poll": 1, "download": 0}

    generation.run_video_download_task(tid)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.params["_video_download_attempts"] == 1
    finally:
        db.close()

    generation.run_video_download_task(tid)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 1
    assert calls == {"poll": 1, "download": 2}
    assert all(k["max_bytes"] == generation.settings.video_download_max_bytes for k in download_kwargs)
    assert all(k["timeout_seconds"] == generation.settings.video_download_timeout_seconds for k in download_kwargs)
    assert all(k["allowed_content_types"][0] == "video/" for k in download_kwargs)


def test_poll_video_success_only_enqueues_download(client, make_user, auth, monkeypatch):
    uid = make_user("13900000973", balance=1000, admin=True)
    h = auth("13900000973")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-done-queued")
    queued_downloads = []
    monkeypatch.setattr(generation, "_enqueue_video_download", lambda task_id, **_kw: queued_downloads.append(task_id))
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: {"status": "succeeded", "url": "https://cdn.example.com/queued.mp4"},
    )
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("poll should not download")),
    )

    generation.poll_video_once(tid)

    assert queued_downloads == [tid]
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.params["_video_result_url"] == "https://cdn.example.com/queued.mp4"
    finally:
        db.close()


def test_poll_video_success_enqueue_failure_holds_for_review(client, make_user, auth, monkeypatch):
    uid = make_user("13900000977", balance=1000, admin=True)
    h = auth("13900000977")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-done-enqueue-fail")
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: {"status": "succeeded", "url": "https://cdn.example.com/queued.mp4"},
    )
    monkeypatch.setattr(
        generation,
        "_enqueue_video_download",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    generation.poll_video_once(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert task.params["_video_result_url"] == "https://cdn.example.com/queued.mp4"
        assert "视频已由上游生成" in task.error
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_video_download_uses_persisted_provider_usage_for_settlement(
    client,
    make_user,
    auth,
    monkeypatch,
    tiny_mp4,
):
    uid = make_user("13900000976", balance=1000, admin=True)
    h = auth("13900000976")
    _config_video(
        client,
        h,
        extra={
            "official_pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "total_per_1m": 31.0,
                "credit_value_cny": 0.10,
            }
        },
    )
    tid = _stranded_video(uid, "ext-usage")
    monkeypatch.setattr(generation, "_enqueue_video_download", lambda _task_id, **_kw: None)
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: {
            "status": "succeeded",
            "url": "https://cdn.example.com/usage.mp4",
            "usage": {"total_tokens": 10_000},
        },
    )
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda _url, subdir, ext, **_kwargs: generation.storage.save_bytes(tiny_mp4, subdir, ext),
    )

    generation.poll_video_once(tid)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.params["_video_result_usage"] == {"total_tokens": 10_000}
    finally:
        db.close()

    generation.run_video_download_task(tid)

    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert t["cost_settled"] == 4


def test_video_download_final_failure_fails_and_refunds(client, make_user, monkeypatch):
    uid = make_user("13900000088", balance=1000)
    db = SessionLocal()
    try:
        model = generation.get_model_config(db, "video")
        model.model_id = "mock-video"
        model.cost_credits = 50
        model.unlock_cost = 0
        model.enabled = True
        model.extra = {"preview_cost": 5}
        db.commit()
    finally:
        db.close()
    tid = _stranded_video(uid, "ext-last-download-fail")
    monkeypatch.setattr(generation, "_enqueue_video_download", lambda _task_id, **_kw: None)
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: {"status": "succeeded", "url": "https://cdn.example.com/bad.mp4"},
    )
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda *_a, **_k: (_ for _ in ()).throw(gateway.GatewayError("cdn timeout", transient=True)),
    )
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.params = {
            **(task.params or {}),
            "_video_download_attempts": generation._VIDEO_DOWNLOAD_MAX_ATTEMPTS - 1,
        }
        db.commit()
    finally:
        db.close()

    generation.poll_video_once(tid)
    generation.run_video_download_task(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "failed"
        assert "视频结果下载失败" in task.error
        assert user.balance_credits == 1000
        assert user.frozen_credits == 0
        row = db.query(GatewayCall).filter(
            GatewayCall.task_id == tid,
            GatewayCall.kind == "video_download",
            GatewayCall.status == "failed",
        ).order_by(GatewayCall.id.desc()).first()
        assert row is not None
        assert row.detail["permanent"] is True
    finally:
        db.close()


def test_video_download_lock_skips_duplicate_finalizer(client, make_user, auth, monkeypatch):
    uid = make_user("13900000072", balance=1000, admin=True)
    h = auth("13900000072")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-lock")
    monkeypatch.setattr(
        "app.services.generation.locks.acquire",
        lambda key, ttl=None: None if key.startswith("video:download:lock:") else "token",
    )
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate download")),
    )

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        model = generation.get_model_config(db, "video")
        assert generation._finalize_or_retry_video_download(
            db,
            task,
            model,
            {"status": "succeeded", "url": "https://cdn.example.com/ok.mp4"},
        ) is False
    finally:
        db.close()

    assert client.get(f"/api/tasks/{tid}", headers=h).json()["status"] == "running"


def test_download_lock_loser_does_not_refresh_alive_key(client, make_user, auth, monkeypatch):
    uid = make_user("13900000981", balance=1000, admin=True)
    h = auth("13900000981")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-lock-no-heartbeat")
    generation._mark_video_download_alive(tid)
    generation_video_flow.redis_client.delete(f"video:download:alive:{tid}")
    monkeypatch.setattr(
        "app.services.generation.locks.acquire",
        lambda key, ttl=None: None if key.startswith("video:download:lock:") else "token",
    )

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {"duration": 2, "_video_result_url": "https://cdn.example.com/ok.mp4"}
        db.commit()
        model = generation.get_model_config(db, "video")
        assert generation._finalize_or_retry_video_download(
            db,
            task,
            model,
            {"status": "succeeded", "url": "https://cdn.example.com/ok.mp4"},
        ) is False
        assert not generation._video_download_alive(tid)
    finally:
        db.close()


def test_video_retry_drops_internal_download_state(client, make_user, auth):
    uid = make_user("13900000069", balance=1000, admin=True)
    h = auth("13900000069")
    _config_video(client, h)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="failed",
            phase="downloading",
            external_task_id="old-ext",
            external_submitted_at=datetime.now(timezone.utc),
            cost_frozen=5,
            cost_settled=0,
            error="download failed",
            prompt={"final_text": "retry"},
            params={
                "duration": 2,
                "_video_result_url": "https://old.example.com/expired.mp4",
                "_video_download_attempts": 5,
            },
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
    finally:
        db.close()

    r = client.post(f"/api/tasks/{tid}/retry", headers=h)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "succeeded"
        assert "_video_result_url" not in (task.params or {})
        assert (task.params or {}).get("_video_download_attempts") == 1
        assert task.external_task_id != "old-ext"
    finally:
        db.close()


def test_resume_skips_healthy_chain(client, make_user, auth):
    uid = make_user("13900000063", balance=1000, admin=True)
    h = auth("13900000063")
    _config_video(client, h)
    tid = _stranded_video(uid, "mock-healthy")

    # a live chain marks itself alive -> recovery must leave it untouched
    generation._mark_poll_alive(tid)
    db = SessionLocal()
    try:
        generation.resume_stuck_videos(db)
    finally:
        db.close()

    # not finalized by recovery (the live chain owns it)
    db = SessionLocal()
    try:
        assert db.get(GenTask, tid).status == "running"
    finally:
        db.close()


def test_reaper_skips_active_video_download(client, make_user, auth):
    uid = make_user("13900000064", balance=1000, admin=True)
    h = auth("13900000064")
    _config_video(client, h)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="downloading",
            external_task_id="ext-downloading",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(hours=2),
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            cost_frozen=5,
            params={"duration": 2, "_video_result_url": "https://cdn.example.com/r.mp4"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
        generation._mark_video_download_alive(tid)

        assert retention.reap_stuck_tasks(db, max_minutes=60) == 0
        db.refresh(t)
        assert t.status == "running"
    finally:
        db.close()


def test_reaper_skips_pending_video_download_without_alive_key(client, make_user, auth):
    uid = make_user("13900000975", balance=1000, admin=True)
    h = auth("13900000975")
    _config_video(client, h)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="downloading",
            external_task_id="ext-pending-download",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(hours=2),
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            cost_frozen=5,
            params={"duration": 2, "_video_result_url": "https://cdn.example.com/pending.mp4"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()

        assert retention.reap_stuck_tasks(db, max_minutes=60) == 0
        db.refresh(t)
        assert t.status == "running"
        assert t.phase == "downloading"
    finally:
        db.close()


def test_reaper_refunds_video_submitting_without_external_id(client, make_user, auth):
    uid = make_user("13900000974", balance=1000, admin=True)
    h = auth("13900000974")
    _config_video(client, h)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="submitting",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            cost_frozen=5,
            params={"duration": 2, "_video_request_id": "video-local-id"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits

        assert retention.reap_stuck_tasks(db, max_minutes=60) == 1
        failed = db.get(GenTask, tid)
        assert failed.status == "failed"
        assert failed.cost_settled == 0
        assert "视频提交状态未知" in (failed.error or "")
        user = db.get(User, uid)
        assert user.balance_credits == balance_after_freeze + frozen_after_freeze
        assert user.frozen_credits == 0
    finally:
        db.close()


def test_reaper_refunds_submitted_video_timeout(client, make_user, auth, monkeypatch):
    uid = make_user("13900000982", balance=1000, admin=True)
    h = auth("13900000982")
    _config_video(client, h)
    monkeypatch.setattr(retention.settings, "video_poll_max_seconds", 60)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="polling",
            external_task_id="ext-reaper-timeout",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(hours=2),
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            cost_frozen=5,
            cost_settled=0,
            params={"duration": 2},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits

        assert retention.reap_stuck_tasks(db, max_minutes=60) == 1
        failed = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert failed.status == "failed"
        assert "视频渲染超时" in (failed.error or "")
        assert user.balance_credits == balance_after_freeze + frozen_after_freeze
        assert user.frozen_credits == 0
    finally:
        db.close()


def test_video_render_timeout_fails_and_refunds(client, make_user, auth, monkeypatch):
    uid = make_user("13900000976", balance=1000, admin=True)
    h = auth("13900000976")
    _config_video(client, h)
    monkeypatch.setattr(generation, "VIDEO_POLL_MAX_SECONDS", 1)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="polling",
            external_task_id="ext-timeout",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            cost_frozen=5,
            params={"duration": 2},
        )
        db.add(task)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=task.id, commit=False)
        db.commit()
        tid = task.id
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    generation.poll_video_once(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "failed"
        assert task.cost_settled == 0
        assert "视频渲染超时" in (task.error or "")
        user = db.get(User, uid)
        assert user.balance_credits == balance_after_freeze + frozen_after_freeze
        assert user.frozen_credits == 0
    finally:
        db.close()


def test_video_download_model_config_error_holds_for_review(client, make_user, auth, monkeypatch):
    uid = make_user("13900000983", balance=1000, admin=True)
    h = auth("13900000983")
    _config_video(client, h)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="downloading",
            external_task_id="ext-downloaded-config-missing",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            cost_frozen=5,
            cost_settled=0,
            params={"duration": 2, "_video_result_url": "https://cdn.example.com/ok.mp4"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    monkeypatch.setattr(generation, "get_model_config", lambda *_args, **_kwargs: None)
    generation.run_video_download_task(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert "上游生成" in (task.error or "")
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_final_text_video_without_poster_gets_public_preview(
    client,
    make_user,
    auth,
    monkeypatch,
    tiny_mp4,
):
    make_user("13900000065", balance=1000, admin=True)
    h = auth("13900000065")
    _config_video(client, h)
    monkeypatch.setattr(generation.settings, "mock_mode", False)
    monkeypatch.setattr(generation.settings, "video_gateway_base_url", "https://video-gateway.test")
    monkeypatch.setattr(generation.settings, "video_gateway_api_key", "sk-test")
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda _url, subdir, ext, **_k: generation.storage.save_bytes(tiny_mp4, subdir, ext),
    )
    monkeypatch.setattr("app.services.video_frames.extract_poster", lambda *_a, **_k: None)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone == "13900000065").first()
        preview = GenTask(
            user_id=user.id,
            category="video",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "pure text video"},
            params={"duration": 2},
            cost_frozen=5,
            cost_settled=5,
        )
        db.add(preview)
        db.commit()
        db.add(GenAsset(
            task_id=preview.id,
            user_id=user.id,
            type="video",
            preview_url="http://localhost:8000/media/video_preview/placeholder.mp4",
            hd_url=None,
            unlocked=True,
        ))
        final_task = GenTask(
            user_id=user.id,
            category="video",
            stage="final",
            status="running",
            phase="downloading",
            parent_task_id=preview.id,
            prompt={"final_text": "pure text video"},
            params={"duration": 2},
            cost_frozen=50,
        )
        db.add(final_task)
        db.flush()
        credits.freeze(db, user.id, 50, biz_ref=final_task.id, commit=False)
        db.commit()
        model = generation.get_model_config(db, "video")
        generation._finalize_video_success(
            db,
            final_task,
            model,
            {"status": "succeeded", "url": "https://cdn.example.com/final.mp4"},
        )
        tid = final_task.id
    finally:
        db.close()

    task = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert task["status"] == "succeeded", task
    asset = task["assets"][0]
    assert asset["preview_url"].startswith("http://localhost:8000/media/preview/")
    assert asset["hd_url"] is None
    assert asset["unlocked"] is False
    assert client.get(asset["preview_url"].replace("http://localhost:8000", "")).status_code == 200
    assert client.get(f"/api/assets/{asset['id']}/download", headers=h).status_code == 402
    db = SessionLocal()
    try:
        stored = db.get(GenAsset, asset["id"])
        assert "/media/video_hd/" in stored.hd_url
    finally:
        db.close()
