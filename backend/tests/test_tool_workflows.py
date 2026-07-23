from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from pydantic import ValidationError
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    CreditTransaction,
    GenerationQuote,
    GenTask,
    ParseRecord,
    ReverseOperation,
    ToolDefinition,
    ToolNodeRun,
    ToolRun,
    ToolVersion,
    User,
    WorkflowDispatch,
    WorkflowRun,
)
from app.schemas import ParseOut
from app.services import (
    generation_quotes,
    tool_workflows,
    video_composition,
    workflow_dispatch,
    workflow_node_adapters,
)
from app.services.catalog_metadata import tool_metadata_snapshot
from app.workflow_schemas import ToolRunCreateIn, WorkflowSpec


@pytest.fixture(autouse=True)
def clear_workflow_handlers():
    tool_workflows._NODE_HANDLERS.clear()
    tool_workflows._COMPENSATION_HANDLERS.clear()
    yield
    tool_workflows._NODE_HANDLERS.clear()
    tool_workflows._COMPENSATION_HANDLERS.clear()


def _seed_tool(
    slug: str,
    nodes: list[dict],
    *,
    output_node: str | None = None,
    credits: int = 0,
) -> None:
    db = SessionLocal()
    try:
        tool = ToolDefinition(
            slug=slug,
            name=slug,
            category="workflow",
            renderer="studio",
            entry_path=f"/?workflow={slug}",
            enabled=True,
        )
        db.add(tool)
        db.flush()
        workflow = {"type": "workflow.v1", "nodes": nodes}
        if output_node:
            workflow["output_node"] = output_node
        db.add(
            ToolVersion(
                tool_definition_id=tool.id,
                version=1,
                schema_version="tool.v1",
                input_schema={},
                workflow=workflow,
                pricing_policy={"type": "server_quote", "credits": credits},
                capabilities={},
                metadata_snapshot=tool_metadata_snapshot(tool),
                is_active=True,
            )
        )
        db.commit()
    finally:
        db.close()


def _quote(client, headers, *, slug: str, request_id: str, payload: dict | None = None):
    request_payload = payload or {"source": "fixture"}
    return client.post(
        "/api/quotes",
        json={
            "kind": "workflow",
            "client_request_id": request_id,
            "request": {
                "tool_slug": slug,
                "client_request_id": request_id,
                "input": request_payload,
            },
        },
        headers=headers,
    )


def _create(client, headers, *, slug: str, request_id: str, payload: dict | None = None):
    request_payload = payload or {"source": "fixture"}
    quoted = _quote(
        client,
        headers,
        slug=slug,
        request_id=request_id,
        payload=request_payload,
    )
    assert quoted.status_code == 201, quoted.text
    return client.post(
        "/api/workflows/runs",
        json={
            "tool_slug": slug,
            "quote_id": quoted.json()["quote_id"],
            "client_request_id": request_id,
            "input": request_payload,
        },
        headers=headers,
    )


def _quoted_body(
    db,
    *,
    user_id: int,
    slug: str,
    request_id: str,
    payload: dict | None = None,
) -> ToolRunCreateIn:
    body = ToolRunCreateIn(
        tool_slug=slug,
        client_request_id=request_id,
        input=payload or {"source": "fixture"},
    )
    quote = generation_quotes.create_workflow_quote(db, user_id=user_id, body=body)
    db.commit()
    return body.model_copy(update={"quote_id": int(quote.id)})


def test_workflow_execution_requires_matching_unmodified_quote(client, make_user, auth):
    make_user("13991000023", balance=100)
    headers = auth("13991000023")
    _seed_tool("workflow-quote-required", [{"key": "compile", "type": "compile"}])
    request_payload = {
        "tool_slug": "workflow-quote-required",
        "client_request_id": "workflow-quote-required-request",
        "input": {"source": "fixture"},
    }

    missing = client.post("/api/workflows/runs", json=request_payload, headers=headers)
    assert missing.status_code == 422

    quoted = _quote(
        client,
        headers,
        slug="workflow-quote-required",
        request_id="workflow-quote-required-request",
    )
    assert quoted.status_code == 201, quoted.text
    quote_id = int(quoted.json()["quote_id"])

    mismatched = client.post(
        "/api/workflows/runs",
        json={
            **request_payload,
            "quote_id": quote_id,
            "client_request_id": "workflow-quote-different-request",
        },
        headers=headers,
    )
    assert mismatched.status_code == 409
    assert mismatched.json()["detail"]["code"] == "QUOTE_MISMATCH"

    with SessionLocal() as db:
        quote = db.get(GenerationQuote, quote_id)
        quote.pricing_snapshot = {**dict(quote.pricing_snapshot or {}), "credits": 999}
        db.commit()
    tampered = client.post(
        "/api/workflows/runs",
        json={**request_payload, "quote_id": quote_id},
        headers=headers,
    )
    assert tampered.status_code == 409
    assert tampered.json()["detail"]["code"] == "QUOTE_SNAPSHOT_INVALID"


def test_workflow_execution_rejects_a_quote_for_another_action_kind(
    client,
    make_user,
    auth,
):
    user_id = make_user("13991000028", balance=100)
    headers = auth("13991000028")
    _seed_tool("workflow-wrong-quote-kind", [{"key": "compile", "type": "compile"}])
    with SessionLocal() as db:
        quote = generation_quotes.create_execution_quote(
            db,
            user_id=user_id,
            kind="reverse",
            client_request_id="workflow-wrong-kind-request",
            request_fingerprint="f" * 64,
            category="image",
            stage="final",
            request_snapshot={},
            model_snapshot={},
            pricing_snapshot={},
            price_breakdown={},
            estimated_credits=0,
        )
        db.commit()
        quote_id = int(quote.id)

    rejected = client.post(
        "/api/workflows/runs",
        json={
            "tool_slug": "workflow-wrong-quote-kind",
            "quote_id": quote_id,
            "client_request_id": "workflow-wrong-kind-request",
            "input": {"source": "fixture"},
        },
        headers=headers,
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "QUOTE_KIND_MISMATCH"
    with SessionLocal() as db:
        assert db.scalar(select(ToolRun).where(ToolRun.quote_id == quote_id)) is None


def test_workflow_quote_expiry_and_insufficient_balance_leave_no_run_or_charge(
    client,
    make_user,
    auth,
):
    user_id = make_user("13991000024", balance=3)
    headers = auth("13991000024")
    _seed_tool(
        "workflow-paid-balance",
        [{"key": "compile", "type": "compile"}],
        credits=8,
    )
    expired_quote = _quote(
        client,
        headers,
        slug="workflow-paid-balance",
        request_id="workflow-paid-expired-request",
    )
    assert expired_quote.status_code == 201, expired_quote.text
    with SessionLocal() as db:
        quote = db.get(GenerationQuote, int(expired_quote.json()["quote_id"]))
        quote.expires_at = tool_workflows.utcnow() - timedelta(seconds=1)
        db.commit()
    expired = client.post(
        "/api/workflows/runs",
        json={
            "tool_slug": "workflow-paid-balance",
            "quote_id": expired_quote.json()["quote_id"],
            "client_request_id": "workflow-paid-expired-request",
            "input": {"source": "fixture"},
        },
        headers=headers,
    )
    assert expired.status_code == 409
    assert expired.json()["detail"]["code"] == "QUOTE_EXPIRED"

    active_quote = _quote(
        client,
        headers,
        slug="workflow-paid-balance",
        request_id="workflow-paid-balance-request",
    )
    assert active_quote.status_code == 201, active_quote.text
    quote_id = int(active_quote.json()["quote_id"])
    rejected = client.post(
        "/api/workflows/runs",
        json={
            "tool_slug": "workflow-paid-balance",
            "quote_id": quote_id,
            "client_request_id": "workflow-paid-balance-request",
            "input": {"source": "fixture"},
        },
        headers=headers,
    )
    assert rejected.status_code == 402, rejected.text
    with SessionLocal() as db:
        quote = db.get(GenerationQuote, quote_id)
        user = db.get(User, user_id)
        assert quote.status == "active"
        assert db.scalar(select(ToolRun).where(ToolRun.quote_id == quote_id)) is None
        assert user.balance_credits == 3
        assert user.frozen_credits == 0
        assert db.scalar(
            select(CreditTransaction).where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.biz_type == "tool_run",
            )
        ) is None


def test_paid_workflow_success_settles_once_and_replays_idempotently(
    client,
    make_user,
    auth,
):
    user_id = make_user("13991000025", balance=50)
    headers = auth("13991000025")
    _seed_tool(
        "workflow-paid-success",
        [{"key": "compile", "type": "compile"}],
        output_node="compile",
        credits=8,
    )
    tool_workflows.register_node_handler(
        "compile",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"prompt": "paid"}),
    )

    first = _create(
        client,
        headers,
        slug="workflow-paid-success",
        request_id="workflow-paid-success-request",
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "succeeded"
    assert body["quote_id"] > 0
    assert body["pricing_snapshot"]["credits"] == 8
    assert body["cost_frozen"] == 8
    assert body["cost_settled"] == 8
    assert body["cost_refunded"] == 0
    assert body["cost_outstanding"] == 0

    replay = _create(
        client,
        headers,
        slug="workflow-paid-success",
        request_id="workflow-paid-success-request",
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == body["id"]
    assert replay.json()["idempotent_replay"] is True

    with SessionLocal() as db:
        user = db.get(User, user_id)
        transactions = list(
            db.scalars(
                select(CreditTransaction)
                .where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.biz_type == "tool_run",
                )
                .order_by(CreditTransaction.id)
            )
        )
        assert user.balance_credits == 42
        assert user.frozen_credits == 0
        assert [row.type for row in transactions] == ["freeze", "settle"]
        assert len({row.biz_ref for row in transactions}) == 1


def test_paid_workflow_failure_refunds_and_requires_a_new_run_to_retry(
    client,
    make_user,
    auth,
):
    user_id = make_user("13991000026", balance=50)
    headers = auth("13991000026")
    _seed_tool(
        "workflow-paid-failure",
        [{"key": "compile", "type": "compile", "max_attempts": 2}],
        credits=7,
    )
    tool_workflows.register_node_handler(
        "compile",
        lambda _context: tool_workflows.NodeExecutionResult.failed("provider failed"),
    )
    created = _create(
        client,
        headers,
        slug="workflow-paid-failure",
        request_id="workflow-paid-failure-request",
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "failed"
    assert body["cost_frozen"] == 7
    assert body["cost_settled"] == 0
    assert body["cost_refunded"] == 7
    assert body["cost_outstanding"] == 0

    retried = client.post(
        f"/api/workflows/runs/{body['id']}/nodes/compile/retry",
        json={"reason": "transient"},
        headers=headers,
    )
    assert retried.status_code == 409
    assert "重新报价" in retried.text
    with SessionLocal() as db:
        user = db.get(User, user_id)
        transactions = list(
            db.scalars(
                select(CreditTransaction)
                .where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.biz_type == "tool_run",
                )
                .order_by(CreditTransaction.id)
            )
        )
        assert user.balance_credits == 50
        assert user.frozen_credits == 0
        assert [row.type for row in transactions] == ["freeze", "refund"]


def test_workflow_executes_the_quoted_version_after_catalog_activation_changes(
    client,
    make_user,
    auth,
):
    make_user("13991000027", balance=50)
    headers = auth("13991000027")
    _seed_tool("workflow-version-lock", [{"key": "compile", "type": "compile"}])
    quoted = _quote(
        client,
        headers,
        slug="workflow-version-lock",
        request_id="workflow-version-lock-request",
    )
    assert quoted.status_code == 201, quoted.text
    quoted_version_id = int(quoted.json()["tool_version_id"])
    with SessionLocal() as db:
        old_version = db.get(ToolVersion, quoted_version_id)
        old_version.is_active = False
        db.add(
            ToolVersion(
                tool_definition_id=old_version.tool_definition_id,
                version=2,
                schema_version="tool.v2",
                input_schema={},
                workflow={"type": "workflow.v1", "nodes": [{"key": "export", "type": "export"}]},
                pricing_policy={"type": "server_quote", "credits": 0},
                capabilities={"version": 2},
                is_active=True,
            )
        )
        db.commit()
    tool_workflows.register_node_handler(
        "compile",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"version": 1}),
    )
    executed = client.post(
        "/api/workflows/runs",
        json={
            "tool_slug": "workflow-version-lock",
            "quote_id": quoted.json()["quote_id"],
            "client_request_id": "workflow-version-lock-request",
            "input": {"source": "fixture"},
        },
        headers=headers,
    )
    assert executed.status_code == 201, executed.text
    assert executed.json()["status"] == "succeeded"
    assert executed.json()["tool_version_id"] == quoted_version_id
    assert executed.json()["nodes"][0]["key"] == "compile"


def test_workflow_quote_keeps_historical_tool_metadata_after_catalog_rename(
    client,
    make_user,
    auth,
):
    make_user("13991000047", balance=50)
    make_user("13991000048", admin=True)
    user_headers = auth("13991000047")
    admin_headers = auth("13991000048")
    original_slug = "workflow-metadata-lock"
    _seed_tool(original_slug, [{"key": "compile", "type": "compile"}])
    quoted = _quote(
        client,
        user_headers,
        slug=original_slug,
        request_id="workflow-metadata-lock-request",
    )
    assert quoted.status_code == 201, quoted.text
    with SessionLocal() as db:
        tool = db.scalar(select(ToolDefinition).where(ToolDefinition.slug == original_slug))
        tool_id = int(tool.id)

    renamed = client.patch(
        f"/api/admin/tools/{tool_id}",
        headers=admin_headers,
        json={
            "slug": "workflow-metadata-lock-renamed",
            "name": "重命名后的工具",
            "entry_path": "/?workflow=workflow-metadata-lock-renamed",
        },
    )
    assert renamed.status_code == 200, renamed.text
    tool_workflows.register_node_handler(
        "compile",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"version": 1}),
    )
    execution_body = {
        "tool_slug": original_slug,
        "quote_id": quoted.json()["quote_id"],
        "client_request_id": "workflow-metadata-lock-request",
        "input": {"source": "fixture"},
    }
    executed = client.post(
        "/api/workflows/runs",
        json=execution_body,
        headers=user_headers,
    )
    assert executed.status_code == 201, executed.text
    with SessionLocal() as db:
        run = db.get(WorkflowRun, int(executed.json()["id"]))
        definition = run.compiled_snapshot["tool"]["definition"]
        version = run.compiled_snapshot["tool"]["version"]
        assert definition["slug"] == original_slug
        assert definition["name"] == original_slug
        assert version["metadata_snapshot"]["slug"] == original_slug
        assert version["metadata_snapshot"]["origin"] == "recorded"

    replay = client.post(
        "/api/workflows/runs",
        json=execution_body,
        headers=user_headers,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == executed.json()["id"]


@pytest.mark.parametrize(
    ("node_type", "prefix"),
    [
        ("reverse", "workflow-reverse"),
        ("compile", "workflow-compile"),
        ("generate", "workflow-generate"),
    ],
)
def test_default_side_effect_request_id_is_stable_across_attempts(node_type, prefix):
    context_fields = {
        "workflow_run_id": 41,
        "tool_run_id": 41,
        "node_run_id": 73,
        "node_key": "side-effect",
        "node_type": node_type,
        "workflow_input": {},
        "dependency_outputs": {},
        "node_input": {},
        "config": {},
    }
    first_attempt = tool_workflows.NodeExecutionContext(
        **context_fields,
        attempt_number=1,
    )
    retry_attempt = tool_workflows.NodeExecutionContext(
        **context_fields,
        attempt_number=2,
    )

    first_key = workflow_node_adapters._default_request_id(first_attempt, prefix)
    retry_key = workflow_node_adapters._default_request_id(retry_attempt, prefix)

    assert first_key == f"{prefix}-41-side-effect"
    assert retry_key == first_key


def test_dispatch_reconciler_backfills_orphaned_run_and_publishes(
    client,
    make_user,
    monkeypatch,
):
    # The client fixture starts the app and creates the shared integration DB.
    del client
    user_id = make_user("13991000020")
    _seed_tool("workflow-dispatch-orphan", [{"key": "compile", "type": "compile"}])
    db = SessionLocal()
    try:
        run, created = tool_workflows.create_run(
            db,
            user_id=user_id,
            body=_quoted_body(
                db,
                user_id=user_id,
                slug="workflow-dispatch-orphan",
                request_id="workflow-dispatch-orphan-request",
            ),
        )
        assert created is True
        run_id = int(run.id)
        db.query(WorkflowDispatch).filter(
            WorkflowDispatch.workflow_run_id == run_id
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    published: list[dict] = []

    def fake_enqueue(task, *args, task_id=None, **kwargs):
        published.append(
            {
                "task_name": task.name,
                "args": args,
                "task_id": task_id,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr("app.tasks.enqueue_with_request_context", fake_enqueue)
    from app.tasks import reconcile_workflow_dispatches_task

    result = reconcile_workflow_dispatches_task()

    assert result["orphan_created"] >= 1
    assert result["published"] == len(published)
    target_publications = [
        item
        for item in published
        if item["task_name"] == "workflow.run"
        and item["args"]
        and int(item["args"][0]) == run_id
    ]
    assert len(target_publications) == 1
    target_publication = target_publications[0]
    with SessionLocal() as db:
        dispatch = db.scalar(
            select(WorkflowDispatch).where(WorkflowDispatch.workflow_run_id == run_id)
        )
        assert dispatch is not None
        assert dispatch.status == "published"
        assert dispatch.publish_attempts == 1
        assert target_publication["task_id"] == dispatch.celery_task_id


def test_dispatch_reconciler_retries_unknown_publish_with_same_task_id(
    make_user,
    monkeypatch,
):
    user_id = make_user("13991000021")
    _seed_tool("workflow-dispatch-unknown", [{"key": "compile", "type": "compile"}])
    db = SessionLocal()
    try:
        run, created = tool_workflows.create_run(
            db,
            user_id=user_id,
            body=_quoted_body(
                db,
                user_id=user_id,
                slug="workflow-dispatch-unknown",
                request_id="workflow-dispatch-unknown-request",
            ),
        )
        assert created is True
        run_id = int(run.id)
        dispatch = db.scalar(
            select(WorkflowDispatch).where(WorkflowDispatch.workflow_run_id == run_id)
        )
        dispatch_id = int(dispatch.id)
        task_id = str(dispatch.celery_task_id)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ack unknown")),
    )
    with SessionLocal() as db:
        outcome = workflow_dispatch.publish_dispatch(db, dispatch_id)
        assert outcome.status == "unknown"
        dispatch = db.get(WorkflowDispatch, dispatch_id)
        dispatch.next_attempt_at = tool_workflows.utcnow() - timedelta(seconds=1)
        db.commit()

    monkeypatch.setattr(
        "app.tasks.enqueue_with_request_context",
        lambda *_args, task_id=None, **_kwargs: SimpleNamespace(id=task_id),
    )
    from app.tasks import reconcile_workflow_dispatches_task

    result = reconcile_workflow_dispatches_task()

    assert result["published"] == 1
    with SessionLocal() as db:
        dispatch = db.get(WorkflowDispatch, dispatch_id)
        assert dispatch.status == "published"
        assert dispatch.publish_attempts == 2
        assert dispatch.celery_task_id == task_id


def test_workflow_schema_whitelists_nodes_and_rejects_missing_dependency_and_cycles():
    spec = WorkflowSpec.model_validate(
        {
            "type": "workflow.v1",
            "studio_preset": {
                "creation_mode": "video_edit",
                "analysis_focus": "storyboard",
                "output_purpose": "storyboard",
                "reference_roles": ["main", "product"],
            },
            "nodes": [
                {"key": "parse", "type": "parse"},
                {
                    "key": "review",
                    "type": "human_review",
                    "depends_on": ["parse"],
                },
                {"key": "export", "type": "export", "depends_on": ["review"]},
            ],
        }
    )
    assert spec.studio_preset is not None
    assert spec.studio_preset.creation_mode == "video_edit"
    assert spec.topological_keys() == ["parse", "review", "export"]
    assert spec.nodes[1].type == "manual_review"

    with pytest.raises(ValidationError, match="依赖不存在"):
        WorkflowSpec.model_validate(
            {"type": "workflow.v1", "nodes": [{"key": "x", "type": "parse", "depends_on": ["missing"]}]}
        )
    with pytest.raises(ValidationError, match="无环 DAG"):
        WorkflowSpec.model_validate(
            {
                "type": "workflow.v1",
                "nodes": [
                    {"key": "a", "type": "parse", "depends_on": ["b"]},
                    {"key": "b", "type": "reverse", "depends_on": ["a"]},
                ],
            }
        )
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            {"type": "workflow.v1", "nodes": [{"key": "x", "type": "shell"}]}
        )


def test_idempotent_start_owner_gate_and_duplicate_worker_execution(
    client, make_user, auth
):
    make_user("13991000001")
    make_user("13991000002")
    owner = auth("13991000001")
    other = auth("13991000002")
    calls = []
    tool_workflows.register_node_handler(
        "compile",
        lambda context: calls.append(context.node_key)
        or tool_workflows.NodeExecutionResult.succeeded({"prompt": "compiled"}),
    )
    _seed_tool("workflow-idempotent", [{"key": "compile", "type": "compile"}])

    first = _create(
        client,
        owner,
        slug="workflow-idempotent",
        request_id="workflow-request-idempotent",
    )
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "succeeded"
    replay = _create(
        client,
        owner,
        slug="workflow-idempotent",
        request_id="workflow-request-idempotent",
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["idempotent_replay"] is True
    assert calls == ["compile"]

    mismatch = _quote(
        client,
        owner,
        slug="workflow-idempotent",
        request_id="workflow-request-idempotent",
        payload={"source": "different"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "QUOTE_IDEMPOTENCY_CONFLICT"
    assert client.get(
        f"/api/workflows/runs/{first.json()['id']}", headers=other
    ).status_code == 404

    tool_workflows.run_workflow(first.json()["id"])
    assert calls == ["compile"]


def test_idempotent_replay_remains_bound_to_original_tool_version(
    client, make_user, auth
):
    make_user("13991000013")
    headers = auth("13991000013")
    tool_workflows.register_node_handler(
        "compile",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"prompt": "v1"}),
    )
    _seed_tool("workflow-version-replay", [{"key": "compile", "type": "compile"}])
    first = _create(
        client,
        headers,
        slug="workflow-version-replay",
        request_id="workflow-request-version-replay",
    )
    assert first.status_code == 201

    db = SessionLocal()
    try:
        tool = db.scalar(
            select(ToolDefinition).where(ToolDefinition.slug == "workflow-version-replay")
        )
        original = db.scalar(
            select(ToolVersion).where(
                ToolVersion.tool_definition_id == tool.id,
                ToolVersion.is_active.is_(True),
            )
        )
        original.is_active = False
        db.add(
            ToolVersion(
                tool_definition_id=tool.id,
                version=2,
                schema_version="tool.v1",
                input_schema={},
                workflow={
                    "type": "workflow.v1",
                    "nodes": [{"key": "compile-v2", "type": "compile"}],
                },
                pricing_policy={"type": "server_quote"},
                capabilities={},
                is_active=True,
            )
        )
        db.commit()
    finally:
        db.close()

    replay = _create(
        client,
        headers,
        slug="workflow-version-replay",
        request_id="workflow-request-version-replay",
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["tool_version_id"] == first.json()["tool_version_id"]
    assert replay.json()["idempotent_replay"] is True


def test_node_dispatch_uses_compare_and_swap_and_schedules_lease_wakeup(
    make_user, monkeypatch
):
    user_id = make_user("13991000012")
    _seed_tool("workflow-cas", [{"key": "compile", "type": "compile"}])
    wakeups = []
    monkeypatch.setattr(
        tool_workflows,
        "_schedule_lease_wakeup",
        lambda run_id, deadline: wakeups.append((run_id, deadline)),
    )
    create_db = SessionLocal()
    first_db = SessionLocal()
    stale_db = SessionLocal()
    verify_db = SessionLocal()
    try:
        run, created = tool_workflows.create_run(
            create_db,
            user_id=user_id,
            body=_quoted_body(
                create_db,
                user_id=user_id,
                slug="workflow-cas",
                request_id="workflow-request-cas",
            ),
        )
        assert created is True
        first_run = first_db.get(type(run), run.id)
        stale_run = stale_db.get(type(run), run.id)
        first_node = tool_workflows._run_nodes(first_db, run.id)[0]
        stale_node = tool_workflows._run_nodes(stale_db, run.id)[0]
        assert tool_workflows._claim_node(first_db, first_run, first_node) is not None
        assert tool_workflows._claim_node(stale_db, stale_run, stale_node) is None
        attempts = tool_workflows._node_attempts(verify_db, first_node.id)
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1
        duplicate = tool_workflows.run_workflow(run.id)
        assert duplicate["status"] == "running"
        assert len(wakeups) == 1
        assert wakeups[0][0] == run.id
        assert wakeups[0][1] is not None
    finally:
        create_db.close()
        first_db.close()
        stale_db.close()
        verify_db.close()


def test_expired_execution_lease_retries_and_rejects_late_result(make_user):
    user_id = make_user("13991000014")
    _seed_tool(
        "workflow-lease-recovery",
        [{"key": "compile", "type": "compile", "max_attempts": 2}],
    )
    db = SessionLocal()
    try:
        run, created = tool_workflows.create_run(
            db,
            user_id=user_id,
            body=_quoted_body(
                db,
                user_id=user_id,
                slug="workflow-lease-recovery",
                request_id="workflow-request-lease-recovery",
            ),
        )
        assert created is True
        node = tool_workflows._run_nodes(db, run.id)[0]
        claimed = tool_workflows._claim_node(db, run, node)
        assert claimed is not None
        stale_token = claimed[0]
        node = db.get(ToolNodeRun, node.id)
        node.available_at = tool_workflows.utcnow() - timedelta(seconds=1)
        db.commit()
        node_id = node.id
        run_id = run.id
    finally:
        db.close()

    tool_workflows.register_node_handler(
        "compile",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"prompt": "fresh"}),
    )
    result = tool_workflows.run_workflow(run_id)
    assert result["status"] == "succeeded"
    assert result["nodes"][0]["attempt_count"] == 2
    assert [item["status"] for item in result["nodes"][0]["attempts"]] == [
        "failed",
        "succeeded",
    ]
    assert result["nodes"][0]["attempts"][0]["error_code"] == "NODE_LEASE_EXPIRED"

    stale_db = SessionLocal()
    try:
        tool_workflows._apply_execution_result(
            stale_db,
            node_id=node_id,
            token=stale_token,
            result=tool_workflows.NodeExecutionResult.succeeded({"prompt": "stale"}),
        )
    finally:
        stale_db.close()
    verify_db = SessionLocal()
    try:
        assert verify_db.get(ToolNodeRun, node_id).output == {"prompt": "fresh"}
    finally:
        verify_db.close()


def test_manual_review_pauses_and_owner_approval_resumes_worker(client, make_user, auth):
    make_user("13991000003")
    headers = auth("13991000003")
    tool_workflows.register_node_handler(
        "parse",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"asset": "asset-1"}),
    )
    tool_workflows.register_node_handler(
        "compile",
        lambda context: tool_workflows.NodeExecutionResult.succeeded(
            {"review": context.dependency_outputs["review"]}
        ),
    )
    _seed_tool(
        "workflow-review",
        [
            {"key": "parse", "type": "parse"},
            {"key": "review", "type": "manual_review", "depends_on": ["parse"]},
            {"key": "compile", "type": "compile", "depends_on": ["review"]},
        ],
        output_node="compile",
    )

    created = _create(
        client,
        headers,
        slug="workflow-review",
        request_id="workflow-request-review",
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "waiting_review"
    assert [node["status"] for node in body["nodes"]] == [
        "succeeded",
        "waiting_review",
        "queued",
    ]

    approved = client.post(
        f"/api/workflows/runs/{body['id']}/nodes/review/review",
        json={"decision": "approve", "output": {"approved": True}, "note": "ok"},
        headers=headers,
    )
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert result["status"] == "succeeded"
    assert result["output"]["review"]["approved"] is True
    assert result["nodes"][1]["attempts"][0]["status"] == "succeeded"


def test_unregistered_nodes_fail_instead_of_faking_success(client, make_user, auth):
    make_user("13991000004")
    headers = auth("13991000004")
    _seed_tool("workflow-no-handler", [{"key": "generate", "type": "generate"}])
    response = _create(
        client,
        headers,
        slug="workflow-no-handler",
        request_id="workflow-request-no-handler",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["nodes"][0]["status"] == "failed"
    assert body["nodes"][0]["error_code"] == "NODE_HANDLER_NOT_REGISTERED"


def test_external_node_requires_admin_and_matching_bound_identity(
    client, make_user, auth
):
    make_user("13991000005")
    make_user("13991000006", admin=True)
    owner = auth("13991000005")
    admin = auth("13991000006")
    tool_workflows.register_node_handler(
        "reverse",
        lambda _context: tool_workflows.NodeExecutionResult.waiting_external(
            external_kind="reverse_operation",
            external_id="reverse-42",
        ),
    )
    _seed_tool("workflow-external", [{"key": "reverse", "type": "reverse"}])
    created = _create(
        client,
        owner,
        slug="workflow-external",
        request_id="workflow-request-external",
    )
    run_id = created.json()["id"]
    assert created.json()["nodes"][0]["status"] == "waiting_external"
    endpoint = f"/api/admin/workflows/runs/{run_id}/nodes/reverse/complete"
    payload = {
        "status": "succeeded",
        "output": {"prompt": "verified"},
        "external_kind": "reverse_operation",
        "external_id": "wrong",
    }
    assert client.post(endpoint, json=payload, headers=owner).status_code == 403
    assert client.post(endpoint, json=payload, headers=admin).status_code == 409
    payload["external_id"] = "reverse-42"
    completed = client.post(endpoint, json=payload, headers=admin)
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["output"]["reverse"]["prompt"] == "verified"


def test_cancel_propagates_and_compensates_completed_side_effects(
    client, make_user, auth
):
    user_id = make_user("13991000007", balance=20)
    headers = auth("13991000007")
    compensated = []
    tool_workflows.register_node_handler(
        "parse",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"object": "stored"}),
    )
    tool_workflows.register_compensation_handler(
        "release-object",
        lambda context: compensated.append(context.previous_output["object"])
        or tool_workflows.NodeExecutionResult.succeeded({"released": True}),
    )
    _seed_tool(
        "workflow-cancel",
        [
            {
                "key": "parse",
                "type": "parse",
                "side_effect": True,
                "compensation": {"type": "release-object"},
            },
            {"key": "review", "type": "manual_review", "depends_on": ["parse"]},
            {"key": "export", "type": "export", "depends_on": ["review"]},
        ],
        credits=5,
    )
    created = _create(
        client,
        headers,
        slug="workflow-cancel",
        request_id="workflow-request-cancel",
    )
    canceled = client.post(
        f"/api/workflows/runs/{created.json()['id']}/cancel", headers=headers
    )
    assert canceled.status_code == 200, canceled.text
    body = canceled.json()
    assert body["status"] == "canceled"
    assert [node["status"] for node in body["nodes"]] == [
        "succeeded",
        "canceled",
        "canceled",
    ]
    assert body["nodes"][0]["compensation_status"] == "succeeded"
    assert body["nodes"][1]["attempts"][0]["status"] == "canceled"
    assert compensated == ["stored"]
    assert body["cost_frozen"] == 5
    assert body["cost_settled"] == 0
    assert body["cost_refunded"] == 5
    with SessionLocal() as db:
        user = db.get(User, user_id)
        transactions = list(
            db.scalars(
                select(CreditTransaction)
                .where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.biz_type == "tool_run",
                )
                .order_by(CreditTransaction.id)
            )
        )
        assert user.balance_credits == 20
        assert user.frozen_credits == 0
        assert [row.type for row in transactions] == ["freeze", "refund"]


def test_compensation_soft_timeout_is_persisted_and_terminalized(
    client, make_user, auth
):
    make_user("13991000015")
    headers = auth("13991000015")
    tool_workflows.register_node_handler(
        "parse",
        lambda _context: tool_workflows.NodeExecutionResult.succeeded({"object": "stored"}),
    )

    def timeout_compensation(_context):
        raise SoftTimeLimitExceeded()

    tool_workflows.register_compensation_handler("release-timeout", timeout_compensation)
    _seed_tool(
        "workflow-compensation-timeout",
        [
            {
                "key": "parse",
                "type": "parse",
                "side_effect": True,
                "compensation": {"type": "release-timeout"},
            },
            {"key": "review", "type": "manual_review", "depends_on": ["parse"]},
        ],
    )
    created = _create(
        client,
        headers,
        slug="workflow-compensation-timeout",
        request_id="workflow-request-compensation-timeout",
    )
    canceled = client.post(
        f"/api/workflows/runs/{created.json()['id']}/cancel",
        headers=headers,
    )
    assert canceled.status_code == 200, canceled.text
    body = canceled.json()
    assert body["status"] == "canceled"
    assert body["error_code"] == "COMPENSATION_FAILED"
    assert body["nodes"][0]["compensation_status"] == "failed"
    compensation_attempt = next(
        item for item in body["nodes"][0]["attempts"] if item["kind"] == "compensation"
    )
    assert compensation_attempt["status"] == "failed"
    assert compensation_attempt["error_code"] == "WORKFLOW_NODE_TIMEOUT"


def test_failure_compensates_in_reverse_order(client, make_user, auth):
    make_user("13991000008")
    headers = auth("13991000008")
    compensation_order = []
    tool_workflows.register_node_handler(
        "parse", lambda _context: tool_workflows.NodeExecutionResult.succeeded({"id": "p"})
    )
    tool_workflows.register_node_handler(
        "reverse", lambda _context: tool_workflows.NodeExecutionResult.succeeded({"id": "r"})
    )
    tool_workflows.register_node_handler(
        "compile",
        lambda _context: tool_workflows.NodeExecutionResult.failed("compile broke", code="COMPILE_FAILED"),
    )
    for key in ("release-parse", "release-reverse"):
        tool_workflows.register_compensation_handler(
            key,
            lambda context, label=key: compensation_order.append(label)
            or tool_workflows.NodeExecutionResult.succeeded(),
        )
    _seed_tool(
        "workflow-failure-compensation",
        [
            {
                "key": "parse",
                "type": "parse",
                "side_effect": True,
                "compensation": {"type": "release-parse"},
            },
            {
                "key": "reverse",
                "type": "reverse",
                "depends_on": ["parse"],
                "side_effect": True,
                "compensation": {"type": "release-reverse"},
            },
            {"key": "compile", "type": "compile", "depends_on": ["reverse"]},
        ],
    )
    response = _create(
        client,
        headers,
        slug="workflow-failure-compensation",
        request_id="workflow-request-failure-compensation",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "COMPILE_FAILED"
    assert compensation_order == ["release-reverse", "release-parse"]
    assert [node["compensation_status"] for node in body["nodes"][:2]] == [
        "succeeded",
        "succeeded",
    ]


def test_failed_node_can_retry_without_repeating_successful_dependencies(
    client, make_user, auth
):
    make_user("13991000009")
    headers = auth("13991000009")
    parse_calls = []
    compile_calls = []
    tool_workflows.register_node_handler(
        "parse",
        lambda _context: parse_calls.append(1)
        or tool_workflows.NodeExecutionResult.succeeded({"asset": "one"}),
    )

    def compile_handler(_context):
        compile_calls.append(1)
        if len(compile_calls) == 1:
            return tool_workflows.NodeExecutionResult.failed("temporary")
        return tool_workflows.NodeExecutionResult.succeeded({"prompt": "ok"})

    tool_workflows.register_node_handler("compile", compile_handler)
    _seed_tool(
        "workflow-retry-node",
        [
            {"key": "parse", "type": "parse"},
            {"key": "compile", "type": "compile", "depends_on": ["parse"]},
        ],
    )
    created = _create(
        client,
        headers,
        slug="workflow-retry-node",
        request_id="workflow-request-retry-node",
    )
    assert created.json()["status"] == "failed"
    retried = client.post(
        f"/api/workflows/runs/{created.json()['id']}/nodes/compile/retry",
        json={"reason": "transient"},
        headers=headers,
    )
    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body["status"] == "succeeded"
    assert parse_calls == [1]
    assert compile_calls == [1, 1]
    assert body["nodes"][1]["attempt_count"] == 2
    assert [item["status"] for item in body["nodes"][1]["attempts"]] == [
        "failed",
        "succeeded",
    ]


def test_admin_summary_requires_admin(client, make_user, auth):
    make_user("13991000010")
    make_user("13991000011", admin=True)
    user = auth("13991000010")
    admin = auth("13991000011")
    assert client.get("/api/admin/workflows/summary", headers=user).status_code == 403
    response = client.get("/api/admin/workflows/summary", headers=admin)
    assert response.status_code == 200
    assert {"runs_by_status", "nodes_by_status", "compensations_by_status", "recent"} <= set(
        response.json()
    )


def test_production_adapter_registration_is_idempotent():
    workflow_node_adapters.register_production_workflow_adapters()
    first = dict(tool_workflows._NODE_HANDLERS)
    workflow_node_adapters.register_production_workflow_adapters()

    assert set(first) == {"parse", "reverse", "compile", "generate", "compose", "export"}
    assert tool_workflows._NODE_HANDLERS == first
    assert tool_workflows._COMPENSATION_HANDLERS["cancel_external_task"] is (
        workflow_node_adapters.cancel_external_task
    )
    assert tool_workflows._COMPENSATION_HANDLERS["cleanup_video_composition"] is (
        workflow_node_adapters.cleanup_video_composition
    )


def test_production_parse_adapter_binds_record_and_resumes_on_completion(
    client, make_user, auth, monkeypatch
):
    make_user("13991000016")
    headers = auth("13991000016")
    workflow_node_adapters.register_production_workflow_adapters()
    _seed_tool(
        "workflow-production-parse",
        [{"key": "parse", "type": "parse"}],
        output_node="parse",
    )

    def fake_submit(body, request, db, user):
        assert request.url.path == "/api/parse"
        record = ParseRecord(user_id=user.id, url=body.url, status="queued")
        db.add(record)
        db.commit()
        db.refresh(record)
        return ParseOut(id=record.id, status=record.status, url=record.url)

    monkeypatch.setattr(workflow_node_adapters.parse_router, "submit_parse", fake_submit)
    created = _create(
        client,
        headers,
        slug="workflow-production-parse",
        request_id="workflow-request-production-parse",
        payload={"parse": {"url": "https://example.com/post/1"}},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    node = body["nodes"][0]
    assert body["status"] == "running"
    assert node["status"] == "waiting_external"
    assert node["external_kind"] == "parse_record"

    db = SessionLocal()
    try:
        record = db.get(ParseRecord, int(node["external_id"]))
        record.status = "done"
        record.assets = [{"type": "image", "url": "http://localhost/media/preview/a.png"}]
        db.commit()
    finally:
        db.close()

    synced = workflow_node_adapters.sync_external_workflow_nodes(
        "parse_record", node["external_id"]
    )
    assert synced["completed"] == 1
    finished = client.get(f"/api/workflows/runs/{body['id']}", headers=headers).json()
    assert finished["status"] == "succeeded"
    assert finished["output"]["parse_id"] == int(node["external_id"])
    assert finished["output"]["assets"][0]["type"] == "image"


def test_production_compile_adapter_runs_local_service(
    client, make_user, auth, monkeypatch
):
    make_user("13991000017")
    headers = auth("13991000017")
    workflow_node_adapters.register_production_workflow_adapters()
    _seed_tool(
        "workflow-production-compile",
        [{"key": "compile", "type": "compile"}],
        output_node="compile",
    )
    captured = {}

    def fake_compile(db, *, user_id, body):
        captured.update(user_id=user_id, body=body)
        return {
            "proposal_id": 31,
            "proposal_version": 1,
            "mode": body.mode,
            "optimization_kind": "model_compile",
            "original": {"final_text": body.prompt},
            "suggestion": {"final_text": "compiled prompt"},
            "segments": [],
            "constraint_coverage": [],
            "warnings": [],
            "provenance": {"reverse_operation_id": None},
            "compiler_profile": {},
            "charged_credits": 0,
        }

    monkeypatch.setattr(
        workflow_node_adapters.prompt_optimization,
        "create_proposal",
        fake_compile,
    )
    created = _create(
        client,
        headers,
        slug="workflow-production-compile",
        request_id="workflow-request-production-compile",
        payload={
            "compile": {
                "prompt": "source prompt",
                "category": "image",
                "target_model_config_id": 1,
            }
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "succeeded"
    assert created.json()["output"]["compiled_prompt"] == "compiled prompt"
    assert captured["body"].mode == "target_model_adaptation"
    assert captured["body"].idempotency_key.startswith("workflow-compile-")


def test_production_generate_adapter_binds_task_and_resumes_on_completion(
    client, make_user, auth, monkeypatch
):
    make_user("13991000018")
    headers = auth("13991000018")
    workflow_node_adapters.register_production_workflow_adapters()
    _seed_tool(
        "workflow-production-generate",
        [{"key": "generate", "type": "generate"}],
        output_node="generate",
    )

    captured: dict[str, int] = {}

    def fake_generate(body, request, response, db, user):
        assert request.url.path == "/api/generate"
        assert body.quote_id > 0
        quote = db.get(GenerationQuote, body.quote_id)
        assert quote is not None
        assert quote.status == "active"
        assert int(quote.user_id) == int(user.id)
        captured["quote_id"] = int(quote.id)
        captured["estimated_credits"] = int(quote.estimated_credits)
        task = GenTask(
            user_id=user.id,
            quote_id=body.quote_id,
            category=body.category,
            stage=body.stage,
            source_type=body.source_type,
            prompt=body.prompt,
            params=body.params,
            client_request_id=body.client_request_id,
            status="queued",
            cost_frozen=quote.estimated_credits,
            cost_settled=0,
        )
        db.add(task)
        db.flush()
        quote.status = "consumed"
        quote.task_id = int(task.id)
        db.commit()
        db.refresh(task)
        return task

    monkeypatch.setattr(workflow_node_adapters.generate_router, "generate", fake_generate)
    created = _create(
        client,
        headers,
        slug="workflow-production-generate",
        request_id="workflow-request-production-generate",
        payload={
            "generate": {
                "category": "image",
                "prompt": {"final_text": "a verified prompt"},
                "params": {"n": 1},
            }
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    node = body["nodes"][0]
    assert node["status"] == "waiting_external"
    assert node["external_kind"] == "generation_task"
    assert captured["quote_id"] > 0

    db = SessionLocal()
    try:
        task = db.get(GenTask, int(node["external_id"]))
        assert task is not None
        assert int(task.quote_id) == captured["quote_id"]
        task.status = "succeeded"
        task.cost_settled = captured["estimated_credits"]
        db.commit()
    finally:
        db.close()
    synced = workflow_node_adapters.sync_external_workflow_nodes(
        "generation_task", node["external_id"]
    )
    assert synced["completed"] == 1
    finished = client.get(f"/api/workflows/runs/{body['id']}", headers=headers).json()
    assert finished["status"] == "succeeded"
    assert finished["output"]["generation_task_id"] == int(node["external_id"])


def test_production_external_cancel_compensates_reverse_operation(
    client, make_user, auth, monkeypatch
):
    make_user("13991000019")
    headers = auth("13991000019")
    workflow_node_adapters.register_production_workflow_adapters()
    _seed_tool("workflow-production-cancel", [{"key": "reverse", "type": "reverse"}])

    def fake_reverse(body, db, user):
        operation = ReverseOperation(
            user_id=user.id,
            client_request_id=body.client_request_id,
            request_fingerprint="f" * 64,
            target=body.target,
            asset_url=body.asset_url,
            request_context=body.model_dump(mode="json"),
            status="queued",
            phase="queued",
            progress=0,
            result_schema_version="reverse.v3",
            cost_frozen=0,
            cost_settled=0,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)
        return {"id": operation.id}

    monkeypatch.setattr(
        workflow_node_adapters.prompt_router,
        "create_reverse_operation",
        fake_reverse,
    )
    created = _create(
        client,
        headers,
        slug="workflow-production-cancel",
        request_id="workflow-request-production-cancel",
        payload={"reverse": {"asset_url": "https://example.com/image.png", "target": "image"}},
    )
    assert created.status_code == 201, created.text
    node = created.json()["nodes"][0]
    assert node["status"] == "waiting_external"

    canceled = client.post(
        f"/api/workflows/runs/{created.json()['id']}/cancel",
        headers=headers,
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["nodes"][0]["compensation_status"] == "succeeded"
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, int(node["external_id"]))
        assert operation.status == "canceled"
        assert operation.cancel_requested is True
    finally:
        db.close()


def test_production_compose_and_export_adapters_run_registered_media_services(
    client, make_user, auth, monkeypatch
):
    make_user("13991000020")
    headers = auth("13991000020")
    workflow_node_adapters.register_production_workflow_adapters()
    calls = []

    def fake_compose(context):
        calls.append(("compose", context.node_key))
        assert context.workflow_input["composition"]["title"] == "ready to render"
        return {
            "schema_version": "video-composition-result.v1",
            "task_id": 91,
            "asset_id": 92,
            "asset_ref": "generated:92",
            "download_url": "/api/me/assets/download?asset_ref=generated:92",
            "manifest": {"title": "ready to render"},
        }

    def fake_export(context):
        calls.append(("export", context.node_key))
        assert context.dependency_outputs["compose"]["asset_ref"] == "generated:92"
        return {
            "schema_version": "video-composition-export.v1",
            "asset_ref": "generated:92",
            "filename": "ready-to-render.mp4",
        }

    monkeypatch.setattr(video_composition, "compose_workflow_node", fake_compose)
    monkeypatch.setattr(video_composition, "export_workflow_node", fake_export)
    _seed_tool(
        "workflow-production-compose-export",
        [
            {
                "key": "compose",
                "type": "compose",
                "side_effect": True,
                "compensation": {"type": "cleanup_video_composition", "config": {}},
            },
            {"key": "export", "type": "export", "depends_on": ["compose"]},
        ],
        output_node="export",
    )

    response = _create(
        client,
        headers,
        slug="workflow-production-compose-export",
        request_id="workflow-request-production-compose-export",
        payload={"composition": {"title": "ready to render"}},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded"
    assert response.json()["output"]["filename"] == "ready-to-render.mp4"
    assert calls == [("compose", "compose"), ("export", "export")]


def test_production_compose_adapter_preserves_truthful_capability_status(
    client, make_user, auth, monkeypatch
):
    make_user("13991000021")
    headers = auth("13991000021")
    workflow_node_adapters.register_production_workflow_adapters()

    def unavailable(_context):
        raise video_composition.VideoCompositionError(
            "ffmpeg unavailable",
            code="VIDEO_COMPOSITION_FFMPEG_UNAVAILABLE",
            capability_status="unsupported",
        )

    monkeypatch.setattr(video_composition, "compose_workflow_node", unavailable)
    _seed_tool("workflow-production-compose-unavailable", [{"key": "compose", "type": "compose"}])
    response = _create(
        client,
        headers,
        slug="workflow-production-compose-unavailable",
        request_id="workflow-request-compose-unavailable",
    )
    body = response.json()
    assert response.status_code == 201, response.text
    assert body["status"] == "failed"
    assert body["nodes"][0]["error_code"] == "VIDEO_COMPOSITION_FFMPEG_UNAVAILABLE"
    assert body["nodes"][0]["output"]["capability_status"] == "unsupported"


def test_failed_export_compensates_composition_result(client, make_user, auth, monkeypatch):
    make_user("13991000022")
    headers = auth("13991000022")
    workflow_node_adapters.register_production_workflow_adapters()
    compensated = []

    monkeypatch.setattr(
        video_composition,
        "compose_workflow_node",
        lambda _context: {
            "schema_version": "video-composition-result.v1",
            "task_id": 301,
            "asset_id": 302,
            "asset_ref": "generated:302",
        },
    )

    def failed_export(_context):
        raise video_composition.VideoCompositionError(
            "export manifest failed",
            code="VIDEO_COMPOSITION_EXPORT_FAILED",
        )

    def cleanup(context):
        compensated.append(dict(context.previous_output or {}))
        return {"status": "compensated", "task_id": 301}

    monkeypatch.setattr(video_composition, "export_workflow_node", failed_export)
    monkeypatch.setattr(video_composition, "compensate_workflow_node", cleanup)
    _seed_tool(
        "workflow-production-compose-compensation",
        [
            {
                "key": "compose",
                "type": "compose",
                "side_effect": True,
                "compensation": {"type": "cleanup_video_composition", "config": {}},
            },
            {"key": "export", "type": "export", "depends_on": ["compose"]},
        ],
        output_node="export",
    )
    response = _create(
        client,
        headers,
        slug="workflow-production-compose-compensation",
        request_id="workflow-request-compose-compensation",
    )
    body = response.json()
    assert response.status_code == 201, response.text
    assert body["status"] == "failed"
    assert body["nodes"][0]["compensation_status"] == "succeeded"
    assert compensated == [
        {
            "schema_version": "video-composition-result.v1",
            "task_id": 301,
            "asset_id": 302,
            "asset_ref": "generated:302",
        }
    ]
