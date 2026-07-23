"""Transactional reverse-result lineage for generation submission."""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.models import (
    CreditTransaction,
    GatewayCall,
    GenAsset,
    GenerationDispatch,
    GenerationQuote,
    GenTask,
    ModelConfig,
    ReverseOperation,
    ReverseResultRevision,
)
from app.services import reverse_lineage
from app.services.config_store import get_model_config


def _seed_reverse_result(
    user_id: int,
    *,
    source: str = "applied",
    output_purpose: str = "generation",
    revision_output_purpose: str | None = None,
) -> tuple[int, int]:
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=f"{user_id:064d}"[-64:],
            target="image",
            asset_url=f"http://example.com/reverse-lineage-{user_id}.png",
            output_purpose=output_purpose,
            request_context={"output_purpose": output_purpose},
            status="succeeded",
            progress=100,
            result={"final_text": "a white ceramic teapot", "structured": {"subject": "teapot"}},
            normalized_result={
                "final_text": "a white ceramic teapot",
                "structured": {"subject": "teapot"},
            },
        )
        db.add(operation)
        db.flush()
        revision_payload = {
            "final_text": "a white ceramic teapot",
            "structured": {"subject": "teapot"},
        }
        if revision_output_purpose is not None:
            revision_payload["output_purpose"] = revision_output_purpose
        fingerprint = reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(
                f"reverse-lineage-source:{user_id}".encode()
            ),
            locator=operation.asset_url,
            method="test_source_sha256",
        )
        fingerprints = [fingerprint]
        source_hash = reverse_lineage.source_content_hash(fingerprints)
        revisions = []
        for version, revision_source, payload in (
            (1, "provider_raw", {"provider": "test", "result": revision_payload}),
            (2, "normalized", revision_payload),
            (3, "user_edit", revision_payload),
            (4, "applied", revision_payload),
        ):
            revision = ReverseResultRevision(
                operation_id=operation.id,
                user_id=user_id,
                version=version,
                source=revision_source,
                payload=dict(payload),
                parent_revision_id=(int(revisions[-1].id) if revisions else None),
                source_content_hash=source_hash,
                source_fingerprints=fingerprints,
                payload_hash=reverse_lineage.canonical_payload_hash(payload),
                lineage_status=reverse_lineage.VERIFIED,
                evidence_review_action="not_applicable",
            )
            db.add(revision)
            db.flush()
            revisions.append(revision)
            if revision_source == source:
                break
        revision = revisions[-1]
        db.commit()
        return int(operation.id), int(revision.id)


def _payload(operation_id: int, revision_id: int, *, request_id: str) -> dict:
    return {
        "client_request_id": request_id,
        "reverse_operation_id": operation_id,
        "reverse_revision_id": revision_id,
        "category": "image",
        "stage": "preview",
        "instruction": "a white ceramic teapot",
        "params": {"n": 1, "size": "1024x1024"},
    }


def test_generation_lineage_is_atomic_and_client_replay_does_not_duplicate_it(
    client, make_user, auth, quote_generation
):
    user_id = make_user("13971100001", balance=100)
    headers = auth("13971100001")
    operation_id, source_revision_id = _seed_reverse_result(user_id)
    payload = _payload(
        operation_id,
        source_revision_id,
        request_id="generation-lineage-replay-001",
    )

    quote = quote_generation(payload, headers=headers)
    quoted_payload = {**payload, "quote_id": quote["quote_id"]}
    first = client.post("/api/generate", json=quoted_payload, headers=headers)
    assert first.status_code == 200, first.text
    replay = client.post("/api/generate", json=quoted_payload, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]

    task_body = first.json()
    assert task_body["reverse_operation_id"] == operation_id
    assert task_body["source_revision_id"] == source_revision_id
    assert task_body["compiled_revision_id"]
    assert task_body["generation_revision_id"]

    with SessionLocal() as db:
        tasks = db.query(GenTask).filter_by(client_request_id="generation-lineage-replay-001").all()
        assert len(tasks) == 1
        task = tasks[0]
        revisions = (
            db.query(ReverseResultRevision)
            .filter_by(operation_id=operation_id)
            .order_by(ReverseResultRevision.version)
            .all()
        )
        assert [row.source for row in revisions] == [
            "provider_raw", "normalized", "user_edit", "applied", "model_compiled", "generation"
        ]
        assert task.source_revision_id == source_revision_id
        assert task.compiled_revision_id == revisions[4].id
        assert task.generation_revision_id == revisions[5].id
        assert revisions[4].parent_revision_id == source_revision_id
        assert revisions[5].parent_revision_id == revisions[4].id
        assert revisions[4].payload["source"]["revision_id"] == source_revision_id
        assert revisions[4].payload["target_model"]["model_config_id"] == task.model_config_id
        assert revisions[5].payload["compiled_revision_id"] == revisions[4].id
        assert revisions[5].payload["generation"]["task_id"] == task.id
        assert revisions[5].payload["generation"]["quote"]["quote_id"] == task.quote_id
        freeze_count = (
            db.query(CreditTransaction)
            .filter_by(biz_type="gen_task", biz_ref=task.id, type="freeze")
            .count()
        )
        assert freeze_count == 1


def test_generation_lineage_rejects_cross_user_operation_and_revision(client, make_user, auth):
    owner_id = make_user("13971100002", balance=100)
    other_id = make_user("13971100003", balance=100)
    other_headers = auth("13971100003")
    operation_id, revision_id = _seed_reverse_result(owner_id)

    blocked = client.post(
        "/api/quotes",
        json=_payload(operation_id, revision_id, request_id="generation-lineage-owner-001"),
        headers=other_headers,
    )
    assert blocked.status_code == 404, blocked.text

    with SessionLocal() as db:
        assert db.query(GenTask).filter_by(user_id=other_id).count() == 0
        assert db.query(GenerationQuote).filter_by(user_id=other_id).count() == 0
        assert db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count() == 4


def test_generation_lineage_rolls_back_revisions_task_quote_consumption_and_freeze(
    client, make_user, auth
):
    user_id = make_user("13971100004", balance=0)
    headers = auth("13971100004")
    operation_id, revision_id = _seed_reverse_result(user_id)
    payload = _payload(operation_id, revision_id, request_id="generation-lineage-rollback-001")

    quote_response = client.post("/api/quotes", json=payload, headers=headers)
    assert quote_response.status_code == 201, quote_response.text
    quote_id = quote_response.json()["quote_id"]
    failed = client.post(
        "/api/generate",
        json={**payload, "quote_id": quote_id},
        headers=headers,
    )
    assert failed.status_code == 400, failed.text

    with SessionLocal() as db:
        quote = db.get(GenerationQuote, quote_id)
        assert quote is not None
        assert quote.status == "active"
        assert quote.task_id is None
        assert db.query(GenTask).filter_by(user_id=user_id).count() == 0
        revisions = db.query(ReverseResultRevision).filter_by(operation_id=operation_id).all()
        assert [row.source for row in revisions] == [
            "provider_raw", "normalized", "user_edit", "applied"
        ]
        assert revisions[-1].id == revision_id
        assert db.query(CreditTransaction).filter_by(user_id=user_id, type="freeze").count() == 0


def test_switching_target_model_reuses_source_and_only_adds_compiled_generation_revisions(
    client, make_user, auth, quote_and_generate
):
    user_id = make_user("13971100005", balance=100)
    headers = auth("13971100005")
    operation_id, revision_id = _seed_reverse_result(user_id)

    with SessionLocal() as db:
        default_model = get_model_config(db, "image")
        assert default_model is not None
        alternate = ModelConfig(
            use="image",
            model_id="lineage-alternate-image",
            display_name="Lineage Alternate Image",
            is_default=False,
            sort_order=999,
            provider=default_model.provider,
            base_url=default_model.base_url,
            api_key_encrypted=default_model.api_key_encrypted,
            gateway_format=default_model.gateway_format,
            cost_credits=default_model.cost_credits,
            unlock_cost=default_model.unlock_cost,
            enabled=True,
            extra=dict(default_model.extra or {}),
        )
        db.add(alternate)
        db.commit()
        default_model_id = int(default_model.id)
        alternate_model_id = int(alternate.id)

    try:
        first = quote_and_generate(
            {
                **_payload(operation_id, revision_id, request_id="generation-lineage-model-001"),
                "model_config_id": default_model_id,
            },
            headers=headers,
        )
        second = quote_and_generate(
            {
                **_payload(operation_id, revision_id, request_id="generation-lineage-model-002"),
                "model_config_id": alternate_model_id,
            },
            headers=headers,
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        with SessionLocal() as db:
            revisions = (
                db.query(ReverseResultRevision)
                .filter_by(operation_id=operation_id)
                .order_by(ReverseResultRevision.version)
                .all()
            )
            assert [row.source for row in revisions] == [
                "provider_raw",
                "normalized",
                "user_edit",
                "applied",
                "model_compiled",
                "generation",
                "model_compiled",
                "generation",
            ]
            compiled_model_ids = {
                row.payload["target_model"]["model_config_id"]
                for row in revisions
                if row.source == "model_compiled"
            }
            assert compiled_model_ids == {default_model_id, alternate_model_id}
            tasks = db.query(GenTask).filter_by(reverse_operation_id=operation_id).all()
            assert len(tasks) == 2
            assert {task.source_revision_id for task in tasks} == {revision_id}
    finally:
        with SessionLocal() as db:
            alternate = db.get(ModelConfig, alternate_model_id)
            if alternate is not None:
                tasks = db.query(GenTask).filter_by(reverse_operation_id=operation_id).all()
                task_ids = [int(task.id) for task in tasks]
                quotes = (
                    db.query(GenerationQuote)
                    .filter(GenerationQuote.task_id.in_(task_ids))
                    .all()
                    if task_ids else []
                )
                for task in tasks:
                    task.quote_id = None
                for quote in quotes:
                    quote.task_id = None
                db.flush()
                if task_ids:
                    db.query(GenerationDispatch).filter(
                        GenerationDispatch.task_id.in_(task_ids)
                    ).delete(synchronize_session=False)
                    db.query(GatewayCall).filter(GatewayCall.task_id.in_(task_ids)).delete(
                        synchronize_session=False
                    )
                    db.query(GenAsset).filter(GenAsset.task_id.in_(task_ids)).delete(
                        synchronize_session=False
                    )
                    db.query(GenTask).filter(GenTask.id.in_(task_ids)).delete(
                        synchronize_session=False
                    )
                for quote in quotes:
                    db.delete(quote)
                db.delete(alternate)
                db.commit()


@pytest.mark.parametrize("source", ["normalized", "user_edit"])
def test_reverse_generation_requires_applied_revision_before_quote(
    client,
    make_user,
    auth,
    source,
):
    user_id = make_user("13971100006", balance=100)
    headers = auth("13971100006")
    operation_id, revision_id = _seed_reverse_result(user_id, source=source)

    blocked = client.post(
        "/api/quotes",
        json=_payload(
            operation_id,
            revision_id,
            request_id=f"lineage-reject-{source}-quote-001",
        ),
        headers=headers,
    )

    assert blocked.status_code == 409, blocked.text
    assert "applied" in blocked.text
    with SessionLocal() as db:
        assert db.query(GenerationQuote).filter_by(user_id=user_id).count() == 0
        assert db.query(GenTask).filter_by(user_id=user_id).count() == 0


@pytest.mark.parametrize(
    ("operation_purpose", "revision_purpose"),
    [
        ("analysis_report", None),
        ("generation", "analysis_report"),
    ],
)
def test_analysis_report_lineage_is_rejected_before_quote(
    client,
    make_user,
    auth,
    operation_purpose,
    revision_purpose,
):
    user_id = make_user("13971100007", balance=100)
    headers = auth("13971100007")
    operation_id, revision_id = _seed_reverse_result(
        user_id,
        output_purpose=operation_purpose,
        revision_output_purpose=revision_purpose,
    )
    payload = _payload(
        operation_id,
        revision_id,
        request_id=f"analysis-report-{operation_purpose}-{revision_purpose or 'operation'}-001",
    )

    quoted = client.post("/api/quotes", json=payload, headers=headers)

    assert quoted.status_code == 409, quoted.text
    assert "分析报告" in quoted.text
    with SessionLocal() as db:
        assert db.query(GenerationQuote).filter_by(user_id=user_id).count() == 0
        assert db.query(GenTask).filter_by(user_id=user_id).count() == 0


def test_reverse_revision_must_belong_to_the_selected_operation(client, make_user, auth):
    user_id = make_user("13971100008", balance=100)
    headers = auth("13971100008")
    first_operation_id, _ = _seed_reverse_result(user_id)
    _, second_revision_id = _seed_reverse_result(user_id)

    blocked = client.post(
        "/api/quotes",
        json=_payload(
            first_operation_id,
            second_revision_id,
            request_id="lineage-operation-revision-mismatch-001",
        ),
        headers=headers,
    )

    assert blocked.status_code == 404, blocked.text
    assert "反推结果版本不存在" in blocked.text


def test_generation_without_reverse_lineage_remains_available(client, make_user, auth):
    user_id = make_user("13971100009", balance=100)
    headers = auth("13971100009")
    payload = {
        "client_request_id": "plain-generation-without-lineage-001",
        "category": "image",
        "stage": "preview",
        "instruction": "a white ceramic teapot",
        "params": {"n": 1, "size": "1024x1024"},
    }

    quote_response = client.post("/api/quotes", json=payload, headers=headers)
    assert quote_response.status_code == 201, quote_response.text
    generated = client.post(
        "/api/generate",
        json={**payload, "quote_id": quote_response.json()["quote_id"]},
        headers=headers,
    )

    assert generated.status_code == 200, generated.text
    assert generated.json()["reverse_operation_id"] is None
    assert generated.json()["source_revision_id"] is None
    with SessionLocal() as db:
        assert db.query(GenTask).filter_by(user_id=user_id).count() == 1
