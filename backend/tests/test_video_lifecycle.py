"""Recoverable video lifecycle: non-blocking submit + self-polling + crash recovery."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import event

from app import tasks as app_tasks
from app.db import SessionLocal, engine
from app.models import (
    AppSetting,
    CreditTransaction,
    GatewayCall,
    GenAsset,
    GenTask,
    UploadedAsset,
    User,
)
from app.services import (
    credits,
    gateway,
    generation,
    generation_common,
    generation_video_download,
    generation_video_flow,
    generation_video_submit,
    retention,
    storage,
    video_frames,
)
from app.services.config_store import get_setting, set_setting


@pytest.fixture(autouse=True)
def cleanup_live_generation_tasks():
    yield
    db = SessionLocal()
    try:
        db.query(GenTask).filter(GenTask.status.in_(("queued", "running"))).update(
            {
                "status": "failed",
                "phase": None,
                "error": "test cleanup",
                "finished_at": datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


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
    asset = t["assets"][0]
    assert "/media/video_preview/" in asset["preview_url"]
    assert asset["preview_url"].endswith(".mp4")
    assert asset["width"] == 640
    assert asset["height"] == 360
    downloaded = client.get(f"/api/assets/{asset['id']}/download", headers=h)
    assert downloaded.status_code == 200
    assert downloaded.content[4:8] == b"ftyp"
    # the state machine persisted the external submission for DB-side recovery
    db = SessionLocal()
    try:
        gt = db.get(GenTask, tid)
        assert gt.status == "succeeded"
        assert gt.phase is None
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
    # Gateway "succeeds" with a URL but the bytes aren't a video. The upstream
    # task may have consumed provider resources, so hold for review instead of
    # refunding automatically.
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
    assert t["status"] == "needs_review"
    assert "上游生成" in t["error"]
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] + me["frozen_credits"] == 1000
    assert me["frozen_credits"] > 0


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
        assert user.balance_credits == 985
        assert user.frozen_credits == 15
    finally:
        db.close()


def test_video_submit_cancel_during_provider_call_preserves_cancel_and_refunds(
    client,
    make_user,
    auth,
):
    uid = make_user("13900000987", balance=1000, admin=True)
    h = auth("13900000987")
    _config_video(client, h)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="queued",
            prompt={"final_text": "cancel while provider is submitting"},
            params={"duration": 2},
            cost_frozen=5,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        credits.freeze(db, uid, 5, biz_ref=task_id, commit=False)
        db.commit()
    finally:
        db.close()

    queued_polls = []
    queued_downloads = []

    def cancel_then_return(*_args, **_kwargs):
        cancel_db = SessionLocal()
        try:
            current = cancel_db.get(GenTask, task_id)
            current.params = {**(current.params or {}), "_cancel_requested": True}
            cancel_db.commit()
        finally:
            cancel_db.close()
        return "ext-canceled-in-flight"

    generation_video_submit.start_video_task(
        task_id,
        submit_video_fn=cancel_then_return,
        try_enqueue_poll_fn=lambda *args, **kwargs: queued_polls.append((args, kwargs)),
        try_enqueue_video_download_fn=lambda *args, **kwargs: queued_downloads.append((args, kwargs)),
    )

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, task_id)
        user = verify_db.get(User, uid)
        assert task.status == "canceled"
        assert task.external_task_id == "ext-canceled-in-flight"
        assert (task.params or {}).get("_cancel_requested") is True
        assert task.cost_settled == 0
        assert user.balance_credits == 1000
        assert user.frozen_credits == 0
        assert verify_db.query(GenAsset).filter(GenAsset.task_id == task_id).count() == 0
        assert verify_db.query(CreditTransaction).filter(
            CreditTransaction.biz_type == "gen_task",
            CreditTransaction.biz_ref == task_id,
            CreditTransaction.type == "settle",
        ).count() == 0
        assert verify_db.query(CreditTransaction).filter(
            CreditTransaction.biz_type == "gen_task",
            CreditTransaction.biz_ref == task_id,
            CreditTransaction.type == "refund",
        ).count() == 1
    finally:
        verify_db.close()
    assert queued_polls == []
    assert queued_downloads == []


def test_unknown_video_submit_recovery_honors_fresh_cancel_request(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13900000988", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="submitting",
            prompt={"final_text": "unknown submit cancel"},
            params={"duration": 2, "_video_request_id": "video-unknown-cancel"},
            cost_frozen=5,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        credits.freeze(db, uid, 5, biz_ref=task_id, commit=False)
        db.commit()

        def cancel_then_find(*_args, **_kwargs):
            cancel_db = SessionLocal()
            try:
                current = cancel_db.get(GenTask, task_id)
                current.params = {**(current.params or {}), "_cancel_requested": True}
                cancel_db.commit()
            finally:
                cancel_db.close()
            return {
                "external_task_id": "ext-unknown-canceled",
                "status": "processing",
            }

        monkeypatch.setattr(
            generation_video_submit,
            "find_video_by_request_id_with_model_config",
            cancel_then_find,
        )
        recovered = generation_video_submit.recover_unknown_submit_by_request_id(
            db,
            task,
            object(),
            dict(task.params or {}),
            {"request_id": "video-unknown-cancel", "duration": 2},
        )
        assert recovered is False
    finally:
        db.close()

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, task_id)
        user = verify_db.get(User, uid)
        assert task.status == "canceled"
        assert task.external_task_id == "ext-unknown-canceled"
        assert (task.params or {}).get("_cancel_requested") is True
        assert user.balance_credits == 1000
        assert user.frozen_credits == 0
    finally:
        verify_db.close()


def test_video_submit_cancel_refund_failure_keeps_external_recovery_anchor(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13900000989", balance=1000, admin=True)
    h = auth("13900000989")
    _config_video(client, h)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="queued",
            prompt={"final_text": "cancel refund failure"},
            params={"duration": 2},
            cost_frozen=5,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        credits.freeze(db, uid, 5, biz_ref=task_id, commit=False)
        db.commit()
    finally:
        db.close()

    def cancel_then_return(*_args, **_kwargs):
        cancel_db = SessionLocal()
        try:
            current = cancel_db.get(GenTask, task_id)
            current.params = {**(current.params or {}), "_cancel_requested": True}
            cancel_db.commit()
        finally:
            cancel_db.close()
        return "ext-cancel-refund-failed"

    monkeypatch.setattr(
        generation_video_submit.credits,
        "refund",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("refund unavailable")),
    )
    queued_polls = []
    generation_video_submit.start_video_task(
        task_id,
        submit_video_fn=cancel_then_return,
        try_enqueue_poll_fn=lambda *args, **kwargs: queued_polls.append((args, kwargs)),
    )

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, task_id)
        user = verify_db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert task.external_task_id == "ext-cancel-refund-failed"
        assert (task.params or {}).get("_cancel_requested") is True
        assert "取消退款失败" in (task.error or "")
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
        assert verify_db.query(CreditTransaction).filter(
            CreditTransaction.biz_type == "gen_task",
            CreditTransaction.biz_ref == task_id,
            CreditTransaction.type.in_(("refund", "settle")),
        ).count() == 0
    finally:
        verify_db.close()
    assert queued_polls == []


@pytest.mark.parametrize(
    ("provider_status", "result_url"),
    [
        (None, None),
        ("succeeded", "https://provider.example/result.mp4"),
    ],
)
def test_video_submit_cancel_crash_after_external_commit_stays_non_executable(
    client,
    make_user,
    monkeypatch,
    provider_status,
    result_url,
):
    uid = make_user(
        "13900000992" if provider_status is None else "13900000993",
        balance=1000,
    )
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="submitting",
            prompt={"final_text": "crash after external anchor"},
            params={
                "duration": 2,
                "_video_request_id": "video-crash-anchor",
                "_cancel_requested": True,
            },
            cost_frozen=5,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        credits.freeze(db, uid, 5, biz_ref=task_id, commit=False)
        db.commit()

        real_refund = generation_video_submit.credits.refund
        monkeypatch.setattr(
            generation_video_submit.credits,
            "refund",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            generation_video_submit.persist_video_submit_result(
                db,
                task_id,
                request_id="video-crash-anchor",
                external_task_id="ext-crash-anchor",
                submitted_params={"duration": 2, "request_id": "video-crash-anchor"},
                provider_status=provider_status,
                result_url=result_url,
            )
    finally:
        db.close()

    monkeypatch.setattr(generation_video_submit.credits, "refund", real_refund)

    queued_polls = []
    queued_downloads = []
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda *args: queued_polls.append(args))
    monkeypatch.setattr(
        generation,
        "_enqueue_video_download",
        lambda *args, **kwargs: queued_downloads.append((args, kwargs)),
    )
    recovery_db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(recovery_db) == 1
        assert generation.resume_stuck_videos(recovery_db) == 0
    finally:
        recovery_db.close()
    assert queued_polls == []
    assert queued_downloads == []

    generation_video_submit.poll_video_once(
        task_id,
        poll_video_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconciling cancel must not poll provider")
        ),
    )
    generation_video_download.run_video_download_task(
        task_id,
        expected_external_task_id="ext-crash-anchor",
        get_model_config_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconciling cancel must not download")
        ),
    )

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, task_id)
        user = verify_db.get(User, uid)
        assert task.status == "canceled"
        assert task.phase == "reconciling"
        assert task.external_task_id == "ext-crash-anchor"
        assert (task.params or {}).get("_cancel_requested") is True
        if result_url:
            assert (task.params or {}).get("_video_result_url") == result_url
        assert user.balance_credits == 1000
        assert user.frozen_credits == 0
        assert verify_db.query(GenAsset).filter(GenAsset.task_id == task_id).count() == 0
        assert verify_db.query(CreditTransaction).filter(
            CreditTransaction.biz_type == "gen_task",
            CreditTransaction.biz_ref == task_id,
            CreditTransaction.type == "refund",
        ).count() == 1
        assert verify_db.query(CreditTransaction).filter(
            CreditTransaction.biz_type == "gen_task",
            CreditTransaction.biz_ref == task_id,
            CreditTransaction.type == "settle",
        ).count() == 0
    finally:
        verify_db.close()


def test_video_cancel_recovery_refund_failure_enters_admin_review(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13900000994", balance=1000, admin=True)
    admin_headers = auth("13900000994")
    uid = make_user("13900000995", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="reconciling",
            external_task_id="ext-cancel-recovery-fail",
            params={"_cancel_requested": True, "_video_request_id": "cancel-recovery-fail"},
            cost_frozen=5,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        credits.freeze(db, uid, 5, biz_ref=task_id, commit=False)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        generation.credits,
        "refund",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("refund unavailable")),
    )
    recovery_db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(recovery_db) == 1
        assert generation.resume_stuck_videos(recovery_db) == 0
    finally:
        recovery_db.close()

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, task_id)
        user = verify_db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert task.external_task_id == "ext-cancel-recovery-fail"
        assert (task.params or {}).get("_cancel_requested") is True
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
    finally:
        verify_db.close()

    listed = client.get("/api/admin/tasks/review", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == task_id for item in listed.json())


def test_video_resume_ignores_reconciling_task_without_cancel(client, make_user):
    uid = make_user("13900000996", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="reconciling",
            external_task_id="ext-admin-reconciling",
            params={"_video_request_id": "admin-reconciling"},
        )
        db.add(task)
        db.commit()
        task_id = task.id
        assert generation.resume_stuck_videos(db) == 0
        db.refresh(task)
        assert task.id == task_id
        assert task.status == "running"
        assert task.phase == "reconciling"
    finally:
        db.close()


@pytest.mark.parametrize("lookup_outcome", ["miss", "error"])
def test_stale_unknown_submit_lookup_cannot_hold_replacement_for_review(
    client,
    make_user,
    auth,
    monkeypatch,
    lookup_outcome,
):
    uid = make_user(f"1390000099{0 if lookup_outcome == 'miss' else 1}", balance=1000, admin=True)
    h = auth(f"1390000099{0 if lookup_outcome == 'miss' else 1}")
    _config_video(client, h)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="queued",
            prompt={"final_text": "stale unknown lookup"},
            params={"duration": 2},
            cost_frozen=5,
        )
        db.add(task)
        db.flush()
        task_id = task.id
        credits.freeze(db, uid, 5, biz_ref=task_id, commit=False)
        db.commit()
    finally:
        db.close()

    def replace_then_lookup(*_args, **_kwargs):
        replace_db = SessionLocal()
        try:
            current = replace_db.get(GenTask, task_id)
            current.params = {**(current.params or {}), "replacement": True}
            current.external_task_id = "ext-replacement"
            current.phase = "polling"
            replace_db.commit()
        finally:
            replace_db.close()
        if lookup_outcome == "error":
            raise RuntimeError("lookup unavailable")
        return None

    monkeypatch.setattr(
        generation_video_submit,
        "find_video_by_request_id_with_model_config",
        replace_then_lookup,
    )
    generation_video_submit.start_video_task(
        task_id,
        submit_video_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("submit accepted state unknown")
        ),
        try_enqueue_poll_fn=lambda *_args, **_kwargs: None,
    )

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, task_id)
        assert task.status == "running"
        assert task.phase == "polling"
        assert task.external_task_id == "ext-replacement"
        assert (task.params or {}).get("replacement") is True
        assert not (task.params or {}).get("_video_submit_state_unknown")
    finally:
        verify_db.close()


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


def test_video_submit_persists_external_id_before_usage_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13900000992", balance=1000, admin=True)
    h = auth("13900000992")
    _config_video(client, h)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="queued",
            cost_frozen=5,
            prompt={"final_text": "persist recovery point first"},
            params={"duration": 2},
        )
        db.add(task)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=task.id, commit=False)
        db.commit()
        tid = task.id
    finally:
        db.close()

    def fail_usage(*_args, **_kwargs):
        raise RuntimeError("usage side effect failed")

    queued_polls = []
    monkeypatch.setattr(generation_video_submit.usage, "record_call", fail_usage)

    generation_video_submit.start_video_task(
        tid,
        submit_video_fn=lambda *_args, **_kwargs: "ext-committed-before-usage",
        try_enqueue_poll_fn=lambda task_id, *_args: queued_polls.append(task_id),
    )

    db = SessionLocal()
    try:
        row = db.get(GenTask, tid)
        assert row.status == "running"
        assert row.phase == "polling"
        assert row.external_task_id == "ext-committed-before-usage"
        assert row.external_submitted_at is not None
        assert queued_polls == [tid]
    finally:
        db.close()


def test_stale_generate_delivery_skips_reconciling_task(client, make_user):
    uid = make_user("13790000101", balance=1000, admin=True)
    tid = _stranded_video(uid, "stale-generate-reconciling")
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "reconciling"
        db.commit()
    finally:
        db.close()

    queued_polls = []
    queued_downloads = []
    generation_video_submit.start_video_task(
        tid,
        submit_video_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale generate delivery must not submit again")
        ),
        try_enqueue_poll_fn=lambda task_id, *_args: queued_polls.append(task_id),
        try_enqueue_video_download_fn=lambda _db, task_id, **_kwargs: queued_downloads.append(task_id),
    )

    assert queued_polls == []
    assert queued_downloads == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "reconciling"
    finally:
        db.close()


def test_stale_generate_delivery_resumes_persisted_download(client, make_user):
    uid = make_user("13790000102", balance=1000, admin=True)
    tid = _stranded_video(uid, "stale-generate-downloading")
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            **(task.params or {}),
            "_video_result_url": "https://cdn.example.com/stale-generate.mp4",
        }
        db.commit()
    finally:
        db.close()

    queued_polls = []
    queued_downloads = []
    generation_video_submit.start_video_task(
        tid,
        submit_video_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale generate delivery must not submit again")
        ),
        try_enqueue_poll_fn=lambda task_id, *_args: queued_polls.append(task_id),
        try_enqueue_video_download_fn=lambda _db, task_id, **_kwargs: queued_downloads.append(task_id),
    )

    assert queued_polls == []
    assert queued_downloads == [tid]
    db = SessionLocal()
    try:
        assert db.get(GenTask, tid).phase == "downloading"
    finally:
        db.close()


@pytest.mark.parametrize("phase", ["downloading", "submitting", "rendering"])
def test_stale_generate_delivery_skips_non_executable_external_phase(
    client,
    make_user,
    phase,
):
    phone = {
        "downloading": "13790000110",
        "submitting": "13790000113",
        "rendering": "13790000114",
    }[phase]
    uid = make_user(phone, balance=1000, admin=True)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase=phase,
            external_task_id=f"stale-generate-{phase}",
            external_submitted_at=datetime.now(timezone.utc),
            cost_frozen=0,
            params={"duration": 2},
        )
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()

    queued_polls = []
    queued_downloads = []
    generation_video_submit.start_video_task(
        tid,
        submit_video_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale generate delivery must not submit again")
        ),
        try_enqueue_poll_fn=lambda task_id, *_args: queued_polls.append(task_id),
        try_enqueue_video_download_fn=lambda _db, task_id, **_kwargs: queued_downloads.append(task_id),
    )

    assert queued_polls == []
    assert queued_downloads == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == phase
        assert task.params == {"duration": 2}
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


def test_admin_settle_needs_review_video_from_local_result(
    client,
    make_user,
    auth,
    tiny_mp4,
):
    uid = make_user("13900000979", balance=1000, admin=True)
    h = auth("13900000979")
    _config_video(client, h)
    db = SessionLocal()
    try:
        video_key = storage.save_bytes(tiny_mp4, "video_preview", "mp4")
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="needs_review",
            phase="reconciling",
            cost_frozen=5,
            cost_settled=0,
            params={
                "duration": 2,
                "_video_request_id": "video-review-local",
                "_video_result_keys": [video_key],
            },
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
    finally:
        db.close()

    listed = client.get("/api/admin/tasks/review", headers=h)
    assert listed.status_code == 200, listed.text
    review_task = next(task for task in listed.json() if task["id"] == tid)
    assert review_task["has_local_results"] is True

    r = client.post(
        f"/api/admin/tasks/{tid}/settle_review",
        json={"note": "use local persisted video"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["cost_settled"] == 5
    assert len(body["assets"]) == 1
    assert body["assets"][0]["type"] == "video"
    assert body["assets"][0]["preview_url"].endswith(f"/media/{video_key}")
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 995

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "succeeded"
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


def test_admin_settle_needs_review_image_counts_existing_assets(client, make_user, auth):
    uid = make_user("13900000987", balance=1000, admin=True)
    h = auth("13900000987")
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="needs_review",
            phase="reconciling",
            cost_frozen=30,
            cost_settled=0,
            prompt={"final_text": "manual image batch"},
            params={"n": 2, "size": "256x256", "_model_snapshot": {"cost_credits": 15, "extra": {}}},
        )
        db.add(t)
        db.flush()
        first_key = storage.save_bytes(b"\x89PNG\r\n\x1a\nfirst", "hd", "png")
        second_key = storage.save_bytes(b"\x89PNG\r\n\x1a\nsecond", "hd", "png")
        db.add_all([
            GenAsset(
                task_id=t.id,
                user_id=uid,
                type="image",
                preview_url=storage.public_url(first_key),
                hd_url=storage.public_url(first_key),
                unlocked=False,
            ),
            GenAsset(
                task_id=t.id,
                user_id=uid,
                type="image",
                preview_url=storage.public_url(second_key),
                hd_url=storage.public_url(second_key),
                unlocked=False,
            ),
        ])
        credits.freeze(db, uid, 30, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
    finally:
        db.close()

    r = client.post(
        f"/api/admin/tasks/{tid}/settle_review",
        json={"note": "provider finished"},
        headers=h,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["cost_settled"] == 30
    assert len(body["assets"]) == 2
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 970
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


def test_admin_review_list_batches_asset_queries(client, make_user, auth):
    uid = make_user("13900000975", balance=1000, admin=True)
    h = auth("13900000975")
    db = SessionLocal()
    try:
        db.add_all([
            GenTask(
                user_id=uid,
                category="image",
                stage="preview",
                status="needs_review",
                phase="reconciling",
                cost_frozen=5,
                cost_settled=0,
                error="待人工复核",
                params={"n": 1},
            )
            for _ in range(3)
        ])
        db.commit()
    finally:
        db.close()

    asset_selects = 0

    def _count_asset_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal asset_selects
        if "FROM gen_assets" in statement and statement.lstrip().upper().startswith("SELECT"):
            asset_selects += 1

    event.listen(engine, "before_cursor_execute", _count_asset_selects)
    try:
        response = client.get("/api/admin/tasks/review?limit=3", headers=h)
    finally:
        event.remove(engine, "before_cursor_execute", _count_asset_selects)

    assert response.status_code == 200, response.text
    assert len(response.json()) == 3
    assert asset_selects == 1


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


def test_final_generation_drops_preview_video_runtime_params(client, make_user, auth, monkeypatch):
    uid = make_user("13900000978", balance=1000, admin=True)
    h = auth("13900000978")
    _config_video(client, h)
    monkeypatch.setattr("app.services.generation_submit._enqueue_generation_task", lambda *_a, **_k: None)
    db = SessionLocal()
    try:
        preview = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="succeeded",
            cost_frozen=5,
            cost_settled=5,
            prompt={"final_text": "product spin"},
            params={
                "duration": 5,
                "resolution": "720p",
                "target_resolution": "1080p",
                "_video_request_id": "preview-request",
                "request_id": "preview-request",
                "_video_result_url": "https://cdn.example.com/preview.mp4",
                "_video_result_usage": {"total_tokens": 1},
                "_video_result_mock": True,
                "_video_download_started_at": "2026-07-05T00:00:00+00:00",
                "_video_download_attempts": 4,
                "_video_result_keys": ["video_preview/old.mp4"],
                "_video_poll_state_unknown": True,
                "_video_download_state_unknown": True,
                "_source_trace": {"mode": "reverse"},
            },
        )
        db.add(preview)
        db.flush()
        db.add(
            GenAsset(
                task_id=preview.id,
                user_id=uid,
                type="video",
                preview_url="http://localhost:8000/media/video_preview/preview.mp4",
                unlocked=True,
            )
        )
        db.commit()
        preview_id = preview.id
    finally:
        db.close()

    r = client.post(
        "/api/generate",
        json={"category": "video", "stage": "final", "parent_task_id": preview_id, "params": {}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        final = db.get(GenTask, r.json()["id"])
        params = final.params or {}
        assert params["duration"] == 5
        assert params["resolution"] == "720p"
        assert params["target_resolution"] == "1080p"
        assert params["_source_trace"] == {"mode": "reverse"}
        assert "request_id" not in params
        assert not any(key.startswith("_video_") for key in params)
    finally:
        db.close()


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

    generation.run_video_download_task(tid, "ext-done")
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.params["_video_download_attempts"] == 1
    finally:
        db.close()

    generation.run_video_download_task(tid, "ext-done")
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 1
    assert calls == {"poll": 1, "download": 2}
    assert all(k["max_bytes"] == generation.settings.video_download_max_bytes for k in download_kwargs)
    assert all(k["timeout_seconds"] == generation.settings.video_download_timeout_seconds for k in download_kwargs)
    assert all(k["allowed_content_types"][0] == "video/" for k in download_kwargs)
    assert all(callable(k["progress_callback"]) for k in download_kwargs)


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


def test_poll_video_success_does_not_revive_concurrent_needs_review_transition(
    client,
    make_user,
    auth,
):
    uid = make_user("13790000115", balance=1000, admin=True)
    h = auth("13790000115")
    _config_video(client, h)
    tid = _stranded_video(uid, "concurrent-needs-review")
    queued_downloads = []

    def _poll_after_review_transition(*_args, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.status = "needs_review"
            task.phase = "reconciling"
            task.error = "manual review won race"
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        return {"status": "succeeded", "url": "https://cdn.example.com/raced.mp4"}

    generation_video_submit.poll_video_once(
        tid,
        poll_video_fn=_poll_after_review_transition,
        try_enqueue_video_download_fn=lambda _db, task_id, **_kwargs: queued_downloads.append(task_id),
    )

    assert queued_downloads == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert task.error == "manual review won race"
        assert "_video_result_url" not in (task.params or {})
    finally:
        db.close()


def test_poll_video_failed_does_not_override_concurrent_download_winner(
    client,
    make_user,
    auth,
):
    uid = make_user("13790000116", balance=1000, admin=True)
    h = auth("13790000116")
    _config_video(client, h)
    tid = _stranded_video(uid, "failed-after-download-winner")
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    def _poll_after_download_winner(*_args, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.phase = "downloading"
            task.params = {
                **(task.params or {}),
                "_video_result_url": "https://cdn.example.com/winner.mp4",
            }
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        return {"status": "failed", "error": "stale provider failure"}

    generation_video_submit.poll_video_once(
        tid,
        poll_video_fn=_poll_after_download_winner,
        try_enqueue_video_download_fn=lambda _db, _task_id, **_kwargs: None,
    )

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.params["_video_result_url"] == "https://cdn.example.com/winner.mp4"
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


@pytest.mark.parametrize(
    ("outcome", "phone"),
    [
        ("error", "13790000117"),
        ("soft_timeout", "13790000118"),
        ("running", "13790000119"),
    ],
)
def test_poll_video_does_not_take_over_replaced_external_task(
    client,
    make_user,
    auth,
    outcome,
    phone,
):
    uid = make_user(phone, balance=1000, admin=True)
    h = auth(phone)
    _config_video(client, h)
    original_external_task_id = f"replaced-before-{outcome}"
    replacement_external_task_id = f"replacement-{outcome}"
    tid = _stranded_video(uid, original_external_task_id)
    queued_polls = []

    def _poll_after_external_replacement(*_args, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        if outcome == "error":
            raise RuntimeError("stale poll network error")
        if outcome == "soft_timeout":
            raise SoftTimeLimitExceeded()
        return {"status": "running"}

    generation_video_submit.poll_video_once(
        tid,
        poll_video_fn=_poll_after_external_replacement,
        try_enqueue_poll_fn=lambda task_id, *_args: queued_polls.append(task_id),
    )

    assert queued_polls == []
    assert generation_video_flow.poll_chain_alive(tid, original_external_task_id)
    assert not generation_video_flow.poll_chain_alive(tid, replacement_external_task_id)
    assert generation_video_flow.bump_poll_errors(tid, original_external_task_id) == 1
    assert generation_video_flow.bump_poll_errors(tid, replacement_external_task_id) == 1
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "polling"
        assert task.external_task_id == replacement_external_task_id
        assert task.error is None
        assert "_video_poll_state_unknown" not in (task.params or {})
    finally:
        db.close()


def test_stale_poll_heartbeat_after_replacement_does_not_mask_current_generation(client):
    task_id = 9_100_001
    old_external_task_id = "stale-poll-generation"
    current_external_task_id = "current-poll-generation"

    generation_video_flow.mark_poll_alive(task_id, current_external_task_id)
    generation_video_flow.mark_poll_alive(task_id, old_external_task_id)

    assert generation_video_flow.poll_chain_alive(task_id, current_external_task_id)
    generation_video_flow.clear_poll_alive(task_id, old_external_task_id)
    assert generation_video_flow.poll_chain_alive(task_id, current_external_task_id)


def test_stale_download_heartbeat_after_replacement_does_not_mask_current_generation(client):
    task_id = 9_100_002
    old_external_task_id = "stale-download-generation"
    current_external_task_id = "current-download-generation"

    generation_video_flow.mark_video_download_alive(task_id, current_external_task_id)
    generation_video_flow.mark_video_download_alive(task_id, old_external_task_id)

    assert generation_video_flow.video_download_alive(task_id, current_external_task_id)
    generation_video_flow.clear_video_download_alive(task_id, old_external_task_id)
    assert generation_video_flow.video_download_alive(task_id, current_external_task_id)


def test_stale_video_download_timeout_does_not_reconcile_replacement(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13790000129", balance=1000, admin=True)
    h = auth("13790000129")
    _config_video(client, h)
    old_external_task_id = "stale-download-timeout-old"
    replacement_external_task_id = "stale-download-timeout-replacement"
    replacement_params = {
        "duration": 4,
        "_video_result_url": "https://cdn.example.com/replacement-timeout.mp4",
        "replacement_generation": True,
    }
    tid = _stranded_video(uid, old_external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            "duration": 2,
            "_video_result_url": "https://cdn.example.com/stale-timeout.mp4",
        }
        db.commit()
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    def _replace_generation_then_timeout(*_args, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            task.phase = "downloading"
            task.params = replacement_params
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        _replace_generation_then_timeout,
    )

    generation.run_video_download_task(tid, old_external_task_id)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.params == replacement_params
        assert task.error is None
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_stale_video_download_success_does_not_finalize_replacement(
    client,
    make_user,
    auth,
    monkeypatch,
    tiny_mp4,
):
    uid = make_user("13790000130", balance=1000, admin=True)
    h = auth("13790000130")
    _config_video(client, h)
    old_external_task_id = "stale-download-success-old"
    replacement_external_task_id = "stale-download-success-replacement"
    replacement_params = {
        "duration": 4,
        "_video_result_url": "https://cdn.example.com/replacement-success.mp4",
        "replacement_generation": True,
    }
    tid = _stranded_video(uid, old_external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            "duration": 2,
            "_video_result_url": "https://cdn.example.com/stale-success.mp4",
        }
        db.commit()
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    written_keys = []

    def _save_then_replace_generation(_url, subdir, ext, **_kwargs):
        key = storage.save_bytes(tiny_mp4, subdir, ext)
        written_keys.append(key)
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            task.phase = "downloading"
            task.params = replacement_params
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        return key

    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        _save_then_replace_generation,
    )

    generation.run_video_download_task(tid, old_external_task_id)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.params == replacement_params
        assert task.error is None
        assert db.query(GenAsset).filter(GenAsset.task_id == tid).count() == 0
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
        credit_types = [
            row.type
            for row in db.query(CreditTransaction)
            .filter(CreditTransaction.biz_ref == tid)
            .order_by(CreditTransaction.id)
        ]
        assert credit_types == ["freeze"]
    finally:
        db.close()
    assert written_keys
    assert all(not storage.local_path(key).exists() for key in written_keys)


def test_stale_queued_video_download_delivery_does_not_run_replacement(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13790000131", balance=1000, admin=True)
    h = auth("13790000131")
    _config_video(client, h)
    old_external_task_id = "stale-queued-download-old"
    replacement_external_task_id = "stale-queued-download-replacement"
    replacement_params = {
        "duration": 4,
        "_video_result_url": "https://cdn.example.com/replacement-queued.mp4",
        "replacement_generation": True,
    }
    tid = _stranded_video(uid, old_external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            "duration": 2,
            "_video_result_url": "https://cdn.example.com/stale-queued.mp4",
        }
        db.commit()
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    queued = {}
    monkeypatch.setattr(generation_video_flow, "local_worker_shutting_down", lambda: False)
    monkeypatch.setattr(
        app_tasks.download_video_task,
        "apply_async",
        lambda *, args, kwargs, **options: queued.update(
            {"args": args, "kwargs": kwargs, "options": options}
        ),
    )
    generation_video_flow.enqueue_video_download(
        tid,
        external_task_id=old_external_task_id,
    )
    assert queued["args"] == (tid, old_external_task_id)
    assert queued["kwargs"] == {}

    concurrent_db = SessionLocal()
    try:
        task = concurrent_db.get(GenTask, tid)
        task.external_task_id = replacement_external_task_id
        task.params = replacement_params
        concurrent_db.commit()
    finally:
        concurrent_db.close()

    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale queued delivery must not download replacement")
        ),
    )
    model_loads = []
    monkeypatch.setattr(
        generation,
        "get_model_config",
        lambda *_args, **_kwargs: model_loads.append(True),
    )
    app_tasks.download_video_task.run(*queued["args"], **queued["kwargs"])
    assert model_loads == []

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.params == replacement_params
        assert task.error is None
        assert db.query(GenAsset).filter(GenAsset.task_id == tid).count() == 0
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_video_download_pre_capture_error_does_not_hold_task(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13790000132", balance=1000, admin=True)
    h = auth("13790000132")
    _config_video(client, h)
    external_task_id = "download-pre-capture-error"
    tid = _stranded_video(uid, external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            "duration": 2,
            "_video_result_url": "https://cdn.example.com/pre-capture.mp4",
        }
        db.commit()
        original_params = dict(task.params)
    finally:
        db.close()

    hold_calls = []

    class BrokenSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("database read failed before identity capture")

        def close(self):
            return None

    monkeypatch.setattr(generation_video_download, "SessionLocal", BrokenSession)
    monkeypatch.setattr(
        generation_video_download,
        "hold_video_download_for_reconciliation",
        lambda *_args, **_kwargs: hold_calls.append((_args, _kwargs)),
    )

    generation_video_download.run_video_download_task(
        tid,
        expected_external_task_id=external_task_id,
    )

    assert hold_calls == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == external_task_id
        assert task.params == original_params
        assert task.error is None
    finally:
        db.close()


@pytest.mark.parametrize("attempts", [0, generation._VIDEO_DOWNLOAD_MAX_ATTEMPTS - 1])
def test_stale_video_download_generic_error_does_not_mutate_replacement(
    client,
    make_user,
    auth,
    monkeypatch,
    attempts,
):
    phone = "13790000133" if attempts == 0 else "13790000134"
    uid = make_user(phone, balance=1000, admin=True)
    h = auth(phone)
    _config_video(client, h)
    old_external_task_id = f"stale-generic-error-old-{attempts}"
    replacement_external_task_id = f"stale-generic-error-replacement-{attempts}"
    replacement_params = {
        "duration": 4,
        "_video_result_url": f"https://cdn.example.com/replacement-error-{attempts}.mp4",
        "replacement_generation": True,
    }
    tid = _stranded_video(uid, old_external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            "duration": 2,
            "_video_result_url": "https://cdn.example.com/stale-error.mp4",
            "_video_download_attempts": attempts,
        }
        db.commit()
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    def _replace_generation_then_error(*_args, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            task.params = replacement_params
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        raise RuntimeError("generic stale download failure")

    queued = []
    monkeypatch.setattr("app.services.gateway.download_to_storage", _replace_generation_then_error)
    monkeypatch.setattr(
        generation,
        "_enqueue_video_download",
        lambda task_id, **kwargs: queued.append((task_id, kwargs.get("external_task_id"))),
    )

    generation.run_video_download_task(tid, old_external_task_id)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.params == replacement_params
        assert task.error is None
        assert db.query(GenAsset).filter(GenAsset.task_id == tid).count() == 0
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()
    if attempts == 0:
        assert queued == [(tid, old_external_task_id)]
    else:
        assert queued == []


def test_stale_video_download_model_loader_result_does_not_hold_replacement(
    client,
    make_user,
    auth,
):
    uid = make_user("13790000135", balance=1000, admin=True)
    h = auth("13790000135")
    _config_video(client, h)
    old_external_task_id = "stale-model-loader-old"
    replacement_external_task_id = "stale-model-loader-replacement"
    replacement_params = {
        "duration": 4,
        "_video_result_url": "https://cdn.example.com/replacement-model.mp4",
        "replacement_generation": True,
    }
    tid = _stranded_video(uid, old_external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            "duration": 2,
            "_video_result_url": "https://cdn.example.com/stale-model.mp4",
        }
        db.commit()
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    def _replace_generation_then_return_no_model(*_args, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            task.params = replacement_params
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        return None

    generation_video_download.run_video_download_task(
        tid,
        expected_external_task_id=old_external_task_id,
        get_model_config_fn=_replace_generation_then_return_no_model,
    )

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.params == replacement_params
        assert task.error is None
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_stale_video_download_snapshot_error_does_not_hold_replacement(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13790000136", balance=1000, admin=True)
    h = auth("13790000136")
    _config_video(client, h)
    old_external_task_id = "stale-snapshot-old"
    replacement_external_task_id = "stale-snapshot-replacement"
    replacement_params = {
        "duration": 4,
        "_video_result_url": "https://cdn.example.com/replacement-snapshot.mp4",
        "replacement_generation": True,
    }
    tid = _stranded_video(uid, old_external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {
            "duration": 2,
            "_video_result_url": "https://cdn.example.com/stale-snapshot.mp4",
        }
        db.commit()
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    def _replace_generation_then_raise_snapshot_error(*_args, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            task.params = replacement_params
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        raise RuntimeError("snapshot resolution failed after replacement")

    monkeypatch.setattr(
        generation_video_download,
        "model_from_snapshot",
        _replace_generation_then_raise_snapshot_error,
    )
    generation_video_download.run_video_download_task(
        tid,
        expected_external_task_id=old_external_task_id,
        get_model_config_fn=generation.get_model_config,
    )

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.params == replacement_params
        assert task.error is None
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_poll_video_rechecks_external_id_after_final_heartbeat(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13790000124", balance=1000, admin=True)
    h = auth("13790000124")
    _config_video(client, h)
    original_external_task_id = "final-heartbeat-old"
    replacement_external_task_id = "final-heartbeat-replacement"
    tid = _stranded_video(uid, original_external_task_id)
    queued_polls = []
    progress_updates = []
    provider_calls = []
    mark_calls = 0
    real_mark_poll_alive = generation_video_flow.mark_poll_alive

    def _replace_after_final_mark(task_id, external_task_id=None):
        nonlocal mark_calls
        mark_calls += 1
        real_mark_poll_alive(task_id, external_task_id or original_external_task_id)
        if mark_calls != 2:
            return
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            concurrent_db.commit()
        finally:
            concurrent_db.close()

    monkeypatch.setattr(generation_video_submit, "mark_poll_alive", _replace_after_final_mark)
    monkeypatch.setattr(
        generation_video_submit,
        "set_progress",
        lambda task_id, percent, status: progress_updates.append((task_id, percent, status)),
    )

    generation_video_submit.poll_video_once(
        tid,
        poll_video_fn=lambda *_args, **_kwargs: provider_calls.append(tid)
        or {"status": "running"},
        try_enqueue_poll_fn=lambda task_id, *_args: queued_polls.append(task_id),
    )

    assert provider_calls == [tid]
    assert mark_calls == 2
    assert queued_polls == []
    assert progress_updates == []
    assert not generation_video_flow.poll_chain_alive(tid, replacement_external_task_id)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "polling"
        assert task.external_task_id == replacement_external_task_id
    finally:
        db.close()


@pytest.mark.parametrize(
    ("winner_status", "winner_phase", "phone"),
    [
        ("needs_review", "reconciling", "13790000120"),
        ("canceled", None, "13790000121"),
    ],
)
def test_poll_video_success_cas_does_not_revive_post_refresh_winner(
    client,
    make_user,
    auth,
    monkeypatch,
    winner_status,
    winner_phase,
    phone,
):
    uid = make_user(phone, balance=1000, admin=True)
    h = auth(phone)
    _config_video(client, h)
    tid = _stranded_video(uid, f"post-refresh-{winner_status}")
    real_persist = generation_video_submit.persist_video_download_result
    queued_downloads = []

    def _persist_after_concurrent_transition(db, task, result):
        concurrent_db = SessionLocal()
        try:
            current = concurrent_db.get(GenTask, tid)
            current.status = winner_status
            current.phase = winner_phase
            current.error = "concurrent state won"
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        return real_persist(db, task, result)

    monkeypatch.setattr(
        generation_video_submit,
        "persist_video_download_result",
        _persist_after_concurrent_transition,
    )
    generation_video_submit.poll_video_once(
        tid,
        poll_video_fn=lambda *_args, **_kwargs: {
            "status": "succeeded",
            "url": "https://cdn.example.com/loser.mp4",
        },
        try_enqueue_video_download_fn=lambda _db, task_id, **_kwargs: queued_downloads.append(task_id),
    )

    assert queued_downloads == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == winner_status
        assert task.phase == winner_phase
        assert task.error == "concurrent state won"
        assert "_video_result_url" not in (task.params or {})
    finally:
        db.close()


@pytest.mark.parametrize(
    ("handoff_source", "phone"),
    [
        ("existing", "13790000122"),
        ("cas_loser", "13790000123"),
    ],
)
def test_download_handoff_enqueue_failure_does_not_change_winner_state(
    client,
    make_user,
    auth,
    monkeypatch,
    handoff_source,
    phone,
):
    uid = make_user(phone, balance=1000, admin=True)
    h = auth(phone)
    _config_video(client, h)
    tid = _stranded_video(uid, f"handoff-{handoff_source}")
    winner_url = f"https://cdn.example.com/{handoff_source}-winner.mp4"

    def _set_download_winner():
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.phase = "downloading"
            task.params = {**(task.params or {}), "_video_result_url": winner_url}
            concurrent_db.commit()
        finally:
            concurrent_db.close()

    if handoff_source == "existing":
        _set_download_winner()
    else:
        real_persist = generation_video_submit.persist_video_download_result

        def _persist_after_download_winner(db, task, result):
            _set_download_winner()
            return real_persist(db, task, result)

        monkeypatch.setattr(
            generation_video_submit,
            "persist_video_download_result",
            _persist_after_download_winner,
        )

    if handoff_source == "existing":
        monkeypatch.setattr(
            generation,
            "_enqueue_video_download",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("redundant handoff enqueue failed")
            ),
        )
        generation.poll_video_once(tid)
    else:
        generation_video_submit.poll_video_once(
            tid,
            poll_video_fn=lambda *_args, **_kwargs: {
                "status": "succeeded",
                "url": "https://cdn.example.com/loser.mp4",
            },
            try_enqueue_video_download_fn=lambda _db, _task_id, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("redundant handoff enqueue failed")
            ),
        )

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.params["_video_result_url"] == winner_url
        assert task.error is None
    finally:
        db.close()


def test_stale_poll_delivery_skips_reconciling_task(client, make_user):
    uid = make_user("13790000103", balance=1000, admin=True)
    tid = _stranded_video(uid, "stale-poll-reconciling")
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "reconciling"
        db.commit()
    finally:
        db.close()

    provider_calls = []
    model_loads = []
    queued_polls = []
    queued_downloads = []

    def _fail_model_load(_db, use):
        model_loads.append(use)
        raise AssertionError("stale poll must return before model loading")

    generation_video_submit.poll_video_once(
        tid,
        get_model_config_fn=_fail_model_load,
        poll_video_fn=lambda *_args, **_kwargs: provider_calls.append(tid)
        or {"status": "running"},
        try_enqueue_poll_fn=lambda task_id, *_args: queued_polls.append(task_id),
        try_enqueue_video_download_fn=lambda _db, task_id, **_kwargs: queued_downloads.append(task_id),
    )

    assert provider_calls == []
    assert model_loads == []
    assert queued_polls == []
    assert queued_downloads == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "reconciling"
    finally:
        db.close()


def test_split_poll_video_uses_default_download_enqueue(client, make_user, auth, monkeypatch):
    uid = make_user("13900000971", balance=1000, admin=True)
    h = auth("13900000971")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-done-default-enqueue")
    queued_downloads = []
    monkeypatch.setattr(generation_video_submit, "enqueue_video_download", lambda task_id, **_kw: queued_downloads.append(task_id))
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: {"status": "succeeded", "url": "https://cdn.example.com/default-queued.mp4"},
    )

    generation_video_submit.poll_video_once(tid)

    assert queued_downloads == [tid]
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.params["_video_result_url"] == "https://cdn.example.com/default-queued.mp4"
    finally:
        db.close()


def test_split_poll_soft_timeout_holds_for_review(client, make_user, auth, monkeypatch):
    uid = make_user("13900000969", balance=1000, admin=True)
    h = auth("13900000969")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-soft-poll")
    queued_polls = []
    monkeypatch.setattr(
        generation_video_submit,
        "enqueue_poll",
        lambda task_id, *_args: queued_polls.append(task_id),
    )

    def timeout_poll(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    generation_video_submit.poll_video_once(tid, poll_video_fn=timeout_poll)

    assert queued_polls == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert "状态查询执行超时" in (task.error or "")
        assert task.params["_video_poll_state_unknown"] is True
        assert user.frozen_credits == 5
        assert user.balance_credits == 995
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


def test_poll_video_success_enqueue_failure_does_not_hold_replacement_generation(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13790000125", balance=1000, admin=True)
    h = auth("13790000125")
    _config_video(client, h)
    old_external_task_id = "enqueue-owner-old"
    replacement_external_task_id = "enqueue-owner-replacement"
    replacement_url = "https://cdn.example.com/replacement.mp4"
    tid = _stranded_video(uid, old_external_task_id)
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "url": "https://cdn.example.com/old-owner.mp4",
        },
    )

    def _replace_generation_then_fail(_task_id, **_kwargs):
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.external_task_id = replacement_external_task_id
            task.params = {**(task.params or {}), "_video_result_url": replacement_url}
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        raise RuntimeError("broker failed after replacement")

    monkeypatch.setattr(generation, "_enqueue_video_download", _replace_generation_then_fail)

    generation.poll_video_once(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.params["_video_result_url"] == replacement_url
        assert task.error is None
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

    generation.run_video_download_task(tid, "ext-usage")

    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert t["cost_settled"] == 5


def test_video_download_final_failure_holds_for_review(client, make_user, monkeypatch):
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
    generation.run_video_download_task(tid, "ext-last-download-fail")

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert "上游生成" in task.error
        assert task.cost_settled == 0
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
        row = db.query(GatewayCall).filter(
            GatewayCall.task_id == tid,
            GatewayCall.kind == "video_download",
            GatewayCall.status == "failed",
        ).order_by(GatewayCall.id.desc()).first()
        assert row is not None
        assert row.detail["permanent"] is True
        assert row.detail["needs_review"] is True
    finally:
        db.close()


def test_video_poll_transient_errors_hold_after_consecutive_limit(client, make_user, auth, monkeypatch):
    uid = make_user("13900000972", balance=1000, admin=True)
    h = auth("13900000972")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-transient-poll")
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: (_ for _ in ()).throw(gateway.GatewayError("temporary 503", transient=True)),
    )
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda _task_id, *_args: None)

    for _ in range(generation._POLL_MAX_CONSEC_ERRORS):
        generation.poll_video_once(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert "状态未知" in (task.error or "")
        assert task.params["_video_poll_state_unknown"] is True
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
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


def test_split_video_download_soft_timeout_holds_for_review(client, make_user, auth, monkeypatch):
    uid = make_user("13900000970", balance=1000, admin=True)
    h = auth("13900000970")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-soft-download")
    queued_downloads = []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.phase = "downloading"
        task.params = {"duration": 2, "_video_result_url": "https://cdn.example.com/soft.mp4"}
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(generation_video_download, "enqueue_video_download", lambda task_id, **_kw: queued_downloads.append(task_id))
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda *_a, **_k: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    generation_video_download.run_video_download_task(
        tid,
        expected_external_task_id="ext-soft-download",
    )

    assert queued_downloads == []
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert "上游生成" in task.error
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
    finally:
        db.close()


def test_download_lock_loser_does_not_refresh_alive_key(client, make_user, auth, monkeypatch):
    uid = make_user("13900000981", balance=1000, admin=True)
    h = auth("13900000981")
    _config_video(client, h)
    external_task_id = "ext-lock-no-heartbeat"
    tid = _stranded_video(uid, external_task_id)
    generation._mark_video_download_alive(tid, external_task_id)
    generation_video_flow.clear_video_download_alive(tid, external_task_id)
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
        assert not generation._video_download_alive(tid, external_task_id)
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
    generation._mark_poll_alive(tid, "mock-healthy")
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


def test_video_recovery_cursor_advances_past_healthy_batch(client, make_user, monkeypatch):
    uid = make_user("13790000104", balance=1000, admin=True)
    set_setting_db = SessionLocal()
    try:
        set_setting(set_setting_db, "video_resume_cursor", 0)
    finally:
        set_setting_db.close()

    healthy_ids = []
    db = SessionLocal()
    try:
        tasks = [
            GenTask(
                user_id=uid,
                category="video",
                stage="preview",
                status="running",
                phase="polling",
                external_task_id=f"healthy-recovery-{index}",
                external_submitted_at=datetime.now(timezone.utc),
                cost_frozen=0,
                params={"duration": 2},
            )
            for index in range(101)
        ]
        db.add_all(tasks)
        db.commit()
        healthy_ids = [task.id for task in tasks[:100]]
        stale_id = tasks[100].id
    finally:
        db.close()

    for index, task_id in enumerate(healthy_ids):
        generation._mark_poll_alive(task_id, f"healthy-recovery-{index}")
    queued = []
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))

    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
        assert generation.resume_stuck_videos(db) == 1
    finally:
        db.close()

    assert queued == [stale_id]


def test_video_recovery_clamps_configured_batch_to_one_thousand(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000111", balance=1000, admin=True)
    monkeypatch.setattr(generation.settings, "video_resume_batch_size", 5000)
    set_setting_db = SessionLocal()
    try:
        set_setting(set_setting_db, "video_resume_cursor", 0)
    finally:
        set_setting_db.close()

    db = SessionLocal()
    try:
        tasks = [
            GenTask(
                user_id=uid,
                category="video",
                stage="preview",
                status="running",
                phase="reconciling",
                external_task_id=f"clamped-recovery-{index}",
                external_submitted_at=datetime.now(timezone.utc),
                cost_frozen=0,
                params={"duration": 2},
            )
            for index in range(1001)
        ]
        db.add_all(tasks)
        db.commit()
        last_candidate_id = tasks[-1].id
    finally:
        db.close()

    examined_ids = []
    monkeypatch.setattr(generation.locks, "acquire", lambda *_args, **_kwargs: "test-owner")
    monkeypatch.setattr(generation.locks, "refresh", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(generation.locks, "release", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        generation,
        "_video_task_action",
        lambda task: examined_ids.append(task.id) or None,
    )
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()

    assert len(examined_ids) == 1000
    assert len(set(examined_ids)) == 1000
    state_db = SessionLocal()
    try:
        state = state_db.get(AppSetting, "video_resume_cursor").value
        assert state["v"] == examined_ids[-1]
        assert state["high_water"] == last_candidate_id
    finally:
        state_db.close()


def test_video_recovery_empty_cycle_persists_zero_cursor(client):
    state_db = SessionLocal()
    try:
        state = state_db.get(AppSetting, "video_resume_cursor")
        if state is None:
            state = AppSetting(key="video_resume_cursor")
            state_db.add(state)
        state.value = {"v": 123, "high_water": 123, "owner": "old-owner"}
        state_db.commit()
    finally:
        state_db.close()

    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()

    verify_db = SessionLocal()
    try:
        state = verify_db.get(AppSetting, "video_resume_cursor").value
        assert state["v"] == 0
        assert state["high_water"] == 0
    finally:
        verify_db.close()


def test_video_recovery_sparse_cycle_restarts_from_zero_after_tail(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000112", balance=1000, admin=True)
    monkeypatch.setattr(generation.settings, "video_resume_batch_size", 10)
    set_setting_db = SessionLocal()
    try:
        set_setting(set_setting_db, "video_resume_cursor", 0)
    finally:
        set_setting_db.close()

    db = SessionLocal()
    try:
        first = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="polling",
            external_task_id="sparse-first",
            external_submitted_at=datetime.now(timezone.utc),
            cost_frozen=0,
            params={"duration": 2},
        )
        db.add(first)
        db.flush()
        first_id = first.id
        db.add_all([
            GenTask(
                user_id=uid,
                category="image",
                stage="preview",
                status="running",
                phase="rendering",
                cost_frozen=0,
                params={"n": 1},
            ),
            GenTask(
                user_id=uid,
                category="video",
                stage="preview",
                status="running",
                phase="polling",
                external_task_id=None,
                cost_frozen=0,
                params={"duration": 2},
            ),
        ])
        db.flush()
        second = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="polling",
            external_task_id="sparse-second",
            external_submitted_at=datetime.now(timezone.utc),
            cost_frozen=0,
            params={"duration": 2},
        )
        db.add(second)
        db.commit()
        second_id = second.id
    finally:
        db.close()

    assert second_id > first_id + 1
    generation._mark_poll_alive(first_id, "sparse-first")
    generation._mark_poll_alive(second_id, "sparse-second")
    queued = []
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()

    state_db = SessionLocal()
    try:
        state = state_db.get(AppSetting, "video_resume_cursor").value
        assert state["v"] == second_id
        assert state["high_water"] == second_id
    finally:
        state_db.close()

    generation_video_flow.clear_poll_alive(first_id, "sparse-first")
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 1
    finally:
        db.close()

    assert queued == [first_id]
    state_db = SessionLocal()
    try:
        state = state_db.get(AppSetting, "video_resume_cursor").value
        assert state["v"] == second_id
        assert state["high_water"] == second_id
    finally:
        state_db.close()


def test_video_recovery_cycle_revisits_old_ids_during_sustained_inserts(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000105", balance=1000, admin=True)
    monkeypatch.setattr(generation.settings, "video_resume_batch_size", 2)
    set_setting_db = SessionLocal()
    try:
        set_setting(set_setting_db, "video_resume_cursor", 0)
    finally:
        set_setting_db.close()

    def _add_healthy_tasks(prefix, count):
        db = SessionLocal()
        try:
            tasks = [
                GenTask(
                    user_id=uid,
                    category="video",
                    stage="preview",
                    status="running",
                    phase="polling",
                    external_task_id=f"{prefix}-{index}",
                    external_submitted_at=datetime.now(timezone.utc),
                    cost_frozen=0,
                    params={"duration": 2},
                )
                for index in range(count)
            ]
            db.add_all(tasks)
            db.commit()
            ids = [task.id for task in tasks]
        finally:
            db.close()
        for index, task_id in enumerate(ids):
            generation._mark_poll_alive(task_id, f"{prefix}-{index}")
        return ids

    original_ids = _add_healthy_tasks("cycle-original", 4)
    queued = []
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()

    generation_video_flow.clear_poll_alive(original_ids[0], "cycle-original-0")
    _add_healthy_tasks("cycle-new-a", 2)
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()
    assert queued == []

    _add_healthy_tasks("cycle-new-b", 2)
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 1
    finally:
        db.close()
    assert queued == [original_ids[0]]


def test_video_recovery_rechecks_phase_after_lock(client, make_user, monkeypatch):
    uid = make_user("13790000106", balance=1000, admin=True)
    tid = _stranded_video(uid, "phase-changed-after-scan")
    set_setting_db = SessionLocal()
    try:
        set_setting(set_setting_db, "video_resume_cursor", 0)
    finally:
        set_setting_db.close()

    real_acquire = generation.locks.acquire
    phase_changed = False

    def _change_phase_after_candidate_scan(key, ttl=None):
        nonlocal phase_changed
        token = real_acquire(key, ttl=ttl)
        if key == f"video:resume:{tid}" and token and not phase_changed:
            phase_changed = True
            concurrent_db = SessionLocal()
            try:
                current = concurrent_db.get(GenTask, tid)
                current.phase = "reconciling"
                concurrent_db.commit()
            finally:
                concurrent_db.close()
        return token

    queued = []
    monkeypatch.setattr(generation.locks, "acquire", _change_phase_after_candidate_scan)
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))
    db = SessionLocal()
    try:
        resumed = generation.resume_stuck_videos(db)
    finally:
        db.close()

    assert phase_changed is True
    assert resumed == 0
    assert queued == []
    db = SessionLocal()
    try:
        assert db.get(GenTask, tid).phase == "reconciling"
    finally:
        db.close()


def test_video_recovery_expired_owner_cannot_claim_successor_db_state(client, monkeypatch):
    expired_token = "video-recovery-expired-owner-a"
    successor_token = "video-recovery-successor-owner-b"
    successor_state = {
        "v": 777,
        "high_water": 999,
        "owner": successor_token,
        "version": 41,
    }
    state_db = SessionLocal()
    try:
        state = state_db.get(AppSetting, "video_resume_cursor")
        if state is None:
            state = AppSetting(key="video_resume_cursor")
            state_db.add(state)
        state.value = successor_state
        state_db.commit()
    finally:
        state_db.close()
    generation.locks.redis_client.set("video:resume:scan", successor_token, ex=600)

    monkeypatch.setattr(generation.locks, "acquire", lambda *_args, **_kwargs: expired_token)
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()

    verify_db = SessionLocal()
    try:
        assert verify_db.get(AppSetting, "video_resume_cursor").value == successor_state
    finally:
        verify_db.close()
    assert generation.locks.redis_client.get("video:resume:scan") == successor_token


def test_video_recovery_claim_cas_rejects_successor_before_sql_executes(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000107", balance=1000, admin=True)
    tid = _stranded_video(uid, "cursor-claim-cas")
    state_db = SessionLocal()
    try:
        state = state_db.get(AppSetting, "video_resume_cursor")
        if state is None:
            state = AppSetting(key="video_resume_cursor")
            state_db.add(state)
        state.value = {"v": 0, "high_water": tid, "owner": "video-recovery-old-owner"}
        state_db.commit()
    finally:
        state_db.close()

    successor_token = "video-recovery-successor-before-cas"
    successor_state = {
        "v": 701,
        "high_water": 909,
        "owner": successor_token,
    }
    injected = False
    refresh_calls = 0
    real_refresh = generation.locks.refresh

    def _count_refreshes(key, token, ttl):
        nonlocal refresh_calls
        refresh_calls += 1
        return real_refresh(key, token, ttl)

    def _successor_before_claim_update(_conn, _cursor, statement, _params, _ctx, _many):
        nonlocal injected
        normalized = " ".join(str(statement).split())
        if (
            injected
            or not normalized.startswith("UPDATE app_settings")
            or "app_settings.value =" not in normalized
        ):
            return
        injected = True
        generation.locks.redis_client.set("video:resume:scan", successor_token, ex=600)
        successor_db = SessionLocal()
        try:
            state = successor_db.get(AppSetting, "video_resume_cursor")
            state.value = successor_state
            successor_db.commit()
        finally:
            successor_db.close()

    queued = []
    monkeypatch.setattr(generation.locks, "refresh", _count_refreshes)
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))
    event.listen(engine, "before_cursor_execute", _successor_before_claim_update)
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()
        event.remove(engine, "before_cursor_execute", _successor_before_claim_update)

    verify_db = SessionLocal()
    try:
        assert verify_db.get(AppSetting, "video_resume_cursor").value == successor_state
    finally:
        verify_db.close()
    assert injected is True
    assert refresh_calls >= 1
    assert queued == []
    assert generation.locks.redis_client.get("video:resume:scan") == successor_token


def test_video_recovery_stops_when_successor_takes_over_after_claim_commit(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000108", balance=1000, admin=True)
    _stranded_video(uid, "cursor-after-claim")
    state_db = SessionLocal()
    try:
        set_setting(state_db, "video_resume_cursor", 0)
    finally:
        state_db.close()

    successor_token = "video-recovery-successor-after-claim"
    successor_state = {
        "v": 811,
        "high_water": 977,
        "owner": successor_token,
    }
    refresh_calls = 0
    candidate_selects = 0
    real_refresh = generation.locks.refresh

    def _successor_after_claim_commit(key, token, ttl):
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            generation.locks.redis_client.set(key, successor_token, ex=600)
            successor_db = SessionLocal()
            try:
                state = successor_db.get(AppSetting, "video_resume_cursor")
                state.value = successor_state
                successor_db.commit()
            finally:
                successor_db.close()
        return real_refresh(key, token, ttl)

    def _count_candidate_selects(_conn, _cursor, statement, _params, _ctx, _many):
        nonlocal candidate_selects
        normalized = " ".join(str(statement).split())
        if normalized.startswith("SELECT") and "FROM gen_tasks" in normalized:
            candidate_selects += 1

    queued = []
    monkeypatch.setattr(generation.locks, "refresh", _successor_after_claim_commit)
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))
    event.listen(engine, "before_cursor_execute", _count_candidate_selects)
    db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(db) == 0
    finally:
        db.close()
        event.remove(engine, "before_cursor_execute", _count_candidate_selects)

    verify_db = SessionLocal()
    try:
        assert verify_db.get(AppSetting, "video_resume_cursor").value == successor_state
    finally:
        verify_db.close()
    assert refresh_calls == 2
    assert candidate_selects == 0
    assert queued == []
    assert generation.locks.redis_client.get("video:resume:scan") == successor_token


def test_video_recovery_cursor_fence_rejects_successor_after_final_refresh(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000109", balance=1000, admin=True)
    _stranded_video(uid, "cursor-final-refresh")
    set_setting_db = SessionLocal()
    try:
        set_setting(set_setting_db, "video_resume_cursor", 0)
    finally:
        set_setting_db.close()

    queued = []
    successor_token = "video-recovery-successor"
    refresh_calls = 0
    takeover_done = False
    real_refresh = generation.locks.refresh

    def _refresh_then_successor_takes_over(key, token, ttl):
        nonlocal refresh_calls, takeover_done
        refresh_calls += 1
        refreshed = real_refresh(key, token, ttl)
        if queued and not takeover_done:
            takeover_done = True
            generation.locks.redis_client.set(key, successor_token, ex=ttl)
            successor_db = SessionLocal()
            try:
                state = successor_db.get(AppSetting, "video_resume_cursor")
                state.value = {
                    "v": 987654,
                    "high_water": 987654,
                    "owner": successor_token,
                }
                successor_db.commit()
            finally:
                successor_db.close()
        return refreshed

    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))
    monkeypatch.setattr(generation.locks, "refresh", _refresh_then_successor_takes_over)
    db = SessionLocal()
    try:
        generation.resume_stuck_videos(db)
    finally:
        db.close()

    verify_db = SessionLocal()
    try:
        assert get_setting(verify_db, "video_resume_cursor", None) == 987654
        assert verify_db.get(AppSetting, "video_resume_cursor").value == {
            "v": 987654,
            "high_water": 987654,
            "owner": successor_token,
        }
    finally:
        verify_db.close()
    assert refresh_calls >= 1
    assert generation.locks.redis_client.get("video:resume:scan") == successor_token


def test_concurrent_video_recovery_claims_only_one_poll_chain(client, make_user, monkeypatch):
    uid = make_user("13900000998", balance=1000, admin=True)
    tid = _stranded_video(uid, "concurrent-recovery")
    enqueue_lock = threading.Lock()
    release_first = threading.Event()
    first_enqueue_started = threading.Event()
    one_recovery_finished = threading.Event()
    queued = []

    def _slow_first_enqueue(task_id, *_args):
        with enqueue_lock:
            queued.append(task_id)
            is_first = len(queued) == 1
        if is_first:
            first_enqueue_started.set()
            assert release_first.wait(timeout=5)

    monkeypatch.setattr(generation, "_poll_chain_alive", lambda _task_id, *_args: False)
    monkeypatch.setattr(generation, "_try_enqueue_poll", _slow_first_enqueue)

    def _resume_once():
        db = SessionLocal()
        try:
            return generation.resume_stuck_videos(db)
        finally:
            db.close()
            one_recovery_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_resume_once) for _ in range(2)]
        assert first_enqueue_started.wait(timeout=5)
        assert one_recovery_finished.wait(timeout=5)
        release_first.set()
        results = [future.result(timeout=5) for future in futures]

    assert queued == [tid]
    assert sorted(results) == [0, 1]


def test_video_recovery_limits_each_scan_batch(client, make_user, monkeypatch):
    uid = make_user("13900000999", balance=1000, admin=True)
    db = SessionLocal()
    try:
        db.add_all([
            GenTask(
                user_id=uid,
                category="video",
                stage="preview",
                status="running",
                phase="polling",
                external_task_id=f"batch-recovery-{index}",
                external_submitted_at=datetime.now(timezone.utc),
                cost_frozen=0,
                params={"duration": 2},
            )
            for index in range(101)
        ])
        db.commit()
    finally:
        db.close()

    queued = []
    monkeypatch.setattr(generation, "_poll_chain_alive", lambda _task_id, *_args: False)
    monkeypatch.setattr(generation, "_video_download_alive", lambda _task_id, *_args: False)
    monkeypatch.setattr(generation, "_mark_poll_alive", lambda _task_id, *_args: None)
    monkeypatch.setattr(generation, "_try_enqueue_poll", lambda task_id, *_args: queued.append(task_id))
    db = SessionLocal()
    try:
        resumed = generation.resume_stuck_videos(db)
    finally:
        db.close()

    assert resumed == 100
    assert len(queued) == 100


def test_video_recovery_download_enqueue_failure_stays_retryable(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000126", balance=1000, admin=True)
    db = SessionLocal()
    try:
        set_setting(db, "video_resume_cursor", 0)
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="downloading",
            external_task_id="recovery-download-retry",
            external_submitted_at=datetime.now(timezone.utc),
            cost_frozen=0,
            params={
                "duration": 2,
                "_video_result_url": "https://cdn.example.com/recovery-retry.mp4",
            },
        )
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()

    attempts = []

    def _fail_once(task_id, **_kwargs):
        attempts.append(task_id)
        if len(attempts) == 1:
            raise RuntimeError("broker temporarily unavailable")

    monkeypatch.setattr(generation, "_enqueue_video_download", _fail_once)
    first_db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(first_db) == 0
    finally:
        first_db.close()

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.error is None
        assert not generation._video_download_alive(tid, "recovery-download-retry")
    finally:
        verify_db.close()

    second_db = SessionLocal()
    try:
        assert generation.resume_stuck_videos(second_db) == 1
    finally:
        second_db.close()

    assert attempts == [tid, tid]
    assert generation._video_download_alive(tid, "recovery-download-retry")


def test_fail_and_refund_fallback_does_not_overwrite_replacement_generation(
    client,
    make_user,
    monkeypatch,
):
    uid = make_user("13790000127", balance=1000, admin=True)
    old_external_task_id = "refund-fallback-old"
    replacement_external_task_id = "refund-fallback-replacement"
    tid = _stranded_video(uid, old_external_task_id)

    def _replace_generation_then_fail(db, *_args, **_kwargs):
        db.rollback()
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.phase = "downloading"
            task.external_task_id = replacement_external_task_id
            task.params = {
                **(task.params or {}),
                "_video_result_url": "https://cdn.example.com/refund-replacement.mp4",
            }
            concurrent_db.commit()
        finally:
            concurrent_db.close()
        raise RuntimeError("refund commit path failed")

    monkeypatch.setattr(generation_common.credits, "refund", _replace_generation_then_fail)
    db = SessionLocal()
    try:
        generation_common.fail_and_refund(
            db,
            tid,
            "stale provider failure",
            expected_status="running",
            expected_phase="polling",
            expected_external_task_id=old_external_task_id,
        )
    finally:
        db.close()

    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "downloading"
        assert task.external_task_id == replacement_external_task_id
        assert task.error is None
        user = verify_db.get(User, uid)
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
    finally:
        verify_db.close()


def test_mark_needs_review_params_update_uses_observed_params_cas(
    client,
    make_user,
):
    uid = make_user("13790000128", balance=1000, admin=True)
    old_external_task_id = "params-cas-old"
    tid = _stranded_video(uid, old_external_task_id)
    replacement_params = {"duration": 2, "concurrent_winner": True}
    injected = False

    def _replace_params_before_review_update(_conn, _cursor, statement, _params, _ctx, _many):
        nonlocal injected
        normalized = " ".join(str(statement).split())
        if injected or not normalized.startswith("UPDATE gen_tasks SET"):
            return
        injected = True
        concurrent_db = SessionLocal()
        try:
            task = concurrent_db.get(GenTask, tid)
            task.params = replacement_params
            concurrent_db.commit()
        finally:
            concurrent_db.close()

    event.listen(engine, "before_cursor_execute", _replace_params_before_review_update)
    db = SessionLocal()
    try:
        changed = generation_common.mark_needs_review(
            db,
            tid,
            "stale review",
            params_update={"review_marker": True},
            expected_status="running",
            expected_phase="polling",
            expected_external_task_id=old_external_task_id,
        )
    finally:
        db.close()
        event.remove(engine, "before_cursor_execute", _replace_params_before_review_update)

    assert injected is True
    assert changed is False
    verify_db = SessionLocal()
    try:
        task = verify_db.get(GenTask, tid)
        assert task.status == "running"
        assert task.phase == "polling"
        assert task.params == replacement_params
        assert task.error is None
    finally:
        verify_db.close()


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
            external_submitted_at=datetime.now(timezone.utc) - timedelta(hours=8),
            created_at=datetime.now(timezone.utc) - timedelta(hours=8),
            cost_frozen=5,
            params={"duration": 2, "_video_result_url": "https://cdn.example.com/r.mp4"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id
        generation._mark_video_download_alive(tid, "ext-downloading")

        assert retention.reap_stuck_tasks(db, max_minutes=60) == 0
        db.refresh(t)
        assert t.status == "running"
    finally:
        db.close()


def test_reaper_skips_pending_video_download_without_alive_key(client, make_user, auth):
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
            external_task_id="ext-pending-download",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
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


def test_reaper_holds_expired_pending_video_download_without_alive_key(client, make_user, auth, monkeypatch):
    uid = make_user("13900000982", balance=1000, admin=True)
    h = auth("13900000982")
    _config_video(client, h)
    monkeypatch.setattr(retention.settings, "video_download_timeout_seconds", 1)
    monkeypatch.setattr(retention.settings, "video_download_max_attempts", 1)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="downloading",
            external_task_id="ext-expired-download",
            external_submitted_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            cost_frozen=5,
            cost_settled=0,
            params={"duration": 2, "_video_result_url": "https://cdn.example.com/expired.mp4"},
        )
        db.add(t)
        db.flush()
        credits.freeze(db, uid, 5, biz_ref=t.id, commit=False)
        db.commit()
        tid = t.id

        assert retention.reap_stuck_tasks(db, max_minutes=1) >= 1
        held = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert held.status == "needs_review"
        assert held.phase == "reconciling"
        assert "结果下载超时" in (held.error or "")
        assert held.params["_video_download_state_unknown"] is True
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
    finally:
        db.close()


def test_reaper_holds_video_submitting_with_request_id_for_review(client, make_user, auth):
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
        held = db.get(GenTask, tid)
        assert held.status == "needs_review"
        assert held.phase == "reconciling"
        assert held.cost_settled == 0
        assert "视频提交状态未知" in (held.error or "")
        user = db.get(User, uid)
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_reaper_holds_video_submitting_after_submit_timeout_before_generic_window(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13900000971", balance=1000, admin=True)
    h = auth("13900000971")
    _config_video(client, h)
    monkeypatch.setattr(retention.settings, "video_submit_timeout_seconds", 1)
    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            phase="submitting",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=6),
            cost_frozen=5,
            params={"duration": 2, "_video_request_id": "video-local-timeout"},
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
        held = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert held.status == "needs_review"
        assert held.phase == "reconciling"
        assert "视频提交状态未知" in (held.error or "")
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_reaper_holds_submitted_video_timeout_for_review(client, make_user, auth, monkeypatch):
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
        held = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert held.status == "needs_review"
        assert held.phase == "reconciling"
        assert "上游任务状态未知" in (held.error or "")
        assert held.params["_video_poll_state_unknown"] is True
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_video_render_timeout_holds_for_review(client, make_user, auth, monkeypatch):
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
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert task.cost_settled == 0
        assert "状态未知" in (task.error or "")
        assert task.params["_video_poll_state_unknown"] is True
        user = db.get(User, uid)
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_poll_video_snapshot_mismatch_holds_for_review(client, make_user, auth):
    uid = make_user("13900000987", balance=1000, admin=True)
    h = auth("13900000987")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-snapshot-drift")
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.params = {
            **(task.params or {}),
            "_model_snapshot": {
                "model_id": "mock-video",
                "gateway_key_fingerprint": "old-fingerprint",
                "base_url": "",
                "provider": "mock",
                "gateway_format": "mock",
                "cost_credits": 50,
                "unlock_cost": 0,
                "extra": {"preview_cost": 5},
            },
        }
        db.commit()
        user = db.get(User, uid)
        balance_after_freeze = user.balance_credits
        frozen_after_freeze = user.frozen_credits
    finally:
        db.close()

    generation.poll_video_once(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert "模型网关配置已变更" in (task.error or "")
        assert user.balance_credits == balance_after_freeze
        assert user.frozen_credits == frozen_after_freeze
    finally:
        db.close()


def test_video_download_local_settlement_failure_holds_for_review(
    client,
    make_user,
    auth,
    monkeypatch,
    tiny_mp4,
):
    uid = make_user("13900000986", balance=1000, admin=True)
    h = auth("13900000986")
    _config_video(client, h)
    tid = _stranded_video(uid, "ext-settle-fail")
    monkeypatch.setattr(generation, "_enqueue_video_download", lambda _task_id, **_kw: None)
    monkeypatch.setattr(
        "app.services.gateway.poll_video",
        lambda *_a, **_k: {"status": "succeeded", "url": "https://cdn.example.com/ok.mp4"},
    )
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda _url, subdir, ext, **_kwargs: storage.save_bytes(tiny_mp4, subdir, ext),
    )
    original_settle = credits.settle

    def fail_settle(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    generation.poll_video_once(tid)
    monkeypatch.setattr(credits, "settle", fail_settle)
    generation.run_video_download_task(tid, "ext-settle-fail")
    monkeypatch.setattr(credits, "settle", original_settle)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert "上游生成" in (task.error or "")
        assert task.cost_settled == 0
        assert user.balance_credits == 995
        assert user.frozen_credits == 5
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
    generation.run_video_download_task(tid, "ext-downloaded-config-missing")

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
    finally:
        db.close()


def test_final_video_localizes_uploaded_poster_without_http_fetch(
    client,
    make_user,
    auth,
    monkeypatch,
    tiny_mp4,
):
    uid = make_user("13900000068", balance=1000, admin=True)
    h = auth("13900000068")
    _config_video(client, h)
    monkeypatch.setattr(generation.settings, "mock_mode", False)
    monkeypatch.setattr(generation.settings, "video_gateway_base_url", "https://video-gateway.test")
    monkeypatch.setattr(generation.settings, "video_gateway_api_key", "sk-test")
    monkeypatch.setattr(
        "app.services.gateway.download_to_storage",
        lambda _url, subdir, ext, **_k: generation.storage.save_bytes(tiny_mp4, subdir, ext),
    )

    upload_key = storage.save_bytes(b"\x89PNG\r\n\x1a\n", "upload", "png")
    db = SessionLocal()
    try:
        db.add(UploadedAsset(
            key=upload_key,
            user_id=uid,
            mime="image/png",
            width=32,
            height=32,
            bytes=8,
            original_filename="poster.png",
        ))
        preview = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "product video"},
            params={"duration": 2, "reference_image_url": storage.upload_api_url(upload_key)},
            cost_frozen=5,
            cost_settled=5,
        )
        db.add(preview)
        db.flush()
        final_task = GenTask(
            user_id=uid,
            category="video",
            stage="final",
            status="running",
            phase="downloading",
            parent_task_id=preview.id,
            prompt={"final_text": "product video"},
            params={"duration": 2, "reference_image_url": storage.upload_api_url(upload_key)},
            cost_frozen=50,
        )
        db.add(final_task)
        db.flush()
        credits.freeze(db, uid, 50, biz_ref=final_task.id, commit=False)
        db.commit()
        tid = final_task.id
    finally:
        db.close()

    def forbidden_http_download(*_args, **_kwargs):
        raise AssertionError("uploaded poster should be read from local storage")

    monkeypatch.setattr("app.services.gateway.download_bytes_limited", forbidden_http_download)
    monkeypatch.setattr(
        "app.services.generation_media.make_image_preview",
        lambda raw, **_kwargs: (b"preview-png", 32, 32),
    )

    db = SessionLocal()
    try:
        final_task = db.get(GenTask, tid)
        model = generation.get_model_config(db, "video")
        generation._finalize_video_success(
            db,
            final_task,
            model,
            {"status": "succeeded", "url": "https://cdn.example.com/final.mp4"},
        )
    finally:
        db.close()

    task = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert task["status"] == "succeeded", task
    assert task["assets"][0]["preview_url"].startswith("http://localhost:8000/media/preview/")

    task = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert task["status"] == "succeeded", task
    asset = task["assets"][0]
    assert asset["preview_url"].startswith("http://localhost:8000/media/preview/")
    assert "/media/video_hd/" in asset["hd_url"]
    assert asset["unlocked"] is True
    assert asset["unlock_cost"] == 0
    assert client.get(asset["preview_url"].replace("http://localhost:8000", "")).status_code == 200
    assert client.get(f"/api/assets/{asset['id']}/download", headers=h).status_code == 200
    db = SessionLocal()
    try:
        stored = db.get(GenAsset, asset["id"])
        assert "/media/video_hd/" in stored.hd_url
    finally:
        db.close()
