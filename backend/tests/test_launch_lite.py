"""Server-authoritative surface checks for the controlled launch edition."""

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import ModelConfig
from app.services.config_store import resolve_model_config, set_setting


def _enabled_model_ids() -> list[int]:
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(ModelConfig)
                .where(ModelConfig.enabled.is_(True))
                .order_by(ModelConfig.use, ModelConfig.is_default.desc(), ModelConfig.id)
            )
        )
    assert {row.use for row in rows} == {"vision", "image", "video", "prompt"}
    return [int(row.id) for row in rows]


@pytest.fixture()
def newly_enabled_model_id():
    with SessionLocal() as db:
        row = ModelConfig(
            use="image",
            model_id="launch-lite-dynamic-image",
            display_name="Launch Lite Dynamic Image",
            is_default=False,
            sort_order=999,
            provider="custom_openai",
            gateway_format="openai",
            cost_credits=5,
            unlock_cost=0,
            enabled=True,
            extra={"capabilities": {"text_to_image": True}},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        model_id = int(row.id)

    yield model_id

    with SessionLocal() as db:
        row = db.get(ModelConfig, model_id)
        if row is not None:
            db.delete(row)
            db.commit()


def test_launch_lite_filters_navigation_models_advanced_apis_and_payments(
    client, make_user, auth, monkeypatch, newly_enabled_model_id
):
    make_user("13971000991")
    headers = auth("13971000991")
    enabled_ids = _enabled_model_ids()
    monkeypatch.setattr(settings, "product_edition", "launch_lite")

    with SessionLocal() as db:
        set_setting(db, "payment_enabled", True)

    navigation = client.get("/api/navigation", headers=headers)
    assert navigation.status_code == 200, navigation.text
    assert navigation.json()["product_edition"] == "launch_lite"
    assert {item["key"] for item in navigation.json()["items"]} == {
        "studio",
        "prompts",
        "profile",
        "recharge",
        "history",
    }
    prompt_library = next(
        item for item in navigation.json()["items"] if item["key"] == "prompts"
    )
    assert prompt_library["label"] == "提示词库"
    account = next(item for item in navigation.json()["items"] if item["key"] == "recharge")
    assert account["label"] == "账户"

    public_config = client.get("/api/config", headers=headers)
    assert public_config.status_code == 200, public_config.text
    payload = public_config.json()
    assert payload["product_edition"] == "launch_lite"
    assert payload["features"]["payment_enabled"] is False
    assert all(
        payload["features"][key] is False
        for key in (
            "reverse_batch_enabled",
            "video_composition_enabled",
            "reproduction_assessment_enabled",
            "recipes_enabled",
            "projects_enabled",
            "tool_workflows_enabled",
        )
    )
    exposed_ids = {
        int(option["id"])
        for options in payload["model_options"].values()
        for option in options
    }
    assert exposed_ids == set(enabled_ids)
    assert newly_enabled_model_id in exposed_ids

    model_catalog = client.get("/api/models", headers=headers)
    assert model_catalog.status_code == 200, model_catalog.text
    assert {int(item["id"]) for item in model_catalog.json()["items"]} == set(enabled_ids)
    assert client.get(
        f"/api/models/{newly_enabled_model_id}", headers=headers
    ).status_code == 200
    with SessionLocal() as db:
        assert resolve_model_config(db, "image", newly_enabled_model_id).id == newly_enabled_model_id

    assert client.get("/api/projects", headers=headers).status_code == 404
    assert client.get("/api/recipes", headers=headers).status_code == 404
    assert client.get("/api/workflows", headers=headers).status_code == 404
    assert client.get("/api/reproduction-assessments", headers=headers).status_code == 404
    assert client.get("/api/prompt/reverse-batches", headers=headers).status_code == 404
    assert client.post(
        "/api/quotes",
        headers=headers,
        json={
            "kind": "workflow",
            "client_request_id": "launch-lite-workflow-quote",
            "request": {},
        },
    ).status_code == 404
    assert client.post(
        "/api/quotes",
        headers=headers,
        json={
            "kind": "reverse_batch",
            "client_request_id": "launch-lite-batch-quote",
            "request": {},
        },
    ).status_code == 404

    payment_config = client.get("/api/payments/config", headers=headers)
    assert payment_config.status_code == 200, payment_config.text
    assert payment_config.json() == {"enabled": False, "packages": [], "providers": []}
    assert client.get("/api/payments/packages", headers=headers).json() == []

    with SessionLocal() as db:
        set_setting(db, "payment_enabled", False)
