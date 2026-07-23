"""Server-authoritative quote coverage for modern reverse workflows."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models import (
    CreditTransaction,
    GenerationQuote,
    ModelConfig,
    ReverseOperation,
    ReverseOperationBatch,
    User,
)
from app.routers import prompt
from app.services import model_versions, reverse_operations
from tests.reverse_helpers import quote_reverse, quote_reverse_batch, quote_reverse_retry


def _stub_submission_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_operations,
        "assert_text_allowed",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reverse_operations,
        "enqueue_operation",
        lambda operation_id: f"queued-{operation_id}",
    )


def _reverse_body(
    request_id: str,
    *,
    asset_url: str | None = None,
    model_config_id: int | None = None,
) -> dict:
    body = {
        "client_request_id": request_id,
        "asset_url": asset_url or f"https://cdn.example.com/{request_id}.jpg",
        "source_type": "image",
        "target": "image",
        "analysis_focus": "product_ad",
        "analysis_precision": "standard",
        "output_purpose": "generation",
        "custom_instruction": "保留包装文字和商标色",
    }
    if model_config_id is not None:
        body["model_config_id"] = int(model_config_id)
    return body


def _batch_body(request_id: str, *, item_count: int = 2) -> dict:
    return {
        "client_request_id": request_id,
        "name": "商品图批量反推",
        "target": "image",
        "analysis_focus": "product_ad",
        "analysis_precision": "standard",
        "output_purpose": "generation",
        "custom_instruction": "保留包装文字和商标色",
        "include_audio": False,
        "items": [
            {
                "asset_url": f"https://cdn.example.com/{request_id}-{index}.jpg",
                "source_type": "image",
            }
            for index in range(item_count)
        ],
    }


def _detail_code(response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _submit_reverse(client, body: dict, quote_id: int, headers):
    return client.post(
        "/api/prompt/reverse-operations",
        json={**body, "quote_id": int(quote_id)},
        headers=headers,
    )


def _create_queued_reverse(client, body: dict, headers) -> tuple[dict, dict]:
    quote = quote_reverse(client, body, headers=headers)
    assert quote.status_code == 201, quote.text
    created = _submit_reverse(client, body, quote.json()["quote_id"], headers)
    assert created.status_code == 202, created.text
    return quote.json(), created.json()


def test_reverse_quote_is_required_kind_scoped_owner_scoped_and_expiring(
    client,
    make_user,
    auth,
    monkeypatch,
):
    owner_id = make_user("13973200001", balance=100)
    other_id = make_user("13973200002", balance=100)
    owner_headers = auth("13973200001")
    other_headers = auth("13973200002")
    _stub_submission_dependencies(monkeypatch)

    missing_body = _reverse_body("reverse-quote-required")
    missing = client.post(
        "/api/prompt/reverse-operations",
        json=missing_body,
        headers=owner_headers,
    )
    assert missing.status_code == 400, missing.text
    assert _detail_code(missing) == "QUOTE_REQUIRED"

    wrong_kind_body = _batch_body("reverse-quote-wrong-kind")
    wrong_kind_quote = quote_reverse_batch(client, wrong_kind_body, headers=owner_headers)
    assert wrong_kind_quote.status_code == 201, wrong_kind_quote.text
    wrong_kind = _submit_reverse(
        client,
        _reverse_body("reverse-quote-wrong-kind"),
        wrong_kind_quote.json()["quote_id"],
        owner_headers,
    )
    assert wrong_kind.status_code == 409, wrong_kind.text
    assert _detail_code(wrong_kind) == "QUOTE_KIND_MISMATCH"

    owner_body = _reverse_body("reverse-quote-owner-bound")
    owner_quote = quote_reverse(client, owner_body, headers=owner_headers)
    assert owner_quote.status_code == 201, owner_quote.text
    cross_owner = _submit_reverse(
        client,
        owner_body,
        owner_quote.json()["quote_id"],
        other_headers,
    )
    assert cross_owner.status_code == 404, cross_owner.text
    assert _detail_code(cross_owner) == "QUOTE_NOT_FOUND"

    expired_body = _reverse_body("reverse-quote-expired")
    expired_quote = quote_reverse(client, expired_body, headers=owner_headers)
    assert expired_quote.status_code == 201, expired_quote.text
    with SessionLocal() as db:
        row = db.get(GenerationQuote, expired_quote.json()["quote_id"])
        row.status = "expired"
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    expired = _submit_reverse(
        client,
        expired_body,
        expired_quote.json()["quote_id"],
        owner_headers,
    )
    assert expired.status_code == 409, expired.text
    assert _detail_code(expired) == "QUOTE_EXPIRED"

    with SessionLocal() as db:
        assert db.query(ReverseOperation).filter_by(user_id=owner_id).count() == 0
        assert db.query(ReverseOperation).filter_by(user_id=other_id).count() == 0


def test_reverse_quote_response_redacts_server_template_but_persists_it_for_execution(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13973200013", balance=100)
    headers = auth("13973200013")
    _stub_submission_dependencies(monkeypatch)
    body = _reverse_body("reverse-quote-template-redaction")

    response = quote_reverse(client, body, headers=headers)

    assert response.status_code == 201, response.text
    public_subject = response.json()["subject_snapshot"]
    assert set(public_subject) == {"retry_of_operation_id", "template"}
    assert public_subject["retry_of_operation_id"] is None
    assert len(public_subject["template"]["fingerprint"]) == 64
    assert "templates" not in response.text
    with SessionLocal() as db:
        quote = db.get(GenerationQuote, response.json()["quote_id"])
        assert quote.subject_snapshot["template_snapshot"]["templates"]["image"]


@pytest.mark.parametrize(
    "field",
    (
        "request_snapshot",
        "model_snapshot",
        "pricing_snapshot",
        "subject_snapshot",
        "price_breakdown",
        "estimated_credits",
    ),
)
def test_reverse_execution_rejects_persisted_quote_snapshot_tampering(
    client,
    make_user,
    auth,
    monkeypatch,
    field,
):
    phone = {
        "request_snapshot": "13973200101",
        "model_snapshot": "13973200102",
        "pricing_snapshot": "13973200103",
        "subject_snapshot": "13973200104",
        "price_breakdown": "13973200105",
        "estimated_credits": "13973200106",
    }[field]
    user_id = make_user(phone, balance=100)
    headers = auth(phone)
    _stub_submission_dependencies(monkeypatch)
    body = _reverse_body(f"reverse-quote-tamper-{field}")
    response = quote_reverse(client, body, headers=headers)
    assert response.status_code == 201, response.text
    quote_id = response.json()["quote_id"]

    with SessionLocal() as db:
        quote = db.get(GenerationQuote, quote_id)
        if field == "estimated_credits":
            quote.estimated_credits = int(quote.estimated_credits) + 1
        else:
            value = deepcopy(getattr(quote, field) or {})
            value["tampered"] = True
            setattr(quote, field, value)
        db.commit()

    rejected = _submit_reverse(client, body, quote_id, headers)
    assert rejected.status_code == 409, rejected.text
    assert _detail_code(rejected) == "QUOTE_SNAPSHOT_INVALID"
    with SessionLocal() as db:
        assert db.query(ReverseOperation).filter_by(user_id=user_id).count() == 0
        quote = db.get(GenerationQuote, quote_id)
        assert quote.status == "active"
        assert quote.consumed_ref_id is None


def test_exact_reverse_replay_does_not_duplicate_operation_freeze_or_quote_consumption(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200004", balance=100)
    headers = auth("13973200004")
    _stub_submission_dependencies(monkeypatch)
    body = _reverse_body("reverse-quote-exact-replay")
    quote = quote_reverse(client, body, headers=headers)
    assert quote.status_code == 201, quote.text
    quote_id = quote.json()["quote_id"]

    first = _submit_reverse(client, body, quote_id, headers)
    replay = _submit_reverse(client, body, quote_id, headers)
    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]

    with SessionLocal() as db:
        operations = db.query(ReverseOperation).filter_by(user_id=user_id).all()
        assert len(operations) == 1
        operation = operations[0]
        assert operation.quote_id == quote_id
        assert db.query(CreditTransaction).filter_by(
            user_id=user_id,
            biz_type="reverse_operation",
            biz_ref=int(operation.id),
            type="freeze",
        ).count() == 1
        quote_row = db.get(GenerationQuote, quote_id)
        assert quote_row.status == "consumed"
        assert quote_row.consumed_ref_type == "reverse_operation"
        assert quote_row.consumed_ref_id == operation.id


def test_existing_reverse_request_rejects_a_replacement_quote(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200005", balance=100)
    headers = auth("13973200005")
    _stub_submission_dependencies(monkeypatch)
    body = _reverse_body("reverse-quote-replacement")
    original_quote, operation = _create_queued_reverse(client, body, headers)

    with SessionLocal() as db:
        source = db.get(GenerationQuote, original_quote["quote_id"])
        replacement = GenerationQuote(
            user_id=int(source.user_id),
            model_config_id=source.model_config_id,
            capability_version_id=source.capability_version_id,
            price_version_id=source.price_version_id,
            tool_version_id=source.tool_version_id,
            kind=source.kind,
            client_request_id=source.client_request_id,
            request_fingerprint=source.request_fingerprint,
            category=source.category,
            stage=source.stage,
            request_snapshot=deepcopy(source.request_snapshot),
            model_snapshot=deepcopy(source.model_snapshot),
            pricing_snapshot=deepcopy(source.pricing_snapshot),
            price_breakdown=deepcopy(source.price_breakdown),
            subject_snapshot=deepcopy(source.subject_snapshot),
            warnings=deepcopy(source.warnings),
            estimated_credits=int(source.estimated_credits),
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db.add(replacement)
        db.commit()
        db.refresh(replacement)
        replacement_quote_id = int(replacement.id)

    rejected = _submit_reverse(client, body, replacement_quote_id, headers)
    assert rejected.status_code == 409, rejected.text
    assert "已绑定其他反推报价" in rejected.text
    with SessionLocal() as db:
        assert db.query(ReverseOperation).filter_by(user_id=user_id).count() == 1
        assert db.get(ReverseOperation, operation["id"]).quote_id == original_quote["quote_id"]
        assert db.get(GenerationQuote, replacement_quote_id).status == "active"


def test_insufficient_balance_rolls_back_operation_freeze_and_quote_consumption(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200006", balance=0)
    headers = auth("13973200006")
    _stub_submission_dependencies(monkeypatch)
    body = _reverse_body("reverse-quote-insufficient-balance")
    quote = quote_reverse(client, body, headers=headers)
    assert quote.status_code == 201, quote.text
    quote_id = quote.json()["quote_id"]
    assert quote.json()["estimated_credits"] > 0

    rejected = _submit_reverse(client, body, quote_id, headers)
    assert rejected.status_code == 400, rejected.text
    with SessionLocal() as db:
        assert db.query(ReverseOperation).filter_by(user_id=user_id).count() == 0
        assert db.query(CreditTransaction).filter_by(
            user_id=user_id,
            biz_type="reverse_operation",
        ).count() == 0
        user = db.get(User, user_id)
        assert user.balance_credits == 0
        assert user.frozen_credits == 0
        quote_row = db.get(GenerationQuote, quote_id)
        assert quote_row.status == "active"
        assert quote_row.consumed_ref_id is None


def test_batch_failure_is_all_or_nothing_and_keeps_quote_active(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200007", balance=5)
    headers = auth("13973200007")
    _stub_submission_dependencies(monkeypatch)
    body = _batch_body("reverse-batch-quote-atomic")
    quote = quote_reverse_batch(client, body, headers=headers)
    assert quote.status_code == 201, quote.text
    quote_id = quote.json()["quote_id"]
    assert quote.json()["estimated_credits"] > 5
    assert quote.json()["subject_snapshot"] == {"item_count": 2}

    rejected = client.post(
        "/api/prompt/reverse-batches",
        json={**body, "quote_id": quote_id},
        headers=headers,
    )
    assert rejected.status_code == 400, rejected.text
    with SessionLocal() as db:
        assert db.query(ReverseOperationBatch).filter_by(user_id=user_id).count() == 0
        assert db.query(ReverseOperation).filter_by(user_id=user_id).count() == 0
        assert db.query(CreditTransaction).filter_by(
            user_id=user_id,
            biz_type="reverse_operation",
        ).count() == 0
        user = db.get(User, user_id)
        assert user.balance_credits == 5
        assert user.frozen_credits == 0
        quote_row = db.get(GenerationQuote, quote_id)
        assert quote_row.status == "active"
        assert quote_row.consumed_ref_id is None


def test_batch_quote_rejects_changed_item_without_partial_creation(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200008", balance=100)
    headers = auth("13973200008")
    _stub_submission_dependencies(monkeypatch)
    body = _batch_body("reverse-batch-quote-item-tamper")
    quote = quote_reverse_batch(client, body, headers=headers)
    assert quote.status_code == 201, quote.text
    quote_id = quote.json()["quote_id"]
    changed = deepcopy(body)
    changed["items"][1]["asset_url"] = "https://cdn.example.com/replaced.jpg"

    rejected = client.post(
        "/api/prompt/reverse-batches",
        json={**changed, "quote_id": quote_id},
        headers=headers,
    )
    assert rejected.status_code == 409, rejected.text
    assert _detail_code(rejected) == "QUOTE_MISMATCH"
    with SessionLocal() as db:
        assert db.query(ReverseOperationBatch).filter_by(user_id=user_id).count() == 0
        assert db.query(ReverseOperation).filter_by(user_id=user_id).count() == 0
        assert db.get(GenerationQuote, quote_id).status == "active"


def test_retry_quote_is_bound_to_its_original_operation(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13973200009", balance=100)
    headers = auth("13973200009")
    _stub_submission_dependencies(monkeypatch)
    _quote_a, operation_a = _create_queued_reverse(
        client,
        _reverse_body("reverse-retry-source-a"),
        headers,
    )
    _quote_b, operation_b = _create_queued_reverse(
        client,
        _reverse_body("reverse-retry-source-b"),
        headers,
    )
    assert reverse_operations.fail_operation(
        operation_a["id"],
        code="GATEWAY_ERROR",
        error="temporary failure A",
    ) is True
    assert reverse_operations.fail_operation(
        operation_b["id"],
        code="GATEWAY_ERROR",
        error="temporary failure B",
    ) is True

    retry_body = {"client_request_id": "reverse-retry-source-bound"}
    quote = quote_reverse_retry(
        client,
        operation_a["id"],
        retry_body,
        headers=headers,
    )
    assert quote.status_code == 201, quote.text
    quote_id = quote.json()["quote_id"]
    wrong_source = client.post(
        f"/api/prompt/reverse-operations/{operation_b['id']}/retry",
        json={**retry_body, "quote_id": quote_id},
        headers=headers,
    )
    assert wrong_source.status_code == 409, wrong_source.text
    assert _detail_code(wrong_source) == "QUOTE_MISMATCH"
    with SessionLocal() as db:
        assert db.get(GenerationQuote, quote_id).status == "active"

    correct_source = client.post(
        f"/api/prompt/reverse-operations/{operation_a['id']}/retry",
        json={**retry_body, "quote_id": quote_id},
        headers=headers,
    )
    assert correct_source.status_code == 202, correct_source.text
    assert correct_source.json()["retry_of_operation_id"] == operation_a["id"]
    with SessionLocal() as db:
        quote_row = db.get(GenerationQuote, quote_id)
        assert quote_row.status == "consumed"
        assert quote_row.consumed_ref_id == correct_source.json()["id"]


def test_zero_credit_reverse_quote_executes_without_fallback_price(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200010", balance=0)
    headers = auth("13973200010")
    _stub_submission_dependencies(monkeypatch)
    original = reverse_operations.reverse_pricing_snapshot

    def zero_price(body, reverse_pricing):
        snapshot = original(body, reverse_pricing)
        snapshot.update(
            {
                "frozen": 0,
                "visual_cost": 0,
                "audio_surcharge": 0,
                "single_image_cost": 0,
            }
        )
        return snapshot

    monkeypatch.setattr(reverse_operations, "reverse_pricing_snapshot", zero_price)
    body = _reverse_body("reverse-quote-zero-credit")
    quote = quote_reverse(client, body, headers=headers)
    assert quote.status_code == 201, quote.text
    assert quote.json()["estimated_credits"] == 0

    created = _submit_reverse(client, body, quote.json()["quote_id"], headers)
    assert created.status_code == 202, created.text
    assert created.json()["cost_frozen"] == 0
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user.balance_credits == 0
        assert user.frozen_credits == 0
        assert db.query(CreditTransaction).filter_by(
            user_id=user_id,
            biz_type="reverse_operation",
            type="freeze",
        ).count() == 0
        assert db.get(GenerationQuote, quote.json()["quote_id"]).status == "consumed"


def test_old_reverse_quote_uses_frozen_price_after_new_price_version_is_published(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200011", balance=100)
    headers = auth("13973200011")
    _stub_submission_dependencies(monkeypatch)
    body = _reverse_body("reverse-quote-frozen-price-version")
    response = quote_reverse(client, body, headers=headers)
    assert response.status_code == 201, response.text
    quote_payload = response.json()
    quote_id = quote_payload["quote_id"]
    quoted_cost = quote_payload["estimated_credits"]

    with SessionLocal() as db:
        quote = db.get(GenerationQuote, quote_id)
        model = db.get(ModelConfig, quote.model_config_id)
        original_extra = deepcopy(model.extra or {})
        changed_extra = deepcopy(original_extra)
        pricing = deepcopy(changed_extra.get("reverse_pricing") or {})
        pricing["image_cost"] = quoted_cost + 17
        changed_extra["reverse_pricing"] = pricing
        model.extra = changed_extra
        _capability, current_price = model_versions.sync_model_versions(db, model)
        db.commit()
        new_price_version_id = int(current_price.id)
        old_price_version_id = int(quote.price_version_id)
    assert new_price_version_id != old_price_version_id

    try:
        created = _submit_reverse(client, body, quote_id, headers)
        assert created.status_code == 202, created.text
        assert created.json()["cost_frozen"] == quoted_cost
        with SessionLocal() as db:
            operation = db.get(ReverseOperation, created.json()["id"])
            assert operation.pricing_snapshot["frozen"] == quoted_cost
            assert operation.quote_id == quote_id
    finally:
        operation_id = locals().get("created")
        if operation_id is not None and created.status_code == 202:
            reverse_operations.fail_operation(
                created.json()["id"],
                code="TEST_CLEANUP",
                error="restore frozen credits after price-version assertion",
            )
        with SessionLocal() as db:
            quote = db.get(GenerationQuote, quote_id)
            model = db.get(ModelConfig, quote.model_config_id)
            model.extra = original_extra
            model_versions.sync_model_versions(db, model)
            db.commit()

    with SessionLocal() as db:
        assert db.get(User, user_id).balance_credits == 100


def test_in_flight_reverse_quote_lock_allows_only_one_operation_and_one_freeze(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13973200012", balance=100)
    headers = auth("13973200012")
    _stub_submission_dependencies(monkeypatch)
    body = _reverse_body("reverse-quote-concurrent-consume")
    quote = quote_reverse(client, body, headers=headers)
    assert quote.status_code == 201, quote.text
    quote_id = quote.json()["quote_id"]

    lock_key = f"reverse:quote:consume:{user_id}:{quote_id}"
    lock_token = prompt.locks.acquire(lock_key, ttl=60)
    assert lock_token
    try:
        blocked = _submit_reverse(client, body, quote_id, headers)
    finally:
        prompt.locks.release(lock_key, lock_token)

    assert blocked.status_code == 409, blocked.text
    assert _detail_code(blocked) == "QUOTE_IN_USE"
    created = _submit_reverse(client, body, quote_id, headers)
    replay = _submit_reverse(client, body, quote_id, headers)
    assert created.status_code == 202, created.text
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == created.json()["id"]
    with SessionLocal() as db:
        operations = db.query(ReverseOperation).filter_by(user_id=user_id).all()
        assert len(operations) == 1
        assert db.query(CreditTransaction).filter_by(
            user_id=user_id,
            biz_type="reverse_operation",
            biz_ref=int(operations[0].id),
            type="freeze",
        ).count() == 1
        quote_row = db.get(GenerationQuote, quote_id)
        assert quote_row.status == "consumed"
        assert quote_row.consumed_ref_id == operations[0].id
