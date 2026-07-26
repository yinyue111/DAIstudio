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


# Per-feature switch matrix: public flag name -> override setting field plus
# the observable surfaces owned by that feature.
FEATURE_MATRIX = {
    "projects_enabled": {
        "setting": "feature_projects_enabled",
        "api_probes": ("/api/projects",),
        "nav_key": "projects",
    },
    "recipes_enabled": {
        "setting": "feature_recipes_enabled",
        "api_probes": ("/api/recipes",),
    },
    "reverse_batch_enabled": {
        "setting": "feature_reverse_batch_enabled",
        "api_probes": ("/api/prompt/reverse-batches",),
        "quote_kind": "reverse_batch",
    },
    "video_composition_enabled": {
        "setting": "feature_video_composition_enabled",
    },
    "reproduction_assessment_enabled": {
        "setting": "feature_reproduction_assessment_enabled",
        "api_probes": ("/api/reproduction-assessments",),
    },
    "tool_workflows_enabled": {
        "setting": "feature_tool_workflows_enabled",
        # No bare GET /api/workflows route exists; the runs listing is the
        # reachable probe behind the same gated prefix.
        "api_probes": ("/api/workflows/runs",),
        "nav_key": "catalog",
        "quote_kind": "workflow",
    },
}

_MATRIX_PHONE = "13971000992"


def _reset_feature_overrides(monkeypatch):
    for spec in FEATURE_MATRIX.values():
        monkeypatch.setattr(settings, spec["setting"], None, raising=False)


def _features(client, headers) -> dict:
    response = client.get("/api/config", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["features"]


def _navigation_keys(client, headers) -> set[str]:
    response = client.get("/api/navigation", headers=headers)
    assert response.status_code == 200, response.text
    return {item["key"] for item in response.json()["items"]}


def _quote_status(client, headers, kind: str) -> int:
    return client.post(
        "/api/quotes",
        headers=headers,
        json={
            "kind": kind,
            "client_request_id": f"feature-matrix-{kind}-quote",
            "request": {},
        },
    ).status_code


def test_full_edition_defaults_enable_every_feature(client, make_user, auth, monkeypatch):
    make_user(_MATRIX_PHONE)
    headers = auth(_MATRIX_PHONE)
    monkeypatch.setattr(settings, "product_edition", "full")
    _reset_feature_overrides(monkeypatch)

    features = _features(client, headers)
    assert all(features[name] is True for name in FEATURE_MATRIX)
    navigation_keys = _navigation_keys(client, headers)
    assert {"catalog", "projects"} <= navigation_keys
    for spec in FEATURE_MATRIX.values():
        for path in spec.get("api_probes", ()):
            assert client.get(path, headers=headers).status_code != 404, path


@pytest.mark.parametrize("feature", sorted(FEATURE_MATRIX))
def test_single_feature_disable_overrides_full_edition(
    client, make_user, auth, monkeypatch, feature
):
    make_user(_MATRIX_PHONE)
    headers = auth(_MATRIX_PHONE)
    monkeypatch.setattr(settings, "product_edition", "full")
    _reset_feature_overrides(monkeypatch)
    spec = FEATURE_MATRIX[feature]
    monkeypatch.setattr(settings, spec["setting"], False, raising=False)

    features = _features(client, headers)
    assert features[feature] is False
    assert all(features[name] is True for name in FEATURE_MATRIX if name != feature)

    for path in spec.get("api_probes", ()):
        assert client.get(path, headers=headers).status_code == 404, path
    if "nav_key" in spec:
        assert spec["nav_key"] not in _navigation_keys(client, headers)
    if "quote_kind" in spec:
        assert _quote_status(client, headers, spec["quote_kind"]) == 404

    # Untouched features keep their surfaces reachable.
    for other, other_spec in FEATURE_MATRIX.items():
        if other == feature:
            continue
        for path in other_spec.get("api_probes", ()):
            assert client.get(path, headers=headers).status_code != 404, path


@pytest.mark.parametrize("feature", sorted(FEATURE_MATRIX))
def test_single_feature_enable_overrides_launch_lite(
    client, make_user, auth, monkeypatch, feature
):
    make_user(_MATRIX_PHONE)
    headers = auth(_MATRIX_PHONE)
    monkeypatch.setattr(settings, "product_edition", "launch_lite")
    _reset_feature_overrides(monkeypatch)
    spec = FEATURE_MATRIX[feature]
    monkeypatch.setattr(settings, spec["setting"], True, raising=False)

    features = _features(client, headers)
    assert features[feature] is True
    assert all(features[name] is False for name in FEATURE_MATRIX if name != feature)

    for path in spec.get("api_probes", ()):
        assert client.get(path, headers=headers).status_code != 404, path
    if "nav_key" in spec:
        assert spec["nav_key"] in _navigation_keys(client, headers)

    # Features left on the edition default stay gated.
    for other, other_spec in FEATURE_MATRIX.items():
        if other == feature:
            continue
        for path in other_spec.get("api_probes", ()):
            assert client.get(path, headers=headers).status_code == 404, path
        if "quote_kind" in other_spec:
            assert _quote_status(client, headers, other_spec["quote_kind"]) == 404


def test_navigation_labels_follow_recipes_switch_not_edition(
    client, make_user, auth, monkeypatch
):
    make_user(_MATRIX_PHONE)
    headers = auth(_MATRIX_PHONE)

    # launch_lite with recipes explicitly re-enabled: prompts keeps its full
    # name instead of flipping back to the lite label, recharge stays "账户".
    monkeypatch.setattr(settings, "product_edition", "launch_lite")
    _reset_feature_overrides(monkeypatch)
    monkeypatch.setattr(settings, "feature_recipes_enabled", True, raising=False)
    navigation = client.get("/api/navigation", headers=headers)
    assert navigation.status_code == 200, navigation.text
    labels = {item["key"]: item["label"] for item in navigation.json()["items"]}
    assert labels["prompts"] == "灵感配方"
    assert labels["recharge"] == "账户"

    # full edition with recipes explicitly disabled: only prompts is renamed.
    monkeypatch.setattr(settings, "product_edition", "full")
    monkeypatch.setattr(settings, "feature_recipes_enabled", False, raising=False)
    navigation = client.get("/api/navigation", headers=headers)
    assert navigation.status_code == 200, navigation.text
    labels = {item["key"]: item["label"] for item in navigation.json()["items"]}
    assert labels["prompts"] == "提示词库"
    assert labels["recharge"] == "充值"


def test_launch_lite_payments_stay_closed_despite_feature_overrides(
    client, make_user, auth, monkeypatch
):
    make_user(_MATRIX_PHONE)
    headers = auth(_MATRIX_PHONE)
    monkeypatch.setattr(settings, "product_edition", "launch_lite")
    for spec in FEATURE_MATRIX.values():
        monkeypatch.setattr(settings, spec["setting"], True, raising=False)
    with SessionLocal() as db:
        set_setting(db, "payment_enabled", True)
    try:
        payment_config = client.get("/api/payments/config", headers=headers)
        assert payment_config.status_code == 200, payment_config.text
        assert payment_config.json() == {"enabled": False, "packages": [], "providers": []}
        assert client.get("/api/payments/packages", headers=headers).json() == []
        assert _features(client, headers)["payment_enabled"] is False
    finally:
        with SessionLocal() as db:
            set_setting(db, "payment_enabled", False)
