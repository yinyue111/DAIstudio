"""Versioned provider routes, immutable quote binding, and circuit health."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.models import (
    GenerationQuote,
    GenTask,
    ModelConfig,
    ModelRoute,
    ModelRouteHealthEvent,
    ModelRouteVersion,
)
from app.services.generation_model_runtime import (
    ModelSnapshotMismatchError,
    model_from_persisted_snapshot,
    model_snapshot,
)
from app.services.model_gateway_config import encrypt_api_key
from app.services.model_routes import (
    attach_route_snapshot,
    publish_route_projection,
    record_route_outcome,
    rollback_route_version,
    select_model_route,
)


def _pricing(cost: int = 9) -> dict:
    return {
        "image": {"1k": cost, "2k": cost, "4k": cost},
        "image_edit": {"1k": cost, "2k": cost, "4k": cost},
        "video_preview_cost": 50,
        "video_per_second": {"480p": 10, "720p": 10, "1080p": 10},
    }


def _create_model(db, suffix: str, *, cost: int = 9) -> ModelConfig:
    model = ModelConfig(
        use="image",
        model_id=f"route-model-{suffix}",
        display_name=f"Route model {suffix}",
        is_default=False,
        sort_order=0,
        provider="custom_openai",
        base_url=f"https://route-model-{suffix}.example.com/v1",
        api_key_encrypted=encrypt_api_key(f"route-model-secret-{suffix}"),
        gateway_format="openai",
        cost_credits=cost,
        unlock_cost=0,
        enabled=True,
        extra={
            "capabilities": {"text_to_image": True},
            "credit_pricing": _pricing(cost),
        },
    )
    db.add(model)
    db.flush()
    return model


def _create_route(
    db,
    model: ModelConfig,
    key: str,
    *,
    priority: int,
    failure_threshold: int = 2,
    cooldown_seconds: int = 30,
) -> ModelRoute:
    route = ModelRoute(
        model_config_id=int(model.id),
        route_key=key,
        name=f"Route {key}",
        model_id=f"{model.model_id}-{key}",
        provider="custom_openai",
        base_url=f"https://{key}-{model.id}.example.com/v1",
        api_key_encrypted=encrypt_api_key(f"secret-{model.id}-{key}"),
        gateway_format="openai",
        extra={},
        priority=priority,
        enabled=True,
        managed_by_model_config=False,
        failure_threshold=failure_threshold,
        window_seconds=60,
        cooldown_seconds=cooldown_seconds,
    )
    db.add(route)
    db.flush()
    return route


def test_default_route_snapshot_and_legacy_snapshot_compatibility(client):
    with SessionLocal() as db:
        model = _create_model(db, "default-snapshot")
        selection = select_model_route(db, model)
        frozen = attach_route_snapshot(model_snapshot(selection.runtime_model), selection)
        db.commit()

        assert selection.route.route_key == "legacy-default"
        assert frozen["route_snapshot"]["route_id"] == selection.route.id
        assert frozen["route_snapshot"]["model_config_id"] == model.id
        assert "api_key" not in str(frozen).lower()

        routed = model_from_persisted_snapshot(db, frozen, model, "image")
        assert routed.route_id == selection.route.id
        assert routed.model_id == model.model_id

        legacy = model_snapshot(model)
        assert "route_snapshot" not in legacy
        compatible = model_from_persisted_snapshot(db, legacy, model, "image")
        assert compatible.model_id == model.model_id


def test_route_priority_circuit_fallback_half_open_and_recovery(client):
    with SessionLocal() as db:
        model = _create_model(db, "circuit")
        primary = _create_route(db, model, "primary", priority=10)
        fallback = _create_route(db, model, "fallback", priority=20)
        db.commit()
        primary_id = int(primary.id)
        fallback_id = int(fallback.id)
        model_id = int(model.id)

    with SessionLocal() as db:
        model = db.get(ModelConfig, model_id)
        selected = select_model_route(db, model)
        assert selected.route.id == primary_id
        assert selected.selection_state == "closed"
        db.commit()

    for attempt in range(2):
        record_route_outcome(
            primary_id,
            operation="image_generate",
            success=False,
            latency_ms=100 + attempt,
            error_code="provider_timeout",
            counts_toward_circuit=True,
        )

    with SessionLocal() as db:
        primary = db.get(ModelRoute, primary_id)
        assert primary.health_status == "open"
        assert primary.window_failures == 2
        model = db.get(ModelConfig, model_id)
        selected = select_model_route(db, model)
        assert selected.route.id == fallback_id
        db.commit()

    with SessionLocal() as db:
        primary = db.get(ModelRoute, primary_id)
        primary.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    with SessionLocal() as db:
        model = db.get(ModelConfig, model_id)
        probe = select_model_route(db, model)
        assert probe.route.id == primary_id
        assert probe.selection_state == "half_open"
        assert probe.route.half_open_claimed_until is not None
        db.commit()

    record_route_outcome(
        primary_id,
        operation="probe",
        success=True,
        latency_ms=45,
    )
    with SessionLocal() as db:
        primary = db.get(ModelRoute, primary_id)
        assert primary.health_status == "closed"
        assert primary.consecutive_failures == 0
        assert primary.cooldown_until is None
        assert db.query(ModelRouteHealthEvent).filter_by(route_id=primary_id).count() == 3


def test_quote_and_generate_endpoints_bind_the_healthy_fallback_route(
    client,
    make_user,
    auth,
):
    phone = "13972000003"
    user_id = make_user(phone, balance=500)
    headers = auth(phone)
    with SessionLocal() as db:
        model = _create_model(db, "endpoint-fallback")
        primary = _create_route(
            db,
            model,
            "endpoint-primary",
            priority=10,
            failure_threshold=1,
        )
        fallback = _create_route(db, model, "endpoint-fallback", priority=20)
        db.commit()
        model_id = int(model.id)
        primary_id = int(primary.id)
        fallback_id = int(fallback.id)

    record_route_outcome(
        primary_id,
        operation="image_generate",
        success=False,
        latency_ms=250,
        error_code="provider_timeout",
        counts_toward_circuit=True,
    )

    payload = {
        "client_request_id": "route-endpoint-fallback-001",
        "category": "image",
        "stage": "preview",
        "instruction": "endpoint fallback ceramic product photo",
        "params": {"n": 1, "size": "1024x1024"},
        "model_config_id": model_id,
    }
    quoted = client.post("/api/quotes", headers=headers, json=payload)
    assert quoted.status_code == 201, quoted.text
    quote_payload = quoted.json()
    assert int(quote_payload["route_id"]) == fallback_id
    assert quote_payload["route_key"] == "endpoint-fallback"
    assert quote_payload["route_health_at_quote"]["status"] == "closed"

    generated = client.post(
        "/api/generate",
        headers=headers,
        json={**payload, "quote_id": int(quote_payload["quote_id"])},
    )
    assert generated.status_code == 200, generated.text
    with SessionLocal() as db:
        task = db.get(GenTask, int(generated.json()["id"]))
        primary = db.get(ModelRoute, primary_id)
        fallback = db.get(ModelRoute, fallback_id)
        assert int(task.user_id) == user_id
        assert int(task.params["_model_snapshot"]["route_snapshot"]["route_id"]) == fallback_id
        assert primary.health_status == "open"
        assert fallback.last_success_at is not None


def test_route_selection_avoids_failed_route_but_reuses_single_route(client):
    with SessionLocal() as db:
        model = _create_model(db, "retry-avoid")
        primary = _create_route(db, model, "retry-primary", priority=10)
        fallback = _create_route(db, model, "retry-fallback", priority=20)
        db.commit()
        model_id = int(model.id)
        primary_id = int(primary.id)
        fallback_id = int(fallback.id)

    with SessionLocal() as db:
        model = db.get(ModelConfig, model_id)
        selected = select_model_route(db, model, avoid_route_ids={primary_id})
        assert int(selected.route.id) == fallback_id

        single = _create_model(db, "retry-single")
        first = select_model_route(db, single)
        repeated = select_model_route(
            db,
            single,
            avoid_route_ids={int(first.route.id)},
        )
        assert int(repeated.route.id) == int(first.route.id)


def test_route_snapshot_rejects_cross_model_rebinding(client):
    with SessionLocal() as db:
        first = _create_model(db, "owner-a")
        second = _create_model(db, "owner-b")
        first_route = _create_route(db, first, "owner-a", priority=1)
        second_route = _create_route(db, second, "owner-b", priority=1)
        selection = select_model_route(db, first)
        assert selection.route.id == first_route.id
        frozen = attach_route_snapshot(model_snapshot(selection.runtime_model), selection)
        frozen["route_snapshot"] = {
            **frozen["route_snapshot"],
            "route_id": int(second_route.id),
        }
        db.commit()

        with pytest.raises(ModelSnapshotMismatchError):
            model_from_persisted_snapshot(db, frozen, first, "image")


def test_route_snapshot_rejects_superseded_route_version(client):
    with SessionLocal() as db:
        model = _create_model(db, "superseded")
        route = _create_route(db, model, "superseded", priority=1)
        selection = select_model_route(db, model)
        frozen = attach_route_snapshot(model_snapshot(selection.runtime_model), selection)

        route.name = "Superseding route projection"
        replacement = publish_route_projection(db, route)
        db.commit()

        assert int(replacement.id) != int(selection.route_version.id)
        with pytest.raises(ModelSnapshotMismatchError, match="路由版本已被替换"):
            model_from_persisted_snapshot(db, frozen, model, "image")


def test_route_rollback_appends_history_without_reviving_old_secret(client):
    with SessionLocal() as db:
        model = _create_model(db, "rollback-secret")
        route = _create_route(db, model, "rollback-secret", priority=1)
        initial = publish_route_projection(db, route)
        initial_secret = route.api_key_encrypted
        initial_name = route.name

        route.name = "Changed route"
        route.api_key_encrypted = encrypt_api_key("rotated-live-secret")
        rotated_secret = route.api_key_encrypted
        changed = publish_route_projection(db, route)

        restored = rollback_route_version(db, route, version=int(initial.version))
        assert int(changed.version) == 2
        assert int(restored.version) == 3
        assert int(restored.source_version_id) == int(initial.id)
        assert route.name == initial_name
        assert route.api_key_encrypted == rotated_secret
        assert route.api_key_encrypted != initial_secret
        assert restored.config_snapshot["api_key_encrypted"] == rotated_secret

        repeated = rollback_route_version(db, route, version=int(initial.version))
        assert int(repeated.version) == 4
        assert int(repeated.source_version_id) == int(initial.id)
        assert route.api_key_encrypted == rotated_secret
        assert db.query(ModelRouteVersion).filter_by(route_id=route.id).count() == 4


def test_admin_route_patch_rejects_active_model_task(client, make_user, auth):
    make_user("13972000004", admin=True)
    user_id = make_user("13972000005")
    admin_headers = auth("13972000004")
    with SessionLocal() as db:
        model = _create_model(db, "active-task-guard")
        route = _create_route(db, model, "active-task-guard", priority=1)
        publish_route_projection(db, route)
        db.add(
            GenTask(
                user_id=user_id,
                model_config_id=int(model.id),
                category="image",
                stage="preview",
                model_use="image",
                status="queued",
                cost_frozen=0,
                cost_settled=0,
            )
        )
        db.commit()
        model_id = int(model.id)
        route_id = int(route.id)

    rejected = client.patch(
        f"/api/admin/models/{model_id}/routes/{route_id}",
        headers=admin_headers,
        json={"name": "Must not supersede a running task route"},
    )
    assert rejected.status_code == 409, rejected.text
    assert "仍有排队" in rejected.text
    with SessionLocal() as db:
        route = db.get(ModelRoute, route_id)
        assert route.name == "Route active-task-guard"
        assert route.config_revision == 1
        db.query(GenTask).filter_by(
            user_id=user_id,
            model_config_id=model_id,
            status="queued",
        ).delete()
        db.commit()


def test_admin_route_fault_drill_history_and_rollback(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13972000006", admin=True)
    admin_headers = auth("13972000006")
    created_model = client.post(
        "/api/admin/models",
        headers=admin_headers,
        json={
            "use": "image",
            "model_id": "route-drill-image-model",
            "display_name": "Route drill image model",
            "is_default": False,
            "provider": "custom_openai",
            "base_url": "https://route-drill-model.example.com/v1",
            "api_key": "route-drill-model-secret",
            "gateway_format": "openai",
            "cost_credits": 9,
            "unlock_cost": 0,
            "enabled": True,
            "extra": {
                "capabilities": {"text_to_image": True},
                "credit_pricing": _pricing(9),
            },
        },
    )
    assert created_model.status_code == 201, created_model.text
    model_id = int(created_model.json()["model"]["id"])
    created_route = client.post(
        f"/api/admin/models/{model_id}/routes",
        headers=admin_headers,
        json={
            "route_key": "drill-primary",
            "name": "Drill primary",
            "model_id": "route-drill-provider-model",
            "provider": "custom_openai",
            "base_url": "https://route-drill-primary.example.com/v1",
            "api_key": "route-drill-primary-secret",
            "gateway_format": "openai",
            "priority": 1,
            "failure_threshold": 1,
            "window_seconds": 60,
            "cooldown_seconds": 30,
        },
    )
    assert created_route.status_code == 201, created_route.text
    route_id = int(created_route.json()["id"])
    initial_version_id = int(created_route.json()["active_version"]["id"])

    monkeypatch.setattr(
        "app.routers.admin_catalog.gateway.list_models",
        lambda _config: [{"id": "route-drill-provider-model"}],
    )
    probe = client.post(
        f"/api/admin/models/{model_id}/routes/{route_id}/probe",
        headers=admin_headers,
    )
    assert probe.status_code == 200, probe.text
    assert probe.json()["models"] == [{"id": "route-drill-provider-model"}]
    events = client.get(
        f"/api/admin/models/{model_id}/routes/{route_id}/health-events",
        headers=admin_headers,
    )
    assert events.status_code == 200, events.text
    assert events.json()["items"][0]["operation"] == "probe"
    assert events.json()["items"][0]["outcome"] == "success"

    record_route_outcome(
        route_id,
        operation="fault_drill",
        success=False,
        error_code="provider_timeout",
        counts_toward_circuit=True,
    )
    reset = client.post(
        f"/api/admin/models/{model_id}/routes/{route_id}/reset-health",
        headers=admin_headers,
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["health_status"] == "closed"
    assert reset.json()["window_failures"] == 0

    changed = client.patch(
        f"/api/admin/models/{model_id}/routes/{route_id}",
        headers=admin_headers,
        json={"name": "Drill primary changed"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["active_version"]["version"] == 2
    history = client.get(
        f"/api/admin/models/{model_id}/routes/{route_id}/versions",
        headers=admin_headers,
    )
    assert history.status_code == 200, history.text
    assert [row["version"] for row in history.json()["versions"]] == [2, 1]

    rollback = client.post(
        f"/api/admin/models/{model_id}/routes/{route_id}/versions/1/rollback",
        headers=admin_headers,
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["name"] == "Drill primary"
    assert rollback.json()["active_version"]["version"] == 3
    assert rollback.json()["active_version"]["source_version_id"] == initial_version_id
    assert "route-drill-primary-secret" not in rollback.text

    retired = client.post(
        f"/api/admin/models/{model_id}/routes/{route_id}/versions/2/retire",
        headers=admin_headers,
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["status"] == "retired"


def test_admin_route_quote_task_binding_secret_rotation_and_no_secret_leak(
    client,
    make_user,
    auth,
):
    make_user("13972000001", admin=True)
    user_id = make_user("13972000002", balance=500)
    admin_headers = auth("13972000001")
    user_headers = auth("13972000002")
    model_response = client.post(
        "/api/admin/models",
        headers=admin_headers,
        json={
            "use": "image",
            "model_id": "route-api-image-model",
            "display_name": "Route API image model",
            "is_default": False,
            "provider": "custom_openai",
            "base_url": "https://route-api-model.example.com/v1",
            "api_key": "base-model-secret",
            "gateway_format": "openai",
            "cost_credits": 9,
            "unlock_cost": 0,
            "enabled": True,
            "extra": {
                "capabilities": {"text_to_image": True},
                "credit_pricing": _pricing(9),
            },
        },
    )
    assert model_response.status_code == 201, model_response.text
    model_id = int(model_response.json()["model"]["id"])

    route_response = client.post(
        f"/api/admin/models/{model_id}/routes",
        headers=admin_headers,
        json={
            "route_key": "primary-api",
            "name": "Primary API route",
            "model_id": "route-api-provider-model",
            "provider": "custom_openai",
            "base_url": "https://route-api-primary.example.com/v1",
            "api_key": "primary-route-secret",
            "gateway_format": "openai",
            "priority": 1,
            "failure_threshold": 2,
            "window_seconds": 60,
            "cooldown_seconds": 30,
        },
    )
    assert route_response.status_code == 201, route_response.text
    route_id = int(route_response.json()["id"])
    assert "primary-route-secret" not in route_response.text
    assert "api_key_encrypted" not in route_response.text

    leaked_extra = client.post(
        f"/api/admin/models/{model_id}/routes",
        headers=admin_headers,
        json={
            "route_key": "secret-in-extra",
            "name": "Rejected secret extra",
            "extra": {"authorization": "Bearer should-not-persist"},
        },
    )
    assert leaked_extra.status_code == 422, leaked_extra.text

    payload = {
        "category": "image",
        "stage": "preview",
        "instruction": "route-bound generated ceramic cup",
        "params": {"n": 1, "size": "1024x1024"},
        "model_config_id": model_id,
    }
    quoted = client.post("/api/quotes", headers=user_headers, json=payload)
    assert quoted.status_code == 201, quoted.text
    quote_payload = quoted.json()
    assert quote_payload["route_id"] == route_id
    assert quote_payload["route_key"] == "primary-api"
    assert quote_payload["route_name"] == "Primary API route"
    assert quote_payload["route_health_at_quote"]["status"] == "closed"

    submitted = client.post(
        "/api/generate",
        headers=user_headers,
        json={**payload, "quote_id": quote_payload["quote_id"]},
    )
    assert submitted.status_code == 200, submitted.text
    with SessionLocal() as db:
        task = db.get(GenTask, submitted.json()["id"])
        quote = db.get(GenerationQuote, quote_payload["quote_id"])
        assert task is not None and quote is not None
        assert task.user_id == user_id
        assert task.params["_model_snapshot"] == quote.model_snapshot
        assert task.params["_model_snapshot"]["route_snapshot"]["route_id"] == route_id

    second_payload = {
        **payload,
        "instruction": "second route-bound ceramic cup",
    }
    second_quote = client.post("/api/quotes", headers=user_headers, json=second_payload)
    assert second_quote.status_code == 201, second_quote.text
    rotated = client.patch(
        f"/api/admin/models/{model_id}/routes/{route_id}",
        headers=admin_headers,
        json={"api_key": "rotated-primary-route-secret"},
    )
    assert rotated.status_code == 200, rotated.text
    assert "rotated-primary-route-secret" not in rotated.text

    rejected = client.post(
        "/api/generate",
        headers=user_headers,
        json={**second_payload, "quote_id": second_quote.json()["quote_id"]},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "MODEL_GATEWAY_SNAPSHOT_STALE"
    with SessionLocal() as db:
        quote = db.get(GenerationQuote, second_quote.json()["quote_id"])
        assert quote.status == "active"
        assert quote.task_id is None
