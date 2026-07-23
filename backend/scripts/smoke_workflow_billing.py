"""PostgreSQL smoke checks for workflow idempotency and non-eager billing."""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import func, select

from app.celery_app import celery_app
from app.db import SessionLocal, engine
from app.models import (
    CreditTransaction,
    GenerationQuote,
    ToolDefinition,
    ToolNodeRun,
    ToolRun,
    ToolVersion,
    User,
    WorkflowDispatch,
    WorkflowRun,
)
from app.services import generation_quotes, tool_workflows
from app.workflow_schemas import ToolRunCreateIn


def _require_postgres() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("smoke_workflow_billing requires PostgreSQL")


def _seed(*, credits: int) -> tuple[int, str]:
    suffix = uuid4().hex[:12]
    slug = f"billing-smoke-{suffix}"
    with SessionLocal() as db:
        user = User(
            phone=f"188{int(time.time() * 1000) % 10**12:012d}",
            password_hash="smoke-not-a-login",
            status="active",
            balance_credits=100,
            frozen_credits=0,
        )
        db.add(user)
        db.flush()
        tool = ToolDefinition(
            slug=slug,
            name="Workflow billing smoke",
            category="workflow",
            renderer="studio",
            entry_path=f"/?workflow={slug}",
            enabled=True,
        )
        db.add(tool)
        db.flush()
        db.add(
            ToolVersion(
                tool_definition_id=int(tool.id),
                version=1,
                schema_version="tool.v1",
                input_schema={},
                workflow={
                    "type": "workflow.v1",
                    "nodes": [{"key": "review", "type": "manual_review"}],
                    "output_node": "review",
                },
                pricing_policy={"type": "server_quote", "credits": int(credits)},
                capabilities={},
                is_active=True,
            )
        )
        db.commit()
        return int(user.id), slug


def _quoted_body(*, user_id: int, slug: str, request_id: str) -> ToolRunCreateIn:
    body = ToolRunCreateIn(
        tool_slug=slug,
        client_request_id=request_id,
        input={"source": "postgres-smoke"},
    )
    with SessionLocal() as db:
        quote = generation_quotes.create_workflow_quote(db, user_id=user_id, body=body)
        db.commit()
        return body.model_copy(update={"quote_id": int(quote.id)})


def _wait_for_status(run_id: int, statuses: set[str], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            status = db.scalar(select(WorkflowRun.status).where(WorkflowRun.id == run_id))
        if status in statuses:
            return str(status)
        time.sleep(0.1)
    raise TimeoutError(f"workflow {run_id} did not reach {sorted(statuses)}")


def _assert_credit_state(
    *,
    user_id: int,
    balance: int,
    frozen: int,
    transaction_types: list[str],
) -> None:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        rows = list(
            db.scalars(
                select(CreditTransaction)
                .where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.biz_type == "tool_run",
                )
                .order_by(CreditTransaction.id)
            )
        )
        assert user is not None
        assert int(user.balance_credits) == balance
        assert int(user.frozen_credits) == frozen
        assert [row.type for row in rows] == transaction_types


def concurrency_smoke() -> dict[str, object]:
    user_id, slug = _seed(credits=8)
    body = _quoted_body(
        user_id=user_id,
        slug=slug,
        request_id=f"concurrent-{uuid4().hex}",
    )
    barrier = threading.Barrier(2)

    def create() -> tuple[int, bool]:
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            run, created = tool_workflows.create_run(db, user_id=user_id, body=body)
            return int(run.id), bool(created)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create(), range(2)))
    run_ids = {run_id for run_id, _created in results}
    assert len(run_ids) == 1
    assert sorted(created for _run_id, created in results) == [False, True]
    run_id = run_ids.pop()

    with SessionLocal() as db:
        assert int(db.scalar(select(func.count(ToolRun.id)).where(ToolRun.user_id == user_id))) == 1
        quote = db.get(GenerationQuote, int(body.quote_id))
        tool_run = db.scalar(select(ToolRun).where(ToolRun.user_id == user_id))
        assert quote is not None and tool_run is not None
        assert quote.status == "consumed"
        assert quote.consumed_ref_type == "tool_run"
        assert int(quote.consumed_ref_id or 0) == int(tool_run.id)
    _assert_credit_state(
        user_id=user_id,
        balance=92,
        frozen=8,
        transaction_types=["freeze"],
    )

    result = tool_workflows.run_workflow(run_id)
    assert result is not None and result["status"] == "waiting_review"
    with SessionLocal() as db:
        tool_workflows.review_node(
            db,
            run_id=run_id,
            user_id=user_id,
            node_key="review",
            approve=True,
            output={"approved": True},
            note="postgres concurrency smoke",
        )
    result = tool_workflows.run_workflow(run_id)
    assert result is not None and result["status"] == "succeeded"
    _assert_credit_state(
        user_id=user_id,
        balance=92,
        frozen=0,
        transaction_types=["freeze", "settle"],
    )
    return {"run_id": run_id, "created_flags": [item[1] for item in results]}


def worker_smoke(*, timeout: float) -> dict[str, object]:
    if celery_app.conf.task_always_eager:
        raise RuntimeError("worker smoke requires task_always_eager=false")
    user_id, slug = _seed(credits=5)

    success_body = _quoted_body(
        user_id=user_id,
        slug=slug,
        request_id=f"worker-success-{uuid4().hex}",
    )
    with SessionLocal() as db:
        success_run, created = tool_workflows.create_run(
            db,
            user_id=user_id,
            body=success_body,
        )
        assert created is True
        success_id = int(success_run.id)
    tool_workflows.enqueue_run(success_id)
    assert _wait_for_status(success_id, {"waiting_review"}, timeout) == "waiting_review"
    with SessionLocal() as db:
        tool_workflows.review_node(
            db,
            run_id=success_id,
            user_id=user_id,
            node_key="review",
            approve=True,
            output={"approved": True},
            note="non-eager success smoke",
        )
    tool_workflows.enqueue_run(success_id)
    assert _wait_for_status(success_id, {"succeeded"}, timeout) == "succeeded"

    cancel_body = _quoted_body(
        user_id=user_id,
        slug=slug,
        request_id=f"worker-cancel-{uuid4().hex}",
    )
    with SessionLocal() as db:
        cancel_run, created = tool_workflows.create_run(
            db,
            user_id=user_id,
            body=cancel_body,
        )
        assert created is True
        cancel_id = int(cancel_run.id)
    tool_workflows.enqueue_run(cancel_id)
    assert _wait_for_status(cancel_id, {"waiting_review"}, timeout) == "waiting_review"
    with SessionLocal() as db:
        tool_workflows.request_cancel(db, run_id=cancel_id, user_id=user_id)
    tool_workflows.enqueue_run(cancel_id)
    assert _wait_for_status(cancel_id, {"canceled"}, timeout) == "canceled"

    _assert_credit_state(
        user_id=user_id,
        balance=95,
        frozen=0,
        transaction_types=["freeze", "settle", "freeze", "refund"],
    )
    with SessionLocal() as db:
        incomplete_dispatches = int(
            db.scalar(
                select(func.count(WorkflowDispatch.id))
                .join(WorkflowRun, WorkflowRun.id == WorkflowDispatch.workflow_run_id)
                .where(
                    WorkflowRun.user_id == user_id,
                    WorkflowDispatch.status != "completed",
                )
            )
            or 0
        )
        assert incomplete_dispatches == 0
        nodes = list(
            db.scalars(
                select(ToolNodeRun)
                .join(WorkflowRun, WorkflowRun.id == ToolNodeRun.workflow_run_id)
                .where(WorkflowRun.user_id == user_id)
            )
        )
        assert sorted(node.status for node in nodes) == ["canceled", "succeeded"]
    return {"succeeded_run_id": success_id, "canceled_run_id": cancel_id}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("concurrency", "worker"))
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    _require_postgres()
    result = concurrency_smoke() if args.mode == "concurrency" else worker_smoke(timeout=args.timeout)
    print(json.dumps({"status": "ok", "mode": args.mode, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
