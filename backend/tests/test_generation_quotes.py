"""Server-authoritative quote coverage for generation submission."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.db import SessionLocal
from app.models import CreditTransaction, GenerationQuote, GenTask, ModelConfig, User
from app.prompt_optimization_schemas import StudioPromptOptimizationIn
from app.services import locks, prompt_optimization
from app.services.generation_dispatch import PrePublishError
from app.services.generation_quotes import (
    consume_execution_quote,
    create_execution_quote,
    create_prompt_optimization_quote,
    find_idempotent_quote,
    lock_execution_quote,
    quoted_model_snapshot,
    validate_prompt_optimization_quote,
)

QUOTE_KINDS = (
    "generation",
    "reverse",
    "reverse_batch",
    "workflow",
    "prompt_optimization",
    "asset_unlock",
)


def _execution_quote(
    db,
    *,
    user_id: int,
    kind: str,
    client_request_id: str,
    fingerprint: str,
) -> GenerationQuote:
    return create_execution_quote(
        db,
        user_id=user_id,
        kind=kind,
        client_request_id=client_request_id,
        request_fingerprint=fingerprint,
        category="workflow" if kind == "workflow" else "image",
        stage="final",
        request_snapshot={"kind": kind},
        model_snapshot={},
        pricing_snapshot={},
        price_breakdown=None,
        estimated_credits=1,
    )


def _payload(*, instruction: str = "a ceramic white teapot") -> dict:
    return {
        "client_request_id": "generation-quote-test-default",
        "category": "image",
        "stage": "preview",
        "instruction": instruction,
        "params": {"n": 1, "size": "1024x1024"},
    }


def _quote(client, headers, payload: dict | None = None) -> dict:
    response = client.post("/api/quotes", json=payload or _payload(), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _prompt_optimization_quote_context(
    db,
    *,
    user_id: int,
    request_id: str,
) -> tuple[StudioPromptOptimizationIn, GenerationQuote, dict]:
    target_model = db.query(ModelConfig).filter_by(use="image", enabled=True).first()
    optimizer = db.query(ModelConfig).filter_by(use="prompt", enabled=True).first()
    assert target_model is not None and optimizer is not None
    body = StudioPromptOptimizationIn(
        prompt="single perfume bottle on white, keep text PURE",
        category="image",
        mode="faithful",
        target_model_config_id=int(target_model.id),
        optimizer_model_config_id=int(optimizer.id),
        idempotency_key=request_id,
    )
    quote = create_prompt_optimization_quote(db, user_id=user_id, body=body)
    body = body.model_copy(update={"quote_id": int(quote.id)})
    prepared = prompt_optimization.prepare_proposal_request(
        db,
        user_id=user_id,
        body=body,
    )
    return body, quote, prepared


def _assert_quote_error(code: str, callback) -> None:
    with pytest.raises(HTTPException) as raised:
        callback()
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == code


@pytest.mark.parametrize("kind", QUOTE_KINDS)
def test_idempotent_quote_returns_active_and_consumed_for_replay(
    client,
    make_user,
    kind,
):
    user_id = make_user("13979991001")
    fingerprint = f"{kind}-replay-fingerprint"
    with SessionLocal() as db:
        active = _execution_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=f"{kind}-active-replay",
            fingerprint=fingerprint,
        )
        consumed = _execution_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=f"{kind}-consumed-replay",
            fingerprint=fingerprint,
        )
        consume_execution_quote(
            consumed,
            ref_type=f"test_{kind}",
            ref_id=int(consumed.id),
        )
        consumed.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

        assert find_idempotent_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=active.client_request_id,
            request_fingerprint=fingerprint,
        ).id == active.id
        assert find_idempotent_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=consumed.client_request_id,
            request_fingerprint=fingerprint,
        ).id == consumed.id


@pytest.mark.parametrize("kind", QUOTE_KINDS)
@pytest.mark.parametrize("terminal_status", ("expired", "canceled"))
def test_unconsumed_terminal_quote_allows_same_request_id_to_be_reissued(
    client,
    make_user,
    kind,
    terminal_status,
):
    user_id = make_user("13979991002")
    request_id = f"{kind}-{terminal_status}-reissue"
    fingerprint = f"{kind}-{terminal_status}-fingerprint"
    with SessionLocal() as db:
        original = _execution_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=request_id,
            fingerprint=fingerprint,
        )
        original.status = terminal_status
        db.commit()

        assert find_idempotent_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=request_id,
            request_fingerprint=fingerprint,
        ) is None
        replacement = _execution_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=request_id,
            fingerprint=fingerprint,
        )
        db.commit()

        assert replacement.id != original.id
        replay = find_idempotent_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=request_id,
            request_fingerprint=fingerprint,
        )
        assert replay is not None and replay.id == replacement.id
        assert replay.status == "active"


@pytest.mark.parametrize("kind", QUOTE_KINDS)
def test_past_due_active_quote_is_expired_and_can_be_reissued(client, make_user, kind):
    user_id = make_user("13979991003")
    request_id = f"{kind}-past-due-reissue"
    fingerprint = f"{kind}-past-due-fingerprint"
    with SessionLocal() as db:
        original = _execution_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=request_id,
            fingerprint=fingerprint,
        )
        original.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        assert find_idempotent_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=request_id,
            request_fingerprint=fingerprint,
        ) is None
        assert original.status == "expired"
        replacement = _execution_quote(
            db,
            user_id=user_id,
            kind=kind,
            client_request_id=request_id,
            fingerprint=fingerprint,
        )
        db.commit()
        assert replacement.status == "active"
        assert replacement.id != original.id


@pytest.mark.parametrize("kind", QUOTE_KINDS)
def test_quote_idempotency_is_user_scoped_and_rejects_fingerprint_tampering(
    client,
    make_user,
    kind,
):
    owner_id = make_user("13979991004")
    other_id = make_user("13979991005")
    request_id = f"{kind}-ownership-tamper"
    fingerprint = f"{kind}-owner-fingerprint"
    with SessionLocal() as db:
        quote = _execution_quote(
            db,
            user_id=owner_id,
            kind=kind,
            client_request_id=request_id,
            fingerprint=fingerprint,
        )
        quote.status = "canceled"
        db.commit()

        assert find_idempotent_quote(
            db,
            user_id=other_id,
            kind=kind,
            client_request_id=request_id,
            request_fingerprint=fingerprint,
        ) is None
        with pytest.raises(HTTPException) as raised:
            find_idempotent_quote(
                db,
                user_id=owner_id,
                kind=kind,
                client_request_id=request_id,
                request_fingerprint=f"{fingerprint}-tampered",
            )
        assert raised.value.status_code == 409
        assert raised.value.detail["code"] == "QUOTE_IDEMPOTENCY_CONFLICT"


def test_explicit_quote_creates_and_consumes_exact_task_quote(client, make_user, auth):
    user_id = make_user("13970000001", balance=100)
    headers = auth("13970000001")
    quote = _quote(client, headers)

    generated = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text
    task_id = generated.json()["id"]

    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        persisted_quote = db.get(GenerationQuote, quote["quote_id"])
        assert task is not None and persisted_quote is not None
        assert task.user_id == user_id
        assert task.quote_id == persisted_quote.id
        assert task.cost_frozen == quote["estimated_credits"] == 8
        assert persisted_quote.status == "consumed"
        assert persisted_quote.task_id == task.id
        assert persisted_quote.price_version_id == quote["price_version_id"]
        assert (task.params or {})["_quote"]["price_version_id"] == quote["price_version_id"]
    finally:
        db.close()


def test_generate_without_quote_is_rejected_without_side_effects(client, make_user, auth):
    user_id = make_user("13970000002", balance=100)
    headers = auth("13970000002")

    generated = client.post("/api/generate", json=_payload(), headers=headers)
    assert generated.status_code == 422, generated.text

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        assert user is not None
        assert db.query(GenerationQuote).filter_by(user_id=user_id).count() == 0
        assert db.query(GenTask).filter_by(user_id=user_id).count() == 0
        assert db.query(CreditTransaction).filter_by(user_id=user_id).count() == 0
        assert user.balance_credits == 100
        assert user.frozen_credits == 0
    finally:
        db.close()


def test_quote_rejects_tampering_cross_user_and_repeated_consumption(client, make_user, auth):
    make_user("13970000003", balance=100)
    make_user("13970000004", balance=100)
    owner_headers = auth("13970000003")
    other_headers = auth("13970000004")
    quote = _quote(client, owner_headers)

    # A second submit while the first holder is still resolving the quote must
    # not create a second task. The normal repeated request is asserted below.
    db = SessionLocal()
    try:
        persisted_quote = db.get(GenerationQuote, quote["quote_id"])
        assert persisted_quote is not None
        quote_user_id = persisted_quote.user_id
    finally:
        db.close()
    lock_key = f"gen:quote:consume:{quote_user_id}:{quote['quote_id']}"
    token = locks.acquire(lock_key, ttl=60)
    assert token is not None
    try:
        in_flight = client.post(
            "/api/generate",
            json={**_payload(), "quote_id": quote["quote_id"]},
            headers=owner_headers,
        )
        assert in_flight.status_code == 409, in_flight.text
        assert in_flight.json()["detail"]["code"] == "QUOTE_IN_USE"
    finally:
        locks.release(lock_key, token)

    tampered = client.post(
        "/api/generate",
        json={**_payload(instruction="a different black vase"), "quote_id": quote["quote_id"]},
        headers=owner_headers,
    )
    assert tampered.status_code == 409, tampered.text
    assert tampered.json()["detail"]["code"] == "QUOTE_MISMATCH"

    cross_user = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=other_headers,
    )
    assert cross_user.status_code == 404, cross_user.text
    assert cross_user.json()["detail"]["code"] == "QUOTE_NOT_FOUND"

    first = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=owner_headers,
    )
    assert first.status_code == 200, first.text
    repeated = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=owner_headers,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == first.json()["id"]

    conflicting_replay = client.post(
        "/api/generate",
        json={
            **_payload(),
            "client_request_id": "generation-quote-conflict-id",
            "quote_id": quote["quote_id"],
        },
        headers=owner_headers,
    )
    assert conflicting_replay.status_code == 409, conflicting_replay.text
    assert conflicting_replay.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    db = SessionLocal()
    try:
        assert db.query(GenTask).filter(GenTask.quote_id == quote["quote_id"]).count() == 1
    finally:
        db.close()


def test_expired_quote_and_insufficient_balance_do_not_create_task(client, make_user, auth):
    make_user("13970000005", balance=100)
    headers = auth("13970000005")
    expired_quote = _quote(client, headers)
    db = SessionLocal()
    try:
        row = db.get(GenerationQuote, expired_quote["quote_id"])
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    expired = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": expired_quote["quote_id"]},
        headers=headers,
    )
    assert expired.status_code == 409, expired.text
    assert expired.json()["detail"]["code"] == "QUOTE_EXPIRED"

    user_id = make_user("13970000006", balance=0)
    poor_headers = auth("13970000006")
    quote = _quote(client, poor_headers)
    insufficient = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=poor_headers,
    )
    assert insufficient.status_code == 400, insufficient.text

    db = SessionLocal()
    try:
        persisted_quote = db.get(GenerationQuote, quote["quote_id"])
        user = db.get(User, user_id)
        assert persisted_quote is not None and user is not None
        assert persisted_quote.status == "active"
        assert persisted_quote.task_id is None
        assert db.query(GenTask).filter(GenTask.quote_id == quote["quote_id"]).count() == 0
        assert user.balance_credits == 0
        assert user.frozen_credits == 0
    finally:
        db.close()


def test_quote_keeps_price_snapshot_when_catalog_price_changes(client, make_user, auth):
    make_user("13970000007", balance=100)
    headers = auth("13970000007")
    quote = _quote(client, headers)

    db = SessionLocal()
    original_extra = None
    try:
        model = db.get(ModelConfig, quote["model_config_id"])
        assert model is not None
        original_extra = dict(model.extra or {})
        extra = dict(original_extra)
        extra["credit_pricing"] = {
            "image": {"1k": 47, "2k": 47, "4k": 47},
            "image_edit": {"1k": 47, "2k": 47, "4k": 47},
            "video_preview_cost": 47,
            "video_per_second": {"480p": 47, "720p": 47, "1080p": 47},
        }
        model.extra = extra
        db.commit()
    finally:
        db.close()

    generated = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text

    db = SessionLocal()
    try:
        task = db.get(GenTask, generated.json()["id"])
        assert task is not None
        assert task.cost_frozen == 8
        pricing = ((task.params or {}).get("_model_snapshot") or {}).get("extra", {}).get("credit_pricing")
        assert pricing["image"]["1k"] == 8
        model = db.get(ModelConfig, quote["model_config_id"])
        assert model is not None and original_extra is not None
        model.extra = original_extra
        db.commit()
    finally:
        db.close()


def test_enqueue_failure_consumes_quote_marks_task_failed_and_refunds(
    client, make_user, auth, monkeypatch
):
    make_user("13970000008", balance=100)
    headers = auth("13970000008")
    quote = _quote(client, headers)

    def reject_enqueue(*_args, **_kwargs):
        raise PrePublishError("publisher rejected before contacting redis")

    monkeypatch.setattr("app.tasks.enqueue_with_request_context", reject_enqueue)
    generated = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=headers,
    )
    assert generated.status_code == 503, generated.text

    db = SessionLocal()
    try:
        persisted_quote = db.get(GenerationQuote, quote["quote_id"])
        assert persisted_quote is not None and persisted_quote.task_id is not None
        task = db.get(GenTask, persisted_quote.task_id)
        assert task is not None
        assert persisted_quote.status == "consumed"
        assert task.status == "failed"
        assert "入队失败" in (task.error or "")
        tx_types = [
            row.type
            for row in db.query(CreditTransaction)
            .filter(CreditTransaction.biz_type == "gen_task", CreditTransaction.biz_ref == task.id)
            .order_by(CreditTransaction.id)
        ]
        assert tx_types == ["freeze", "refund"]
        user = db.get(User, task.user_id)
        assert user is not None
        assert user.balance_credits == 100
        assert user.frozen_credits == 0
    finally:
        db.close()


def test_quote_rejects_capability_snapshot_version_drift_without_side_effects(
    client,
    make_user,
    auth,
):
    user_id = make_user("13970000009", balance=100)
    headers = auth("13970000009")
    quote = _quote(client, headers)
    db = SessionLocal()
    try:
        row = db.get(GenerationQuote, quote["quote_id"])
        snapshot = dict(row.model_snapshot)
        extra = dict(snapshot["extra"])
        capabilities = dict(extra.get("capabilities") or {})
        capabilities["text_to_image"] = not bool(capabilities.get("text_to_image"))
        extra["capabilities"] = capabilities
        snapshot["extra"] = extra
        row.model_snapshot = snapshot
        db.commit()
        user = db.get(User, user_id)
        before = (
            int(user.balance_credits),
            int(user.frozen_credits),
            db.query(GenTask).filter_by(user_id=user_id).count(),
            db.query(CreditTransaction).filter_by(user_id=user_id).count(),
        )
    finally:
        db.close()

    response = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote["quote_id"]},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "QUOTE_SNAPSHOT_INVALID"

    db = SessionLocal()
    try:
        row = db.get(GenerationQuote, quote["quote_id"])
        user = db.get(User, user_id)
        after = (
            int(user.balance_credits),
            int(user.frozen_credits),
            db.query(GenTask).filter_by(user_id=user_id).count(),
            db.query(CreditTransaction).filter_by(user_id=user_id).count(),
        )
        assert row.status == "active"
        assert row.task_id is None
        assert after == before
    finally:
        db.close()


def test_quoted_snapshot_preserves_explicit_zero_credit_values():
    quote = GenerationQuote(
        model_snapshot={"cost_credits": 0, "unlock_cost": 0},
        pricing_snapshot={},
    )
    snapshot = quoted_model_snapshot(
        quote,
        {"cost_credits": 99, "unlock_cost": 12, "extra": {}},
    )
    assert snapshot["cost_credits"] == 0
    assert snapshot["unlock_cost"] == 0
