from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models import (
    AuditLog,
    GenerationQuote,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    PaymentOrder,
    PromptOptimizationProposal,
    ReverseOperation,
    ToolDefinition,
    ToolRun,
    ToolVersion,
    WorkflowRun,
)
from app.services import credits


@pytest.fixture(autouse=True)
def _quiesce_seeded_billing_reverse_operations():
    yield
    with SessionLocal() as db:
        db.query(ReverseOperation).filter(
            ReverseOperation.request_fingerprint == "c" * 64,
            ReverseOperation.status == "running",
        ).update({"status": "canceled"}, synchronize_session=False)
        db.commit()


def _seed_billing_rows(user_id: int, other_user_id: int) -> dict[str, int]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with SessionLocal() as db:
        generation = GenTask(
            user_id=user_id,
            category="video",
            stage="final",
            prompt={"final_text": "product launch"},
            model_use="video",
            params={
                "_model_snapshot": {
                    "model_name": "Video Prime",
                    "model_id": "video-prime-v1",
                    "provider": "test-provider",
                }
            },
            status="succeeded",
            cost_frozen=20,
            cost_settled=15,
            created_at=now - timedelta(minutes=4),
            finished_at=now - timedelta(minutes=3),
        )
        reverse = ReverseOperation(
            user_id=user_id,
            request_fingerprint="c" * 64,
            target="image",
            asset_url="https://example.com/billing.png",
            analysis_focus="style",
            status="running",
            progress=40,
            model_snapshot={
                "model_name": "Vision Prime",
                "model_id": "vision-prime-v1",
                "provider": "test-provider",
            },
            cost_frozen=12,
            cost_settled=0,
            created_at=now - timedelta(minutes=2),
        )
        payment = PaymentOrder(
            order_no=f"billing-{user_id}",
            user_id=user_id,
            provider="alipay",
            package_id="starter",
            amount_cents=990,
            credits=100,
            status="paid",
            paid_at=now - timedelta(minutes=1),
            created_at=now - timedelta(minutes=1),
        )
        tool = ToolDefinition(
            slug=f"billing-workflow-{user_id}",
            name="分镜合成工具",
            category="workflow",
            renderer="studio",
            entry_path="/?workflow=billing",
            enabled=True,
        )
        db.add_all([generation, reverse, payment, tool])
        db.flush()
        version = ToolVersion(
            tool_definition_id=tool.id,
            version=1,
            schema_version="tool.v1",
            input_schema={},
            workflow={"type": "workflow.v1", "nodes": [{"key": "compose", "type": "compose"}]},
            pricing_policy={"type": "server_quote", "credits": 8},
            capabilities={},
            is_active=True,
        )
        db.add(version)
        db.flush()
        tool_run = ToolRun(
            user_id=user_id,
            tool_definition_id=tool.id,
            tool_version_id=version.id,
            client_request_id=f"billing-workflow-{user_id}-request",
            request_fingerprint="d" * 64,
            status="succeeded",
            input_snapshot={"composition": {"title": "Billing clip"}},
            pricing_snapshot={"type": "server_quote", "credits": 8},
            cost_frozen=8,
            cost_settled=8,
            created_at=now - timedelta(seconds=45),
            finished_at=now - timedelta(seconds=30),
        )
        db.add(tool_run)
        db.flush()
        workflow_run = WorkflowRun(
            tool_run_id=tool_run.id,
            user_id=user_id,
            workflow_schema_version="workflow.v1",
            workflow_snapshot=version.workflow,
            status="succeeded",
            created_at=now - timedelta(seconds=45),
            finished_at=now - timedelta(seconds=30),
        )
        db.add(workflow_run)
        db.flush()

        credits.freeze(db, user_id, 20, generation.id, commit=False)
        credits.settle(db, user_id, 20, 15, generation.id, commit=False)
        credits.freeze(
            db,
            user_id,
            12,
            reverse.id,
            biz_type="reverse_operation",
            commit=False,
        )
        credits.freeze(
            db,
            user_id,
            8,
            tool_run.id,
            biz_type="tool_run",
            commit=False,
        )
        credits.settle(
            db,
            user_id,
            8,
            8,
            tool_run.id,
            biz_type="tool_run",
            commit=False,
        )
        credits.grant(
            db,
            user_id,
            100,
            note=f"payment alipay {payment.order_no}",
            biz_type="payment",
            biz_ref=payment.id,
            commit=False,
        )
        credits.grant(db, user_id, 7, note="运营补偿 A", biz_type="admin", commit=False)
        credits.grant(db, user_id, 9, note="运营补偿 B", biz_type="admin", commit=False)
        credits.consume(
            db,
            user_id,
            3,
            biz_type="prompt_optimize",
            note="model=prompt-prime-v1",
            commit=False,
        )
        credits.grant(db, other_user_id, 999, note="other user", biz_type="admin", commit=False)
        db.commit()

        return {
            "generation_id": int(generation.id),
            "reverse_id": int(reverse.id),
            "payment_id": int(payment.id),
            "tool_run_id": int(tool_run.id),
            "workflow_run_id": int(workflow_run.id),
        }


def test_credit_transactions_are_filtered_paginated_and_user_scoped(client, make_user, auth):
    user_id = make_user("13971300001", balance=200)
    other_user_id = make_user("13971300002", balance=2000)
    ids = _seed_billing_rows(user_id, other_user_id)
    headers = auth("13971300001")

    first = client.get("/api/me/credit-transactions?limit=2", headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 9
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"]
    assert all(item["change"] != 999 for item in body["items"])

    second = client.get(
        "/api/me/credit-transactions",
        params={"limit": 10, "cursor": body["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    second_ids = {item["id"] for item in second.json()["items"]}
    assert second_ids.isdisjoint({item["id"] for item in body["items"]})

    freezes = client.get(
        "/api/me/credit-transactions",
        params={"type": "freeze", "biz_type": "gen_task"},
        headers=headers,
    )
    assert freezes.status_code == 200, freezes.text
    assert freezes.json()["total"] == 1
    assert freezes.json()["items"][0]["biz_ref"] == ids["generation_id"]
    assert freezes.json()["items"][0]["frozen_delta"] == 20

    invalid = client.get(
        "/api/me/credit-transactions?cursor=not-a-cursor",
        headers=headers,
    )
    assert invalid.status_code == 400
    assert "游标" in invalid.text


def test_billing_entries_aggregate_credit_lifecycle_and_enrich_business_data(
    client,
    make_user,
    auth,
):
    user_id = make_user("13971300003", balance=200)
    other_user_id = make_user("13971300004", balance=2000)
    ids = _seed_billing_rows(user_id, other_user_id)
    headers = auth("13971300003")

    generation = client.get(
        "/api/me/billing/entries?kind=generation",
        headers=headers,
    )
    assert generation.status_code == 200, generation.text
    assert generation.json()["total"] == 1
    item = generation.json()["items"][0]
    assert item["key"] == f"gen_task:{ids['generation_id']}"
    assert item["title"] == "视频生成"
    assert item["status"] == "succeeded"
    assert item["model_name"] == "Video Prime"
    assert item["frozen_credits"] == 20
    assert item["settled_credits"] == 15
    assert item["refunded_credits"] == 5
    assert item["outstanding_frozen_credits"] == 0
    assert item["net_consumed_credits"] == 15
    assert item["net_balance_change"] == -15
    assert item["transaction_count"] == 2

    reverse = client.get("/api/me/billing/entries?kind=reverse", headers=headers)
    assert reverse.status_code == 200, reverse.text
    reverse_item = reverse.json()["items"][0]
    assert reverse_item["key"] == f"reverse_operation:{ids['reverse_id']}"
    assert reverse_item["status"] == "running"
    assert reverse_item["status_group"] == "active"
    assert reverse_item["outstanding_frozen_credits"] == 12
    assert reverse_item["net_consumed_credits"] == 0
    assert reverse_item["model_name"] == "Vision Prime"

    recharge = client.get("/api/me/billing/entries?kind=recharge", headers=headers)
    assert recharge.status_code == 200, recharge.text
    recharge_item = recharge.json()["items"][0]
    assert recharge_item["key"] == f"payment:{ids['payment_id']}"
    assert recharge_item["title"] == "充值 100 积分"
    assert recharge_item["credited_credits"] == 100
    assert recharge_item["net_consumed_credits"] == 0

    grants = client.get("/api/me/billing/entries?kind=grant", headers=headers)
    assert grants.status_code == 200, grants.text
    assert grants.json()["total"] == 2
    assert {item["credited_credits"] for item in grants.json()["items"]} == {7, 9}
    assert len({item["key"] for item in grants.json()["items"]}) == 2

    prompt = client.get("/api/me/billing/entries?kind=prompt", headers=headers)
    assert prompt.status_code == 200, prompt.text
    prompt_item = prompt.json()["items"][0]
    assert prompt_item["title"] == "提示词优化"
    assert prompt_item["model_id"] == "prompt-prime-v1"
    assert prompt_item["net_consumed_credits"] == 3

    workflow = client.get("/api/me/billing/entries?kind=workflow", headers=headers)
    assert workflow.status_code == 200, workflow.text
    assert workflow.json()["total"] == 1
    workflow_item = workflow.json()["items"][0]
    assert workflow_item["key"] == f"tool_run:{ids['tool_run_id']}"
    assert workflow_item["kind"] == "workflow"
    assert workflow_item["title"] == "分镜合成工具"
    assert workflow_item["status"] == "succeeded"
    assert workflow_item["related_kind"] == "workflow"
    assert workflow_item["related_id"] == ids["workflow_run_id"]
    assert f"工作流运行 #{ids['workflow_run_id']}" in workflow_item["subtitle"]
    assert workflow_item["frozen_credits"] == 8
    assert workflow_item["settled_credits"] == 8
    assert workflow_item["refunded_credits"] == 0
    assert workflow_item["outstanding_frozen_credits"] == 0
    assert workflow_item["net_consumed_credits"] == 8


def test_billing_entries_cursor_and_validation(client, make_user, auth):
    user_id = make_user("13971300005", balance=300)
    other_user_id = make_user("13971300006", balance=2000)
    _seed_billing_rows(user_id, other_user_id)
    headers = auth("13971300005")

    first = client.get("/api/me/billing/entries?limit=2", headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 7
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"]

    second = client.get(
        "/api/me/billing/entries",
        params={"limit": 10, "cursor": body["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert {item["key"] for item in body["items"]}.isdisjoint(
        {item["key"] for item in second.json()["items"]}
    )

    invalid = client.get("/api/me/billing/entries?cursor=broken", headers=headers)
    assert invalid.status_code == 400
    bad_kind = client.get("/api/me/billing/entries?kind=unknown", headers=headers)
    assert bad_kind.status_code == 422


def test_prompt_optimization_bill_links_proposal_quote_and_conserves_balances(
    client,
    make_user,
    auth,
):
    user_id = make_user("13971300007", balance=100)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with SessionLocal() as db:
        target = db.query(ModelConfig).filter_by(use="image", enabled=True).first()
        optimizer = db.query(ModelConfig).filter_by(use="prompt", enabled=True).first()
        assert target is not None and optimizer is not None
        capability = db.query(ModelCapabilityVersion).filter_by(
            model_config_id=target.id,
            is_active=True,
        ).first()
        assert capability is not None
        quote = GenerationQuote(
            user_id=user_id,
            model_config_id=optimizer.id,
            capability_version_id=capability.id,
            kind="prompt_optimization",
            client_request_id="billing-prompt-proposal",
            request_fingerprint="e" * 64,
            category="image",
            stage="preview",
            request_snapshot={"mode": "commercial"},
            model_snapshot={"model_id": optimizer.model_id},
            pricing_snapshot={"credits": 3},
            price_breakdown={"base": 3},
            subject_snapshot={},
            warnings=[],
            estimated_credits=3,
            status="consumed",
            expires_at=now + timedelta(minutes=30),
            consumed_at=now,
        )
        db.add(quote)
        db.flush()
        proposal = PromptOptimizationProposal(
            user_id=user_id,
            target_model_config_id=target.id,
            capability_version_id=capability.id,
            optimizer_model_config_id=optimizer.id,
            quote_id=quote.id,
            idempotency_key="billing-prompt-proposal",
            status="accepted",
            version=1,
            category="image",
            mode="commercial",
            optimization_kind="rewrite",
            original={"final_text": "原提示词"},
            suggestion={"final_text": "商业增强提示词"},
            diff=[],
            constraint_coverage=[],
            warnings=[],
            provenance={"optimizer_model_id": optimizer.model_id},
            catalog_snapshot={"capability_version_id": capability.id},
            charged_credits=3,
            metrics={"execution_status": "succeeded"},
            expires_at=now + timedelta(minutes=30),
        )
        db.add(proposal)
        db.flush()
        quote.consumed_ref_type = "prompt_optimization"
        quote.consumed_ref_id = proposal.id
        credits.freeze(
            db,
            user_id,
            3,
            proposal.id,
            biz_type="prompt_optimize",
            commit=False,
        )
        credits.settle(
            db,
            user_id,
            3,
            3,
            proposal.id,
            biz_type="prompt_optimize",
            commit=False,
        )
        db.commit()
        proposal_id = int(proposal.id)
        quote_id = int(quote.id)
        optimizer_name = optimizer.display_name

    response = client.get(
        "/api/me/billing/entries?kind=prompt",
        headers=auth("13971300007"),
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["related_kind"] == "prompt_optimization"
    assert item["related_id"] == proposal_id
    assert item["quote_id"] == quote_id
    assert item["quoted_credits"] == 3
    assert item["quote_status"] == "consumed"
    assert item["status"] == "accepted"
    assert item["model_name"] == optimizer_name
    assert item["subtitle"] == f"商业增强 · 提案 #{proposal_id}"
    assert item["net_consumed_credits"] == 3
    assert item["balance_conserved"] is True
    assert item["reservation_conserved"] is True


def test_consumed_refund_reduces_net_spend_and_preserves_conservation(
    client,
    make_user,
    auth,
):
    user_id = make_user("13971300008", balance=100)
    with SessionLocal() as db:
        credits.consume(
            db,
            user_id,
            5,
            biz_type="prompt_optimize",
            biz_ref=8801,
            note="model=legacy-prompt",
            commit=False,
        )
        credits.refund_consumed(
            db,
            user_id,
            2,
            biz_type="prompt_optimize",
            biz_ref=8801,
            note="partial provider refund",
            commit=False,
        )
        db.commit()

    response = client.get(
        "/api/me/billing/entries?kind=prompt",
        headers=auth("13971300008"),
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["frozen_credits"] == 0
    assert item["consumed_refunded_credits"] == 2
    assert item["refunded_credits"] == 2
    assert item["net_consumed_credits"] == 3
    assert item["net_balance_change"] == -3
    assert item["balance_conserved"] is True
    assert item["reservation_conserved"] is True


def test_review_billing_exposes_pending_and_audited_refund_resolution(
    client,
    make_user,
    auth,
):
    user_id = make_user("13971300009", balance=100)
    make_user("13971300010", balance=100, admin=True)
    with SessionLocal() as db:
        task = GenTask(
            user_id=user_id,
            category="video",
            stage="preview",
            prompt={"final_text": "review billing"},
            model_use="video",
            params={},
            status="needs_review",
            phase="reconciling",
            error="供应商结果状态不明确",
            cost_frozen=11,
            cost_settled=0,
        )
        db.add(task)
        db.flush()
        credits.freeze(db, user_id, 11, task.id, commit=False)
        db.commit()
        task_id = int(task.id)

    user_headers = auth("13971300009")
    pending = client.get("/api/me/billing/entries?kind=generation", headers=user_headers)
    assert pending.status_code == 200, pending.text
    pending_item = pending.json()["items"][0]
    assert pending_item["review_status"] == "pending"
    assert pending_item["review_reason"] == "供应商结果状态不明确"
    assert pending_item["outstanding_frozen_credits"] == 11
    assert pending_item["balance_conserved"] is True
    assert pending_item["reservation_conserved"] is True

    admin_headers = auth("13971300010")
    resolved = client.post(
        f"/api/admin/tasks/{task_id}/refund_review",
        json={"note": "供应商确认未产出，已退款"},
        headers=admin_headers,
    )
    assert resolved.status_code == 200, resolved.text

    billed = client.get("/api/me/billing/entries?kind=generation", headers=user_headers)
    assert billed.status_code == 200, billed.text
    item = billed.json()["items"][0]
    assert item["review_status"] == "refunded"
    assert item["review_note"] == "供应商确认未产出，已退款"
    assert item["reviewed_at"]
    assert item["reservation_refunded_credits"] == 11
    assert item["refunded_credits"] == 11
    assert item["outstanding_frozen_credits"] == 0
    assert item["net_consumed_credits"] == 0
    assert item["balance_conserved"] is True
    assert item["reservation_conserved"] is True
    with SessionLocal() as db:
        audit_row = db.query(AuditLog).filter_by(
            action="refund_review_task",
            biz_type="gen_task",
            biz_id=task_id,
        ).one()
        assert audit_row.detail["target_user_id"] == user_id


def test_review_billing_exposes_audited_settlement_note(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13971300011", balance=100)
    make_user("13971300012", balance=100, admin=True)
    with SessionLocal() as db:
        task = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            prompt={"final_text": "review settlement billing"},
            model_use="image",
            params={"n": 1, "size": "256x256"},
            status="needs_review",
            phase="reconciling",
            error="供应商已产出，等待人工补结果",
            cost_frozen=7,
            cost_settled=0,
        )
        db.add(task)
        db.flush()
        credits.freeze(db, user_id, 7, task.id, commit=False)
        db.commit()
        task_id = int(task.id)

    from tests.conftest import _test_png_bytes

    monkeypatch.setattr(
        "app.services.gateway.download_bytes_limited",
        lambda *_args, **_kwargs: _test_png_bytes(),
    )
    resolved = client.post(
        f"/api/admin/tasks/{task_id}/settle_review",
        json={
            "result_url": "https://cdn.example.com/review-settled.png",
            "admin_password": "pass123456",
            "note": "供应商确认已产出，补入结果后结算",
        },
        headers=auth("13971300012"),
    )
    assert resolved.status_code == 200, resolved.text

    billed = client.get(
        "/api/me/billing/entries?kind=generation",
        headers=auth("13971300011"),
    )
    assert billed.status_code == 200, billed.text
    item = billed.json()["items"][0]
    assert item["review_status"] == "settled"
    assert item["review_note"] == "供应商确认已产出，补入结果后结算"
    assert item["reviewed_at"]
    assert item["settled_credits"] == 7
    assert item["refunded_credits"] == 0
    assert item["outstanding_frozen_credits"] == 0
    assert item["net_consumed_credits"] == 7
    assert item["balance_conserved"] is True
    assert item["reservation_conserved"] is True
    with SessionLocal() as db:
        audit_row = db.query(AuditLog).filter_by(
            action="settle_review_task",
            biz_type="gen_task",
            biz_id=task_id,
        ).one()
        assert audit_row.detail["note"] == "供应商确认已产出，补入结果后结算"
