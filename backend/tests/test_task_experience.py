import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from app.db import SessionLocal, engine
from app.models import GatewayCall, GenTask, User, UserDraft
from app.routers.me import STUDIO_DRAFT_MAX_FUTURE_SKEW_MS, save_draft
from app.schemas import UserDraftIn


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


def test_task_output_includes_saved_prompt_and_model_snapshot(client, make_user, auth):
    uid = make_user("13900009112", balance=1000)
    h = auth("13900009112")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "用户提交的产品提示词"},
            params={
                "_generation_prompt": "实际发送给模型的最终提示词",
                "_model_snapshot": {"model_id": "gpt-image-test", "provider": "openai"},
            },
            model_use="image",
            cost_frozen=10,
            cost_settled=10,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    body = client.get(f"/api/tasks/{task_id}", headers=h).json()

    assert body["prompt_text"] == "实际发送给模型的最终提示词"
    assert body["prompt_text_source"] == "generation"
    assert body["request_prompt_text"] == "用户提交的产品提示词"
    assert body["generation_prompt_text"] == "实际发送给模型的最终提示词"
    assert body["model_id"] == "gpt-image-test"
    assert body["model_provider"] == "openai"


def test_task_output_marks_legacy_request_prompt_and_recovers_gateway_model(client, make_user, auth):
    uid = make_user("13900009113", balance=1000)
    h = auth("13900009113")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "旧任务只保存了用户原始请求"},
            params={},
            model_use="image",
            cost_frozen=10,
            cost_settled=10,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.flush()
        db.add(
            GatewayCall(
                user_id=uid,
                task_id=task.id,
                kind="image",
                model_id="legacy-image-model",
                status="ok",
            )
        )
        db.commit()
        task_id = task.id
    finally:
        db.close()

    body = client.get(f"/api/tasks/{task_id}", headers=h).json()

    assert body["prompt_text"] == "旧任务只保存了用户原始请求"
    assert body["prompt_text_source"] == "request"
    assert body["request_prompt_text"] == "旧任务只保存了用户原始请求"
    assert body["generation_prompt_text"] is None
    assert body["model_id"] == "legacy-image-model"
    assert body["model_provider"] is None


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


def test_user_studio_draft_rejects_stale_or_conflicting_saved_at(client, make_user, auth):
    uid = make_user("13900009112", balance=1000)
    h = auth("13900009112")

    payload_200 = {"savedAt": 200, "workspaces": {"image": {"prompt": "two hundred"}}}
    saved = client.put("/api/me/drafts/studio", headers=h, json={"payload": payload_200})
    assert saved.status_code == 200, saved.text
    updated_at_200 = saved.json()["updated_at"]

    older = client.put(
        "/api/me/drafts/studio",
        headers=h,
        json={"payload": {"savedAt": 100, "workspaces": {"image": {"prompt": "one hundred"}}}},
    )
    assert older.status_code == 200, older.text
    assert older.json()["payload"] == payload_200
    assert older.json()["updated_at"] == updated_at_200

    equal_same = client.put("/api/me/drafts/studio", headers=h, json={"payload": payload_200})
    assert equal_same.status_code == 200, equal_same.text
    assert equal_same.json()["payload"] == payload_200
    assert equal_same.json()["updated_at"] == updated_at_200

    equal_different = client.put(
        "/api/me/drafts/studio",
        headers=h,
        json={"payload": {"savedAt": 200, "workspaces": {"image": {"prompt": "conflict"}}}},
    )
    assert equal_different.status_code == 200, equal_different.text
    assert equal_different.json()["payload"] == payload_200
    assert equal_different.json()["updated_at"] == updated_at_200

    payload_300 = {"savedAt": 300, "workspaces": {"image": {"prompt": "three hundred"}}}
    newer = client.put("/api/me/drafts/studio", headers=h, json={"payload": payload_300})
    assert newer.status_code == 200, newer.text
    assert newer.json()["payload"] == payload_300

    db = SessionLocal()
    try:
        row = db.query(UserDraft).filter(UserDraft.user_id == uid, UserDraft.key == "studio").one()
        assert row.payload == payload_300
    finally:
        db.close()


def test_user_studio_draft_sanitizes_first_explicit_invalid_saved_at(client, make_user, auth):
    make_user("13900009115", balance=1000)
    h = auth("13900009115")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    invalid_values = [
        ("negative", -1),
        ("zero", 0),
        ("fractional", 1.5),
        ("unsafe_integer", 2**53),
        ("legacy_max", 253_402_300_799_999),
        ("far_future", now_ms + 2 * 24 * 60 * 60 * 1000),
        ("huge_float", 1e308),
    ]

    for label, invalid_saved_at in invalid_values:
        response = client.put(
            "/api/me/drafts/studio",
            headers=h,
            json={
                "payload": {
                    "savedAt": invalid_saved_at,
                    "workspaces": {"image": {"prompt": label}},
                }
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["payload"] == {
            "workspaces": {"image": {"prompt": label}}
        }

    valid_payload = {"savedAt": 500, "workspaces": {"image": {"prompt": "valid"}}}
    valid = client.put("/api/me/drafts/studio", headers=h, json={"payload": valid_payload})
    assert valid.status_code == 200, valid.text
    assert valid.json()["payload"] == valid_payload


def test_user_studio_draft_legacy_future_clock_can_be_replaced(client, make_user, auth):
    uid = make_user("13900009116", balance=1000)
    h = auth("13900009116")
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    legacy_clocks = [
        253_402_300_799_999,
        now_ms + 2 * 24 * 60 * 60 * 1000,
    ]

    for index, legacy_clock in enumerate(legacy_clocks):
        db = SessionLocal()
        try:
            row = (
                db.query(UserDraft)
                .filter(UserDraft.user_id == uid, UserDraft.key == "studio")
                .one_or_none()
            )
            legacy_payload = {
                "savedAt": legacy_clock,
                "workspaces": {"image": {"prompt": f"legacy {index}"}},
            }
            if row is None:
                row = UserDraft(
                    user_id=uid,
                    key="studio",
                    payload=legacy_payload,
                    updated_at=now,
                )
                db.add(row)
            else:
                row.payload = legacy_payload
                row.updated_at = now
            db.commit()
        finally:
            db.close()

        replacement = {
            "savedAt": now_ms + index,
            "workspaces": {"image": {"prompt": f"replacement {index}"}},
        }
        saved = client.put("/api/me/drafts/studio", headers=h, json={"payload": replacement})
        assert saved.status_code == 200, saved.text
        assert saved.json()["payload"] == replacement


def test_user_studio_draft_valid_future_boundary_does_not_block_current_save(
    client,
    make_user,
    auth,
):
    uid = make_user("13900009118", balance=1000)
    h = auth("13900009118")
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    boundary_payload = {
        "savedAt": now_ms + STUDIO_DRAFT_MAX_FUTURE_SKEW_MS,
        "workspaces": {"image": {"prompt": "future boundary"}},
    }

    db = SessionLocal()
    try:
        db.add(
            UserDraft(
                user_id=uid,
                key="studio",
                payload=boundary_payload,
                updated_at=now,
            )
        )
        db.commit()
    finally:
        db.close()

    replacement = {
        "savedAt": now_ms,
        "workspaces": {"image": {"prompt": "current save"}},
    }
    saved = client.put("/api/me/drafts/studio", headers=h, json={"payload": replacement})
    assert saved.status_code == 200, saved.text
    assert saved.json()["payload"] == replacement


def test_user_studio_draft_rejects_far_future_saved_at_after_valid_clock(
    client,
    make_user,
    auth,
):
    make_user("13900009117", balance=1000)
    h = auth("13900009117")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    valid_payload = {
        "savedAt": now_ms,
        "workspaces": {"image": {"prompt": "valid"}},
    }
    seeded = client.put("/api/me/drafts/studio", headers=h, json={"payload": valid_payload})
    assert seeded.status_code == 200, seeded.text

    for label, future_clock in [
        ("legacy_max", 253_402_300_799_999),
        ("far_future", now_ms + 2 * 24 * 60 * 60 * 1000),
    ]:
        future = client.put(
            "/api/me/drafts/studio",
            headers=h,
            json={
                "payload": {
                    "savedAt": future_clock,
                    "workspaces": {"image": {"prompt": label}},
                }
            },
        )
        assert future.status_code == 200, future.text
        assert future.json()["payload"] == valid_payload, label


def test_user_studio_draft_rejects_invalid_saved_at_after_valid_clock(client, make_user, auth):
    make_user("13900009113", balance=1000)
    h = auth("13900009113")
    invalid_requests = [
        ("missing", {"json": {"payload": {"workspaces": {"image": {"prompt": "missing"}}}}}),
        ("bool", {"json": {"payload": {"savedAt": True, "workspaces": {}}}}),
        ("string", {"json": {"payload": {"savedAt": "1200", "workspaces": {}}}}),
        ("null", {"json": {"payload": {"savedAt": None, "workspaces": {}}}}),
        ("array", {"json": {"payload": {"savedAt": [], "workspaces": {}}}}),
        ("object", {"json": {"payload": {"savedAt": {}, "workspaces": {}}}}),
        ("negative", {"json": {"payload": {"savedAt": -1, "workspaces": {}}}}),
        ("fractional", {"json": {"payload": {"savedAt": 1.5, "workspaces": {}}}}),
        ("unsafe_integer", {"json": {"payload": {"savedAt": 2**53, "workspaces": {}}}}),
        (
            "over_limit",
            {
                "json": {
                    "payload": {"savedAt": 253_402_300_800_000, "workspaces": {}}
                }
            },
        ),
        ("huge_float", {"json": {"payload": {"savedAt": 1e308, "workspaces": {}}}}),
        (
            "nan",
            {
                "content": '{"payload":{"savedAt":NaN,"workspaces":{"image":{"prompt":"nan"}}}}',
                "headers": {**h, "Content-Type": "application/json"},
            },
        ),
        (
            "infinity",
            {
                "content": '{"payload":{"savedAt":Infinity,"workspaces":{"image":{"prompt":"inf"}}}}',
                "headers": {**h, "Content-Type": "application/json"},
            },
        ),
    ]

    for index, (label, request_kwargs) in enumerate(invalid_requests, start=1):
        valid_payload = {
            "savedAt": 1_000 + index,
            "workspaces": {"image": {"prompt": f"valid before {label}"}},
        }
        seeded = client.put("/api/me/drafts/studio", headers=h, json={"payload": valid_payload})
        assert seeded.status_code == 200, seeded.text
        assert seeded.json()["payload"] == valid_payload

        if "headers" not in request_kwargs:
            request_kwargs["headers"] = h
        invalid = client.put("/api/me/drafts/studio", **request_kwargs)
        assert invalid.status_code == 200, invalid.text
        assert invalid.json()["payload"] == valid_payload, label


def test_user_studio_draft_concurrent_older_request_cannot_overwrite_newer(
    client,
    make_user,
    auth,
):
    uid = make_user("13900009114", balance=1000)
    h = auth("13900009114")
    initial = {"savedAt": 100, "workspaces": {"image": {"prompt": "initial"}}}
    seeded = client.put("/api/me/drafts/studio", headers=h, json={"payload": initial})
    assert seeded.status_code == 200, seeded.text

    newer_boundary = threading.Event()
    release_newer = threading.Event()
    older_boundary_attempted = threading.Event()
    older_draft_read = threading.Event()
    errors = []

    def observe_boundary_attempt(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        normalized = " ".join(statement.lower().split())
        if (
            threading.current_thread().name == "older-studio-draft-writer"
            and normalized.startswith("update users set id=users.id")
        ):
            older_boundary_attempted.set()

    def observe_serialization_boundary(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        normalized = " ".join(statement.lower().split())
        if (
            threading.current_thread().name == "newer-studio-draft-writer"
            and normalized.startswith("update users set id=users.id")
        ):
            newer_boundary.set()
            if not release_newer.wait(timeout=5):
                raise AssertionError("timed out waiting to release the newer draft request")
        if (
            threading.current_thread().name == "older-studio-draft-writer"
            and normalized.startswith("select")
            and "from user_drafts" in normalized
        ):
            older_draft_read.set()

    def write_in_independent_session(payload):
        db = SessionLocal()
        try:
            user = db.get(User, uid)
            save_draft("studio", UserDraftIn(payload=payload), db=db, user=user)
        except BaseException as exc:  # surface thread failures in the test thread
            errors.append(exc)
        finally:
            db.close()

    older_payload = {"savedAt": 200, "workspaces": {"image": {"prompt": "older"}}}
    newer_payload = {"savedAt": 300, "workspaces": {"image": {"prompt": "newer"}}}
    event.listen(engine, "before_cursor_execute", observe_boundary_attempt)
    event.listen(engine, "after_cursor_execute", observe_serialization_boundary)
    newer_thread = threading.Thread(
        target=write_in_independent_session,
        args=(newer_payload,),
        name="newer-studio-draft-writer",
    )
    older_thread = threading.Thread(
        target=write_in_independent_session,
        args=(older_payload,),
        name="older-studio-draft-writer",
    )
    try:
        newer_thread.start()
        assert newer_boundary.wait(timeout=5), "newer request did not acquire its serialization boundary"
        older_thread.start()
        assert older_boundary_attempted.wait(timeout=5), "older request did not attempt the boundary"
        older_crossed_boundary = older_draft_read.wait(timeout=0.5)
        release_newer.set()
        newer_thread.join(timeout=5)
        older_thread.join(timeout=5)
    finally:
        release_newer.set()
        newer_thread.join(timeout=5)
        older_thread.join(timeout=5)
        event.remove(engine, "before_cursor_execute", observe_boundary_attempt)
        event.remove(engine, "after_cursor_execute", observe_serialization_boundary)

    assert not newer_thread.is_alive(), "newer draft request did not finish"
    assert not older_thread.is_alive(), "older draft request did not finish"
    assert not older_crossed_boundary, "older request read the draft before the newer request committed"
    assert errors == []
    db = SessionLocal()
    try:
        row = db.query(UserDraft).filter(UserDraft.user_id == uid, UserDraft.key == "studio").one()
        assert row.payload == newer_payload
    finally:
        db.close()


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
