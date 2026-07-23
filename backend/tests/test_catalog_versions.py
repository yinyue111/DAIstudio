"""Public catalog, immutable versions, rollback, and configurable tools."""

import json

import pytest

from app.db import SessionLocal
from app.models import (
    CreditTransaction,
    GenerationQuote,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    ReverseOperation,
    ToolDefinition,
    ToolVersion,
    User,
)
from app.services import reverse_operations
from app.services.config_store import set_setting
from tests.reverse_helpers import post_reverse


def _model_body() -> dict:
    return {
        "use": "image",
        "model_id": "catalog-version-image-test",
        "display_name": "目录版本图片模型",
        "provider": "openai",
        "base_url": "https://models.example.com/v1",
        "api_key": "catalog-version-secret",
        "gateway_format": "openai",
        "cost_credits": 5,
        "unlock_cost": 1,
        "enabled": True,
        "is_default": False,
        "extra": {
            "capabilities": {
                "text_to_image": True,
                "resolutions": ["1k"],
                "internal_route": "never-public",
            },
            "credit_pricing": {
                "image": {"1k": 5, "2k": 6, "4k": 7},
                "image_edit": {"1k": 5, "2k": 6, "4k": 7},
                "video_preview_cost": 50,
                "video_per_second": {"480p": 10, "720p": 10, "1080p": 10},
            },
        },
    }


def test_authenticated_navigation_catalog_is_versioned_and_permission_aware(
    client, make_user, auth
):
    make_user("13971000003")
    response = client.get("/api/navigation", headers=auth("13971000003"))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == 1
    assert payload["default_key"] == "studio"
    assert payload["items"][0]["key"] == "studio"
    assert payload["items"][0]["href"] == "/"
    assert all(item["enabled"] and item["visible"] for item in payload["items"])
    assert "admin" not in {item["key"] for item in payload["items"]}

    make_user("13971000004", admin=True)
    admin_payload = client.get("/api/navigation", headers=auth("13971000004")).json()
    admin = next(item for item in admin_payload["items"] if item["key"] == "admin")
    assert admin["permission"] == "admin"
    assert admin["reserve_desktop"] is True
    assert "permission" not in next(item for item in payload["items"] if item["key"] == "studio")

    client.cookies.clear()
    unauthenticated = client.get("/api/navigation")
    assert unauthenticated.status_code == 401


def test_navigation_catalog_applies_server_feature_states(client, make_user, auth):
    make_user("13971000007")
    headers = auth("13971000007")
    with SessionLocal() as db:
        set_setting(
            db,
            "navigation_states",
            {"catalog": "disabled", "prompts": "hidden"},
        )
    try:
        response = client.get("/api/navigation", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        items = {item["key"]: item for item in payload["items"]}
        assert items["studio"]["enabled"] is True
        assert items["catalog"]["enabled"] is False
        assert items["catalog"]["visible"] is True
        assert items["catalog"]["disabled_reason"] == "该功能已由管理员暂停使用"
        assert "prompts" not in items
        assert "admin" not in items
    finally:
        with SessionLocal() as db:
            set_setting(db, "navigation_states", {})


def test_admin_settings_reject_navigation_changes_to_reserved_entries(client, make_user, auth):
    make_user("13971000008", admin=True)
    response = client.put(
        "/api/admin/settings",
        headers=auth("13971000008"),
        json={"navigation_states": {"studio": "hidden"}},
    )
    assert response.status_code == 422, response.text


def test_public_model_catalog_history_and_admin_rollback(client, make_user, auth):
    make_user("13971000001", admin=True)
    make_user("13971000002")
    admin_headers = auth("13971000001")
    user_headers = auth("13971000002")
    created = client.post("/api/admin/models", json=_model_body(), headers=admin_headers)
    assert created.status_code == 201, created.text
    model_id = created.json()["model"]["id"]
    try:
        public = client.get("/api/models?use=image&q=目录版本", headers=user_headers)
        assert public.status_code == 200, public.text
        item = next(row for row in public.json()["items"] if row["id"] == model_id)
        assert item["capability_version"]["version"] == 1
        assert item["price_version"]["version"] == 1
        assert item["capabilities"] == {"text_to_image": True, "resolutions": ["1k"]}
        assert "secret" not in public.text
        assert "internal_route" not in public.text

        patched = client.patch(
            f"/api/admin/models/{model_id}",
            headers=admin_headers,
            json={
                "cost_credits": 11,
                "unlock_cost": 3,
                "extra": {
                    "capabilities": {
                        "text_to_image": True,
                        "image_to_image": True,
                        "resolutions": ["1k", "2k"],
                    },
                    "credit_pricing": {
                        "image": {"1k": 11, "2k": 12, "4k": 13},
                        "image_edit": {"1k": 11, "2k": 12, "4k": 13},
                        "video_preview_cost": 50,
                        "video_per_second": {
                            "480p": 10,
                            "720p": 10,
                            "1080p": 10,
                        },
                    },
                },
            },
        )
        assert patched.status_code == 200, patched.text
        history = client.get(f"/api/admin/models/{model_id}/versions", headers=admin_headers)
        assert history.status_code == 200, history.text
        payload = history.json()
        assert [row["version"] for row in payload["capability_versions"]] == [2, 1]
        assert [row["status"] for row in payload["capability_versions"]] == [
            "published",
            "disabled",
        ]
        assert [row["version"] for row in payload["price_versions"]] == [2, 1]
        assert [row["status"] for row in payload["price_versions"]] == [
            "published",
            "disabled",
        ]
        capability_v1_id = payload["capability_versions"][1]["id"]
        price_v1_id = payload["price_versions"][1]["id"]

        capability_rollback = client.post(
            f"/api/admin/models/{model_id}/versions/activate",
            json={"kind": "capability", "version": 1},
            headers=admin_headers,
        )
        assert capability_rollback.status_code == 200, capability_rollback.text
        price_rollback = client.post(
            f"/api/admin/models/{model_id}/versions/activate",
            json={"kind": "price", "version": 1},
            headers=admin_headers,
        )
        assert price_rollback.status_code == 200, price_rollback.text
        detail = client.get(f"/api/models/{model_id}", headers=user_headers).json()
        assert detail["capability_version"]["version"] == 3
        assert detail["capability_version"]["source_version_id"] == capability_v1_id
        assert detail["price_version"]["version"] == 3
        assert detail["price_version"]["source_version_id"] == price_v1_id
        assert detail["cost_credits"] == 5
        assert detail["unlock_cost"] == 1
        assert detail["capabilities"] == {"text_to_image": True, "resolutions": ["1k"]}
    finally:
        with SessionLocal() as db:
            db.query(ModelCapabilityVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelPriceVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelConfig).filter_by(id=model_id).delete()
            db.commit()


def test_model_version_full_lifecycle_keeps_historical_quote_references_stable(
    client,
    make_user,
    auth,
):
    make_user("13971000005", admin=True)
    make_user("13971000006")
    admin_headers = auth("13971000005")
    user_headers = auth("13971000006")
    created = client.post("/api/admin/models", json=_model_body(), headers=admin_headers)
    assert created.status_code == 201, created.text
    model_id = created.json()["model"]["id"]
    quote_id = None
    try:
        initial = client.get(
            f"/api/admin/models/{model_id}/versions",
            headers=admin_headers,
        ).json()
        capability_v1 = initial["capability_version"]
        price_v1 = initial["price_version"]

        capability_draft = client.post(
            f"/api/admin/models/{model_id}/versions",
            headers=admin_headers,
            json={
                "kind": "capability",
                "schema_version": "capability.v2",
                "capabilities": {"text_to_image": True, "resolutions": ["2k"]},
            },
        )
        assert capability_draft.status_code == 201, capability_draft.text
        capability_payload = capability_draft.json()
        assert capability_payload["capability_version"]["version"] == 1
        capability_v2 = capability_payload["capability_versions"][0]
        assert capability_v2["version"] == 2
        assert capability_v2["status"] == "draft"
        assert capability_v2["activated_at"] is None
        public_before_publish = client.get(f"/api/models/{model_id}", headers=user_headers).json()
        assert public_before_publish["capability_version"]["version"] == 1

        edited = client.patch(
            f"/api/admin/models/{model_id}/versions/capability/2",
            headers=admin_headers,
            json={
                "capabilities": {
                    "text_to_image": True,
                    "image_to_image": True,
                    "resolutions": ["2k", "4k"],
                }
            },
        )
        assert edited.status_code == 200, edited.text
        published_capability = client.post(
            f"/api/admin/models/{model_id}/versions/capability/2/publish",
            headers=admin_headers,
        )
        assert published_capability.status_code == 200, published_capability.text
        assert published_capability.json()["capability_version"]["version"] == 2
        assert (
            client.patch(
                f"/api/admin/models/{model_id}/versions/capability/2",
                headers=admin_headers,
                json={"capabilities": {"text_to_image": False}},
            ).status_code
            == 409
        )

        price_draft = client.post(
            f"/api/admin/models/{model_id}/versions",
            headers=admin_headers,
            json={
                "kind": "price",
                "schema_version": "credit-price.v2",
                "base_cost_credits": 13,
                "unlock_cost_credits": 4,
                "pricing": {
                    "image": {"1k": 13, "2k": 14, "4k": 15},
                    "image_edit": {"1k": 13, "2k": 14, "4k": 15},
                    "video_preview_cost": 50,
                    "video_per_second": {
                        "480p": 10,
                        "720p": 10,
                        "1080p": 10,
                    },
                },
            },
        )
        assert price_draft.status_code == 201, price_draft.text
        published_price = client.post(
            f"/api/admin/models/{model_id}/versions/price/2/publish",
            headers=admin_headers,
        )
        assert published_price.status_code == 200, published_price.text
        price_v2 = published_price.json()["price_version"]

        quoted = client.post(
            "/api/quotes",
            headers=user_headers,
            json={
                "client_request_id": "catalog-version-history-quote",
                "model_config_id": model_id,
                "category": "image",
                "stage": "preview",
                "instruction": "historical reference",
                "params": {"n": 1, "size": "1024x1024"},
            },
        )
        assert quoted.status_code == 201, quoted.text
        quote_id = quoted.json()["quote_id"]
        with SessionLocal() as db:
            capability_v2_row = (
                db.query(ModelCapabilityVersion)
                .filter_by(
                    model_config_id=model_id,
                    version=2,
                )
                .one()
            )
            price_v2_row = (
                db.query(ModelPriceVersion)
                .filter_by(
                    model_config_id=model_id,
                    version=2,
                )
                .one()
            )
            capability_v2_id = int(capability_v2_row.id)
            price_v2_id = int(price_v2_row.id)
            capability_snapshot = dict(capability_v2_row.capabilities)
            price_snapshot = dict(price_v2_row.pricing)
            quote = db.get(GenerationQuote, quote_id)
            assert quote.capability_version_id == capability_v2_id
            assert quote.price_version_id == price_v2_id

        capability_rollback = client.post(
            f"/api/admin/models/{model_id}/versions/capability/1/rollback",
            headers=admin_headers,
        )
        assert capability_rollback.status_code == 200, capability_rollback.text
        rolled_capability = capability_rollback.json()["capability_version"]
        assert rolled_capability["version"] == 3
        assert rolled_capability["source_version_id"] == capability_v1["id"]
        price_rollback = client.post(
            f"/api/admin/models/{model_id}/versions/price/1/rollback",
            headers=admin_headers,
        )
        assert price_rollback.status_code == 200, price_rollback.text
        rolled_price = price_rollback.json()["price_version"]
        assert rolled_price["version"] == 3
        assert rolled_price["source_version_id"] == price_v1["id"]

        retired_capability = client.post(
            f"/api/admin/models/{model_id}/versions/capability/2/retire",
            headers=admin_headers,
        )
        assert retired_capability.status_code == 200, retired_capability.text
        retired_price = client.post(
            f"/api/admin/models/{model_id}/versions/price/2/retire",
            headers=admin_headers,
        )
        assert retired_price.status_code == 200, retired_price.text
        assert (
            client.post(
                f"/api/admin/models/{model_id}/versions/capability/2/rollback",
                headers=admin_headers,
            ).status_code
            == 409
        )

        with SessionLocal() as db:
            quote = db.get(GenerationQuote, quote_id)
            assert quote.capability_version_id == capability_v2_id
            assert quote.price_version_id == price_v2_id
            capability_v2_row = db.get(ModelCapabilityVersion, capability_v2_id)
            price_v2_row = db.get(ModelPriceVersion, price_v2_id)
            assert capability_v2_row.status == "retired"
            assert price_v2_row.status == "retired"
            assert capability_v2_row.capabilities == capability_snapshot
            assert price_v2_row.pricing == price_snapshot

        disabled_model = client.patch(
            f"/api/admin/models/{model_id}",
            headers=admin_headers,
            json={"enabled": False},
        )
        assert disabled_model.status_code == 200, disabled_model.text
        off_sale = client.get(
            f"/api/admin/models/{model_id}/versions",
            headers=admin_headers,
        ).json()
        off_sale_capability = off_sale["capability_version"]
        off_sale_price = off_sale["price_version"]
        assert off_sale_capability["version"] > rolled_capability["version"]
        assert off_sale_capability["metadata_snapshot"]["enabled"] is False
        assert off_sale_price["version"] == rolled_price["version"]
        assert (
            client.post(
                f"/api/admin/models/{model_id}/versions/capability/"
                f"{off_sale_capability['version']}/disable",
                headers=admin_headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/admin/models/{model_id}/versions/price/"
                f"{off_sale_price['version']}/disable",
                headers=admin_headers,
            ).status_code
            == 200
        )
        no_active = client.get(
            f"/api/admin/models/{model_id}/versions",
            headers=admin_headers,
        ).json()
        assert no_active["capability_version"] is None
        assert no_active["price_version"] is None

        reenabled = client.patch(
            f"/api/admin/models/{model_id}",
            headers=admin_headers,
            json={"enabled": True},
        )
        assert reenabled.status_code == 200, reenabled.text
        active_again = client.get(
            f"/api/admin/models/{model_id}/versions",
            headers=admin_headers,
        ).json()
        assert active_again["capability_version"]["version"] > off_sale_capability["version"]
        assert active_again["capability_version"]["metadata_snapshot"]["enabled"] is True
        assert active_again["price_version"]["version"] > off_sale_price["version"]
        assert price_v2["base_cost_credits"] == 13
        with SessionLocal() as db:
            quote = db.get(GenerationQuote, quote_id)
            assert quote.capability_version_id == capability_v2_id
            assert quote.price_version_id == price_v2_id
    finally:
        with SessionLocal() as db:
            if quote_id is not None:
                db.query(GenerationQuote).filter_by(id=quote_id).delete()
            db.query(ModelCapabilityVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelPriceVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelConfig).filter_by(id=model_id).delete()
            db.commit()


def test_reverse_provider_cost_snapshots_survive_price_publish_and_rollback(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13971000009", admin=True)
    user_id = make_user("13971000010", balance=100)
    admin_headers = auth("13971000009")
    user_headers = auth("13971000010")
    initial_provider_cost = {
        "image_cost": 2,
        "video_preset_costs": {"fast": 3, "standard": 4, "fine": 6},
        "audio_surcharge": 1,
    }
    initial_reverse_pricing = {
        "image_cost": 5,
        "video_preset_costs": {"fast": 8, "standard": 12, "fine": 18},
        "audio_surcharge": 2,
        "provider_cost_credits": initial_provider_cost,
    }
    created = client.post(
        "/api/admin/models",
        headers=admin_headers,
        json={
            "use": "vision",
            "model_id": "reverse-provider-version-test",
            "display_name": "反推成本版本测试",
            "provider": "openai",
            "base_url": "https://models.example.com/v1",
            "api_key": "reverse-provider-version-secret",
            "gateway_format": "openai",
            "cost_credits": 5,
            "unlock_cost": 0,
            "enabled": True,
            "is_default": False,
            "extra": {
                "capabilities": {"image_analysis": True},
                "reverse_pricing": initial_reverse_pricing,
            },
        },
    )
    assert created.status_code == 201, created.text
    model_id = created.json()["model"]["id"]
    quote_ids: list[int] = []
    operation_id = None
    operation_quote_id = None
    monkeypatch.setattr(
        reverse_operations,
        "enqueue_operation",
        lambda _operation_id: "queued",
    )
    try:
        quote = client.post(
            "/api/quotes",
            headers=user_headers,
            json={
                "kind": "reverse",
                "client_request_id": "reverse-provider-version-quote-v1",
                "request": {
                    "asset_url": "https://cdn.example.com/provider-v1.png",
                    "source_type": "image",
                    "target": "image",
                    "model_config_id": model_id,
                },
            },
        )
        assert quote.status_code == 201, quote.text
        quote_ids.append(quote.json()["quote_id"])
        operation = post_reverse(
            client,
            {
                "client_request_id": "reverse-provider-version-operation-v1",
                "asset_url": "https://cdn.example.com/provider-operation-v1.png",
                "source_type": "image",
                "target": "image",
                "model_config_id": model_id,
            },
            headers=user_headers,
        )
        assert operation.status_code == 202, operation.text
        operation_id = operation.json()["id"]
        operation_quote_id = operation.json()["quote_id"]

        with SessionLocal() as db:
            old_quote = db.get(GenerationQuote, quote_ids[0])
            old_operation = db.get(ReverseOperation, operation_id)
            assert (
                old_quote.pricing_snapshot["allocation"]["provider_cost_credits"]
                == initial_provider_cost
            )
            assert old_operation.pricing_snapshot["provider_cost_credits"] == initial_provider_cost
        assert reverse_operations.fail_operation(
            operation_id,
            code="TEST_COMPLETED",
            error="release frozen credits before publishing the next price version",
        )

        replacement_provider_cost = {
            "image_cost": 7,
            "video_preset_costs": {"fast": 9, "standard": 11, "fine": 13},
            "audio_surcharge": 3,
        }
        price_draft = client.post(
            f"/api/admin/models/{model_id}/versions",
            headers=admin_headers,
            json={
                "kind": "price",
                "schema_version": "credit-price.v3",
                "base_cost_credits": 5,
                "unlock_cost_credits": 0,
                "pricing": {
                    "reverse": {
                        **initial_reverse_pricing,
                        "provider_cost_credits": replacement_provider_cost,
                    }
                },
            },
        )
        assert price_draft.status_code == 201, price_draft.text
        published = client.post(
            f"/api/admin/models/{model_id}/versions/price/2/publish",
            headers=admin_headers,
        )
        assert published.status_code == 200, published.text

        replacement_quote = client.post(
            "/api/quotes",
            headers=user_headers,
            json={
                "kind": "reverse",
                "client_request_id": "reverse-provider-version-quote-v2",
                "request": {
                    "asset_url": "https://cdn.example.com/provider-v2.png",
                    "source_type": "image",
                    "target": "image",
                    "model_config_id": model_id,
                },
            },
        )
        assert replacement_quote.status_code == 201, replacement_quote.text
        quote_ids.append(replacement_quote.json()["quote_id"])

        rolled_back = client.post(
            f"/api/admin/models/{model_id}/versions/price/1/rollback",
            headers=admin_headers,
        )
        assert rolled_back.status_code == 200, rolled_back.text
        with SessionLocal() as db:
            old_quote = db.get(GenerationQuote, quote_ids[0])
            replacement_quote_row = db.get(GenerationQuote, quote_ids[1])
            old_operation = db.get(ReverseOperation, operation_id)
            model = db.get(ModelConfig, model_id)
            assert (
                old_quote.pricing_snapshot["allocation"]["provider_cost_credits"]
                == initial_provider_cost
            )
            assert old_operation.pricing_snapshot["provider_cost_credits"] == initial_provider_cost
            assert (
                replacement_quote_row.pricing_snapshot["allocation"]["provider_cost_credits"]
                == replacement_provider_cost
            )
            assert model.extra["reverse_pricing"]["provider_cost_credits"] == initial_provider_cost
    finally:
        with SessionLocal() as db:
            if operation_id is not None:
                db.query(CreditTransaction).filter_by(
                    biz_type="reverse_operation",
                    biz_ref=operation_id,
                ).delete()
                db.query(ReverseOperation).filter_by(id=operation_id).delete()
                user = db.get(User, user_id)
                if user is not None:
                    user.balance_credits = 100
                    user.frozen_credits = 0
            cleanup_quote_ids = [*quote_ids]
            if operation_quote_id is not None:
                cleanup_quote_ids.append(operation_quote_id)
            if cleanup_quote_ids:
                db.query(GenerationQuote).filter(GenerationQuote.id.in_(cleanup_quote_ids)).delete(
                    synchronize_session=False
                )
            db.query(ModelCapabilityVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelPriceVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelConfig).filter_by(id=model_id).delete()
            db.commit()


def test_configurable_tool_catalog_versions_and_visibility(client, make_user, auth):
    make_user("13971000003", admin=True)
    make_user("13971000004")
    admin_headers = auth("13971000003")
    user_headers = auth("13971000004")
    created = client.post(
        "/api/admin/tools",
        headers=admin_headers,
        json={
            "slug": "catalog-tool-test",
            "name": "目录工具测试",
            "description": "可版本化工具",
            "category": "workflow",
            "renderer": "studio",
            "entry_path": "/?workflow=catalog-tool-test",
            "icon": "wand-sparkles",
            "featured": True,
            "initial_version": {
                "input_schema": {"fields": [{"name": "prompt", "type": "text"}]},
                "workflow": {"type": "studio_preset", "creation_mode": "image_edit"},
                "pricing_policy": {"type": "server_quote"},
                "capabilities": {"reverse": True},
            },
        },
    )
    assert created.status_code == 201, created.text
    tool_id = created.json()["id"]
    try:
        public = client.get("/api/tools?featured=true", headers=user_headers)
        assert public.status_code == 200, public.text
        item = next(row for row in public.json()["items"] if row["id"] == tool_id)
        assert item["slug"] == "catalog-tool-test"
        assert item["active_version"]["version"] == 1
        assert item["active_version"]["workflow"]["creation_mode"] == "image_edit"

        version = client.post(
            f"/api/admin/tools/{tool_id}/versions",
            headers=admin_headers,
            json={
                "schema_version": "tool.v2",
                "input_schema": {"fields": [{"name": "asset", "type": "image"}]},
                "workflow": {"type": "studio_preset", "creation_mode": "video_edit"},
                "pricing_policy": {"type": "server_quote", "stages": ["reverse", "generate"]},
                "capabilities": {"reverse": True, "generate": True},
            },
        )
        assert version.status_code == 201, version.text
        assert version.json()["active_version"]["version"] == 1
        assert version.json()["versions"][0]["status"] == "draft"
        published = client.post(
            f"/api/admin/tools/{tool_id}/versions/2/publish",
            headers=admin_headers,
        )
        assert published.status_code == 200, published.text
        assert published.json()["active_version"]["version"] == 2
        history = client.get(f"/api/admin/tools/{tool_id}/versions", headers=admin_headers).json()
        assert [row["version"] for row in history["versions"]] == [2, 1]

        rollback = client.post(
            f"/api/admin/tools/{tool_id}/versions/1/activate",
            headers=admin_headers,
        )
        assert rollback.status_code == 200, rollback.text
        assert rollback.json()["active_version"]["version"] == 3
        assert (
            rollback.json()["active_version"]["source_version_id"] == history["versions"][1]["id"]
        )
        assert rollback.json()["active_version"]["workflow"]["creation_mode"] == ("image_edit")

        disabled = client.patch(
            f"/api/admin/tools/{tool_id}",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert disabled.status_code == 200, disabled.text
        assert tool_id not in {
            row["id"] for row in client.get("/api/tools", headers=user_headers).json()["items"]
        }
    finally:
        with SessionLocal() as db:
            db.query(ToolVersion).filter_by(tool_definition_id=tool_id).delete()
            db.query(ToolDefinition).filter_by(id=tool_id).delete()
            db.commit()


def test_model_catalog_metadata_is_versioned_and_rollback_keeps_current_gateway(
    client,
    make_user,
    auth,
):
    make_user("13971000021", admin=True)
    headers = auth("13971000021")
    body = {
        **_model_body(),
        "model_id": "catalog-metadata-model-original",
        "display_name": "目录元数据原始模型",
        "sort_order": 7,
        "is_default": False,
    }
    created = client.post("/api/admin/models", json=body, headers=headers)
    assert created.status_code == 201, created.text
    model_id = int(created.json()["model"]["id"])
    try:
        initial = client.get(
            f"/api/admin/models/{model_id}/versions",
            headers=headers,
        ).json()
        metadata_v1 = initial["capability_version"]["metadata_snapshot"]
        assert metadata_v1 == {
            "schema_version": "model-catalog-metadata.v1",
            "origin": "recorded",
            "model_id": "catalog-metadata-model-original",
            "display_name": "目录元数据原始模型",
            "is_default": False,
            "sort_order": 7,
            "enabled": True,
        }
        serialized = json.dumps(metadata_v1, ensure_ascii=False)
        assert "catalog-version-secret" not in serialized
        assert "models.example.com" not in serialized
        assert "provider" not in metadata_v1
        assert "base_url" not in metadata_v1

        with SessionLocal() as db:
            row = db.get(ModelConfig, model_id)
            gateway_before = (row.provider, row.base_url, row.api_key_encrypted)

        changed = client.patch(
            f"/api/admin/models/{model_id}",
            headers=headers,
            json={
                "model_id": "catalog-metadata-model-renamed",
                "display_name": "目录元数据新名称",
                "sort_order": 41,
            },
        )
        assert changed.status_code == 200, changed.text
        history = client.get(
            f"/api/admin/models/{model_id}/versions",
            headers=headers,
        ).json()
        assert history["capability_version"]["version"] == 2
        assert history["capability_version"]["metadata_snapshot"]["model_id"] == (
            "catalog-metadata-model-renamed"
        )
        assert history["capability_versions"][1]["metadata_snapshot"] == metadata_v1

        rolled = client.post(
            f"/api/admin/models/{model_id}/versions/capability/1/rollback",
            headers=headers,
        )
        assert rolled.status_code == 200, rolled.text
        active = rolled.json()["capability_version"]
        assert active["version"] == 3
        assert active["metadata_snapshot"] == metadata_v1
        with SessionLocal() as db:
            row = db.get(ModelConfig, model_id)
            assert row.model_id == "catalog-metadata-model-original"
            assert row.display_name == "目录元数据原始模型"
            assert row.sort_order == 7
            assert (row.provider, row.base_url, row.api_key_encrypted) == gateway_before
    finally:
        with SessionLocal() as db:
            db.query(ModelCapabilityVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelPriceVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelConfig).filter_by(id=model_id).delete()
            db.commit()


def test_tool_metadata_change_draft_publish_and_rollback_restore_snapshots(
    client,
    make_user,
    auth,
):
    make_user("13971000022", admin=True)
    headers = auth("13971000022")
    created = client.post(
        "/api/admin/tools",
        headers=headers,
        json={
            "slug": "catalog-metadata-tool-original",
            "name": "元数据工具原名",
            "description": "原始说明",
            "category": "workflow",
            "renderer": "studio",
            "entry_path": "/?workflow=catalog-metadata-tool-original",
            "icon": "wand-sparkles",
            "sort_order": 8,
            "featured": False,
            "initial_version": {
                "input_schema": {},
                "workflow": {"type": "studio_preset", "creation_mode": "image"},
                "pricing_policy": {"type": "server_quote"},
                "capabilities": {"image": True},
            },
        },
    )
    assert created.status_code == 201, created.text
    tool_id = int(created.json()["id"])
    try:
        metadata_v1 = created.json()["active_version"]["metadata_snapshot"]
        draft = client.post(
            f"/api/admin/tools/{tool_id}/versions",
            headers=headers,
            json={
                "schema_version": "tool.v2",
                "input_schema": {},
                "workflow": {"type": "studio_preset", "creation_mode": "video"},
                "pricing_policy": {"type": "server_quote"},
                "capabilities": {"video": True},
            },
        )
        assert draft.status_code == 201, draft.text
        assert draft.json()["versions"][0]["metadata_snapshot"] == metadata_v1

        changed = client.patch(
            f"/api/admin/tools/{tool_id}",
            headers=headers,
            json={
                "slug": "catalog-metadata-tool-renamed",
                "name": "元数据工具新名",
                "description": "新说明",
                "entry_path": "/?workflow=catalog-metadata-tool-renamed",
                "sort_order": 31,
                "featured": True,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["active_version"]["version"] == 3
        assert changed.json()["active_version"]["metadata_snapshot"]["slug"] == (
            "catalog-metadata-tool-renamed"
        )

        published_draft = client.post(
            f"/api/admin/tools/{tool_id}/versions/2/publish",
            headers=headers,
        )
        assert published_draft.status_code == 200, published_draft.text
        assert published_draft.json()["slug"] == "catalog-metadata-tool-original"
        assert published_draft.json()["name"] == "元数据工具原名"
        assert published_draft.json()["active_version"]["metadata_snapshot"] == metadata_v1

        rollback = client.post(
            f"/api/admin/tools/{tool_id}/versions/3/rollback",
            headers=headers,
        )
        assert rollback.status_code == 200, rollback.text
        assert rollback.json()["slug"] == "catalog-metadata-tool-renamed"
        assert rollback.json()["name"] == "元数据工具新名"
        assert rollback.json()["active_version"]["version"] == 4
        assert rollback.json()["active_version"]["metadata_snapshot"]["origin"] == "recorded"
    finally:
        with SessionLocal() as db:
            db.query(ToolVersion).filter_by(tool_definition_id=tool_id).delete()
            db.query(ToolDefinition).filter_by(id=tool_id).delete()
            db.commit()


def test_model_catalog_audit_failure_rolls_back_metadata_and_version(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13971000023", admin=True)
    headers = auth("13971000023")
    body = {
        **_model_body(),
        "model_id": "catalog-audit-atomic-model",
        "display_name": "审计原子模型",
        "is_default": False,
    }
    created = client.post("/api/admin/models", json=body, headers=headers)
    assert created.status_code == 201, created.text
    model_id = int(created.json()["model"]["id"])
    with SessionLocal() as db:
        before_count = db.query(ModelCapabilityVersion).filter_by(model_config_id=model_id).count()

    def fail_required_audit(*_args, **_kwargs):
        raise RuntimeError("required audit unavailable")

    monkeypatch.setattr("app.routers.admin_models.audit.log_required", fail_required_audit)
    try:
        with pytest.raises(RuntimeError, match="required audit unavailable"):
            client.patch(
                f"/api/admin/models/{model_id}",
                headers=headers,
                json={"display_name": "不应保存的名称"},
            )
        with SessionLocal() as db:
            row = db.get(ModelConfig, model_id)
            assert row.display_name == "审计原子模型"
            assert (
                db.query(ModelCapabilityVersion).filter_by(model_config_id=model_id).count()
                == before_count
            )
    finally:
        with SessionLocal() as db:
            db.query(ModelCapabilityVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelPriceVersion).filter_by(model_config_id=model_id).delete()
            db.query(ModelConfig).filter_by(id=model_id).delete()
            db.commit()


def test_tool_catalog_audit_failure_rolls_back_metadata_and_version(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13971000024", admin=True)
    headers = auth("13971000024")
    created = client.post(
        "/api/admin/tools",
        headers=headers,
        json={
            "slug": "catalog-audit-atomic-tool",
            "name": "审计原子工具",
            "description": "工具审计失败时不得留下半成品版本",
            "category": "workflow",
            "renderer": "studio",
            "entry_path": "/?workflow=catalog-audit-atomic-tool",
            "icon": "wand-sparkles",
            "sort_order": 14,
            "featured": False,
            "initial_version": {
                "input_schema": {},
                "workflow": {"type": "studio_preset", "creation_mode": "image"},
                "pricing_policy": {"type": "server_quote"},
                "capabilities": {"image": True},
            },
        },
    )
    assert created.status_code == 201, created.text
    tool_id = int(created.json()["id"])
    with SessionLocal() as db:
        before_count = db.query(ToolVersion).filter_by(tool_definition_id=tool_id).count()

    def fail_required_audit(*_args, **_kwargs):
        raise RuntimeError("required audit unavailable")

    monkeypatch.setattr("app.routers.admin_catalog.audit.log_required", fail_required_audit)
    try:
        with pytest.raises(RuntimeError, match="required audit unavailable"):
            client.patch(
                f"/api/admin/tools/{tool_id}",
                headers=headers,
                json={"name": "不应保存的工具名称"},
            )
        with SessionLocal() as db:
            row = db.get(ToolDefinition, tool_id)
            assert row.name == "审计原子工具"
            versions = (
                db.query(ToolVersion)
                .filter_by(tool_definition_id=tool_id)
                .order_by(ToolVersion.version)
                .all()
            )
            assert len(versions) == before_count
            assert versions[-1].is_active is True
            assert versions[-1].metadata_snapshot["name"] == "审计原子工具"
    finally:
        with SessionLocal() as db:
            db.query(ToolVersion).filter_by(tool_definition_id=tool_id).delete()
            db.query(ToolDefinition).filter_by(id=tool_id).delete()
            db.commit()
