from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import GatewayCall, GenTask


def test_task_output_includes_standard_error_type(client, make_user, auth):
    uid = make_user("13900009100", balance=1000)
    h = auth("13900009100")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="failed",
            prompt={"final_text": "x"},
            params={"n": 1},
            error="gateway timeout 502 while generating image",
            cost_frozen=10,
            cost_settled=0,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    res = client.get(f"/api/tasks/{task_id}", headers=h)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["error_type"] == "provider_timeout"
    assert body["error_message"] == body["error"]


def test_needs_review_task_output_keeps_reconciliation_error_type(client, make_user, auth):
    uid = make_user("13900009111", balance=1000)
    h = auth("13900009111")
    db = SessionLocal()
    try:
        provider_task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="needs_review",
            phase="reconciling",
            prompt={"final_text": "x"},
            params={"_video_submit_state_unknown": True},
            error="视频提交状态未知,上游可能已接受任务,冻结积分暂不退回。",
            cost_frozen=30,
            cost_settled=0,
            finished_at=datetime.now(timezone.utc),
        )
        local_task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="needs_review",
            phase="reconciling",
            prompt={"final_text": "x"},
            params={"_image_result_keys": ["preview/a.png"]},
            error="图片已由上游生成,但本地落账失败,需要系统恢复或管理员确认。",
            cost_frozen=20,
            cost_settled=0,
            finished_at=datetime.now(timezone.utc),
        )
        db.add_all([provider_task, local_task])
        db.commit()
        provider_task_id = provider_task.id
        local_task_id = local_task.id
    finally:
        db.close()

    provider_res = client.get(f"/api/tasks/{provider_task_id}", headers=h)
    local_res = client.get(f"/api/tasks/{local_task_id}", headers=h)

    assert provider_res.status_code == 200, provider_res.text
    assert local_res.status_code == 200, local_res.text
    assert provider_res.json()["error_type"] == "provider_error"
    assert local_res.json()["error_type"] == "system_error"


def test_video_task_eta_uses_historical_finished_tasks(client, make_user, auth):
    uid = make_user("13900009101", balance=1000)
    h = auth("13900009101")
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        for minutes in (4, 6):
            db.add(
                GenTask(
                    user_id=uid,
                    category="video",
                    stage="preview",
                    status="succeeded",
                    prompt={"final_text": "done"},
                    params={"duration": 5, "resolution": "1080p"},
                    cost_frozen=30,
                    cost_settled=30,
                    created_at=now - timedelta(days=1, minutes=minutes),
                    finished_at=now - timedelta(days=1),
                )
            )
        running = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            prompt={"final_text": "running"},
            params={"duration": 5, "resolution": "1080p"},
            cost_frozen=30,
            cost_settled=0,
            created_at=now - timedelta(minutes=1),
        )
        db.add(running)
        db.commit()
        task_id = running.id
    finally:
        db.close()

    res = client.get(f"/api/tasks/{task_id}", headers=h)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["eta_source"] == "history"
    assert 180 <= body["eta_total_seconds"] <= 420
    assert 120 <= body["eta_remaining_seconds"] <= 360


def test_video_task_eta_tolerates_bad_duration(client, make_user, auth):
    uid = make_user("13900009110", balance=1000)
    h = auth("13900009110")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="video",
            stage="preview",
            status="running",
            prompt={"final_text": "running"},
            params={"duration": "bad", "resolution": "1080p"},
            cost_frozen=30,
            cost_settled=0,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        db.add(task)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    res = client.get(f"/api/tasks/{task_id}", headers=h)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["eta_source"] == "fallback"
    assert body["eta_total_seconds"] >= 120
    assert body["eta_remaining_seconds"] >= 30


def test_user_studio_draft_crud(client, make_user, auth):
    make_user("13900009102", balance=1000)
    h = auth("13900009102")

    saved = client.put(
        "/api/me/drafts/studio",
        headers=h,
        json={"payload": {"prompt": "柔和布光产品图", "mode": "image"}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["key"] == "studio"
    assert saved.json()["payload"]["prompt"] == "柔和布光产品图"

    loaded = client.get("/api/me/drafts/studio", headers=h)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["payload"]["mode"] == "image"

    deleted = client.delete("/api/me/drafts/studio", headers=h)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True

    missing = client.get("/api/me/drafts/studio", headers=h)
    assert missing.status_code == 200, missing.text
    assert missing.json()["payload"] == {}


def test_admin_usage_dashboard_and_model_cost_dashboard(client, make_user, auth):
    uid = make_user("13900009103", balance=1000, admin=True)
    h = auth("13900009103")
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        db.add(
            GenTask(
                user_id=uid,
                category="image",
                stage="preview",
                status="succeeded",
                prompt={"final_text": "x"},
                params={"n": 1, "_error_type": "system_error"},
                model_use="image",
                cost_frozen=10,
                cost_settled=8,
                created_at=now - timedelta(minutes=3),
                finished_at=now,
            )
        )
        db.add(
            GenTask(
                user_id=uid,
                category="video",
                stage="preview",
                status="failed",
                prompt={"final_text": "x"},
                params={"duration": 5, "resolution": "720p", "_error_type": "provider_error"},
                model_use="video",
                cost_frozen=20,
                cost_settled=0,
                error="provider bad gateway",
                created_at=now - timedelta(minutes=8),
                finished_at=now,
            )
        )
        db.add(
            GenTask(
                user_id=uid,
                category="image",
                stage="preview",
                status="needs_review",
                phase="reconciling",
                prompt={"final_text": "x"},
                params={"_image_result_keys": ["preview/a.png"]},
                model_use="image",
                cost_frozen=10,
                cost_settled=0,
                error="图片已由上游生成,但本地落账失败,需要系统恢复或管理员确认。",
                created_at=now - timedelta(minutes=5),
                finished_at=now,
            )
        )
        db.add(
            GatewayCall(
                user_id=uid,
                task_id=1,
                kind="image",
                model_id="mock-image",
                status="ok",
                latency_ms=1200,
                total_tokens=100,
                detail={"estimated_cost_credits": 3},
                created_at=now,
            )
        )
        db.add(
            GatewayCall(
                user_id=uid,
                task_id=2,
                kind="video_submit",
                model_id="mock-video",
                status="failed",
                latency_ms=3000,
                total_tokens=0,
                detail={"estimated_cost_credits": 9},
                created_at=now,
            )
        )
        db.commit()
    finally:
        db.close()

    dashboard = client.get("/api/admin/usage/dashboard", headers=h)
    assert dashboard.status_code == 200, dashboard.text
    dashboard_body = dashboard.json()
    assert dashboard_body["summary"]["task_count"] >= 2
    assert dashboard_body["summary"]["success_count"] >= 1
    assert dashboard_body["failure_reasons"]["provider_error"] >= 1
    assert dashboard_body["failure_reasons"]["system_error"] >= 1
    assert dashboard_body["avg_duration_seconds"] > 0

    costs = client.get("/api/admin/usage/model-costs", headers=h)
    assert costs.status_code == 200, costs.text
    cost_rows = costs.json()["models"]
    assert any(row["model_id"] == "mock-image" and row["call_count"] >= 1 for row in cost_rows)
    assert any(row["model_id"] == "mock-video" and row["failed_count"] >= 1 for row in cost_rows)
