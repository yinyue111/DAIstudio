from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from alembic import command
from alembic.config import Config
from app.config import settings
from app.db import SessionLocal
from app.models import (
    GatewayCall,
    GenAsset,
    GenerationDispatch,
    GenerationQuote,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
)
from app.services.catalog import public_capabilities, public_model_option
from app.services.config_store import (
    ModelConfigResolutionError,
    get_model_config,
    resolve_model_config,
)
from app.services.generation_model_runtime import model_snapshot
from app.services.generation_request import validate_generation_params
from app.services.model_capabilities import (
    ModelCapabilityError,
    assert_generation_capability,
    assert_reverse_capability,
)
from app.services.model_gateway_config import decrypt_row_api_key


def test_public_catalog_exposes_only_supported_product_video_templates():
    assert public_capabilities(
        {
            "capabilities": {
                "product_video_templates": [
                    "stable_showcase",
                    "unknown-template",
                    "stable_showcase",
                    3,
                ],
            }
        }
    ) == {"product_video_templates": ["stable_showcase"]}


def test_public_catalog_exposes_platform_video_input_capabilities():
    assert public_capabilities(
        {
            "capabilities": {
                "video_to_video": True,
                "video_reference": False,
                "video_edit": True,
                "audio_reference": False,
                "max_reference_videos": 1,
                "max_reference_audio": 0,
                "vendor_max_reference_videos": 3,
            }
        }
    ) == {
        "video_to_video": True,
        "video_reference": False,
        "video_edit": True,
        "audio_reference": False,
        "max_reference_videos": 1,
        "max_reference_audio": 0,
    }


def test_public_catalog_exposes_canonical_image_mask_capability_only():
    assert public_capabilities(
        {
            "capabilities": {
                "mask_edit": True,
                "image_mask": True,
                "inpainting": True,
            }
        }
    ) == {"mask_edit": True}


def test_admin_catalog_create_patch_and_public_secret_boundary(
    client, make_user, auth, quote_and_generate
):
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

        generated = quote_and_generate(
            headers=user_headers,
            payload={
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
                tasks = db.query(GenTask).filter_by(model_config_id=created_id).all()
                task_ids = [int(task.id) for task in tasks]
                quotes = db.query(GenerationQuote).filter_by(
                    model_config_id=created_id
                ).all()
                for task in tasks:
                    task.quote_id = None
                for quote in quotes:
                    quote.task_id = None
                db.flush()
                if task_ids:
                    db.query(GenerationDispatch).filter(
                        GenerationDispatch.task_id.in_(task_ids)
                    ).delete(synchronize_session=False)
                    db.query(GenAsset).filter(GenAsset.task_id.in_(task_ids)).delete(
                        synchronize_session=False
                    )
                    db.query(GatewayCall).filter(
                        GatewayCall.task_id.in_(task_ids)
                    ).delete(synchronize_session=False)
                    db.query(GenTask).filter(GenTask.id.in_(task_ids)).delete(
                        synchronize_session=False
                    )
                for quote in quotes:
                    db.delete(quote)
                row = db.get(ModelConfig, created_id)
                if row is not None:
                    db.delete(row)
                original = db.get(ModelConfig, original_default_id)
                if original is not None:
                    original.is_default = True
                db.commit()


def test_admin_model_purpose_can_change_and_soft_delete_preserves_history(
    client, make_user, auth
):
    make_user("13900001921", admin=True)
    headers = auth("13900001921")
    payload = {
        "use": "image",
        "model_id": "movable-soft-delete-model",
        "display_name": "Movable Soft Delete Model",
        "cost_credits": 3,
        "unlock_cost": 0,
        "enabled": True,
        "is_default": False,
        "sort_order": 900,
        "extra": {"capabilities": {"text_to_image": True}},
    }
    created = client.post("/api/admin/models", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    model_config_id = int(created.json()["model"]["id"])

    moved = client.patch(
        f"/api/admin/models/{model_config_id}",
        headers=headers,
        json={"use": "video"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["model"]["use"] == "video"

    deleted = client.delete(f"/api/admin/models/{model_config_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}
    listed_ids = {
        int(item["id"])
        for item in client.get("/api/admin/models", headers=headers).json()["models"]
    }
    assert model_config_id not in listed_ids
    with SessionLocal() as db:
        historical_row = db.get(ModelConfig, model_config_id)
        assert historical_row is not None
        assert historical_row.deleted_at is not None
        assert historical_row.enabled is False
        assert historical_row.is_default is False
        with pytest.raises(ModelConfigResolutionError, match="不存在"):
            resolve_model_config(db, "video", model_config_id)

    recreated = client.post(
        "/api/admin/models",
        headers=headers,
        json={**payload, "use": "video"},
    )
    assert recreated.status_code == 201, recreated.text
    recreated_id = int(recreated.json()["model"]["id"])
    assert recreated_id != model_config_id
    with SessionLocal() as db:
        for row_id in (model_config_id, recreated_id):
            row = db.get(ModelConfig, row_id)
            if row is not None:
                db.delete(row)
        db.commit()


def test_admin_cannot_move_or_delete_model_with_active_tasks(client, make_user, auth):
    user_id = make_user("13900001922", admin=True)
    headers = auth("13900001922")
    with SessionLocal() as db:
        default = get_model_config(db, "image")
        assert default is not None
        row = ModelConfig(
            use="image",
            model_id="active-task-protected-model",
            display_name="Active Task Protected Model",
            is_default=False,
            sort_order=901,
            provider=default.provider,
            base_url=default.base_url,
            api_key_encrypted=default.api_key_encrypted,
            gateway_format=default.gateway_format,
            cost_credits=1,
            unlock_cost=0,
            enabled=True,
            extra=dict(default.extra or {}),
        )
        db.add(row)
        db.flush()
        task = GenTask(
            user_id=user_id,
            model_config_id=row.id,
            category="image",
            stage="preview",
            prompt={"final_text": "active task"},
            model_use="image",
            params={},
            status="running",
            cost_frozen=0,
            cost_settled=0,
        )
        db.add(task)
        db.commit()
        model_config_id = int(row.id)

    moved = client.patch(
        f"/api/admin/models/{model_config_id}", headers=headers, json={"use": "video"}
    )
    assert moved.status_code == 409
    deleted = client.delete(f"/api/admin/models/{model_config_id}", headers=headers)
    assert deleted.status_code == 409

    with SessionLocal() as db:
        db.query(GenTask).filter_by(model_config_id=model_config_id).delete()
        row = db.get(ModelConfig, model_config_id)
        if row is not None:
            db.delete(row)
        db.commit()


def test_retry_keeps_task_bound_catalog_model_when_default_differs(client, make_user, auth):
    uid = make_user("13900001912", balance=1000)
    headers = auth("13900001912")
    task_id = None
    bound_model_id = None
    try:
        with SessionLocal() as db:
            default_model = get_model_config(db, "image")
            assert default_model is not None
            bound_model = ModelConfig(
                use="image",
                model_id="retry-bound-catalog-image",
                display_name="Retry Bound Catalog Image",
                is_default=False,
                sort_order=999,
                provider=default_model.provider,
                base_url=default_model.base_url,
                api_key_encrypted=default_model.api_key_encrypted,
                gateway_format=default_model.gateway_format,
                cost_credits=3,
                unlock_cost=0,
                enabled=True,
                extra=dict(default_model.extra or {}),
            )
            db.add(bound_model)
            db.flush()
            snapshot = model_snapshot(bound_model)
            task = GenTask(
                user_id=uid,
                model_config_id=bound_model.id,
                category="image",
                stage="preview",
                prompt={"final_text": "retry with the originally selected catalog model"},
                model_use="image",
                params={"n": 1, "size": "256x256", "_model_snapshot": snapshot},
                status="failed",
                cost_frozen=0,
                cost_settled=0,
            )
            db.add(task)
            db.commit()
            task_id = task.id
            bound_model_id = bound_model.id

        response = client.post(f"/api/tasks/{task_id}/retry", headers=headers)
        assert response.status_code == 200, response.text

        with SessionLocal() as db:
            retried = db.get(GenTask, task_id)
            assert retried is not None
            assert retried.model_config_id == bound_model_id
            assert retried.params["_model_snapshot"]["model_config_id"] == bound_model_id
            assert retried.params["_model_snapshot"]["model_id"] == "retry-bound-catalog-image"
            call = (
                db.query(GatewayCall)
                .filter(GatewayCall.task_id == task_id, GatewayCall.kind == "image")
                .one()
            )
            assert call.model_config_id == bound_model_id
    finally:
        if bound_model_id is not None:
            with SessionLocal() as db:
                tasks = (
                    db.query(GenTask).filter(GenTask.id == task_id).all()
                    if task_id is not None
                    else []
                )
                task_ids = [int(task.id) for task in tasks]
                quotes = db.query(GenerationQuote).filter(
                    GenerationQuote.model_config_id == bound_model_id
                ).all()
                for task in tasks:
                    task.quote_id = None
                for quote in quotes:
                    quote.task_id = None
                db.flush()
                if task_ids:
                    db.query(GenerationDispatch).filter(
                        GenerationDispatch.task_id.in_(task_ids)
                    ).delete(synchronize_session=False)
                    db.query(GenAsset).filter(GenAsset.task_id.in_(task_ids)).delete(
                        synchronize_session=False
                    )
                    db.query(GatewayCall).filter(
                        sa.or_(
                            GatewayCall.task_id.in_(task_ids),
                            GatewayCall.model_config_id == bound_model_id,
                        )
                    ).delete(synchronize_session=False)
                    db.query(GenTask).filter(GenTask.id.in_(task_ids)).delete(
                        synchronize_session=False
                    )
                for quote in quotes:
                    db.delete(quote)
                db.query(ModelCapabilityVersion).filter(
                    ModelCapabilityVersion.model_config_id == bound_model_id
                ).delete(synchronize_session=False)
                db.query(ModelPriceVersion).filter(
                    ModelPriceVersion.model_config_id == bound_model_id
                ).delete(synchronize_session=False)
                db.query(ModelConfig).filter(ModelConfig.id == bound_model_id).delete(
                    synchronize_session=False
                )
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
            db.query(ModelCapabilityVersion).filter(
                ModelCapabilityVersion.model_config_id.in_(created_ids)
            ).delete(synchronize_session=False)
            db.query(ModelPriceVersion).filter(
                ModelPriceVersion.model_config_id.in_(created_ids)
            ).delete(synchronize_session=False)
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
            return [{"id": "grok-imagine-image-2.0"}]

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
        assert probed.json()["models"][0]["recommended_uses"] == ["image"]

        created = client.post(
            "/api/admin/models",
            headers=headers,
            json={
                "use": "image",
                "model_id": "grok-imagine-image-2.0",
                "display_name": "Grok Imagine Image 2.0",
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
            assert target.extra == {
                "image_transport": "grok_images",
                "response_format": "b64_json",
                "edit_path": "/images/edits",
                "edit_payload_format": "json",
                "multi_image_edit_enabled": True,
                "capabilities": {
                    "text_to_image": True,
                    "image_to_image": True,
                    "reference_image": True,
                    "multi_reference": True,
                    "max_reference_images": 2,
                    "mask_edit": False,
                },
            }

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
            db.query(ModelCapabilityVersion).filter(
                ModelCapabilityVersion.model_config_id.in_(created_ids)
            ).delete(synchronize_session=False)
            db.query(ModelPriceVersion).filter(
                ModelPriceVersion.model_config_id.in_(created_ids)
            ).delete(synchronize_session=False)
            db.query(ModelConfig).filter(ModelConfig.id.in_(created_ids)).delete(
                synchronize_session=False
            )
            db.commit()


def test_admin_catalog_accepts_anthropic_vision_and_enforces_enabled_default(
    client, make_user, auth
):
    make_user("13900001905", admin=True)
    headers = auth("13900001905")
    anthropic_vision = client.post(
        "/api/admin/models",
        headers=headers,
        json={
            "use": "vision",
            "model_id": "supported-anthropic-vision",
            "display_name": "Supported Anthropic Vision",
            "provider": "antigravity",
            "base_url": "https://vision.example.com/antigravity",
            "api_key": "anthropic-vision-secret",
            "gateway_format": "anthropic",
            "cost_credits": 2,
        },
    )
    assert anthropic_vision.status_code == 201, anthropic_vision.text
    anthropic_vision_id = anthropic_vision.json()["model"]["id"]
    public_config = client.get("/api/config", headers=headers)
    assert public_config.status_code == 200, public_config.text
    assert any(
        item["id"] == anthropic_vision_id
        for item in public_config.json()["model_options"]["vision"]
    )

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

    with SessionLocal() as db:
        db.query(ModelCapabilityVersion).filter_by(
            model_config_id=anthropic_vision_id
        ).delete(synchronize_session=False)
        db.query(ModelPriceVersion).filter_by(
            model_config_id=anthropic_vision_id
        ).delete(synchronize_session=False)
        db.query(ModelConfig).filter_by(id=anthropic_vision_id).delete(
            synchronize_session=False
        )
        db.commit()


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


def test_known_grok_15_profile_rejects_text_video_when_admin_metadata_is_missing():
    model = type(
        "Grok15Model",
        (),
        {
            "use": "video",
            "model_id": "grok-imagine-video-1.5",
            "provider": "grok",
            "gateway_format": "openai",
            "extra": {},
        },
    )()

    with pytest.raises(ModelCapabilityError, match="不支持文生视频"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url=None,
            source_type=None,
            params={},
        )

    assert_generation_capability(
        model,
        category="video",
        source_asset_url="https://example.com/first.png",
        source_type="image",
        params={},
    )


def test_known_profile_does_not_reenable_an_admin_disabled_supported_mode():
    model = type(
        "GptImage2Model",
        (),
        {
            "use": "image",
            "model_id": "gpt-image-2",
            "provider": "yinyue",
            "gateway_format": "openai",
            "extra": {"capabilities": {"text_to_image": False}},
        },
    )()

    with pytest.raises(ModelCapabilityError, match="不支持文生图"):
        assert_generation_capability(
            model,
            category="image",
            source_asset_url=None,
            source_type=None,
            params={},
        )


def test_grok_image_family_profile_supports_custom_openai_generation_and_editing():
    model = type(
        "GrokImage20Model",
        (),
        {
            "id": 200,
            "use": "image",
            "model_id": "grok-imagine-image-2.0",
            "display_name": "Grok Imagine Image 2.0",
            "provider": "custom_openai",
            "gateway_format": "openai",
            "is_default": False,
            "sort_order": 50,
            "cost_credits": 8,
            "unlock_cost": 0,
            "extra": {},
        },
    )()

    assert_generation_capability(
        model,
        category="image",
        source_asset_url=None,
        source_type=None,
        params={},
    )
    assert_generation_capability(
        model,
        category="image",
        source_asset_url="https://example.com/source.png",
        source_type="image",
        params={"style_reference_image": "https://example.com/style.png"},
    )
    option = public_model_option(model)
    assert option["capabilities"] == {
        "text_to_image": True,
        "image_to_image": True,
        "reference_image": True,
        "multi_reference": True,
        "max_reference_images": 2,
        "mask_edit": False,
    }


def test_known_seedance_15_profile_overrides_stale_reference_flags():
    model = type(
        "Seedance15Model",
        (),
        {
            "use": "video",
            "model_id": "doubao-seedance-1-5-pro-251215",
            "provider": "volcengine_ark",
            "gateway_format": "ark",
            "extra": {
                "capabilities": {
                    "reference_image": True,
                    "multi_reference": True,
                    "video_to_video": True,
                }
            },
        },
    )()

    with pytest.raises(ModelCapabilityError, match="不支持主体或风格参考图"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/product.png",
            source_type="image",
            params={"product_reference_image": "https://example.com/product.png"},
        )
    with pytest.raises(ModelCapabilityError, match="不支持视频输入"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/source.mp4",
            source_type="video",
            params={},
        )

    assert_generation_capability(
        model,
        category="video",
        source_asset_url="https://example.com/product.png",
        source_type="image",
        params={
            "subject_mode": "product",
            "video_image_input_mode": "first_frame",
            "first_frame_image": "https://example.com/product.png",
        },
    )
    with pytest.raises(ModelCapabilityError, match="不能同时提交产品"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/product.png",
            source_type="image",
            params={
                "subject_mode": "product",
                "video_image_input_mode": "first_frame",
                "first_frame_image": "https://example.com/product.png",
                "product_reference_image": "https://example.com/product.png",
            },
        )


def test_video_image_input_mode_request_contract_is_strict():
    params = validate_generation_params(
        "video",
        {
            "subject_mode": "product",
            "video_image_input_mode": " FIRST_FRAME ",
            "reference_image_url": "https://example.com/product.png",
            "product_reference_image": "https://example.com/product.png",
            "product_detail_images": ["https://example.com/detail.png"],
            "style_reference_image": "https://example.com/style.png",
            "character_reference_image": "https://example.com/character.png",
            "product_lock_mode": "locked",
            "product_video_template": "prompt_driven",
        },
    )
    assert params["video_image_input_mode"] == "first_frame"
    assert params["first_frame_image"] == "https://example.com/product.png"
    for key in (
        "reference_image_url",
        "product_reference_image",
        "product_detail_images",
        "style_reference_image",
        "character_reference_image",
        "product_lock_mode",
        "product_video_template",
    ):
        assert key not in params

    with pytest.raises(HTTPException, match="video_image_input_mode 不支持"):
        validate_generation_params(
            "video",
            {"video_image_input_mode": "automatic"},
        )


def test_subject_reference_mode_requires_the_matching_subject_image():
    model = type(
        "ReferenceVideoModel",
        (),
        {
            "use": "video",
            "model_id": "grok-imagine-video",
            "provider": "grok",
            "gateway_format": "openai",
            "extra": {},
        },
    )()

    assert_generation_capability(
        model,
        category="video",
        source_asset_url="https://example.com/product.png",
        source_type="image",
        params={
            "subject_mode": "product",
            "video_image_input_mode": "subject_reference",
            "product_reference_image": "https://example.com/product.png",
        },
    )
    with pytest.raises(ModelCapabilityError, match="必须提供产品参考图"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/product.png",
            source_type="image",
            params={
                "subject_mode": "product",
                "video_image_input_mode": "subject_reference",
            },
        )


def test_verified_video_profiles_split_reference_from_edit_semantics():
    seedance = type(
        "Seedance20Model",
        (),
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-260128",
            "provider": "volcengine_ark",
            "gateway_format": "ark",
            "extra": {},
        },
    )()
    grok = type(
        "GrokVideoModel",
        (),
        {
            "use": "video",
            "model_id": "grok-imagine-video",
            "provider": "grok",
            "gateway_format": "openai",
            "extra": {},
        },
    )()

    assert_generation_capability(
        seedance,
        category="video",
        source_asset_url="https://example.com/source.mp4",
        source_type="video",
        params={"product_reference_image": "https://example.com/product.png"},
    )
    assert_generation_capability(
        grok,
        category="video",
        source_asset_url="https://example.com/source.mp4",
        source_type="video",
        params={},
    )
    with pytest.raises(ModelCapabilityError, match="不能同时提交独立参考图"):
        assert_generation_capability(
            grok,
            category="video",
            source_asset_url="https://example.com/source.mp4",
            source_type="video",
            params={"style_reference_image": "https://example.com/style.png"},
        )


def test_known_image_profile_enforces_platform_total_reference_limit():
    model = type(
        "GptImage2Model",
        (),
        {
            "use": "image",
            "model_id": "gpt-image-2",
            "provider": "yinyue",
            "gateway_format": "openai",
            "extra": {"capabilities": {"max_reference_images": 16}},
        },
    )()

    assert_generation_capability(
        model,
        category="image",
        source_asset_url="https://example.com/source.png",
        source_type="image",
        params={"style_reference_image": "https://example.com/style.png"},
    )
    with pytest.raises(ModelCapabilityError, match="最多支持 2 张"):
        assert_generation_capability(
            model,
            category="image",
            source_asset_url="https://example.com/source.png",
            source_type="image",
            params={
                "style_reference_image": "https://example.com/style.png",
                "character_reference_image": "https://example.com/character.png",
            },
        )


@pytest.mark.parametrize(
    ("provider", "gateway_format"),
    [
        ("custom_openai", "openai"),
        ("custom_openai", "ark"),
        ("volcengine_ark", "openai"),
    ],
)
def test_verified_profile_requires_the_matching_provider_adapter(
    provider,
    gateway_format,
):
    model = type(
        "UnverifiedSeedance20Model",
        (),
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-pro-unverified",
            "provider": provider,
            "gateway_format": gateway_format,
            "extra": {
                "capabilities": {
                    "text_to_video": True,
                    "image_to_video": True,
                    "reference_image": True,
                    "multi_reference": True,
                    "max_reference_images": 10,
                }
            },
        },
    )()

    with pytest.raises(ModelCapabilityError, match="不支持图生视频"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/first.png",
            source_type="image",
            params={},
        )


def test_verified_profile_accepts_matching_environment_gateway_snapshot():
    model = type(
        "SeedanceEnvSnapshot",
        (),
        {
            "use": "video",
            "model_id": "doubao-seedance-1-5-pro-251215",
            "provider": "env",
            "gateway_format": "ark",
            "gateway_source": "env",
            "extra": {
                "capabilities": {
                    "text_to_video": True,
                    "image_to_video": True,
                    "reference_image": False,
                    "first_last_frame": True,
                    "multi_reference": False,
                    "video_to_video": False,
                }
            },
        },
    )()

    assert_generation_capability(
        model,
        category="video",
        source_asset_url=None,
        source_type=None,
        params={},
    )


@pytest.mark.parametrize("legacy_alias", ["image_mask", "inpainting"])
def test_verified_image_profile_drops_legacy_mask_alias_overrides(legacy_alias):
    model = type(
        "GeminiImageModel",
        (),
        {
            "use": "image",
            "model_id": "gemini-3.1-flash-image",
            "provider": "antigravity",
            "gateway_format": "anthropic",
            "extra": {
                "capabilities": {
                    "image_to_image": True,
                    "reference_image": True,
                    legacy_alias: True,
                }
            },
        },
    )()

    with pytest.raises(ModelCapabilityError, match="不支持蒙版编辑"):
        assert_generation_capability(
            model,
            category="image",
            source_asset_url="https://example.com/source.png",
            source_type="image",
            params={"mask_image_url": "https://example.com/mask.png"},
        )


def test_public_catalog_uses_route_clamped_capabilities():
    model = ModelConfig(
        id=99001,
        use="video",
        model_id="doubao-seedance-2-0-pro-unverified",
        display_name="Wrong Seedance route",
        provider="custom_openai",
        gateway_format="openai",
        is_default=False,
        sort_order=0,
        cost_credits=1,
        unlock_cost=0,
        enabled=True,
        extra={
            "capabilities": {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 10,
            }
        },
    )

    capabilities = public_model_option(model)["capabilities"]

    assert capabilities["text_to_video"] is False
    assert capabilities["image_to_video"] is False
    assert capabilities["reference_image"] is False
    assert capabilities["multi_reference"] is False
    assert "max_reference_images" not in capabilities


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


def test_seedance_15_supports_frame_pair_but_rejects_independent_references():
    model = type(
        "Seedance15Model",
        (),
        {
            "extra": {
                "capabilities": {
                    "text_to_video": True,
                    "image_to_video": True,
                    "reference_image": False,
                    "first_last_frame": True,
                    "multi_reference": False,
                }
            }
        },
    )()

    assert_generation_capability(
        model,
        category="video",
        source_asset_url="https://example.com/first.png",
        source_type="image",
        params={"last_frame_image": "https://example.com/last.png"},
    )

    with pytest.raises(ModelCapabilityError, match="不支持主体或风格参考图"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/product.png",
            source_type="image",
            params={
                "product_reference_image": "https://example.com/product.png",
                "product_detail_images": ["https://example.com/detail-a.png"],
            },
        )


def test_seedance_20_limit_counts_theme_with_eight_detail_images():
    model = type(
        "Seedance20Model",
        (),
        {
            "use": "video",
            "model_id": "doubao-seedance-2-0-260128",
            "provider": "volcengine_ark",
            "gateway_format": "ark",
            "extra": {
                "capabilities": {
                    "image_to_video": True,
                    "multi_reference": True,
                    "max_reference_images": 10,
                }
            }
        },
    )()
    eight_details = [f"https://example.com/detail-{index}.png" for index in range(8)]
    base_params = {
        "product_reference_image": "https://example.com/product.png",
        "product_detail_images": eight_details,
    }

    assert_generation_capability(
        model,
        category="video",
        source_asset_url=None,
        source_type=None,
        params=base_params,
    )
    with pytest.raises(ModelCapabilityError, match="最多支持 9 张"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url=None,
            source_type=None,
            params={
                **base_params,
                "product_detail_images": [
                    *eight_details,
                    "https://example.com/detail-9.png",
                ],
            },
        )


def test_first_last_frame_pair_uses_its_own_explicit_capability():
    model = type(
        "VideoModel",
        (),
        {
            "extra": {
                "capabilities": {
                    "text_to_video": True,
                    "image_to_video": True,
                    "first_last_frame": True,
                    "multi_reference": False,
                    "max_reference_images": 2,
                }
            }
        },
    )()

    assert_generation_capability(
        model,
        category="video",
        source_asset_url="https://example.com/first.png",
        source_type="image",
        params={"last_frame_image": "https://example.com/last.png"},
    )


def test_first_last_frame_pair_does_not_bypass_general_multi_reference_gate():
    model = type(
        "VideoModel",
        (),
        {
            "extra": {
                "capabilities": {
                    "image_to_video": True,
                    "first_last_frame": True,
                    "multi_reference": False,
                    "max_reference_images": 2,
                }
            }
        },
    )()

    with pytest.raises(ModelCapabilityError, match="不支持主体或风格参考图"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/source.png",
            source_type="image",
            params={
                "first_frame_image": "https://example.com/first.png",
                "last_frame_image": "https://example.com/last.png",
                "style_reference_image": "https://example.com/style.png",
            },
        )


def test_first_last_frame_requires_explicit_support_and_a_resolvable_first_frame():
    unsupported = type(
        "VideoModel",
        (),
        {
            "extra": {
                "capabilities": {
                    "image_to_video": True,
                    "first_last_frame": False,
                }
            }
        },
    )()
    with pytest.raises(ModelCapabilityError, match="未明确支持首尾帧"):
        assert_generation_capability(
            unsupported,
            category="video",
            source_asset_url="https://example.com/first.png",
            source_type="image",
            params={"last_frame_image": "https://example.com/last.png"},
        )

    supported = type(
        "VideoModel",
        (),
        {"extra": {"capabilities": {"image_to_video": True, "first_last_frame": True}}},
    )()
    with pytest.raises(ModelCapabilityError, match="尾帧素材必须配合首帧"):
        assert_generation_capability(
            supported,
            category="video",
            source_asset_url=None,
            source_type=None,
            params={"last_frame_image": "https://example.com/last.png"},
        )


def test_same_url_first_last_roles_still_require_explicit_support():
    model = type(
        "VideoModel",
        (),
        {
            "extra": {
                "capabilities": {
                    "image_to_video": True,
                    "first_last_frame": False,
                }
            }
        },
    )()

    with pytest.raises(ModelCapabilityError, match="未明确支持首尾帧"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/loop.png",
            source_type="image",
            params={"last_frame_image": "https://example.com/loop.png"},
        )


@pytest.mark.parametrize(
    ("capabilities", "template", "expected_error"),
    [
        ({}, "prompt_driven", None),
        ({"product_video_templates": ["stable_showcase"]}, "stable_showcase", None),
        (
            {"product_video_templates": ["stable_showcase"]},
            "prompt_driven",
            "不支持当前产品视频策略",
        ),
        ({"product_video_templates": []}, "stable_showcase", "不支持当前产品视频策略"),
        ({}, "unknown-template", "策略不受支持"),
    ],
)
def test_product_video_template_is_capability_driven(
    capabilities,
    template,
    expected_error,
):
    model = type("VideoModel", (), {"extra": {"capabilities": capabilities}})()
    def assert_template_capability():
        assert_generation_capability(
            model,
            category="video",
            source_asset_url="https://example.com/product.png",
            source_type="image",
            params={
                "product_reference_image": "https://example.com/product.png",
                "product_video_template": template,
            },
        )

    if expected_error:
        with pytest.raises(ModelCapabilityError, match=expected_error):
            assert_template_capability()
    else:
        assert_template_capability()


def test_product_video_template_requires_product_reference_image():
    model = type("VideoModel", (), {"extra": {"capabilities": {}}})()

    with pytest.raises(ModelCapabilityError, match="必须配合产品参考图"):
        assert_generation_capability(
            model,
            category="video",
            source_asset_url=None,
            source_type="image",
            params={"product_video_template": "prompt_driven"},
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
