"""Durable generation dispatch and broker-ambiguity coverage."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kombu.exceptions import OperationalError

from app.db import SessionLocal
from app.models import CreditTransaction, GenerationDispatch, GenTask, User
from app.services import generation_dispatch
from app.services.generation_dispatch import (
    MAX_PUBLISH_ATTEMPTS,
    PrePublishError,
    publish_dispatch,
    reconcile_dispatches,
)


def _payload(*, request_id: str) -> dict:
    return {
        "client_request_id": request_id,
        "category": "image",
        "stage": "preview",
        "instruction": "durable dispatch test image",
        "params": {"n": 1, "size": "1024x1024"},
    }


def _latest_dispatch(task_id: int) -> GenerationDispatch:
    with SessionLocal() as db:
        row = (
            db.query(GenerationDispatch)
            .filter_by(task_id=task_id)
            .order_by(GenerationDispatch.attempt.desc())
            .first()
        )
        assert row is not None
        db.expunge(row)
        return row


def test_normal_publish_records_deterministic_dispatch(
    client, make_user, auth, quote_and_generate
):
    make_user("13999070001", balance=100)
    response = quote_and_generate(
        _payload(request_id="dispatch-normal-001"),
        headers=auth("13999070001"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dispatch_status"] == "completed"
    assert body["dispatch_attempt"] == 1
    assert body["dispatch_publish_attempts"] == 1
    assert body["dispatch_reconciliation_required"] is False
    assert body["dispatch_task_id"].startswith(
        f"generation-{body['id']}-attempt-1-dispatch-"
    )


def test_definite_pre_accept_failure_refunds_once(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    user_id = make_user("13999070002", balance=100)
    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PrePublishError("rejected before broker publish")
        ),
    )
    response = quote_and_generate(
        _payload(request_id="dispatch-rejected-001"),
        headers=auth("13999070002"),
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "GENERATION_DISPATCH_REJECTED"
    task_id = int(response.json()["detail"]["task_id"])
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        user = db.get(User, user_id)
        assert task.status == "failed"
        assert user.balance_credits == 100
        assert user.frozen_credits == 0
        assert [
            row.type
            for row in db.query(CreditTransaction)
            .filter_by(biz_type="gen_task", biz_ref=task_id)
            .order_by(CreditTransaction.id)
        ] == ["freeze", "refund"]


def test_worker_terminal_result_wins_when_publish_ack_raises(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    user_id = make_user("13999070003", balance=100)
    from app import tasks as task_module

    original = task_module.enqueue_with_request_context

    def accepted_then_ack_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("publisher wrapper failed after broker acceptance")

    monkeypatch.setattr(task_module, "enqueue_with_request_context", accepted_then_ack_lost)
    response = quote_and_generate(
        _payload(request_id="dispatch-terminal-wins-001"),
        headers=auth("13999070003"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert response.json()["dispatch_status"] == "completed"
    with SessionLocal() as db:
        user = db.get(User, user_id)
        task = db.get(GenTask, response.json()["id"])
        assert task.status == "succeeded"
        assert task.cost_settled == task.cost_frozen
        assert user.frozen_credits == 0


def test_unclassified_runtime_error_is_ambiguous_and_keeps_freeze(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    user_id = make_user("13999070006", balance=100)
    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("publisher outcome is not provable")
        ),
    )

    response = quote_and_generate(
        _payload(request_id="dispatch-runtime-unknown-001"),
        headers=auth("13999070006"),
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["dispatch_status"] == "unknown"
    assert body["dispatch_reconciliation_required"] is True
    with SessionLocal() as db:
        task = db.get(GenTask, body["id"])
        user = db.get(User, user_id)
        assert task.status == "queued"
        assert user.frozen_credits == task.cost_frozen
        assert [
            row.type
            for row in db.query(CreditTransaction)
            .filter_by(biz_type="gen_task", biz_ref=task.id)
            .order_by(CreditTransaction.id)
        ] == ["freeze"]


def test_worker_running_proves_delivery_when_publish_wrapper_raises(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    user_id = make_user("13999070007", balance=100)

    def accepted_then_running(_celery_task, generation_task_id, **_kwargs):
        with SessionLocal() as worker_db:
            task = worker_db.get(GenTask, int(generation_task_id))
            task.status = "running"
            task.phase = "rendering"
            worker_db.commit()
        raise RuntimeError("publisher wrapper failed after worker claim")

    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        accepted_then_running,
    )
    response = quote_and_generate(
        _payload(request_id="dispatch-running-proof-001"),
        headers=auth("13999070007"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["dispatch_status"] == "completed"
    assert body["dispatch_reconciliation_required"] is False
    with SessionLocal() as db:
        task = db.get(GenTask, body["id"])
        user = db.get(User, user_id)
        assert task.status == "running"
        assert user.frozen_credits == task.cost_frozen
        assert db.query(CreditTransaction).filter_by(
            biz_type="gen_task",
            biz_ref=task.id,
            type="refund",
        ).count() == 0


def test_reconcile_does_not_republish_after_worker_claim(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    make_user("13999070008", balance=100)
    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OperationalError("acknowledgement unknown")
        ),
    )
    response = quote_and_generate(
        _payload(request_id="dispatch-running-reconcile-001"),
        headers=auth("13999070008"),
    )
    assert response.status_code == 202, response.text
    task_id = int(response.json()["id"])
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        task.status = "running"
        task.phase = "rendering"
        dispatch = db.query(GenerationDispatch).filter_by(task_id=task_id).one()
        dispatch.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        original_attempts = dispatch.publish_attempts
        db.commit()

    with SessionLocal() as db:
        counts = reconcile_dispatches(db)
    assert counts["completed"] == 1
    dispatch = _latest_dispatch(task_id)
    assert dispatch.status == "completed"
    assert dispatch.publish_attempts == original_attempts


def test_worker_claim_wins_race_with_dispatch_escalation(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    make_user("13999070009", balance=100)
    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OperationalError("acknowledgement unknown")
        ),
    )
    response = quote_and_generate(
        _payload(request_id="dispatch-escalation-race-001"),
        headers=auth("13999070009"),
    )
    assert response.status_code == 202, response.text
    task_id = int(response.json()["id"])
    with SessionLocal() as db:
        dispatch = db.query(GenerationDispatch).filter_by(task_id=task_id).one()
        dispatch.publish_attempts = MAX_PUBLISH_ATTEMPTS
        dispatch.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    def worker_claims_before_escalation(db, claimed_task_id, _now):
        task = db.get(GenTask, claimed_task_id)
        task.status = "running"
        task.phase = "rendering"
        db.commit()
        return False

    monkeypatch.setattr(
        generation_dispatch,
        "_claim_dispatch_needs_review",
        worker_claims_before_escalation,
    )
    with SessionLocal() as db:
        counts = reconcile_dispatches(db)

    assert counts["completed"] == 1
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        dispatch = db.query(GenerationDispatch).filter_by(task_id=task_id).one()
        assert task.status == "running"
        assert task.phase == "rendering"
        assert dispatch.status == "completed"
        assert dispatch.publish_attempts == MAX_PUBLISH_ATTEMPTS


def test_unknown_publish_returns_accepted_then_reconciles_same_id(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    make_user("13999070004", balance=100)
    headers = auth("13999070004")
    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationalError("ack unknown")),
    )
    response = quote_and_generate(
        _payload(request_id="dispatch-unknown-001"),
        headers=headers,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    task_id = int(body["id"])
    celery_id = body["dispatch_task_id"]
    assert body["dispatch_status"] == "unknown"
    assert body["dispatch_reconciliation_required"] is True
    task_center = client.get("/api/task-center?kind=generation", headers=headers)
    assert task_center.status_code == 200, task_center.text
    projected = next(item for item in task_center.json()["items"] if item["id"] == task_id)
    assert projected["dispatch_status"] == "unknown"
    assert projected["dispatch_task_id"] == celery_id
    assert projected["dispatch_reconciliation_required"] is True
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        assert task.status == "queued"
        assert task.phase == "reconciling"
        assert task.cost_frozen > 0
        dispatch = db.query(GenerationDispatch).filter_by(task_id=task_id).one()
        dispatch.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    monkeypatch.undo()
    with SessionLocal() as db:
        counts = reconcile_dispatches(db)
    # The suite intentionally keeps earlier ambiguous publishes for the same
    # global reconciler, so another dispatch can become due while this test is
    # running. Verify this dispatch below instead of assuming a global count.
    assert counts["completed"] >= 1
    dispatch = _latest_dispatch(task_id)
    assert dispatch.celery_task_id == celery_id
    assert dispatch.publish_attempts == 2
    assert dispatch.status == "completed"
    with SessionLocal() as db:
        duplicate = publish_dispatch(db, dispatch.id)
        assert duplicate.status == "completed"
        task = db.get(GenTask, task_id)
        gateway_calls = db.query(CreditTransaction).filter_by(
            biz_type="gen_task", biz_ref=task_id, type="settle"
        ).count()
        assert task.status == "succeeded"
        assert gateway_calls == 1


def test_retry_creates_distinct_attempt_and_unknown_keeps_refreeze(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    user_id = make_user("13999070005", balance=100)
    headers = auth("13999070005")
    created = quote_and_generate(
        _payload(request_id="dispatch-retry-001"),
        headers=headers,
    )
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        task.status = "failed"
        task.error = "known post-settlement test failure"
        task.finished_at = datetime.now(timezone.utc)
        db.commit()

    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationalError("retry ack unknown")),
    )
    retried = client.post(f"/api/tasks/{task_id}/retry", headers=headers)
    assert retried.status_code == 202, retried.text
    body = retried.json()
    assert body["dispatch_attempt"] == 2
    assert body["dispatch_status"] == "unknown"
    with SessionLocal() as db:
        attempts = list(
            db.query(GenerationDispatch)
            .filter_by(task_id=task_id)
            .order_by(GenerationDispatch.attempt)
        )
        user = db.get(User, user_id)
        task = db.get(GenTask, task_id)
        assert [row.attempt for row in attempts] == [1, 2]
        assert attempts[0].celery_task_id != attempts[1].celery_task_id
        assert task.status == "queued"
        assert user.frozen_credits == task.cost_frozen
