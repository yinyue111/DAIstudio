"""Studio prompt optimization ownership, lineage, coverage, and decisions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from app.db import SessionLocal
from app.models import (
    AuditLog,
    CreditTransaction,
    GenerationQuote,
    ModelCapabilityVersion,
    ModelConfig,
    PromptOptimizationProposal,
    ReverseOperation,
    ReverseResultRevision,
    User,
)
from app.services import (
    credits,
    gateway,
    generation_quotes,
    prompt_optimization,
    reverse_lineage,
    video_prompt_compiler,
)
from tests.test_reverse_revision_lineage import (
    _generation_request,
    _reviewed_evidence,
    _seed_operation,
)


def _target_model_id(use: str = "image") -> int:
    with SessionLocal() as db:
        row = db.query(ModelConfig).filter_by(use=use, enabled=True).first()
        assert row is not None
        return int(row.id)


def _proposal_body(**patch) -> dict:
    body = {
        "prompt": "single perfume bottle on white, keep text \"PURE\"",
        "category": "image",
        "mode": "faithful",
        "target_model_config_id": _target_model_id(),
        "idempotency_key": "proposal-test-default",
    }
    body.update(patch)
    return body


def _quote_proposal(client, headers, body: dict) -> tuple[dict, dict]:
    request = deepcopy(body)
    request.pop("quote_id", None)
    response = client.post(
        "/api/quotes",
        json={
            "kind": "prompt_optimization",
            "client_request_id": request["idempotency_key"],
            "request": request,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    quote = response.json()
    return {**request, "quote_id": quote["quote_id"]}, quote


def _post_quoted_proposal(client, headers, body: dict):
    quoted_body, quote = _quote_proposal(client, headers, body)
    response = client.post(
        "/api/studio/prompt-optimizations",
        json=quoted_body,
        headers=headers,
    )
    return response, quote, quoted_body


def _decision_body(proposal: dict, **patch) -> dict:
    body = {
        "proposal_version": proposal["proposal_version"],
        "idempotency_key": f"decision-{proposal['proposal_id']}",
        "accepted_segment_ids": [],
        "rejected_segment_ids": [],
    }
    body.update(patch)
    return body


def _fake_optimizer(*_args, **_kwargs):
    return {
        "prompt": "single premium perfume bottle on white, keep text PURE",
        "field_suggestions": {"structured.scene": "bright white studio"},
        "constraint_coverage": [],
        "usage": None,
        "latency_ms": 1,
    }


def _seed_stale_paid_proposal(user_id: int, *, credits_reserved: int = 37) -> int:
    with SessionLocal() as db:
        target_model = db.query(ModelConfig).filter_by(use="image", enabled=True).first()
        optimizer = db.query(ModelConfig).filter_by(use="prompt", enabled=True).first()
        assert target_model is not None
        assert optimizer is not None
        capability = db.query(ModelCapabilityVersion).filter_by(
            model_config_id=target_model.id,
            is_active=True,
        ).first()
        assert capability is not None
        quote = generation_quotes.create_execution_quote(
            db,
            user_id=user_id,
            kind="prompt_optimization",
            client_request_id="stale-prompt-quote",
            request_fingerprint="a" * 64,
            category="image",
            stage="preview",
            request_snapshot={"mode": "faithful"},
            model_snapshot={"model_config_id": optimizer.id},
            pricing_snapshot={"credits": credits_reserved},
            price_breakdown=None,
            estimated_credits=credits_reserved,
            model_config_id=optimizer.id,
        )
        proposal = PromptOptimizationProposal(
            user_id=user_id,
            target_model_config_id=target_model.id,
            capability_version_id=capability.id,
            optimizer_model_config_id=optimizer.id,
            quote_id=quote.id,
            idempotency_key="stale-paid-proposal",
            status="proposed",
            version=1,
            category="image",
            mode="faithful",
            optimization_kind="rewrite",
            original={"final_text": "source"},
            suggestion={"final_text": "source"},
            diff=[],
            constraint_coverage=[],
            warnings=[],
            provenance={},
            catalog_snapshot={"capability_version_id": capability.id},
            charged_credits=0,
            metrics={"request_hash": "b" * 64, "execution_status": "running"},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        db.add(proposal)
        db.flush()
        credits.freeze(
            db,
            user_id,
            credits_reserved,
            proposal.id,
            biz_type="prompt_optimize",
            commit=False,
        )
        generation_quotes.consume_execution_quote(
            quote,
            ref_type="prompt_optimization",
            ref_id=proposal.id,
        )
        db.commit()
        proposal_id = int(proposal.id)
    with SessionLocal() as db:
        proposal = db.get(PromptOptimizationProposal, proposal_id)
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        proposal.updated_at = stale_at
        db.commit()
    return proposal_id


def test_stale_paid_prompt_optimization_is_refunded_once(client, make_user):
    user_id = make_user("13978000991", balance=100)
    proposal_id = _seed_stale_paid_proposal(user_id)

    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user.balance_credits == 63
        assert user.frozen_credits == 37
        assert prompt_optimization.reap_stale_running_proposals(db, max_seconds=60) == 1

    with SessionLocal() as db:
        user = db.get(User, user_id)
        proposal = db.get(PromptOptimizationProposal, proposal_id)
        assert user.balance_credits == 100
        assert user.frozen_credits == 0
        assert proposal.status == "expired"
        assert proposal.charged_credits == 0
        assert proposal.metrics["execution_status"] == "failed"
        assert "积分已退回" in proposal.metrics["execution_error"]
        rows = db.query(CreditTransaction).filter_by(
            user_id=user_id,
            biz_type="prompt_optimize",
            biz_ref=proposal_id,
        ).order_by(CreditTransaction.id).all()
        assert [row.type for row in rows] == ["freeze", "refund"]
        assert prompt_optimization.reap_stale_running_proposals(db, max_seconds=60) == 0


def test_paid_rewrite_requires_a_confirmed_service_quote(
    client, make_user, auth, monkeypatch
):
    make_user("13978000023", balance=1000)
    headers = auth("13978000023")
    monkeypatch.setattr(
        "app.services.prompt_optimization._invoke_optimizer",
        _fake_optimizer,
    )
    request = _proposal_body(idempotency_key="paid-rewrite-quote-required")

    unquoted = client.post(
        "/api/studio/prompt-optimizations",
        json=request,
        headers=headers,
    )
    assert unquoted.status_code == 422, unquoted.text
    assert "服务端报价" in unquoted.text

    quoted, quote = _quote_proposal(client, headers, request)
    assert quote["kind"] == "prompt_optimization"
    created = client.post(
        "/api/studio/prompt-optimizations",
        json=quoted,
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["charged_credits"] == quote["estimated_credits"]


def test_lineage_owner_tamper_and_legacy_are_rejected(client, make_user, auth, monkeypatch):
    owner_id = make_user("13978000001")
    make_user("13978000002")
    owner_headers = auth("13978000001")
    other_headers = auth("13978000002")
    operation_id, revision_ids = _seed_operation(owner_id, through="normalized", suffix="optimizer-owner")
    monkeypatch.setattr("app.services.prompt_optimization._invoke_optimizer", _fake_optimizer)
    lineage = {
        "prompt": None,
        "category": None,
        "reverse_operation_id": operation_id,
        "reverse_revision_id": revision_ids["normalized"],
        "target_model_config_id": _target_model_id(),
        "mode": "faithful",
        "idempotency_key": "owner-lineage-proposal",
    }

    ok, _, quoted_lineage = _post_quoted_proposal(
        client,
        owner_headers,
        lineage,
    )
    assert ok.status_code == 201, ok.text
    replay = client.post(
        "/api/studio/prompt-optimizations",
        json=quoted_lineage,
        headers=owner_headers,
    )
    assert replay.status_code == 201
    assert replay.json()["proposal_id"] == ok.json()["proposal_id"]
    cross_owner_decision = client.post(
        f"/api/studio/prompt-optimizations/{ok.json()['proposal_id']}/reject",
        json=_decision_body(ok.json()),
        headers=other_headers,
    )
    assert cross_owner_decision.status_code == 404
    cross_owner = client.post(
        "/api/studio/prompt-optimizations",
        json={**lineage, "idempotency_key": "cross-owner-proposal"},
        headers=other_headers,
    )
    assert cross_owner.status_code == 404
    tampered = client.post(
        "/api/studio/prompt-optimizations",
        json={
            **lineage,
            "prompt": "forged context",
            "idempotency_key": "tampered-context-proposal",
        },
        headers=owner_headers,
    )
    assert tampered.status_code == 422
    assert "服务端" in tampered.text

    legacy_operation, legacy_ids = _seed_operation(
        owner_id,
        through="normalized",
        lineage_status=reverse_lineage.LEGACY_UNVERIFIED,
        suffix="optimizer-legacy",
    )
    legacy = client.post(
        "/api/studio/prompt-optimizations",
        json={
            **lineage,
            "reverse_operation_id": legacy_operation,
            "reverse_revision_id": legacy_ids["normalized"],
            "idempotency_key": "legacy-lineage-proposal",
        },
        headers=owner_headers,
    )
    assert legacy.status_code == 409
    assert "未完成血缘验证" in legacy.text


def test_history_list_and_detail_are_owner_scoped_and_paginated(
    client, make_user, auth, monkeypatch
):
    make_user("13978000021")
    make_user("13978000022")
    owner_headers = auth("13978000021")
    other_headers = auth("13978000022")
    monkeypatch.setattr(
        "app.services.prompt_optimization._invoke_optimizer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("免费模型编译不应调用优化器")
        ),
    )

    proposal_ids = []
    for index in range(3):
        response = client.post(
            "/api/studio/prompt-optimizations",
            json=_proposal_body(
                prompt=f"history prompt {index}",
                mode="target_model_adaptation",
                idempotency_key=f"history-owner-proposal-{index}",
            ),
            headers=owner_headers,
        )
        assert response.status_code == 201, response.text
        proposal_ids.append(response.json()["proposal_id"])

    foreign = client.post(
        "/api/studio/prompt-optimizations",
        json=_proposal_body(
            prompt="foreign history prompt",
            mode="target_model_adaptation",
            idempotency_key="history-foreign-proposal",
        ),
        headers=other_headers,
    )
    assert foreign.status_code == 201, foreign.text

    first_page = client.get(
        "/api/studio/prompt-optimizations?limit=2&offset=0",
        headers=owner_headers,
    )
    assert first_page.status_code == 200, first_page.text
    page = first_page.json()
    assert page["limit"] == 2
    assert page["offset"] == 0
    assert page["has_more"] is True
    assert [row["proposal_id"] for row in page["items"]] == list(
        reversed(proposal_ids[-2:])
    )
    assert all(row["can_decide"] is True for row in page["items"])
    assert all(row["status"] == "proposed" for row in page["items"])
    assert all("foreign" not in row["original_preview"] for row in page["items"])

    second_page = client.get(
        "/api/studio/prompt-optimizations?limit=2&offset=2",
        headers=owner_headers,
    )
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["has_more"] is False
    assert [row["proposal_id"] for row in second_page.json()["items"]] == [
        proposal_ids[0]
    ]

    detail = client.get(
        f"/api/studio/prompt-optimizations/{proposal_ids[-1]}",
        headers=owner_headers,
    )
    assert detail.status_code == 200, detail.text
    reopened = detail.json()
    assert reopened["proposal_id"] == proposal_ids[-1]
    assert reopened["status"] == "proposed"
    assert reopened["can_decide"] is True
    assert reopened["original"]["final_text"] == "history prompt 2"
    assert reopened["suggestion"]["final_text"]
    assert reopened["created_at"]
    assert reopened["expires_at"]

    hidden = client.get(
        f"/api/studio/prompt-optimizations/{proposal_ids[-1]}",
        headers=other_headers,
    )
    assert hidden.status_code == 404


@pytest.mark.parametrize(
    ("selected_source", "phone_suffix"),
    [("normalized", "11"), ("user_edit", "12"), ("applied", "13")],
)
def test_accept_branches_from_exact_selected_lineage_parent(
    client,
    make_user,
    auth,
    monkeypatch,
    selected_source,
    phone_suffix,
):
    user_id = make_user(f"139780000{phone_suffix}", balance=1000)
    headers = auth(f"139780000{phone_suffix}")
    payload = {
        "final_text": f"selected {selected_source} prompt",
        "structured": {"subject": "perfume bottle", "source_marker": selected_source},
    }
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=payload,
        through=selected_source,
        suffix=f"optimizer-selected-{selected_source}",
    )
    selected_id = revision_ids[selected_source]
    expected_parent_id = (
        revision_ids["user_edit"] if selected_source == "applied" else selected_id
    )
    expected_parent_source = "user_edit" if selected_source == "applied" else selected_source
    monkeypatch.setattr("app.services.prompt_optimization._invoke_optimizer", _fake_optimizer)

    proposed, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=selected_id,
            idempotency_key=f"selected-{selected_source}-proposal",
        ),
    )
    assert proposed.status_code == 201, proposed.text
    proposal = proposed.json()
    assert proposal["original"]["structured"]["source_marker"] == selected_source
    assert proposal["provenance"]["selected_revision_id"] == selected_id
    assert proposal["provenance"]["selected_revision_source"] == selected_source
    assert proposal["provenance"]["resolved_parent_revision_id"] == expected_parent_id
    assert (
        proposal["provenance"]["resolved_parent_revision_source"]
        == expected_parent_source
    )

    if selected_source == "user_edit":
        newer_payload = deepcopy(payload)
        newer_payload["final_text"] = "newer revision that was not selected"
        newer = client.post(
            f"/api/prompt/reverse-operations/{operation_id}/revisions",
            json={
                "source": "user_edit",
                "parent_revision_id": selected_id,
                "payload": newer_payload,
            },
            headers=headers,
        )
        assert newer.status_code == 200, newer.text
        assert newer.json()["id"] != selected_id

    accepted = client.post(
        f"/api/studio/prompt-optimizations/{proposal['proposal_id']}/accept",
        json=_decision_body(proposal),
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text
    revision = accepted.json()["revision"]
    assert revision["selected_revision_id"] == selected_id
    assert revision["selected_revision_source"] == selected_source
    assert revision["resolved_parent_revision_id"] == expected_parent_id
    assert revision["resolved_parent_revision_source"] == expected_parent_source
    assert revision["payload"]["structured"]["source_marker"] == selected_source

    with SessionLocal() as db:
        optimized = db.get(ReverseResultRevision, revision["user_edit_revision_id"])
        applied = db.get(ReverseResultRevision, revision["id"])
        assert optimized.parent_revision_id == expected_parent_id
        chain = reverse_lineage.validate_revision_chain(
            db,
            applied,
            terminal_source="applied",
        )
        assert chain[1].id == optimized.id
        assert chain[2].id == expected_parent_id

    if selected_source == "normalized":
        request = _generation_request(
            operation_id,
            revision["id"],
            "optimized-lineage-generation-001",
        )
        quoted = client.post("/api/quotes", json=request, headers=headers)
        assert quoted.status_code == 201, quoted.text
        generated = client.post(
            "/api/generate",
            json={**request, "quote_id": quoted.json()["quote_id"]},
            headers=headers,
        )
        assert generated.status_code == 200, generated.text
        with SessionLocal() as db:
            sources = [
                row.source
                for row in db.query(ReverseResultRevision)
                .filter_by(operation_id=operation_id)
                .order_by(ReverseResultRevision.version)
            ]
            assert sources == [
                "provider_raw",
                "normalized",
                "user_edit",
                "applied",
                "model_compiled",
                "generation",
            ]


def test_accept_revalidates_selected_revision_and_rejects_unsupported_source(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000014")
    headers = auth("13978000014")
    operation_id, revision_ids = _seed_operation(
        user_id,
        through="normalized",
        suffix="optimizer-decision-tamper",
    )
    monkeypatch.setattr("app.services.prompt_optimization._invoke_optimizer", _fake_optimizer)
    unsupported = client.post(
        "/api/studio/prompt-optimizations",
        json=_proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["provider_raw"],
            idempotency_key="unsupported-provider-proposal",
        ),
        headers=headers,
    )
    assert unsupported.status_code == 409, unsupported.text
    assert "不支持从 provider_raw" in unsupported.text

    proposed, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["normalized"],
            idempotency_key="decision-tamper-proposal",
        ),
    )
    assert proposed.status_code == 201, proposed.text
    proposal = proposed.json()
    with SessionLocal() as db:
        selected = db.get(ReverseResultRevision, revision_ids["normalized"])
        selected.payload = {**selected.payload, "final_text": "tampered after proposal"}
        db.commit()

    rejected = client.post(
        f"/api/studio/prompt-optimizations/{proposal['proposal_id']}/accept",
        json=_decision_body(proposal),
        headers=headers,
    )
    assert rejected.status_code == 409, rejected.text
    assert "内容哈希不一致" in rejected.text
    with SessionLocal() as db:
        assert (
            db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count()
            == 2
        )


def test_concurrent_cas_loser_rolls_back_both_revisions_and_decision_audit(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000015")
    headers = auth("13978000015")
    operation_id, revision_ids = _seed_operation(
        user_id,
        through="normalized",
        suffix="optimizer-cas-rollback",
    )
    monkeypatch.setattr("app.services.prompt_optimization._invoke_optimizer", _fake_optimizer)
    proposed, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["normalized"],
            idempotency_key="cas-rollback-proposal",
        ),
    )
    assert proposed.status_code == 201, proposed.text
    proposal = proposed.json()
    original_execute = Session.execute

    def lose_decision_cas(session, statement, *args, **kwargs):
        if (
            isinstance(statement, Update)
            and getattr(statement.table, "name", None)
            == "prompt_optimization_proposals"
        ):
            return SimpleNamespace(rowcount=0)
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", lose_decision_cas)
    response = client.post(
        f"/api/studio/prompt-optimizations/{proposal['proposal_id']}/accept",
        json=_decision_body(proposal),
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert "并发处理" in response.text

    with SessionLocal() as db:
        assert (
            db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count()
            == 2
        )
        stored = db.get(PromptOptimizationProposal, proposal["proposal_id"])
        assert stored.status == "proposed"
        assert stored.version == 1
        assert db.query(AuditLog).filter_by(
            biz_type="prompt_optimization",
            biz_id=proposal["proposal_id"],
            action="studio.prompt_optimization.accepted",
        ).count() == 0


def test_expired_proposal_creates_no_revision_or_decision_audit(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000018")
    headers = auth("13978000018")
    operation_id, revision_ids = _seed_operation(
        user_id,
        through="normalized",
        suffix="optimizer-expired",
    )
    monkeypatch.setattr("app.services.prompt_optimization._invoke_optimizer", _fake_optimizer)
    proposed, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["normalized"],
            idempotency_key="expired-lineage-proposal",
        ),
    )
    assert proposed.status_code == 201, proposed.text
    proposal = proposed.json()
    with SessionLocal() as db:
        stored = db.get(PromptOptimizationProposal, proposal["proposal_id"])
        stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    response = client.post(
        f"/api/studio/prompt-optimizations/{proposal['proposal_id']}/accept",
        json=_decision_body(proposal),
        headers=headers,
    )
    assert response.status_code == 410, response.text
    with SessionLocal() as db:
        assert (
            db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count()
            == 2
        )
        stored = db.get(PromptOptimizationProposal, proposal["proposal_id"])
        assert stored.status == "expired"
        assert stored.version == 2
        assert db.query(AuditLog).filter_by(
            biz_type="prompt_optimization",
            biz_id=proposal["proposal_id"],
            action="studio.prompt_optimization.accepted",
        ).count() == 0


def test_partial_accept_creates_server_revision_and_reject_creates_none(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000003")
    headers = auth("13978000003")
    payload = {
        "final_text": "single perfume bottle",
        "structured": {"subject": "perfume bottle", "scene": "white table"},
        "image_evidence": _reviewed_evidence(),
        "source_marker": "selected-normalized-payload",
    }
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=payload,
        through="normalized",
        suffix="optimizer-partial",
    )
    monkeypatch.setattr("app.services.prompt_optimization._invoke_optimizer", _fake_optimizer)
    response, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["normalized"],
            idempotency_key="partial-proposal",
        ),
    )
    assert response.status_code == 201, response.text
    proposal = response.json()
    final_segment = next(row for row in proposal["segments"] if row["field_path"] == "final_text")
    scene_segment = next(row for row in proposal["segments"] if row["field_path"] == "structured.scene")
    accepted = client.post(
        f"/api/studio/prompt-optimizations/{proposal['proposal_id']}/accept",
        json=_decision_body(
            proposal,
            accepted_segment_ids=[final_segment["id"]],
            rejected_segment_ids=[scene_segment["id"]],
        ),
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text
    decision = accepted.json()
    assert decision["status"] == "partially_accepted"
    assert decision["revision"]["source"] == "applied"
    assert decision["revision"]["user_edit_revision_id"] > 0
    assert decision["revision"]["payload"]["structured"]["scene"] == "white table"
    assert decision["revision"]["payload"]["final_text"].startswith("single premium")
    assert decision["revision"]["payload"]["image_evidence"] == _reviewed_evidence()
    assert decision["revision"]["payload"]["source_marker"] == "selected-normalized-payload"
    repeated = client.post(
        f"/api/studio/prompt-optimizations/{proposal['proposal_id']}/accept",
        json=_decision_body(
            proposal,
            accepted_segment_ids=[final_segment["id"]],
            rejected_segment_ids=[scene_segment["id"]],
        ),
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["revision"]["id"] == decision["revision"]["id"]

    reject_operation, reject_ids = _seed_operation(
        user_id,
        payload=deepcopy(payload),
        through="normalized",
        suffix="optimizer-reject",
    )
    rejected_response, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=reject_operation,
            reverse_revision_id=reject_ids["normalized"],
            idempotency_key="reject-proposal",
        ),
    )
    assert rejected_response.status_code == 201, rejected_response.text
    rejected_proposal = rejected_response.json()
    with SessionLocal() as db:
        before = db.query(ReverseResultRevision).filter_by(operation_id=reject_operation).count()
    rejected = client.post(
        f"/api/studio/prompt-optimizations/{rejected_proposal['proposal_id']}/reject",
        json=_decision_body(rejected_proposal),
        headers=headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    with SessionLocal() as db:
        after = db.query(ReverseResultRevision).filter_by(operation_id=reject_operation).count()
    assert after == before


def test_constraint_coverage_only_verifies_deterministic_evidence(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000004")
    headers = auth("13978000004")
    payload = {
        "final_text": "show one bottle with exact text \"PURE\"",
        "negative": "watermark, extra bottle",
        "structured": {"不可改项": "bottle identity and cap shape"},
        "parameters": {"ratio": "9:16", "vDuration": 5},
    }
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=payload,
        through="normalized",
        suffix="optimizer-coverage",
    )

    def fake_with_exact_text(*_args, **_kwargs):
        return {
            "prompt": "premium shot; exact text PURE",
            "constraint_coverage": [],
            "usage": None,
            "latency_ms": 1,
        }

    monkeypatch.setattr(
        "app.services.prompt_optimization._invoke_optimizer",
        fake_with_exact_text,
    )
    response, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["normalized"],
            idempotency_key="coverage-proposal",
            protected_constraints=[
                {"type": "aspect_ratio", "value": "9:16"},
                {"type": "negative_list", "value": ["watermark", "extra bottle"]},
            ],
        ),
    )
    assert response.status_code == 201, response.text
    rows = response.json()["constraint_coverage"]
    by_type = {row["type"]: row for row in rows}
    assert by_type["aspect_ratio"]["status"] == "verified"
    assert by_type["duration"]["status"] == "verified"
    assert by_type["negative_list"]["status"] == "verified"
    assert by_type["exact_text"]["status"] == "verified"
    assert by_type["semantic"]["status"] == "unverified"
    assert not any("preserved" in row for row in rows)


def test_constraint_coverage_rejects_substring_and_metadata_false_positives(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000016")
    headers = auth("13978000016")
    payload = {
        "final_text": 'keep exact text "PURE"',
        "negative": "caterpillar, watermarking",
        "structured": {"不可改项": "bottle identity"},
    }
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=payload,
        through="normalized",
        suffix="optimizer-coverage-false-positive",
    )

    monkeypatch.setattr(
        "app.services.prompt_optimization._invoke_optimizer",
        lambda *_args, **_kwargs: {
            "prompt": "premium bottle on white",
            "field_suggestions": {"structured.metadata": "PURE cat watermark"},
            "constraint_coverage": [],
            "usage": None,
            "latency_ms": 1,
        },
    )
    response, _, _ = _post_quoted_proposal(
        client,
        headers,
        _proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["normalized"],
            idempotency_key="coverage-false-positive-proposal",
            protected_constraints=[
                {"type": "negative_list", "value": ["cat", "watermark"]},
            ],
        ),
    )
    assert response.status_code == 201, response.text
    rows = response.json()["constraint_coverage"]
    requested_negative = next(
        row for row in rows
        if row["type"] == "negative_list" and row["value"] == ["cat", "watermark"]
    )
    exact_text = next(row for row in rows if row["type"] == "exact_text")
    semantic = next(row for row in rows if row["type"] == "semantic")
    assert requested_negative["status"] == "unverified"
    assert exact_text["status"] == "unverified"
    assert semantic["status"] == "unverified"


def test_model_switch_compiles_from_catalog_without_reverse_or_credit_cost(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000005")
    headers = auth("13978000005")
    target_model_id = _target_model_id()
    with SessionLocal() as db:
        target_model = db.get(ModelConfig, target_model_id)
        original_extra = deepcopy(target_model.extra)
        configured_extra = deepcopy(original_extra or {})
        configured_extra["prompt_profile"] = {
            "name": "studio-preview-profile",
            "prefix": "TARGET-IMAGE-PREFIX",
            "suffix": "TARGET-IMAGE-SUFFIX",
            "separator": " | ",
        }
        target_model.extra = configured_extra
        db.commit()
    payload = {
        "final_text": "single bottle",
        "parameters": {"ratio": "21:9", "resolution": "8k"},
    }
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=payload,
        through="normalized",
        suffix="optimizer-compile",
    )
    monkeypatch.setattr(
        "app.services.prompt_optimization._invoke_optimizer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compiler called optimizer")),
    )
    with SessionLocal() as db:
        operation_count = db.query(ReverseOperation).count()
        credit_count = db.query(CreditTransaction).filter_by(user_id=user_id).count()
    try:
        response = client.post(
            "/api/studio/prompt-optimizations",
            json=_proposal_body(
                prompt=None,
                category=None,
                reverse_operation_id=operation_id,
                reverse_revision_id=revision_ids["normalized"],
                target_model_config_id=target_model_id,
                mode="target_model_adaptation",
                idempotency_key="compile-model-switch",
            ),
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["optimization_kind"] == "model_compile"
        assert body["charged_credits"] == 0
        assert body["original"]["final_text"] == "single bottle"
        assert body["suggestion"]["final_text"].startswith("TARGET-IMAGE-PREFIX | ")
        assert body["suggestion"]["final_text"].endswith(" | TARGET-IMAGE-SUFFIX")
        compiled = body["suggestion"]["model_compiled"]
        assert compiled["source_prompt"] == body["original"]["final_text"]
        assert compiled["prompt"] == body["suggestion"]["final_text"]
        assert compiled["compiler"]["version"] == "image-generation-runtime.v1"
        assert compiled["compiler"]["prompt_profile"]["name"] == "studio-preview-profile"
        assert body["provenance"]["reverse_revision_id"] == revision_ids["normalized"]
        assert body["compiler_profile"]["capability_version_id"] > 0
        assert any(row["action"] in {"dropped", "unverified"} for row in body["warnings"])
        with SessionLocal() as db:
            assert db.query(ReverseOperation).count() == operation_count
            assert db.query(CreditTransaction).filter_by(user_id=user_id).count() == credit_count
            stored = db.get(PromptOptimizationProposal, body["proposal_id"])
            assert stored.capability_version_id == body["compiler_profile"]["capability_version_id"]
    finally:
        with SessionLocal() as db:
            target_model = db.get(ModelConfig, target_model_id)
            target_model.extra = original_extra
            db.commit()


def test_video_model_compile_uses_runtime_compiler_and_separates_audio_tracks(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978999051")
    headers = auth("13978999051")
    source_prompt = "Shot 1：产品稳定入镜"
    payload = {
        "final_text": source_prompt,
        "structured": {
            "字幕卖点": "干湿两用",
            "旁白": "温柔开启新一天",
            "音效": "水滴声",
        },
        "parameters": {"ratio": "9:16", "vDuration": 5, "resolution": "1080p"},
    }
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=payload,
        through="normalized",
        suffix="optimizer-video-runtime-compile",
    )
    with SessionLocal() as db:
        operation = db.get(ReverseOperation, operation_id)
        operation.target = "video"
        operation.asset_url = "http://example.com/runtime-compile-source.mp4"
        operation.request_context = {
            "source_type": "video",
            "output_purpose": "generation",
            "sources": [{
                "asset_url": operation.asset_url,
                "source_type": "video",
                "role": "primary",
            }],
        }
        db.commit()
        operation_count = db.query(ReverseOperation).count()
        credit_count = db.query(CreditTransaction).filter_by(user_id=user_id).count()
    monkeypatch.setattr(
        "app.services.prompt_optimization._invoke_optimizer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("model compile reran the optimizer or reverse analysis")
        ),
    )
    monkeypatch.setattr(
        "app.services.gateway.reverse_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("model compile reran visual reverse")
        ),
    )

    response = client.post(
        "/api/studio/prompt-optimizations",
        json=_proposal_body(
            prompt=None,
            category=None,
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_ids["normalized"],
            target_model_config_id=_target_model_id("video"),
            mode="target_model_adaptation",
            idempotency_key="video-runtime-model-compile-preview",
            protected_constraints=[
                {"type": "exact_text", "value": "干湿两用"},
                {"type": "exact_text", "value": "温柔开启新一天"},
            ],
        ),
        headers=headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["original"]["final_text"] == source_prompt
    visual_prompt = body["suggestion"]["final_text"]
    assert visual_prompt.startswith("风格设定：")
    assert "\n场景脚本：\n" in visual_prompt
    assert "\n技术约束：" in visual_prompt
    assert "干湿两用" not in visual_prompt
    assert "温柔开启新一天" not in visual_prompt
    assert "水滴声" not in visual_prompt
    compiled = body["suggestion"]["model_compiled"]
    assert compiled["source_prompt"] == source_prompt
    assert compiled["prompt"] == visual_prompt
    assert compiled["compiler"]["version"] == video_prompt_compiler.COMPILER_VERSION
    assert compiled["compiler"]["submit_contract_version"] == (
        video_prompt_compiler.VIDEO_SUBMIT_CONTRACT_VERSION
    )
    assert compiled["post_production"] == {
        "subtitles": ["干湿两用"],
        "voiceover": "温柔开启新一天",
        "sfx": ["水滴声"],
    }
    assert compiled["reference_roles"] == [{
        "role": "motion_analysis",
        "source": "source_asset_url",
        "mode": "analysis_only",
    }]
    exact_text_coverage = {
        row["value"]: row for row in body["constraint_coverage"]
        if row["type"] == "exact_text"
    }
    assert exact_text_coverage["干湿两用"]["status"] == "verified"
    assert exact_text_coverage["温柔开启新一天"]["status"] == "verified"
    with SessionLocal() as db:
        assert db.query(ReverseOperation).count() == operation_count
        assert db.query(CreditTransaction).filter_by(user_id=user_id).count() == credit_count


def test_compiler_dropped_warnings_match_actual_candidate_removal(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13978000017")
    headers = auth("13978000017")
    target_model_id = _target_model_id()
    with SessionLocal() as db:
        capability = db.query(ModelCapabilityVersion).filter_by(
            model_config_id=target_model_id,
            is_active=True,
        ).one()
        original_capabilities = deepcopy(capability.capabilities)
        configured = deepcopy(original_capabilities)
        configured.update({
            "aspect_ratios": ["1:1"],
            "resolutions": ["1024x1024"],
            "prompt_profile": {"dropped_fields": ["structured.scene"]},
        })
        capability.capabilities = configured
        db.commit()
    try:
        payload = {
            "final_text": "single bottle",
            "structured": {"subject": "bottle", "scene": "white table"},
            "parameters": {"ratio": "21:9", "resolution": "8k"},
        }
        operation_id, revision_ids = _seed_operation(
            user_id,
            payload=payload,
            through="normalized",
            suffix="optimizer-actual-drops",
        )
        monkeypatch.setattr(
            "app.services.prompt_optimization._invoke_optimizer",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("compiler called optimizer")
            ),
        )
        response = client.post(
            "/api/studio/prompt-optimizations",
            json=_proposal_body(
                prompt=None,
                category=None,
                reverse_operation_id=operation_id,
                reverse_revision_id=revision_ids["normalized"],
                target_model_config_id=target_model_id,
                mode="target_model_adaptation",
                idempotency_key="actual-drops-proposal",
            ),
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert "ratio" not in body["suggestion"]["parameters"]
        assert "resolution" not in body["suggestion"]["parameters"]
        assert "scene" not in body["suggestion"]["structured"]
        dropped = [row for row in body["warnings"] if row["action"] == "dropped"]
        assert {row["field"] for row in dropped} == {
            "aspect_ratio",
            "resolution",
            "structured.scene",
        }
        for warning in dropped:
            if warning["field"] == "structured.scene":
                assert "scene" not in body["suggestion"]["structured"]
            else:
                assert warning["field"] not in body["suggestion"]["parameters"]
    finally:
        with SessionLocal() as db:
            capability = db.query(ModelCapabilityVersion).filter_by(
                model_config_id=target_model_id,
                is_active=True,
            ).one()
            capability.capabilities = original_capabilities
            db.commit()
