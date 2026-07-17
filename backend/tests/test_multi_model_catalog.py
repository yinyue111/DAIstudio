from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic import command
from alembic.config import Config
from app.config import settings
from app.db import SessionLocal
from app.models import GatewayCall, GenTask, ModelConfig
from app.services.config_store import (
    ModelConfigResolutionError,
    get_model_config,
    resolve_model_config,
)
from app.services.model_capabilities import (
    ModelCapabilityError,
    assert_generation_capability,
    assert_reverse_capability,
)
from app.services.model_gateway_config import decrypt_row_api_key


def test_admin_catalog_create_patch_and_public_secret_boundary(client, make_user, auth):
    make_user("13900001901", admin=True)
    make_user("13900001902")
    admin_headers = auth("13900001901")
    user_headers = auth("13900001902")

    with SessionLocal() as db:
        original_default = get_model_config(db, "image")
        assert original_default is not None
        original_default_id = original_default.id

    created_id = None
    try:
        created = client.post(
            "/api/admin/models",
            headers=admin_headers,
            json={
                "use": "image",
                "model_id": "catalog-image-test",
                "display_name": "Catalog Image Test",
                "provider": "openai",
                "base_url": "https://models.example.com/v1",
                "api_key": "catalog-super-secret",
                "gateway_format": "openai",
                "cost_credits": 7,
                "unlock_cost": 2,
                "enabled": True,
                "is_default": False,
                "sort_order": 20,
                "extra": {
                    "capabilities": {
                        "text_to_image": True,
                        "image_to_image": True,
                        "resolutions": ["1024x1024"],
                        "internal_endpoint": "https://secret.example.com",
                    },
                    "private_metadata": "do-not-publish",
                },
            },
        )
        assert created.status_code == 201, created.text
        created_model = created.json()["model"]
        created_id = created_model["id"]
        assert created_model["display_name"] == "Catalog Image Test"
        assert created_model["is_default"] is False
        assert created_model["api_key_configured"] is True
        assert "catalog-super-secret" not in created.text

        public = client.get("/api/config", headers=user_headers)
        assert public.status_code == 200, public.text
        option = next(
            item for item in public.json()["model_options"]["image"]
            if item["id"] == created_id
        )
        assert option["name"] == "Catalog Image Test"
        assert option["provider_label"] == "OpenAI"
        assert option["capabilities"] == {
            "text_to_image": True,
            "image_to_image": True,
            "resolutions": ["1024x1024"],
        }
        assert "catalog-super-secret" not in public.text
        assert "models.example.com" not in public.text
        assert "do-not-publish" not in public.text
        assert "internal_endpoint" not in public.text

        generated = client.post(
            "/api/generate",
            headers=user_headers,
            json={
                "client_request_id": "catalog-image-generation-001",
                "category": "image",
                "stage": "preview",
                "instruction": "catalog model binding test",
                "params": {"n": 1, "size": "256x256"},
                "model_config_id": created_id,
            },
        )
        assert generated.status_code == 200, generated.text
        generated_task = generated.json()
        assert generated_task["model_config_id"] == created_id
        assert generated_task["model_name"] == "Catalog Image Test"
        assert generated_task["model_id"] == "catalog-image-test"
        with SessionLocal() as db:
            task = db.get(GenTask, generated_task["id"])
            assert task.model_config_id == created_id
            assert task.params["_model_snapshot"]["model_config_id"] == created_id
            call = (
                db.query(GatewayCall)
                .filter(GatewayCall.task_id == task.id, GatewayCall.kind == "image")
                .one()
            )
            assert call.model_config_id == created_id

        made_default = client.patch(
            f"/api/admin/models/{created_id}",
            headers=admin_headers,
            json={"is_default": True, "sort_order": 1},
        )
        assert made_default.status_code == 200, made_default.text
        with SessionLocal() as db:
            defaults = db.query(ModelConfig).filter_by(use="image", is_default=True).all()
            assert [row.id for row in defaults] == [created_id]
            assert resolve_model_config(db, "image").id == created_id

        cannot_disable_default = client.patch(
            f"/api/admin/models/{created_id}",
            headers=admin_headers,
            json={"enabled": False},
        )
        assert cannot_disable_default.status_code == 409
        restored_default = client.patch(
            f"/api/admin/models/{original_default_id}",
            headers=admin_headers,
            json={"is_default": True},
        )
        assert restored_default.status_code == 200, restored_default.text
        disabled = client.patch(
            f"/api/admin/models/{created_id}",
            headers=admin_headers,
            json={"enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        public = client.get("/api/config", headers=user_headers).json()
        assert created_id not in {item["id"] for item in public["model_options"]["image"]}
        with SessionLocal() as db:
            assert resolve_model_config(db, "image").id == original_default_id
            with pytest.raises(ModelConfigResolutionError, match="已停用"):
                resolve_model_config(db, "image", created_id)
            with pytest.raises(ModelConfigResolutionError, match="不支持 video"):
                resolve_model_config(db, "video", original_default_id)
    finally:
        if created_id is not None:
            with SessionLocal() as db:
                row = db.get(ModelConfig, created_id)
                if row is not None:
                    db.delete(row)
                original = db.get(ModelConfig, original_default_id)
                if original is not None:
                    original.is_default = True
                db.commit()


def test_admin_catalog_rejects_duplicate_model_id(client, make_user, auth):
    make_user("13900001903", admin=True)
    headers = auth("13900001903")
    with SessionLocal() as db:
        image = get_model_config(db, "image")
        assert image is not None
        model_id = image.model_id
    response = client.post(
        "/api/admin/models",
        headers=headers,
        json={
            "use": "image",
            "model_id": model_id,
            "cost_credits": 2,
        },
    )
    assert response.status_code == 409


def test_admin_catalog_bulk_import_reuses_one_encrypted_provider_connection(
    client, make_user, auth
):
    make_user("13900001908", admin=True)
    headers = auth("13900001908")
    model_ids = ["bulk-grok-image", "bulk-grok-video"]
    created_ids = []
    try:
        response = client.post(
            "/api/admin/models/import",
            headers=headers,
            json={
                "provider": "grok",
                "base_url": "https://models.example.com/v1",
                "api_key": "bulk-import-secret",
                "gateway_format": "openai",
                "models": [
                    {
                        "use": "image",
                        "model_id": model_ids[0],
                        "display_name": model_ids[0],
                        "cost_credits": 15,
                        "extra": {"image_transport": "grok_images"},
                    },
                    {
                        "use": "video",
                        "model_id": model_ids[1],
                        "display_name": model_ids[1],
                        "cost_credits": 16,
                        "extra": {"submit_path": "/v1/videos/generations"},
                    },
                ],
            },
        )
        assert response.status_code == 201, response.text
        imported = response.json()["models"]
        created_ids = [item["id"] for item in imported]
        assert len(imported) == 2
        assert "bulk-import-secret" not in response.text
        with SessionLocal() as db:
            rows = db.query(ModelConfig).filter(ModelConfig.id.in_(created_ids)).all()
            assert {row.provider for row in rows} == {"grok"}
            assert all(row.api_key_encrypted for row in rows)
            assert all(row.api_key_encrypted != "bulk-import-secret" for row in rows)
    finally:
        with SessionLocal() as db:
            db.query(ModelConfig).filter(ModelConfig.id.in_(created_ids)).delete(
                synchronize_session=False
            )
            db.commit()


def test_admin_probe_uses_the_requested_catalog_row_not_the_default(
    client, make_user, auth, monkeypatch
):
    make_user("13900001909", admin=True)
    headers = auth("13900001909")
    created_id = None
    seen = {}
    try:
        created = client.post(
            "/api/admin/models",
            headers=headers,
            json={
                "use": "prompt",
                "model_id": "probe-antigravity-row",
                "provider": "antigravity",
                "base_url": "https://models.example.com/antigravity",
                "api_key": "probe-row-secret",
                "gateway_format": "anthropic",
                "cost_credits": 1,
                "is_default": False,
            },
        )
        assert created.status_code == 201, created.text
        created_id = created.json()["model"]["id"]

        def fake_list_models(config):
            seen["config"] = config
            return [{"id": "gemini-3.1-flash-image"}]

        monkeypatch.setattr("app.routers.admin_models.gateway.list_models", fake_list_models)
        probed = client.post(
            "/api/admin/models/probe",
            headers=headers,
            json={"model_config_id": created_id, "use": "prompt"},
        )
        assert probed.status_code == 200, probed.text
        assert seen["config"].provider == "antigravity"
        assert seen["config"].gateway_format == "anthropic"
        assert seen["config"].api_key == "probe-row-secret"
        assert probed.json()["models"][0]["recommended_uses"] == ["image"]
    finally:
        if created_id is not None:
            with SessionLocal() as db:
                row = db.get(ModelConfig, created_id)
                if row is not None:
                    db.delete(row)
                    db.commit()


def test_admin_create_reuses_existing_provider_without_resubmitting_secret(
    client, make_user, auth, monkeypatch
):
    make_user("13900001910", admin=True)
    headers = auth("13900001910")
    created_ids = []
    seen = {}
    try:
        source = client.post(
            "/api/admin/models",
            headers=headers,
            json={
                "use": "prompt",
                "model_id": "saved-provider-source",
                "display_name": "已保存供应商来源",
                "provider": "grok",
                "base_url": "https://models.example.com/v1",
                "api_key": "saved-provider-secret",
                "gateway_format": "openai",
                "cost_credits": 1,
                "is_default": False,
            },
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["model"]["id"]
        created_ids.append(source_id)

        catalog = client.get("/api/admin/models", headers=headers)
        assert catalog.status_code == 200, catalog.text
        connection = next(
            item
            for item in catalog.json()["provider_connections"]
            if item["id"] == source_id
        )
        assert connection == {
            "id": source_id,
            "provider": "grok",
            "label": "Grok / xAI Compatible",
            "gateway_format": "openai",
            "source_model_id": "saved-provider-source",
            "source_display_name": "已保存供应商来源",
        }
        assert "base_url" not in connection
        assert "api_key" not in connection
        assert "saved-provider-secret" not in catalog.text

        def fake_list_models(config):
            seen["probe"] = config
            return [{"id": "grok-imagine-image"}]

        monkeypatch.setattr("app.routers.admin_models.gateway.list_models", fake_list_models)
        probed = client.post(
            "/api/admin/models/probe",
            headers=headers,
            json={"provider_config_id": source_id, "use": "image"},
        )
        assert probed.status_code == 200, probed.text
        assert seen["probe"].use == "image"
        assert seen["probe"].provider == "grok"
        assert seen["probe"].api_key == "saved-provider-secret"

        created = client.post(
            "/api/admin/models",
            headers=headers,
            json={
                "use": "image",
                "model_id": "grok-imagine-image",
                "display_name": "Grok Imagine Image",
                "provider_config_id": source_id,
                "cost_credits": 15,
                "is_default": False,
            },
        )
        assert created.status_code == 201, created.text
        target_id = created.json()["model"]["id"]
        created_ids.append(target_id)
        with SessionLocal() as db:
            target = db.get(ModelConfig, target_id)
            assert target.provider == "grok"
            assert target.base_url == "https://models.example.com/v1"
            assert target.gateway_format == "openai"
            assert decrypt_row_api_key(target) == "saved-provider-secret"

        conflict = client.post(
            "/api/admin/models",
            headers=headers,
            json={
                "use": "video",
                "model_id": "invalid-mixed-provider-source",
                "provider_config_id": source_id,
                "base_url": "https://override.example.com/v1",
                "cost_credits": 16,
            },
        )
        assert conflict.status_code == 422
        assert "不能与 Base URL" in conflict.text
    finally:
        with SessionLocal() as db:
            db.query(ModelConfig).filter(ModelConfig.id.in_(created_ids)).delete(
                synchronize_session=False
            )
            db.commit()


def test_admin_catalog_enforces_supported_protocol_and_enabled_default(
    client, make_user, auth
):
    make_user("13900001905", admin=True)
    headers = auth("13900001905")
    anthropic_vision = client.post(
        "/api/admin/models",
        headers=headers,
        json={
            "use": "vision",
            "model_id": "unsupported-anthropic-vision",
            "provider": "anthropic",
            "gateway_format": "anthropic",
            "cost_credits": 2,
        },
    )
    assert anthropic_vision.status_code == 422
    assert "不支持 Anthropic" in anthropic_vision.text

    disabled_default = client.post(
        "/api/admin/models",
        headers=headers,
        json={
            "use": "image",
            "model_id": "disabled-default-image",
            "enabled": False,
            "is_default": True,
            "cost_credits": 2,
        },
    )
    assert disabled_default.status_code == 400
    assert "默认模型必须处于启用状态" in disabled_default.text

    with SessionLocal() as db:
        prompt_default = resolve_model_config(db, "prompt")
        assert prompt_default is not None
        prompt_default_id = prompt_default.id
        assert db.query(ModelConfig).filter(ModelConfig.use == "prompt").count() == 1
    unset_only_default = client.patch(
        f"/api/admin/models/{prompt_default_id}",
        headers=headers,
        json={"is_default": False},
    )
    assert unset_only_default.status_code == 409
    assert "必须保留一个" in unset_only_default.text


def test_reverse_capabilities_only_reject_explicitly_unsupported_modes():
    legacy = type("LegacyModel", (), {"extra": {}})()
    assert_reverse_capability(legacy, target="image", source_type="image")

    image_only = type(
        "ImageOnlyModel",
        (),
        {"extra": {"capabilities": {"image_analysis": True, "video_analysis": False}}},
    )()
    assert_reverse_capability(image_only, target="image", source_type="image")
    with pytest.raises(ModelCapabilityError, match="不支持视频分析"):
        assert_reverse_capability(image_only, target="video", source_type="video")

    no_portraits = type(
        "NoPortraitModel",
        (),
        {"extra": {"capabilities": {"image_analysis": True, "portrait_profile": False}}},
    )()
    with pytest.raises(ModelCapabilityError, match="不支持人物档案"):
        assert_reverse_capability(no_portraits, target="portrait_profile", source_type="image")


@pytest.mark.parametrize(
    ("capabilities", "message"),
    [
        ({"max_reference_images": 4}, "未明确支持多参考图"),
        ({"multi_reference": True}, "未声明有效"),
        ({"multi_reference": True, "max_reference_images": True}, "未声明有效"),
        ({"multi_reference": True, "max_reference_images": 2}, "最多支持 2 张"),
    ],
)
def test_product_details_require_strict_video_multi_reference_capabilities(
    capabilities,
    message,
):
    model = type("VideoModel", (), {"extra": {"capabilities": capabilities}})()

    with pytest.raises(ModelCapabilityError, match=message):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/product.png",
            source_type="image",
            params={
                "product_reference_image": "https://example.com/product.png",
                "product_detail_images": [
                    "https://example.com/detail-a.png",
                    "https://example.com/detail-b.png",
                ],
            },
        )


def test_product_detail_capability_counts_unique_reference_urls():
    model = type(
        "VideoModel",
        (),
        {
            "extra": {
                "capabilities": {
                    "image_to_video": True,
                    "multi_reference": True,
                    "max_reference_images": 3,
                }
            }
        },
    )()

    assert_generation_capability(
        model,
        category="video",
        source_asset_url="https://example.com/product.png",
        source_type="image",
        params={
            "product_reference_image": "https://example.com/product.png",
            "product_detail_images": [
                "https://example.com/detail-a.png",
                "https://example.com/detail-b.png",
            ],
            "first_frame_image": "https://example.com/product.png",
        },
    )


def test_admin_runtime_change_blocks_only_tasks_using_that_model(client, make_user, auth):
    admin_id = make_user("13900001904", admin=True)
    headers = auth("13900001904")
    created_ids: list[int] = []
    task_id = None
    try:
        for suffix in ("active", "idle"):
            response = client.post(
                "/api/admin/models",
                headers=headers,
                json={
                    "use": "image",
                    "model_id": f"catalog-{suffix}-model",
                    "display_name": f"Catalog {suffix.title()}",
                    "cost_credits": 2,
                    "is_default": False,
                },
            )
            assert response.status_code == 201, response.text
            created_ids.append(response.json()["model"]["id"])

        with SessionLocal() as db:
            task = GenTask(
                user_id=admin_id,
                model_config_id=created_ids[0],
                category="image",
                stage="preview",
                model_use="image",
                status="queued",
                cost_frozen=0,
                cost_settled=0,
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            task_id = task.id

        idle_change = client.patch(
            f"/api/admin/models/{created_ids[1]}",
            headers=headers,
            json={"model_id": "catalog-idle-model-v2"},
        )
        assert idle_change.status_code == 200, idle_change.text
        active_change = client.patch(
            f"/api/admin/models/{created_ids[0]}",
            headers=headers,
            json={"model_id": "catalog-active-model-v2"},
        )
        assert active_change.status_code == 409
        assert "仍有排队" in active_change.text
    finally:
        with SessionLocal() as db:
            if task_id is not None:
                task = db.get(GenTask, task_id)
                if task is not None:
                    db.delete(task)
                    db.flush()
            for model_config_id in created_ids:
                row = db.get(ModelConfig, model_config_id)
                if row is not None:
                    db.delete(row)
            db.commit()


def test_0036_catalog_migration_backfills_and_enforces_one_default(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0036-catalog.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0035_async_reverse_operations")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    before = sa.inspect(engine)
    assert {idx["name"]: idx for idx in before.get_indexes("model_configs")}[
        "ix_model_configs_use"
    ]["unique"]

    command.upgrade(cfg, "0036_multi_model_catalog")
    inspected = sa.inspect(engine)
    columns = {column["name"] for column in inspected.get_columns("model_configs")}
    assert {"display_name", "is_default", "sort_order"} <= columns
    indexes = {idx["name"]: idx for idx in inspected.get_indexes("model_configs")}
    assert not indexes["ix_model_configs_use"]["unique"]
    assert bool(indexes["uq_model_configs_default_per_use"]["unique"])
    assert bool(indexes["uq_model_configs_use_model_id"]["unique"])
    for table in ("gen_tasks", "reverse_operations", "gateway_calls"):
        assert "model_config_id" in {
            column["name"] for column in inspected.get_columns(table)
        }

    with engine.begin() as connection:
        rows = connection.execute(
            sa.text("SELECT use, model_id, display_name, is_default FROM model_configs")
        ).mappings().all()
        assert rows
        assert all(row["display_name"] == row["model_id"] for row in rows)
        assert all(bool(row["is_default"]) for row in rows)
        connection.execute(
            sa.text(
                "INSERT INTO model_configs "
                "(use, model_id, display_name, is_default, sort_order, cost_credits, unlock_cost, enabled) "
                "VALUES ('prompt', 'prompt-secondary', 'Prompt Secondary', 0, 10, 2, 0, 1)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO model_configs "
                "(use, model_id, display_name, is_default, sort_order, cost_credits, unlock_cost, enabled) "
                "VALUES ('image', 'image-default', 'Image Default', 1, 0, 2, 0, 1)"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO model_configs "
                    "(use, model_id, display_name, is_default, sort_order, cost_credits, unlock_cost, enabled) "
                    "VALUES ('image', 'image-default-2', 'Image Default 2', 1, 20, 2, 0, 1)"
                )
            )


def test_0036_downgrade_blocks_multi_row_catalog(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-0036-downgrade.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(cfg, "0036_multi_model_catalog")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO model_configs "
                "(use, model_id, display_name, is_default, sort_order, cost_credits, unlock_cost, enabled) "
                "VALUES ('prompt', 'prompt-secondary', 'Prompt Secondary', 0, 10, 2, 0, 1)"
            )
        )
    with pytest.raises(RuntimeError, match="multiple model configs"):
        command.downgrade(cfg, "0035_async_reverse_operations")
