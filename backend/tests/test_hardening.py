"""Hardening: input validation on generate params + server-side reverse switch."""

import threading

import pytest
from sqlalchemy import event
from starlette.websockets import WebSocketDisconnect

from app.db import SessionLocal, engine
from app.models import CreditTransaction, GatewayCall, GenAsset, GenTask, ParseRecord, User
from app.services import gateway, generation, storage
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


def test_generate_rejects_invalid_subject_mode(client, make_user, auth):
    make_user("13900000143", balance=1000)
    h = auth("13900000143")
    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "portrait style transfer"},
        "params": {"n": 1, "size": "256x256", "subject_mode": "face_swap"},
    }, headers=h)
    assert r.status_code == 400
    assert "subject_mode" in r.text


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
    assert task["cost_frozen"] == 60
    assert task["cost_settled"] == 30
    assert client.get("/api/me", headers=h).json()["balance_credits"] == 970


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


def test_canceled_task_cannot_be_revived_by_success_claim(client, make_user):
    uid = make_user("13900000057", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="canceled",
            cost_frozen=10,
            prompt={"final_text": "canceled"},
            params={"n": 1, "size": "256x256"},
        )
        db.add(task)
        db.commit()
        tid = task.id

        assert generation.claim_terminal(db, tid, "succeeded", cost_settled=10) is False
        db.rollback()
        assert db.get(GenTask, tid).status == "canceled"
    finally:
        db.close()


def test_image_worker_does_not_revive_queued_task_canceled_during_claim(client, make_user, monkeypatch):
    from sqlalchemy.orm import Session as OrmSession

    uid = make_user("13900000059", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="queued",
            cost_frozen=10,
            prompt={"final_text": "cancel race"},
            params={"n": 1, "size": "256x256"},
        )
        db.add(task)
        db.commit()
        tid = task.id
    finally:
        db.close()

    original_execute = OrmSession.execute
    canceled = {"done": False}

    def cancel_before_claim(self, statement, *args, **kwargs):
        text = str(statement)
        if not canceled["done"] and "UPDATE gen_tasks SET status" in text and "status = :status_1" in text:
            canceled["done"] = True
            race_db = SessionLocal()
            try:
                race_task = race_db.get(GenTask, tid)
                race_task.status = "canceled"
                race_db.commit()
            finally:
                race_db.close()
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(OrmSession, "execute", cancel_before_claim)
    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("canceled task generated")),
    )

    generation.run_image_task(tid)
    db = SessionLocal()
    try:
        assert db.get(GenTask, tid).status == "canceled"
    finally:
        db.close()


def test_image_worker_honors_cancel_before_settlement_after_save(client, make_user, monkeypatch):
    uid = make_user("13900000058", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="queued",
            cost_frozen=10,
            prompt={"final_text": "cancel after image return"},
            params={"n": 1, "size": "256x256"},
        )
        db.add(task)
        db.flush()
        from app.services import credits

        credits.freeze(db, uid, 10, biz_ref=task.id, commit=False)
        db.commit()
        tid = task.id
    finally:
        db.close()

    original_save_bytes = storage.save_bytes
    canceled = {"done": False}

    def save_and_cancel_once(*args, **kwargs):
        key = original_save_bytes(*args, **kwargs)
        if not canceled["done"]:
            canceled["done"] = True
            race_db = SessionLocal()
            try:
                race_task = race_db.get(GenTask, tid)
                race_task.params = {**(race_task.params or {}), "_cancel_requested": True}
                race_db.commit()
            finally:
                race_db.close()
        return key

    monkeypatch.setattr(storage, "save_bytes", save_and_cancel_once)

    generation.run_image_task(tid)
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assert task.status == "canceled"
        assert task.cost_settled == 0
        assert user.balance_credits == 1000
        assert user.frozen_credits == 0
    finally:
        db.close()


def test_image_terminal_tx_cancel_wins_after_files_saved(client, make_user, monkeypatch):
    from app.services import credits
    from app.services.generation_media import EditMaskResult

    uid = make_user("13900000458", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            source_asset_url="http://example.com/product.png",
            source_type="image",
            category="image",
            stage="preview",
            status="queued",
            cost_frozen=10,
            prompt={"final_text": "pixel lock cancel race", "instruction": "replace background"},
            params={
                "n": 1,
                "size": "256x256",
                "subject_mode": "product",
                "edit_mask_mode": "protect_subject",
                "product_pixel_lock": "strict",
            },
        )
        db.add(task)
        db.flush()
        credits.freeze(db, uid, 10, biz_ref=task.id, commit=False)
        db.commit()
        tid = task.id
    finally:
        db.close()

    monkeypatch.setattr(
        "app.services.generation_image_flow.gateway_reference_image",
        lambda *_args, **_kwargs: "data:image/png;base64,cmVm",
    )
    monkeypatch.setattr(
        "app.services.generation_image_flow.gateway_image_edit_mask",
        lambda *_args, **_kwargs: EditMaskResult(
            data_uri="data:image/png;base64,bWFzaw==",
            mode="alpha_subject",
            confidence=1.0,
            bbox=(0, 0, 31, 31),
            width=32,
            height=32,
            reason="test",
        ),
    )
    monkeypatch.setattr(
        "app.services.generation_image_flow.composite_product_subject_pixels",
        lambda _db, _task, raw, *_args, **_kwargs: (
            raw,
            {"_product_composite_placement": "test"},
        ),
    )
    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda *_args, **_kwargs: [gateway._mock_image("race", "256x256", 0)],
    )

    files_saved = threading.Event()
    cancel_committed = threading.Event()
    cancel_errors: list[BaseException] = []
    written_keys: list[str] = []
    original_save_bytes = storage.save_bytes
    original_save_bytes_named = storage.save_bytes_named

    def request_cancel_after_all_files() -> None:
        try:
            assert files_saved.wait(5), "image files were not all saved"
            race_db = SessionLocal()
            try:
                race_task = race_db.get(GenTask, tid)
                race_task.params = {**(race_task.params or {}), "_cancel_requested": True}
                race_db.commit()
            finally:
                race_db.close()
        except BaseException as exc:  # noqa: BLE001
            cancel_errors.append(exc)
        finally:
            cancel_committed.set()

    cancel_thread = threading.Thread(target=request_cancel_after_all_files, daemon=True)
    cancel_thread.start()

    def track_save_bytes(*args, **kwargs):
        key = original_save_bytes(*args, **kwargs)
        written_keys.append(key)
        return key

    def save_model_ref_then_cancel(*args, **kwargs):
        key = original_save_bytes_named(*args, **kwargs)
        written_keys.append(key)
        files_saved.set()
        assert cancel_committed.wait(5), "cancel transaction did not commit"
        return key

    monkeypatch.setattr(storage, "save_bytes", track_save_bytes)
    monkeypatch.setattr(storage, "save_bytes_named", save_model_ref_then_cancel)

    generation.run_image_task(tid)
    cancel_thread.join(timeout=5)

    assert not cancel_thread.is_alive()
    assert cancel_errors == []
    assert len(written_keys) == 3
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assets = db.query(GenAsset).filter(GenAsset.task_id == tid).all()
        settlements = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.biz_type == "gen_task",
                CreditTransaction.biz_ref == tid,
                CreditTransaction.type == "settle",
            )
            .all()
        )
        assert task.status == "canceled"
        assert task.cost_settled == 0
        assert user.balance_credits == 1000
        assert user.frozen_credits == 0
        assert assets == []
        assert settlements == []
        assert all(not storage.local_path(key).exists() for key in written_keys)
    finally:
        db.close()


def test_image_terminal_tx_blocks_running_cancel_and_commits_once(
    client,
    make_user,
    monkeypatch,
):
    from fastapi import HTTPException

    from app.routers.tasks import cancel_task
    from app.services import credits

    uid = make_user("13900000459", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="queued",
            cost_frozen=10,
            prompt={"final_text": "terminal transaction wins"},
            params={"n": 1, "size": "256x256"},
        )
        db.add(task)
        db.flush()
        credits.freeze(db, uid, 10, biz_ref=task.id, commit=False)
        db.commit()
        tid = task.id
    finally:
        db.close()

    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda *_args, **_kwargs: [gateway._mock_image("terminal", "256x256", 0)],
    )

    cancel_sql_started = threading.Event()
    terminal_boundary_acquired = threading.Event()
    cancel_thread_ident: dict[str, int | None] = {"value": None}
    cancel_outcome: list[int] = []
    cancel_errors: list[BaseException] = []
    worker_thread_ident = threading.get_ident()

    def observe_cancel_update(_conn, _cursor, statement, _parameters, _context, _executemany):
        if (
            threading.get_ident() == cancel_thread_ident["value"]
            and statement.lstrip().upper().startswith("UPDATE GEN_TASKS")
        ):
            cancel_sql_started.set()

    def launch_cancel_after_terminal_boundary(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        normalized = " ".join(statement.lower().split())
        if (
            threading.get_ident() == worker_thread_ident
            and "update gen_tasks set status=gen_tasks.status" in normalized
            and not terminal_boundary_acquired.is_set()
        ):
            terminal_boundary_acquired.set()
            cancel_thread.start()
            assert cancel_sql_started.wait(5), "running cancel did not attempt its UPDATE"

    def attempt_running_cancel() -> None:
        cancel_thread_ident["value"] = threading.get_ident()
        cancel_db = SessionLocal()
        try:
            cancel_user = cancel_db.get(User, uid)
            try:
                result = cancel_task(tid, db=cancel_db, user=cancel_user)
            except HTTPException as exc:
                cancel_outcome.append(exc.status_code)
            else:
                cancel_outcome.append(200 if result is not None else 204)
        except BaseException as exc:  # noqa: BLE001
            cancel_errors.append(exc)
        finally:
            cancel_db.close()

    cancel_thread = threading.Thread(target=attempt_running_cancel, daemon=True)
    settle_calls = 0
    original_settle = credits.settle

    def settle_after_cancel_update_starts(*args, **kwargs):
        nonlocal settle_calls
        settle_calls += 1
        assert terminal_boundary_acquired.is_set()
        return original_settle(*args, **kwargs)

    event.listen(engine, "before_cursor_execute", observe_cancel_update)
    event.listen(engine, "after_cursor_execute", launch_cancel_after_terminal_boundary)
    monkeypatch.setattr(credits, "settle", settle_after_cancel_update_starts)
    try:
        generation.run_image_task(tid)
        if terminal_boundary_acquired.is_set():
            cancel_thread.join(timeout=5)
    finally:
        event.remove(engine, "after_cursor_execute", launch_cancel_after_terminal_boundary)
        event.remove(engine, "before_cursor_execute", observe_cancel_update)

    assert terminal_boundary_acquired.is_set()
    assert not cancel_thread.is_alive()
    assert cancel_errors == []
    assert cancel_outcome == [409]
    assert settle_calls == 1
    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        assets = db.query(GenAsset).filter(GenAsset.task_id == tid).all()
        settlements = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.biz_type == "gen_task",
                CreditTransaction.biz_ref == tid,
                CreditTransaction.type == "settle",
            )
            .all()
        )
        assert task.status == "succeeded"
        assert not (task.params or {}).get("_cancel_requested")
        assert task.error is None
        assert len(assets) == 1
        assert len(settlements) == 1
    finally:
        db.close()


def test_image_settlement_failure_preserves_recoverable_assets(client, make_user, monkeypatch):
    from app.services import credits

    uid = make_user("13900000460", balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            status="queued",
            cost_frozen=10,
            prompt={"final_text": "recoverable settlement failure"},
            params={"n": 1, "size": "256x256"},
        )
        db.add(task)
        db.flush()
        credits.freeze(db, uid, 10, biz_ref=task.id, commit=False)
        db.commit()
        tid = task.id
    finally:
        db.close()

    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda *_args, **_kwargs: [gateway._mock_image("recover", "256x256", 0)],
    )
    monkeypatch.setattr(
        credits,
        "settle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("settle exploded")),
    )

    generation.run_image_task(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assets = db.query(GenAsset).filter(GenAsset.task_id == tid).all()
        settlements = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.biz_type == "gen_task",
                CreditTransaction.biz_ref == tid,
                CreditTransaction.type == "settle",
            )
            .all()
        )
        result_keys = list((task.params or {}).get("_image_result_keys") or [])
        assert task.status == "needs_review"
        assert task.phase == "reconciling"
        assert task.cost_settled == 0
        assert "settle exploded" in (task.error or "")
        assert task.params["_saved_n"] == 1
        assert len(result_keys) == 3
        assert {key.split("/", 1)[0] for key in result_keys} == {"hd", "preview", "model_ref"}
        assert all(storage.local_path(key).exists() for key in result_keys)
        assert len(assets) == 1
        assert assets[0].hd_url == storage.public_url(next(key for key in result_keys if key.startswith("hd/")))
        assert assets[0].preview_url == storage.public_url(
            next(key for key in result_keys if key.startswith("preview/"))
        )
        assert settlements == []
        assert user.balance_credits == 990
        assert user.frozen_credits == 10
    finally:
        db.close()


_PIXEL_LOCK_RECOVERY_META = {
    "_product_composite_placement": "bbox_match",
    "_product_composite_source_bbox": [2, 3, 30, 31],
    "_product_composite_target_bbox": [4, 5, 28, 29],
    "_product_composite_target_confidence": 0.93,
}


def _create_frozen_pixel_lock_task(make_user, phone: str, *, n: int, frozen: int) -> tuple[int, int]:
    from app.services import credits

    uid = make_user(phone, balance=1000)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            source_asset_url="http://example.com/product.png",
            source_type="image",
            category="image",
            stage="preview",
            status="queued",
            cost_frozen=frozen,
            prompt={"final_text": "pixel lock recovery", "instruction": "replace background"},
            params={
                "n": n,
                "size": "256x256",
                "subject_mode": "product",
                "edit_mask_mode": "protect_subject",
                "product_pixel_lock": "strict",
            },
        )
        db.add(task)
        db.flush()
        credits.freeze(db, uid, frozen, biz_ref=task.id, commit=False)
        db.commit()
        return uid, task.id
    finally:
        db.close()


def _mock_pixel_lock_generation(monkeypatch, images) -> None:
    from app.services.generation_media import EditMaskResult

    monkeypatch.setattr(
        "app.services.generation_image_flow.gateway_reference_image",
        lambda *_args, **_kwargs: "data:image/png;base64,cmVm",
    )
    monkeypatch.setattr(
        "app.services.generation_image_flow.gateway_image_edit_mask",
        lambda *_args, **_kwargs: EditMaskResult(
            data_uri="data:image/png;base64,bWFzaw==",
            mode="alpha_subject",
            confidence=1.0,
            bbox=(0, 0, 31, 31),
            width=32,
            height=32,
            reason="test",
        ),
    )
    monkeypatch.setattr(
        "app.services.generation_image_flow.composite_product_subject_pixels",
        lambda _db, _task, raw, *_args, **_kwargs: (
            raw,
            dict(_PIXEL_LOCK_RECOVERY_META),
        ),
    )
    monkeypatch.setattr("app.services.gateway.gen_image", lambda *_args, **_kwargs: images)


def _assert_pixel_lock_recovery(task: GenTask, assets: list[GenAsset]) -> None:
    result_keys = list((task.params or {}).get("_image_result_keys") or [])
    assert task.status == "needs_review"
    assert task.phase == "reconciling"
    assert task.params["_product_pixel_lock"] == "strict"
    assert task.params["_product_composite_applied_count"] == 1
    for key, value in _PIXEL_LOCK_RECOVERY_META.items():
        assert task.params[key] == value
    assert len(result_keys) == 3
    assert {key.split("/", 1)[0] for key in result_keys} == {"hd", "preview", "model_ref"}
    assert all(storage.local_path(key).exists() for key in result_keys)
    assert len(assets) == 1
    assert assets[0].hd_url == storage.public_url(next(key for key in result_keys if key.startswith("hd/")))
    assert assets[0].preview_url == storage.public_url(
        next(key for key in result_keys if key.startswith("preview/"))
    )


def test_pixel_lock_unknown_submit_preserves_recovery_metadata(client, make_user, monkeypatch):
    uid, tid = _create_frozen_pixel_lock_task(
        make_user,
        "13900000461",
        n=2,
        frozen=20,
    )
    _mock_pixel_lock_generation(
        monkeypatch,
        gateway.ImageBatchResult(
            [gateway._mock_image("unknown pixel lock", "256x256", 0)],
            failures=[
                gateway.ImageSubrequestFailure(
                    index=1,
                    message="read timed out",
                    submit_state_unknown=True,
                    retryable_refill=False,
                )
            ],
        ),
    )

    generation.run_image_task(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assets = db.query(GenAsset).filter(GenAsset.task_id == tid).all()
        _assert_pixel_lock_recovery(task, assets)
        assert task.params["_image_submit_state_unknown"] is True
        assert task.params["_requested_n"] == 2
        assert task.params["_saved_n"] == 1
        assert user.balance_credits == 980
        assert user.frozen_credits == 20
    finally:
        db.close()


def test_pixel_lock_settlement_failure_preserves_recovery_metadata(
    client,
    make_user,
    monkeypatch,
):
    from app.services import credits

    uid, tid = _create_frozen_pixel_lock_task(
        make_user,
        "13900000462",
        n=1,
        frozen=10,
    )
    _mock_pixel_lock_generation(
        monkeypatch,
        [gateway._mock_image("settlement pixel lock", "256x256", 0)],
    )
    monkeypatch.setattr(
        credits,
        "settle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("settle exploded")),
    )

    generation.run_image_task(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assets = db.query(GenAsset).filter(GenAsset.task_id == tid).all()
        _assert_pixel_lock_recovery(task, assets)
        assert task.params["_saved_n"] == 1
        assert "settle exploded" in (task.error or "")
        assert user.balance_credits == 990
        assert user.frozen_credits == 10
    finally:
        db.close()


@pytest.mark.parametrize(
    "failure_point",
    ["acquire_image_terminal_boundary", "claim_terminal"],
)
def test_image_terminal_persistence_error_preserves_recoverable_result(
    client,
    make_user,
    monkeypatch,
    failure_point,
):
    from sqlalchemy.exc import OperationalError

    uid, tid = _create_frozen_pixel_lock_task(
        make_user,
        f"1390000046{3 if failure_point == 'acquire_image_terminal_boundary' else 4}",
        n=1,
        frozen=10,
    )
    _mock_pixel_lock_generation(
        monkeypatch,
        [gateway._mock_image(failure_point, "256x256", 0)],
    )

    def raise_database_error(*_args, **_kwargs):
        raise OperationalError("UPDATE gen_tasks", {}, RuntimeError(f"{failure_point} failed"))

    monkeypatch.setattr(
        f"app.services.generation_image_flow.{failure_point}",
        raise_database_error,
    )

    generation.run_image_task(tid)

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        user = db.get(User, uid)
        assets = db.query(GenAsset).filter(GenAsset.task_id == tid).all()
        settlements = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.biz_type == "gen_task",
                CreditTransaction.biz_ref == tid,
                CreditTransaction.type == "settle",
            )
            .all()
        )
        _assert_pixel_lock_recovery(task, assets)
        assert task.cost_settled == 0
        assert failure_point in (task.error or "")
        assert settlements == []
        assert user.balance_credits == 990
        assert user.frozen_credits == 10
    finally:
        db.close()


def test_partial_image_generation_exposes_gateway_slot_failure(client, make_user, auth, monkeypatch):
    make_user("13900000049", balance=1000)
    h = auth("13900000049")

    def partial_with_gateway_failure(prompt, model_id, n=4, size="1024x1024", **_kwargs):
        return gateway.ImageBatchResult(
            [gateway._mock_image(prompt, "256x256", 0), gateway._mock_image(prompt, "256x256", 1)],
            failures=[
                gateway.ImageSubrequestFailure(
                    index=3,
                    message="temporary account unavailable",
                    submit_state_unknown=False,
                    retryable_refill=True,
                )
            ],
        )

    monkeypatch.setattr("app.services.gateway.gen_image", partial_with_gateway_failure)

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "partial"},
        "params": {"n": 4, "size": "256x256"},
    }, headers=h)

    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded"
    assert task["partial"] is True
    assert task["requested_count"] == 4
    assert task["saved_count"] == 2
    assert task["skipped_count"] == 2
    assert task["partial_errors"] == ["temporary account unavailable"]
    assert task["cost_frozen"] == 60
    assert task["cost_settled"] == 30


def test_partial_image_generation_unknown_slot_returns_saved_images(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13900000050", balance=1000)
    h = auth("13900000050")

    def partial_with_unknown_failure(prompt, model_id, n=4, size="1024x1024", **_kwargs):
        return gateway.ImageBatchResult(
            [gateway._mock_image(prompt, "256x256", 0), gateway._mock_image(prompt, "256x256", 1)],
            failures=[
                gateway.ImageSubrequestFailure(
                    index=3,
                    message="read timed out",
                    submit_state_unknown=True,
                    retryable_refill=False,
                )
            ],
        )

    monkeypatch.setattr("app.services.gateway.gen_image", partial_with_unknown_failure)

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "partial unknown"},
        "params": {"n": 4, "size": "256x256"},
    }, headers=h)

    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "needs_review"
    assert task["partial"] is True
    assert task["requested_count"] == 4
    assert task["saved_count"] == 2
    assert task["skipped_count"] == 2
    assert task["partial_errors"] == ["read timed out"]
    assert len(task["assets"]) == 2
    assert task["cost_frozen"] == 60
    assert task["cost_settled"] == 0
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 940
    assert me["frozen_credits"] == 60

    db = SessionLocal()
    try:
        db_task = db.get(GenTask, r.json()["id"])
        assert db_task.params["_image_submit_state_unknown"] is True
        assert db_task.params["_requested_n"] == 4
        assert db_task.params["_saved_n"] == 2
        assert db_task.params["_image_result_keys"]
        assert db_task.status == "needs_review"
    finally:
        db.close()


def test_full_image_generation_unknown_submit_holds_for_review(
    client,
    make_user,
    auth,
    monkeypatch,
):
    phone = "13900000352"
    make_user(phone, balance=1000)
    h = auth(phone)

    def all_unknown_failure(*_args, **_kwargs):
        raise gateway.GatewayError(
            "图像网关未返回任何结果: read timed out",
            transient=True,
            submit_state_unknown=True,
        )

    monkeypatch.setattr("app.services.gateway.gen_image", all_unknown_failure)

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "full unknown"},
        "params": {"n": 4, "size": "256x256"},
    }, headers=h)

    assert r.status_code == 200, r.text
    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "needs_review"
    assert task["partial"] is True
    assert task["requested_count"] == 4
    assert task["saved_count"] == 0
    assert task["skipped_count"] == 4
    assert task["cost_frozen"] == 60
    assert task["cost_settled"] == 0
    assert len(task["assets"]) == 0
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 940
    assert me["frozen_credits"] == 60

    db = SessionLocal()
    try:
        db_task = db.get(GenTask, r.json()["id"])
        assert db_task.params["_image_submit_state_unknown"] is True
        assert db_task.params["_requested_n"] == 4
        assert db_task.params["_saved_n"] == 0
        assert "_image_result_keys" not in db_task.params
    finally:
        db.close()


def test_review_task_list_marks_image_local_results_availability(client, make_user, auth):
    make_user("13900001951", balance=1000, admin=True)
    h = auth("13900001951")
    db = SessionLocal()
    try:
        hd_key = storage.save_bytes(gateway._mock_image("held", "256x256", 0), "hd", "png")
        preview_key = storage.save_bytes(gateway._mock_image("held", "64x64", 0), "preview", "png")
        ready = GenTask(
            user_id=db.query(User).filter(User.phone == "13900001951").one().id,
            category="image",
            stage="preview",
            status=generation.NEEDS_REVIEW,
            cost_frozen=10,
            prompt={"final_text": "ready"},
            params={"_image_result_keys": [hd_key, preview_key]},
        )
        missing = GenTask(
            user_id=ready.user_id,
            category="image",
            stage="preview",
            status=generation.NEEDS_REVIEW,
            cost_frozen=10,
            prompt={"final_text": "missing"},
            params={"_image_result_keys": ["hd/missing.png", preview_key]},
        )
        db.add_all([ready, missing])
        db.commit()
        ready_id = ready.id
        missing_id = missing.id
    finally:
        db.close()

    rows = client.get("/api/admin/tasks/review", headers=h).json()
    by_id = {row["id"]: row for row in rows}
    assert by_id[ready_id]["has_local_results"] is True
    assert by_id[missing_id]["has_local_results"] is False


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


def test_websocket_ticket_rate_limit(client, make_user, auth, monkeypatch):
    uid = make_user("13900000943", balance=1000)
    h = auth("13900000943")
    monkeypatch.setattr("app.routers.tasks.settings.ws_ticket_rate_per_minute", 1)

    db = SessionLocal()
    try:
        task = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            prompt={"final_text": "x"},
            model_use="image",
            params={"n": 1, "size": "256x256"},
            status="running",
            cost_frozen=5,
            cost_settled=0,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    assert client.post(f"/api/tasks/{task_id}/ws-ticket", headers=h).status_code == 200
    limited = client.post(f"/api/tasks/{task_id}/ws-ticket", headers=h)
    assert limited.status_code == 429
    assert "WebSocket" in limited.text


def test_websocket_connect_rate_limit(client, make_user, auth, monkeypatch):
    uid = make_user("13900000944", balance=1000)
    h = auth("13900000944")
    monkeypatch.setattr("app.routers.tasks.settings.ws_ticket_rate_per_minute", 10)
    monkeypatch.setattr("app.routers.ws.settings.ws_connect_rate_per_minute", 1)

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

    first_ticket = client.post(f"/api/tasks/{task_id}/ws-ticket", headers=h).json()["ticket"]
    second_ticket = client.post(f"/api/tasks/{task_id}/ws-ticket", headers=h).json()["ticket"]
    with client.websocket_connect(f"/ws/tasks/{task_id}?ticket={first_ticket}") as ws:
        assert ws.receive_json()["status"] == "succeeded"

    try:
        with client.websocket_connect(f"/ws/tasks/{task_id}?ticket={second_ticket}") as ws:
            ws.receive_json()
        connected = True
    except Exception:
        connected = False
    assert not connected


def test_event_websocket_drops_event_returned_during_token_revocation(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13900000945", balance=1000)
    headers = auth("13900000945")
    ticket = client.post("/api/events/ws-ticket", headers=headers).json()["ticket"]
    reads = 0
    observed_block_ms = []

    def revoke_while_reading(_user_id, _last_id, *, block_ms, count):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise WebSocketDisconnect()
        observed_block_ms.append(block_ms)
        assert count == 20
        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            user.token_version += 1
            db.commit()
        finally:
            db.close()
        return "1-0", [{"id": "1-0", "type": "secret", "payload": {"value": 2}}]

    monkeypatch.setattr("app.routers.ws.read_user_events", revoke_while_reading)

    with client.websocket_connect(f"/ws/events?ticket={ticket}") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == 4401
    assert max(observed_block_ms) <= 5000


def test_task_websocket_rechecks_revocation_before_next_status_frame(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13900000946", balance=1000)
    headers = auth("13900000946")
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            prompt={"final_text": "x"},
            model_use="image",
            params={"n": 1, "size": "256x256"},
            status="running",
            cost_frozen=5,
            cost_settled=0,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    set_progress(task_id, 50, "running")
    waits = 0

    def revoke_before_third_frame(_task_id, _last_id, *, block_ms):
        nonlocal waits
        waits += 1
        assert block_ms == 5000
        if waits == 2:
            revoke_db = SessionLocal()
            try:
                user = revoke_db.get(User, user_id)
                user.token_version += 1
                revoke_db.commit()
            finally:
                revoke_db.close()
        if waits > 2:
            raise WebSocketDisconnect()
        return f"{waits}-0", {"percent": 50, "status": "running"}

    monkeypatch.setattr("app.routers.ws.wait_progress_event", revoke_before_third_frame)
    ticket = client.post(f"/api/tasks/{task_id}/ws-ticket", headers=headers).json()["ticket"]

    with client.websocket_connect(f"/ws/tasks/{task_id}?ticket={ticket}") as ws:
        assert ws.receive_json()["status"] == "running"
        assert ws.receive_json()["status"] == "running"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == 4401


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


def test_generate_accepts_2k_size(client, make_user, auth, monkeypatch):
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
        "params": {"n": 1, "size": "2048x2048"},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert seen["size"] == "2048x2048"


def test_generate_respects_lower_max_image_dim_area(client, make_user, auth, monkeypatch):
    make_user("13900000034", balance=1000)
    h = auth("13900000034")
    monkeypatch.setattr("app.routers.generate.settings.max_image_dim", 2048)

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "too many pixels for a 2k-limited deployment"},
        "params": {"n": 1, "size": "2048x2560"},
    }, headers=h)

    assert r.status_code == 400
    assert "总像素不超过" in r.text


def test_2k_generation_keeps_gateway_actual_result(client, make_user, auth, monkeypatch):
    make_user("13900000239", balance=1000)
    h = auth("13900000239")

    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda prompt, _model, n=1, size="2048x2048", **_kwargs: [
            gateway._mock_image(prompt, "1024x1024", 0)
        ],
    )

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "2k request accepts actual gateway result"},
        "params": {"n": 1, "size": "2048x2048"},
    }, headers=h)
    assert r.status_code == 200, r.text

    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded"
    assert task["assets"][0]["width"] == 1024
    assert task["assets"][0]["height"] == 1024


def test_admin_settings_respects_lower_max_image_dim_area(client, make_user, auth, monkeypatch):
    make_user("13900000035", balance=1000, admin=True)
    h = auth("13900000035")
    monkeypatch.setattr("app.routers.admin.app_config.max_image_dim", 2048)

    r = client.put("/api/admin/settings", json={
        "image_size": "2048x2560",
        "admin_password": "pass123456",
    }, headers=h)

    assert r.status_code == 400
    assert "总像素不超过" in r.text


def test_4k_generation_keeps_actual_gateway_result(client, make_user, auth, monkeypatch):
    make_user("13900000032", balance=1000)
    h = auth("13900000032")
    monkeypatch.setattr("app.routers.generate.settings.max_image_dim", 3840)

    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda prompt, _model, n=1, size="2880x2880", **_kwargs: [
            gateway._mock_image(prompt, "1024x1024", 0)
        ],
    )

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "4k request but gateway downscales"},
        "params": {"n": 1, "size": "2880x2880"},
    }, headers=h)
    assert r.status_code == 200, r.text

    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded"
    assert task["assets"][0]["width"] == 1024
    assert task["assets"][0]["height"] == 1024
    assert task["assets"][0]["quality_status"] == "ok"
    assert task["assets"][0]["preview_url"]
    db = SessionLocal()
    try:
        call = (
            db.query(GatewayCall)
            .filter(GatewayCall.task_id == r.json()["id"], GatewayCall.kind == "image")
            .order_by(GatewayCall.id.desc())
            .first()
        )
        assert call is not None
        assert call.status == "ok"
        assert call.detail["size"] == "2880x2880"
        assert call.detail["returned_sizes"] == ["1024x1024"]
        assert call.detail["saved_n"] == 1
    finally:
        db.close()


def test_4k_generation_saves_gateway_auto_downgrade_result(client, make_user, auth, monkeypatch):
    make_user("13900009039", balance=1000)
    h = auth("13900009039")
    monkeypatch.setattr("app.routers.generate.settings.max_image_dim", 3840)

    def fake_gen_image(prompt, _model, n=1, size="2880x2880", **_kwargs):
        result = gateway.ImageBatchResult(
            [gateway._mock_image(prompt, "1024x1024", 0)],
            diagnostics=[
                gateway.ImageResponseDiagnostic(
                    model="gpt-image-2-codex",
                    size="auto",
                    quality="auto",
                    output_format="jpeg",
                    selected_source="b64_json",
                )
            ],
        )
        return result

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "4k request but gateway auto-downgrades"},
        "params": {"n": 1, "size": "2880x2880"},
    }, headers=h)
    assert r.status_code == 200, r.text

    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded"
    assert task["error"] is None
    assert task["assets"][0]["width"] == 1024
    assert task["assets"][0]["height"] == 1024
    db = SessionLocal()
    try:
        call = (
            db.query(GatewayCall)
            .filter(GatewayCall.task_id == r.json()["id"], GatewayCall.kind == "image")
            .order_by(GatewayCall.id.desc())
            .first()
        )
        assert call is not None
        assert call.detail["gateway_echo_sizes"] == ["auto"]
        assert call.detail["gateway_echo_qualities"] == ["auto"]
        assert call.detail["gateway_echo_models"] == ["gpt-image-2-codex"]
        assert call.detail["gateway_selected_sources"] == ["b64_json"]
    finally:
        db.close()


def test_4k_generation_keeps_gateway_full_resolution_result(client, make_user, auth, monkeypatch):
    make_user("13900000033", balance=1000)
    h = auth("13900000033")
    monkeypatch.setattr("app.routers.generate.settings.max_image_dim", 3840)

    monkeypatch.setattr(
        "app.services.gateway.gen_image",
        lambda prompt, _model, n=1, size="2880x2880", **_kwargs: [
            gateway._mock_image(prompt, "2880x2880", 0)
        ],
    )

    r = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "near full 4k"},
        "params": {"n": 1, "size": "2880x2880"},
    }, headers=h)
    assert r.status_code == 200, r.text

    task = client.get(f"/api/tasks/{r.json()['id']}", headers=h).json()
    assert task["status"] == "succeeded"
    assert task["assets"][0]["width"] == 2880
    assert task["assets"][0]["height"] == 2880


def test_reverse_portrait_prompt_is_compacted_for_image_gateway(client, make_user, auth, monkeypatch):
    make_user("13900000031", balance=1000)
    h = auth("13900000031")
    seen = {}

    def fake_gen_image(prompt, image_model_id, n=4, size="1024x1024",
                       reference_image_url=None, edit_path=None, extra_payload=None,
                       **_kwargs):
        seen["prompt"] = prompt
        from app.services.gateway import _mock_image
        return [_mock_image(prompt, "256x256", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)
    noisy = "；".join(
        [
            "人物图，参考图复刻",
            "主体在画面 52% 位置，肩宽 38%，腰线 44%，腿长 61%",
            "身材曲线: 肩颈、胸腰臀、腰臀比明显",
            "尺码三围: S/M，胸围/腰围/臀围比例可见",
            "露肤度: 高露肤，低胸，腿部可见",
            "构图为竖版中景，柔和侧逆光，小红书写真风格",
        ]
        * 80
    )

    r = client.post("/api/generate", json={
        "source_asset_url": "http://x/y.png",
        "source_type": "image",
        "source_asset_meta": {"subject_mode": "portrait"},
        "category": "image",
        "stage": "preview",
        "prompt": {
            "图像类型": "人物图",
            "主体": "单人半身人像，面向镜头",
            "人物比例": "自然头身比，姿态稳定",
            "身材体态": "站姿自然，肩颈舒展",
            "身材曲线": "胸腰臀曲线明显",
            "尺码三围": "S/M，胸围/腰围/臀围比例",
            "露肤度": "高露肤，低胸",
            "妆发五官": "黑色长发，自然妆容，五官清晰",
            "构图": "主体居中 52%，留白 18%",
            "光线": "左侧柔光，暖色调",
            "风格": "小红书商业写真",
            "final_text": noisy,
        },
        "params": {"n": 1, "size": "1024x1024", "subject_mode": "portrait"},
    }, headers=h)
    assert r.status_code == 200, r.text
    sent = seen["prompt"]
    assert len(sent) <= 1500
    assert "生成版提示词" in sent
    assert "主体" in sent and "构图" in sent and "光线" in sent and "风格" in sent
    assert "三围" not in sent
    assert "胸围" not in sent
    assert "低胸" not in sent
    assert "%" not in sent
    db = SessionLocal()
    try:
        stored = db.get(GenTask, r.json()["id"])
        assert stored.prompt["final_text"] == noisy
        assert "低胸" in stored.prompt["final_text"]
    finally:
        db.close()


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


def test_generate_accepts_max_15_second_video_duration(client, make_user, auth):
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
        "params": {"duration": 15, "resolution": "480p", "ratio": "9:16"},
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


def test_content_safety_does_not_block_configured_word_inside_larger_latin_word(client, make_user, auth):
    make_user("13900000252", balance=1000, admin=True)
    h = auth("13900000252")
    s = client.put(
        "/api/admin/settings",
        json={
            "content_safety_enabled": True,
            "content_safety_banned_terms": "sex",
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert s.status_code == 200, s.text

    allowed = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "a travel poster for Sussex cliffs"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert allowed.status_code == 200, allowed.text

    blocked = client.post("/api/generate", json={
        "category": "image",
        "stage": "preview",
        "prompt": {"final_text": "a poster containing sex as a standalone banned token"},
        "params": {"n": 1, "size": "256x256"},
    }, headers=h)
    assert blocked.status_code == 400
    assert "内容安全拦截" in blocked.text

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
