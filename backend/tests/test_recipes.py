import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from time import perf_counter

import pytest

from app.db import SessionLocal
from app.models import (
    AuditLog,
    CreationRecipe,
    CreationRecipeShare,
    CreationRecipeUsageEvent,
    CreationRecipeVersion,
    GenerationQuote,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    ReverseOperation,
)
from app.routers.recipes import (
    _MAX_PUBLIC_COLLECTION_ITEMS,
    _MAX_PUBLIC_PAYLOAD_DEPTH,
    _MAX_PUBLIC_PAYLOAD_NODES,
    _MAX_PUBLIC_STRING_LENGTH,
    _contains_url_uri_or_secret,
    _public_recipe_payload,
)
from app.services.config_store import set_settings


def _payload(prompt: str) -> dict:
    return {
        "schema_version": "creation-recipe.v1",
        "analysis_focus": "product_ad",
        "prompt": prompt,
        "structured": {"主体": "银色香水瓶"},
        "generation_params": {"ratio": "3:4", "quality": "2k"},
    }


def _create_recipe(
    client,
    headers,
    *,
    title="商品主视觉配方",
    visibility="private",
    payload=None,
    source_operation_id=None,
    cover_asset_url="/media/recipe-cover.png",
) -> dict:
    response = client.post(
        "/api/recipes",
        json={
            "title": title,
            "category": "image",
            "visibility": visibility,
            "source_operation_id": source_operation_id,
            "cover_asset_url": cover_asset_url,
            "payload": payload or _payload("银色香水瓶主视觉"),
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _submit_and_approve_recipe(client, recipe_id: int, owner_headers, admin_headers) -> dict:
    submitted = client.post(
        f"/api/recipes/{recipe_id}/submit-review",
        headers=owner_headers,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["moderation_status"] == "pending"
    reviewed = client.post(
        f"/api/admin/recipes/{recipe_id}/review",
        json={"action": "approve", "note": "内容与素材来源检查通过"},
        headers=admin_headers,
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["moderation_status"] == "approved"
    return reviewed.json()


def _sensitive_payload() -> dict:
    payload = _payload("保留这段可执行的公开提示词")
    payload.update({
        "source_operation_id": 7001,
        "reverse_snapshot_v3": {
            "version": 3,
            "target": "image",
            "selected": {
                "id": 801,
                "type": "image",
                "url": "/api/uploads/upload/owner-primary.png",
                "preview_url": "/api/uploads/upload_preview/owner-primary.png",
            },
            "product_asset": {
                "type": "image",
                "url": "/media/upload/owner-product.png",
            },
            "assets": [
                {"type": "image", "url": "/media/upload/owner-primary.png"},
                {"type": "image", "url": "/media/upload/owner-style.png"},
            ],
            "sources": [
                {
                    "asset_id": 901,
                    "asset_url": "/api/uploads/upload/owner-primary.png",
                    "source_type": "image",
                    "role": "primary",
                },
                {
                    "asset_id": 902,
                    "asset_url": "https://private.example.com/style.png?X-Amz-Signature=owner-secret",
                    "source_type": "image",
                    "role": "style",
                },
            ],
            "source_operation_id": 7001,
            "reverse_applied_revision_id": 7002,
            "subject_profile": {
                "description": "保留主体特征描述",
                "portrait_url": "/media/upload/owner-portrait.png",
            },
        },
        "generation": {
            "ratio": "3:4",
            "quality": "2k",
            "model_config_id": 33,
            "generation_model_config_id": 34,
            "model_id": "owner-provider-model",
            "model_selections": {"image": 33, "video": 34, "vision": 35, "prompt": 36},
            "reference_image_url": "/api/uploads/upload/owner-primary.png",
            "style_reference_image": "/media/upload/owner-style.png",
            "reference_images": [
                "/media/upload/owner-primary.png",
                "/media/upload/owner-style.png",
            ],
        },
        "nested": [
            {
                "caption": "保留证据文字",
                "media": {
                    "image_url": "/media/upload/evidence.png",
                    "region": [0.1, 0.2, 0.4, 0.5],
                },
            },
            {
                "documentation": "https://docs.example.com/models/image-v2",
                "download": "https://cdn.example.com/unsigned-private.png",
            },
        ],
        "unknown_fields": {
            "attachment": "https://cdn.example.com/owner-attachment.png",
            "src": "//cdn.example.com/owner-src.png",
            "uri": "s3://owner-bucket/private/object.png",
            "someUrl": "https://cdn.example.com/owner-camel.png",
            "domain_path": "cdn.example.com/owner/no-scheme.png",
            "azureSas": (
                "https://owner.blob.core.windows.net/private/file.png"
                "?sv=2024-11-04&se=2099-01-01T00%3A00%3A00Z&sp=r&sig=owner-secret"
            ),
            "embedded": "download from https://cdn.example.com/owner-inline.png now",
            "relative_html": "<img src='/api/assets/91/stream'>",
            "ordinaryText": "keep this ordinary non URL text",
            "ratioText": "ratio:3:4",
            "urlLabel": "a semantic label, not a locator",
        },
        "source_urls": [
            "https://cdn.example.com/owner-source-a.png",
            "/media/upload/owner-source-b.png",
        ],
        "reference_urls": [
            "blob:https://studio.example.com/owner-blob",
            "file:///Users/owner/private.png",
            "data:image/png;base64,b3duZXItc2VjcmV0",
            "safe reference label",
        ],
        "other_uris": [
            "urn:owner:private:asset",
            "mailto:owner@example.com",
            "ipfs://owner-private-cid",
        ],
        "model_routing": {
            "modelConfigId": 41,
            "targetModelConfigId": 42,
            "optimizerModelConfigId": 43,
            "modelId": "owner-model-id",
            "ownerID": 44,
            "assetIds": [45, 46],
            "sourceOperationId": 47,
            "requestFingerprint": "owner-fingerprint",
        },
        "legacy_credentials": {
            "accessToken": "owner-secret-token",
            "privateKey": "owner-secret-private-key",
            "authorization": "Bearer owner-secret-bearer",
            "note": "keep the non-secret note",
        },
        "opaque_credential": "Bearer owner-secret-opaque",
        "dangerous_keys": {
            "https://cdn.example.com/private/key.png": "ordinary child under URL key",
            "/media/upload/private-key.png": {"label": "ordinary nested child"},
            "https%3A%2F%2Fcdn.example.com%2Fprivate%2Fencoded-key.png": "encoded key child",
            "../private": "dot parent key child",
            "./private": "dot current key child",
            "/private": "single absolute key child",
            "/imagine": "genuine prompt command key",
            "safeSetting": "keep safe key and value",
        },
        "locator_edge_cases": {
            "embedded_equal": "asset=cdn.example.com/private/equal.png",
            "embedded_colon": "asset:cdn.example.com/private/colon.png",
            "embedded_at": "asset@cdn.example.com/private/at.png",
            "bare_protected_path": "media/private/no-leading-slash.png",
            "dot_relative": "./media/private/dot.png",
            "parent_relative": "../../media/private/parent.png",
            "dot_backslash_relative": ".\\media\\private\\backslash.png",
            "windows_drive_path": "C:\\media\\private\\drive.png",
            "windows_user_backslash_path": "C:\\Users\\owner\\private.png",
            "windows_user_forward_path": "C:/Users/owner/private.png",
            "posix_user_path": "/Users/owner/private.png",
            "dot_parent_extensionless": "../private",
            "dot_current_extensionless": "./private",
            "dot_backslash_extensionless": ".\\private",
            "single_posix_extensionless": "/private",
            "unapproved_slash_command": "/describe",
            "ipv4_path": "127.0.0.1/private/ip.png",
            "localhost_path": "localhost:8000/private/local.png",
            "prefixed_ipv4_path": "target:127.0.0.1/private/prefixed-ip.png",
            "prefixed_localhost_path": (
                "target:localhost:8000/private/prefixed-local.png"
            ),
            "encoded_url": "https%3A%2F%2Fcdn.example.com%2Fprivate%2Fencoded.png",
            "double_encoded_url": (
                "https%253A%252F%252Fcdn.example.com%252Fprivate%252Fdouble.png"
            ),
            "deep_encoded_url": (
                "https%252525253A%252525252F%252525252Fcdn.example.com"
                "%252525252Fprivate%252525252Fdeep.png"
            ),
            "safe_percent": "opacity 50% with no locator",
            "safe_camera_instruction": "camera: close-up, slow dolly in",
            "safe_mention_instruction": "@director keep the product centered",
            "safe_slash": "/",
            "safe_ratio_slash": "render at a 16/9 cinematic ratio",
            "safe_model_namespace": "use black-forest-labs/FLUX.1-dev",
            "safe_natural_slashes": "image/video generation; input / output",
            "safe_slash_command": "use /imagine for concept ideation",
            "safe_exact_slash_command": "/imagine",
        },
        "identifier_edge_cases": {
            "ref": "owner-ref-edge",
            "refs": ["owner-ref-a", "owner-ref-b"],
            "fingerprint": "owner-fingerprint-edge",
            "IDs": [71, 72],
            "assetFingerPrint": "owner-asset-fingerprint",
            "refValue": "owner-ref-value",
            "fingerprintValue": "owner-fingerprint-value",
            "referenceStrength": 0.75,
            "refinementMode": "faithful",
        },
        "safe_settings": {
            "ratio": "16:9",
            "steps": 28,
            "model": "gpt-image-1",
            "reference_strength": 0.6,
            "style": "cinematic product photography",
        },
    })
    return payload


def _terminal_reverse_operation(user_id: int) -> int:
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=f"{user_id:064d}"[-64:],
            target="image",
            asset_url="/api/uploads/upload/owner-primary.png",
            status="succeeded",
            progress=100,
            result={"final_text": "保留这段可执行的公开提示词", "structured": {}},
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)
        return int(operation.id)
    finally:
        db.close()


def _replace_recipe_payload(recipe_id: int, payload: dict) -> None:
    db = SessionLocal()
    try:
        recipe = db.get(CreationRecipe, recipe_id)
        version = db.query(CreationRecipeVersion).filter_by(
            recipe_id=recipe_id,
            version=recipe.current_version,
        ).one()
        version.payload = payload
        db.commit()
    finally:
        db.close()


def _assert_public_payload_has_no_leaks(payload: dict) -> None:
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "://",
        "/media/",
        "/api/uploads/",
        "/api/assets/",
        "./media/",
        "data:",
        "blob:",
        "file:",
        "mailto:",
        "urn:",
        "sig=",
        "https%3A",
        "https%253A",
        "cdn.example.com/private",
        "media/private/",
        "127.0.0.1/private",
        "localhost:8000/private",
        "owner-primary",
        "owner-style",
        "owner-secret",
        "owner-ref",
        "owner-fingerprint",
        "Bearer ",
        "ordinary child under URL key",
        "ordinary nested child",
        "encoded key child",
        "dot parent key child",
        "dot current key child",
        "single absolute key child",
        "../private",
        "./private",
        '"/private"',
        '"/describe"',
    ):
        assert forbidden not in serialized


def test_recipe_version_list_activate_metadata_and_monotonic_versions(
    client,
    make_user,
    auth,
):
    make_user("13710000992", balance=100)
    make_user("13710000993", balance=100)
    owner_headers = auth("13710000992")
    other_headers = auth("13710000993")
    recipe = _create_recipe(client, owner_headers)

    second = client.post(
        f"/api/recipes/{recipe['id']}/versions",
        json={"payload": _payload("修改后的生成稿")},
        headers=owner_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2

    versions = client.get(
        f"/api/recipes/{recipe['id']}/versions",
        headers=owner_headers,
    )
    assert versions.status_code == 200, versions.text
    assert [item["version"] for item in versions.json()] == [2, 1]
    assert client.get(
        f"/api/recipes/{recipe['id']}/versions",
        headers=other_headers,
    ).status_code == 404

    activated = client.post(
        f"/api/recipes/{recipe['id']}/versions/1/activate",
        headers=owner_headers,
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["current_version"] == 1
    assert activated.json()["version"]["payload"]["prompt"] == "银色香水瓶主视觉"

    # Creating after a rollback must append to the ledger instead of colliding with v2.
    third = client.post(
        f"/api/recipes/{recipe['id']}/versions",
        json={"payload": _payload("回滚后的新版本")},
        headers=owner_headers,
    )
    assert third.status_code == 200, third.text
    assert third.json()["version"] == 3

    patched = client.patch(
        f"/api/recipes/{recipe['id']}",
        json={
            "title": "精修商品主视觉",
            "visibility": "public",
            "cover_asset_url": "/api/uploads/recipe-cover.png",
        },
        headers=owner_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "精修商品主视觉"
    assert patched.json()["visibility"] == "public"
    assert patched.json()["moderation_status"] == "draft"
    assert patched.json()["cover_asset_url"] == "/api/uploads/recipe-cover.png"
    assert client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"title": None},
        headers=owner_headers,
    ).status_code == 422
    assert client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"visibility": None},
        headers=owner_headers,
    ).status_code == 422
    assert client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"title": "越权修改"},
        headers=other_headers,
    ).status_code == 404
    assert client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"cover_asset_url": "javascript:alert(1)"},
        headers=owner_headers,
    ).status_code == 422


def test_recipe_versions_freeze_trusted_model_catalog_versions(
    client,
    make_user,
    auth,
):
    make_user("13710000952", balance=100)
    headers = auth("13710000952")
    db = SessionLocal()
    try:
        image_model = db.query(ModelConfig).filter_by(use="image", enabled=True).first()
        prompt_model = db.query(ModelConfig).filter_by(use="prompt", enabled=True).first()
        assert image_model is not None
        assert prompt_model is not None
        image_capability = db.query(ModelCapabilityVersion).filter_by(
            model_config_id=image_model.id,
            is_active=True,
        ).one()
        image_price = db.query(ModelPriceVersion).filter_by(
            model_config_id=image_model.id,
            is_active=True,
        ).one()
        prompt_capability = db.query(ModelCapabilityVersion).filter_by(
            model_config_id=prompt_model.id,
            is_active=True,
        ).one()
        prompt_price = db.query(ModelPriceVersion).filter_by(
            model_config_id=prompt_model.id,
            is_active=True,
        ).one()
        model_ids = {"image": int(image_model.id), "prompt": int(prompt_model.id)}
        expected = {
            "image": {
                "model_config_id": int(image_model.id),
                "use": "image",
                "model_id": image_model.model_id,
                "capability_version_id": int(image_capability.id),
                "capability_version": int(image_capability.version),
                "capability_schema_version": image_capability.schema_version,
                "price_version_id": int(image_price.id),
                "price_version": int(image_price.version),
                "price_schema_version": image_price.schema_version,
            },
            "prompt": {
                "model_config_id": int(prompt_model.id),
                "use": "prompt",
                "model_id": prompt_model.model_id,
                "capability_version_id": int(prompt_capability.id),
                "capability_version": int(prompt_capability.version),
                "capability_schema_version": prompt_capability.schema_version,
                "price_version_id": int(prompt_price.id),
                "price_version": int(prompt_price.version),
                "price_schema_version": prompt_price.schema_version,
            },
        }
    finally:
        db.close()

    payload = _payload("冻结模型目录版本")
    payload["generation"] = {
        "model_selections": model_ids,
        "seed": 778899,
        "catalog_versions": {
            "image": {
                "model_config_id": model_ids["image"],
                "capability_version_id": 999999,
                "price_version_id": 999999,
            },
        },
    }
    created = _create_recipe(client, headers, payload=payload)
    assert created["version"]["payload"]["generation"]["catalog_versions"] == expected
    assert created["version"]["payload"]["generation"]["seed"] == 778899

    next_payload = deepcopy(created["version"]["payload"])
    created_version = client.post(
        f"/api/recipes/{created['id']}/versions",
        json={"payload": next_payload},
        headers=headers,
    )
    assert created_version.status_code == 200, created_version.text
    assert created_version.json()["payload"]["generation"]["catalog_versions"] == expected


def test_public_discovery_detail_and_clone_permissions(client, make_user, auth):
    make_user("13710000994", balance=100)
    make_user("13710000995", balance=100)
    make_user("13710000990", balance=100, admin=True)
    owner_headers = auth("13710000994")
    viewer_headers = auth("13710000995")
    admin_headers = auth("13710000990")
    public_recipe = _create_recipe(
        client,
        owner_headers,
        title="可公开派生的商品配方",
        visibility="public",
    )
    private_recipe = _create_recipe(
        client,
        owner_headers,
        title="私有配方",
        visibility="private",
    )
    second = client.post(
        f"/api/recipes/{public_recipe['id']}/versions",
        json={"payload": _payload("公开配方当前版本")},
        headers=owner_headers,
    )
    assert second.status_code == 200, second.text
    approved = _submit_and_approve_recipe(
        client,
        public_recipe["id"],
        owner_headers,
        admin_headers,
    )
    assert approved["approved_version"] == 2

    # Public discovery is readable without authentication and only returns public rows.
    client.cookies.clear()
    discovered = client.get("/api/recipes/public?q=可公开&category=image")
    assert discovered.status_code == 200, discovered.text
    assert [item["id"] for item in discovered.json()] == [public_recipe["id"]]
    detail = client.get(f"/api/recipes/public/{public_recipe['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["source_operation_id"] is None
    assert detail.json()["favorite"] is False
    assert detail.json()["current_version"] == 2
    assert detail.json()["version"]["payload"]["prompt"] == "公开配方当前版本"
    assert client.get(f"/api/recipes/public/{private_recipe['id']}").status_code == 404

    cloned = client.post(
        f"/api/recipes/{public_recipe['id']}/clone",
        json={"title": "我的派生配方"},
        headers=viewer_headers,
    )
    assert cloned.status_code == 200, cloned.text
    clone = cloned.json()
    assert clone["title"] == "我的派生配方"
    assert clone["visibility"] == "private"
    assert clone["source_operation_id"] is None
    assert clone["version"]["payload"]["derived_from_recipe_id"] == public_recipe["id"]
    assert clone["version"]["payload"]["derived_from_recipe_version"] == 2
    assert clone["version"]["payload"]["prompt"] == "公开配方当前版本"

    # A public consumer cannot ask for a non-current historical version.
    assert client.post(
        f"/api/recipes/{public_recipe['id']}/clone",
        json={"version": 1},
        headers=viewer_headers,
    ).status_code == 404
    assert client.post(
        f"/api/recipes/{private_recipe['id']}/clone",
        json={},
        headers=viewer_headers,
    ).status_code == 404

    # The owner may derive any of their own historical versions.
    owner_clone = client.post(
        f"/api/recipes/{public_recipe['id']}/clone",
        json={"version": 1},
        headers=owner_headers,
    )
    assert owner_clone.status_code == 200, owner_clone.text
    assert owner_clone.json()["version"]["payload"]["derived_from_recipe_version"] == 1

    assert client.delete(
        f"/api/recipes/{private_recipe['id']}",
        headers=owner_headers,
    ).status_code == 200
    db = SessionLocal()
    try:
        deleted = db.get(CreationRecipe, private_recipe["id"])
        assert deleted is not None
        assert deleted.deleted_at is not None
        assert db.query(CreationRecipeVersion).filter_by(
            recipe_id=private_recipe["id"],
        ).count() == 1
    finally:
        db.close()


def test_recipe_metadata_changes_are_versioned_and_activation_restores_full_snapshot(
    client,
    make_user,
    auth,
):
    make_user("13710000891", balance=100)
    make_user("13710000892", balance=100, admin=True)
    owner_headers = auth("13710000891")
    admin_headers = auth("13710000892")
    recipe = _create_recipe(
        client,
        owner_headers,
        title="原始私有配方",
        visibility="private",
        cover_asset_url="/media/original-recipe-cover.png",
    )

    changed = client.patch(
        f"/api/recipes/{recipe['id']}",
        json={
            "title": "公开候选配方",
            "visibility": "public",
            "cover_asset_url": "/media/public-recipe-cover.png",
        },
        headers=owner_headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["current_version"] == 2

    versions = client.get(
        f"/api/recipes/{recipe['id']}/versions",
        headers=owner_headers,
    )
    assert versions.status_code == 200, versions.text
    history = versions.json()
    assert [item["version"] for item in history] == [2, 1]
    assert history[0]["payload"] == history[1]["payload"]
    assert history[0]["metadata_snapshot"] == {
        "schema_version": "creation-recipe-metadata.v1",
        "origin": "recorded",
        "title": "公开候选配方",
        "visibility": "public",
        "cover_asset_url": "/media/public-recipe-cover.png",
    }
    assert history[1]["metadata_snapshot"]["title"] == "原始私有配方"
    assert history[1]["metadata_snapshot"]["visibility"] == "private"

    approved = _submit_and_approve_recipe(
        client,
        recipe["id"],
        owner_headers,
        admin_headers,
    )
    assert approved["approved_version"] == 2
    client.cookies.clear()
    public_detail = client.get(f"/api/recipes/public/{recipe['id']}")
    assert public_detail.status_code == 200, public_detail.text
    assert public_detail.json()["version"]["metadata_snapshot"] == {}

    restored = client.post(
        f"/api/recipes/{recipe['id']}/versions/1/activate",
        headers=owner_headers,
    )
    assert restored.status_code == 200, restored.text
    restored_payload = restored.json()
    assert restored_payload["current_version"] == 1
    assert restored_payload["title"] == "原始私有配方"
    assert restored_payload["visibility"] == "private"
    assert restored_payload["cover_asset_url"] == "/media/original-recipe-cover.png"
    assert restored_payload["moderation_status"] == "draft"
    assert restored_payload["approved_version"] is None

    replayed = client.post(
        f"/api/recipes/{recipe['id']}/versions/2/activate",
        headers=owner_headers,
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["title"] == "公开候选配方"
    assert replayed.json()["visibility"] == "public"
    assert replayed.json()["cover_asset_url"] == "/media/public-recipe-cover.png"


def test_required_recipe_audit_failure_rolls_back_business_mutation(
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13710000893", balance=100)
    headers = auth("13710000893")
    recipe = _create_recipe(client, headers, title="审计回滚原始标题")

    def fail_required_audit(*_args, **_kwargs):
        raise RuntimeError("required audit unavailable")

    monkeypatch.setattr("app.routers.recipes.audit.log_required", fail_required_audit)
    with pytest.raises(RuntimeError, match="required audit unavailable"):
        client.patch(
            f"/api/recipes/{recipe['id']}",
            json={"title": "不得落库的标题"},
            headers=headers,
        )

    db = SessionLocal()
    try:
        row = db.get(CreationRecipe, recipe["id"])
        assert row is not None
        assert row.title == "审计回滚原始标题"
        assert row.current_version == 1
        assert db.query(CreationRecipeVersion).filter_by(recipe_id=row.id).count() == 1
    finally:
        db.close()


def test_recipe_mutations_are_audited_and_soft_delete_preserves_governance_history(
    client,
    make_user,
    auth,
):
    make_user("13710000894", balance=100)
    make_user("13710000895", balance=100)
    make_user("13710000896", balance=100, admin=True)
    owner_headers = auth("13710000894")
    viewer_headers = auth("13710000895")
    admin_headers = auth("13710000896")
    audit_secret = "RECIPE_AUDIT_PAYLOAD_MUST_NOT_LEAK"
    recipe = _create_recipe(
        client,
        owner_headers,
        title="审计脱敏配方",
        visibility="private",
        payload=_payload(audit_secret),
        cover_asset_url="/media/audit-secret-cover.png",
    )
    made_public = client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"visibility": "public"},
        headers=owner_headers,
    )
    assert made_public.status_code == 200, made_public.text
    _submit_and_approve_recipe(
        client,
        recipe["id"],
        owner_headers,
        admin_headers,
    )
    shared = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    )
    assert shared.status_code == 200, shared.text
    share = shared.json()
    applied = client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "apply",
            "share_slug": share["slug"],
            "client_event_id": "soft-delete-retained-usage",
        },
        headers=viewer_headers,
    )
    assert applied.status_code == 200, applied.text
    revoked = client.delete(
        f"/api/recipes/{recipe['id']}/shares/{share['id']}",
        headers=owner_headers,
    )
    assert revoked.status_code == 200, revoked.text

    version = client.post(
        f"/api/recipes/{recipe['id']}/versions",
        json={"payload": _payload("审计脱敏配方新版本")},
        headers=owner_headers,
    )
    assert version.status_code == 200, version.text
    activated = client.post(
        f"/api/recipes/{recipe['id']}/versions/2/activate",
        headers=owner_headers,
    )
    assert activated.status_code == 200, activated.text
    _submit_and_approve_recipe(
        client,
        recipe["id"],
        owner_headers,
        admin_headers,
    )
    replacement_share = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    )
    assert replacement_share.status_code == 200, replacement_share.text
    replacement = replacement_share.json()
    cloned = client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={"version": 2, "title": "审计脱敏派生配方"},
        headers=owner_headers,
    )
    assert cloned.status_code == 200, cloned.text
    favorited = client.post(
        f"/api/recipes/{recipe['id']}/favorite",
        headers=owner_headers,
    )
    assert favorited.status_code == 200, favorited.text
    deleted = client.delete(
        f"/api/recipes/{recipe['id']}",
        headers=owner_headers,
    )
    assert deleted.status_code == 200, deleted.text

    assert client.get(f"/api/recipes/{recipe['id']}", headers=owner_headers).status_code == 404
    assert client.get(f"/api/recipes/public/{recipe['id']}").status_code == 404
    assert client.get(f"/api/recipes/shared/{replacement['slug']}").status_code == 404
    assert client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={"share_slug": replacement["slug"]},
        headers=viewer_headers,
    ).status_code == 404
    assert client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    ).status_code == 404

    db = SessionLocal()
    try:
        row = db.get(CreationRecipe, recipe["id"])
        assert row is not None and row.deleted_at is not None
        assert db.query(CreationRecipeVersion).filter_by(recipe_id=row.id).count() == 3
        shares = db.query(CreationRecipeShare).filter_by(recipe_id=row.id).all()
        assert len(shares) == 2
        assert all(share_row.status == "revoked" for share_row in shares)
        assert db.query(CreationRecipeUsageEvent).filter_by(recipe_id=row.id).count() == 2
        logs = db.query(AuditLog).filter_by(biz_type="creation_recipe").all()
        actions = {log.action for log in logs}
        assert {
            "create_creation_recipe",
            "update_creation_recipe_metadata",
            "submit_creation_recipe_review",
            "approve_creation_recipe",
            "create_creation_recipe_share",
            "revoke_creation_recipe_share",
            "create_creation_recipe_version",
            "activate_creation_recipe_version",
            "clone_creation_recipe",
            "toggle_creation_recipe_favorite",
            "delete_creation_recipe",
        } <= actions
        serialized_details = json.dumps(
            [log.detail for log in logs],
            ensure_ascii=False,
            sort_keys=True,
        )
        assert audit_secret not in serialized_details
        deletion_log = next(
            log
            for log in logs
            if log.action == "delete_creation_recipe" and log.biz_id == row.id
        )
        assert deletion_log.detail["version_count"] == 3
        assert deletion_log.detail["share_count"] == 2
        assert deletion_log.detail["usage_count"] == 2
        assert deletion_log.detail["revoked_share_count"] == 1
    finally:
        db.close()


def test_public_recipe_redacts_nested_private_assets_and_clone_cannot_inherit_them(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000998", balance=100)
    make_user("13710000999", balance=100)
    make_user("13710000989", balance=100, admin=True)
    owner_headers = auth("13710000998")
    viewer_headers = auth("13710000999")
    admin_headers = auth("13710000989")
    operation_id = _terminal_reverse_operation(owner_id)
    sensitive_payload = _sensitive_payload()
    recipe = _create_recipe(
        client,
        owner_headers,
        title="需要素材脱敏的公开配方",
        visibility="public",
        payload=_payload("保留这段可执行的公开提示词"),
        source_operation_id=operation_id,
        cover_asset_url="https://cdn.example.com/unsigned-owner-cover.png",
    )
    _submit_and_approve_recipe(client, recipe["id"], owner_headers, admin_headers)
    # Simulate a legacy payload written before data URI and credential validation existed.
    _replace_recipe_payload(recipe["id"], sensitive_payload)

    # Owner endpoints remain lossless even when the recipe is public.
    owner_detail = client.get(f"/api/recipes/{recipe['id']}", headers=owner_headers)
    assert owner_detail.status_code == 200, owner_detail.text
    owner_json = owner_detail.json()
    assert owner_json["source_operation_id"] == operation_id
    assert owner_json["cover_asset_url"] == "https://cdn.example.com/unsigned-owner-cover.png"
    assert owner_json["version"]["payload"] == sensitive_payload
    owner_list = client.get("/api/recipes", headers=owner_headers)
    assert owner_list.status_code == 200, owner_list.text
    owner_list_item = next(item for item in owner_list.json() if item["id"] == recipe["id"])
    assert owner_list_item["version"]["payload"]["reverse_snapshot_v3"]["sources"][1][
        "role"
    ] == "style"

    client.cookies.clear()
    public_list = client.get("/api/recipes/public?q=素材脱敏")
    public_detail = client.get(f"/api/recipes/public/{recipe['id']}")
    assert public_list.status_code == 200, public_list.text
    assert public_detail.status_code == 200, public_detail.text
    assert len(public_list.json()) == 1

    for public_json in (public_list.json()[0], public_detail.json()):
        assert public_json["source_operation_id"] is None
        assert public_json["cover_asset_url"] is None
        payload = public_json["version"]["payload"]
        assert payload["prompt"] == "保留这段可执行的公开提示词"
        assert payload["structured"] == {"主体": "银色香水瓶"}
        assert payload["generation_params"] == {"ratio": "3:4", "quality": "2k"}
        assert payload["generation"]["ratio"] == "3:4"
        assert payload["generation"]["model_config_id"] == 33
        assert "generation_model_config_id" not in payload["generation"]
        assert "model_id" not in payload["generation"]
        assert payload["generation"]["model_selections"] == {
            "image": 33,
            "video": 34,
            "vision": 35,
            "prompt": 36,
        }
        assert payload["generation"]["reference_images"] == []
        assert payload["reverse_snapshot_v3"]["selected"] == {"type": "image"}
        assert payload["reverse_snapshot_v3"]["product_asset"] == {"type": "image"}
        assert payload["reverse_snapshot_v3"]["assets"] == [
            {"type": "image"},
            {"type": "image"},
        ]
        assert payload["reverse_snapshot_v3"]["sources"] == [
            {"source_type": "image", "role": "primary"},
            {"source_type": "image", "role": "style"},
        ]
        assert "source_operation_id" not in payload
        assert "source_operation_id" not in payload["reverse_snapshot_v3"]
        assert "reverse_applied_revision_id" not in payload["reverse_snapshot_v3"]
        assert payload["reverse_snapshot_v3"]["subject_profile"]["description"] == "保留主体特征描述"
        assert "portrait_url" not in payload["reverse_snapshot_v3"]["subject_profile"]
        assert payload["nested"][0]["caption"] == "保留证据文字"
        assert payload["nested"][0]["media"]["region"] == [0.1, 0.2, 0.4, 0.5]
        assert "image_url" not in payload["nested"][0]["media"]
        assert "documentation" not in payload["nested"][1]
        assert "download" not in payload["nested"][1]
        assert payload["unknown_fields"] == {
            "ordinaryText": "keep this ordinary non URL text",
            "ratioText": "ratio:3:4",
            "urlLabel": "a semantic label, not a locator",
        }
        assert payload["source_urls"] == []
        assert payload["reference_urls"] == ["safe reference label"]
        assert payload["other_uris"] == []
        assert payload["model_routing"] == {
            "modelConfigId": 41,
            "targetModelConfigId": 42,
        }
        assert payload["legacy_credentials"] == {"note": "keep the non-secret note"}
        assert "opaque_credential" not in payload
        assert payload["dangerous_keys"] == {
            "/imagine": "genuine prompt command key",
            "safeSetting": "keep safe key and value",
        }
        assert payload["locator_edge_cases"] == {
            "safe_percent": "opacity 50% with no locator",
            "safe_camera_instruction": "camera: close-up, slow dolly in",
            "safe_mention_instruction": "@director keep the product centered",
            "safe_slash": "/",
            "safe_ratio_slash": "render at a 16/9 cinematic ratio",
            "safe_model_namespace": "use black-forest-labs/FLUX.1-dev",
            "safe_natural_slashes": "image/video generation; input / output",
            "safe_slash_command": "use /imagine for concept ideation",
            "safe_exact_slash_command": "/imagine",
        }
        assert payload["identifier_edge_cases"] == {
            "referenceStrength": 0.75,
            "refinementMode": "faithful",
        }
        assert payload["safe_settings"] == {
            "ratio": "16:9",
            "steps": 28,
            "model": "gpt-image-1",
            "reference_strength": 0.6,
            "style": "cinematic product photography",
        }
        access = payload["public_asset_access"]
        assert access["status"] == "unavailable"
        assert access["reason"] == "private_source_assets_not_shared"
        assert access["removed_count"] >= 2
        _assert_public_payload_has_no_leaks(payload)

    owner_after_public_read = client.get(
        f"/api/recipes/{recipe['id']}",
        headers=owner_headers,
    )
    assert owner_after_public_read.status_code == 200, owner_after_public_read.text
    assert owner_after_public_read.json()["version"]["payload"] == sensitive_payload

    cloned = client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={"title": "已脱敏的公开派生配方"},
        headers=viewer_headers,
    )
    assert cloned.status_code == 200, cloned.text
    clone_json = cloned.json()
    clone_payload = clone_json["version"]["payload"]
    assert clone_json["cover_asset_url"] is None
    assert clone_payload["derived_from_recipe_id"] == recipe["id"]
    assert clone_payload["derived_from_recipe_version"] == recipe["current_version"]
    assert clone_payload["public_asset_access"]["status"] == "unavailable"
    assert clone_payload["reverse_snapshot_v3"]["sources"] == [
        {"source_type": "image", "role": "primary"},
        {"source_type": "image", "role": "style"},
    ]
    assert clone_payload["generation"]["reference_images"] == []
    _assert_public_payload_has_no_leaks(clone_payload)
    db = SessionLocal()
    try:
        stored_clone = db.query(CreationRecipeVersion).filter_by(
            recipe_id=clone_json["id"],
            version=1,
        ).one()
        assert stored_clone.payload == clone_payload
        _assert_public_payload_has_no_leaks(stored_clone.payload)
    finally:
        db.close()

    # The source owner may still clone their own historical data with all references intact.
    owner_clone = client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={"title": "保留素材的 owner 副本"},
        headers=owner_headers,
    )
    assert owner_clone.status_code == 200, owner_clone.text
    owner_clone_json = owner_clone.json()
    assert owner_clone_json["cover_asset_url"] == "https://cdn.example.com/unsigned-owner-cover.png"
    assert owner_clone_json["version"]["payload"]["reverse_snapshot_v3"]["sources"][0][
        "asset_url"
    ] == "/api/uploads/upload/owner-primary.png"
    assert owner_clone_json["version"]["payload"]["reference_urls"][2].startswith("data:")
    assert owner_clone_json["version"]["payload"]["legacy_credentials"]["accessToken"] == (
        "owner-secret-token"
    )
    assert "https://cdn.example.com/private/key.png" in owner_clone_json["version"]["payload"][
        "dangerous_keys"
    ]
    assert owner_clone_json["version"]["payload"]["identifier_edge_cases"]["ref"] == (
        "owner-ref-edge"
    )
    assert owner_clone_json["version"]["payload"]["locator_edge_cases"][
        "double_encoded_url"
    ].startswith("https%253A")
    assert owner_clone_json["version"]["payload"]["locator_edge_cases"][
        "windows_user_backslash_path"
    ] == "C:\\Users\\owner\\private.png"
    assert owner_clone_json["version"]["payload"]["locator_edge_cases"][
        "posix_user_path"
    ] == "/Users/owner/private.png"
    assert owner_clone_json["version"]["payload"]["locator_edge_cases"][
        "dot_parent_extensionless"
    ] == "../private"
    assert owner_clone_json["version"]["payload"]["locator_edge_cases"][
        "single_posix_extensionless"
    ] == "/private"
    assert "/private" in owner_clone_json["version"]["payload"]["dangerous_keys"]


def test_recipe_metadata_publish_and_clone_enforce_current_content_safety(
    client,
    make_user,
    auth,
):
    make_user("13710000996", balance=100)
    make_user("13710000997", balance=100)
    owner_headers = auth("13710000996")
    viewer_headers = auth("13710000997")
    recipe = _create_recipe(client, owner_headers, title="safe-title", visibility="private")
    unsafe_version = client.post(
        f"/api/recipes/{recipe['id']}/versions",
        json={"payload": _payload("forbidden-recipe-content")},
        headers=owner_headers,
    )
    assert unsafe_version.status_code == 200, unsafe_version.text

    db = SessionLocal()
    try:
        set_settings(db, {
            "content_safety_enabled": True,
            "content_safety_banned_terms": "forbidden-recipe-content",
        })
    finally:
        db.close()
    try:
        blocked_publish = client.patch(
            f"/api/recipes/{recipe['id']}",
            json={"visibility": "public"},
            headers=owner_headers,
        )
        assert blocked_publish.status_code == 400, blocked_publish.text
        blocked_share = client.post(
            f"/api/recipes/{recipe['id']}/shares",
            json={},
            headers=owner_headers,
        )
        assert blocked_share.status_code == 409, blocked_share.text

        # Simulate an already-public row from before the moderation rule changed.
        db = SessionLocal()
        try:
            row = db.get(CreationRecipe, recipe["id"])
            row.visibility = "public"
            row.moderation_status = "approved"
            row.approved_version = row.current_version
            db.commit()
        finally:
            db.close()
        blocked_clone = client.post(
            f"/api/recipes/{recipe['id']}/clone",
            json={},
            headers=viewer_headers,
        )
        assert blocked_clone.status_code == 400, blocked_clone.text
    finally:
        db = SessionLocal()
        try:
            set_settings(db, {
                "content_safety_enabled": False,
                "content_safety_banned_terms": "",
            })
        finally:
            db.close()


def test_recipe_payload_v1_rejects_ambiguous_or_mismatched_envelopes(
    client,
    make_user,
    auth,
):
    make_user("13710000980", balance=100)
    headers = auth("13710000980")
    base = {
        "title": "结构校验配方",
        "category": "image",
        "payload": _payload("结构正确的提示词"),
    }
    invalid_payloads = [
        {"prompt": "缺少 schema_version"},
        {"schema_version": "creation-recipe.v2", "prompt": "未知版本"},
        {"schema_version": "creation-recipe.v1", "unexpected": "任意 JSON"},
        {"schema_version": "creation-recipe.v1", "prompt": ""},
        {
            "schema_version": "creation-recipe.v1",
            "prompt": "旧快照",
            "reverse_snapshot_v3": {"version": 2, "target": "image"},
        },
        {
            "schema_version": "creation-recipe.v1",
            "prompt": "派生字段不完整",
            "derived_from_recipe_id": 8,
        },
    ]
    for payload in invalid_payloads:
        response = client.post(
            "/api/recipes",
            json={**base, "payload": payload},
            headers=headers,
        )
        assert response.status_code == 422, response.text

    target_mismatch = client.post(
        "/api/recipes",
        json={
            **base,
            "payload": {
                "schema_version": "creation-recipe.v1",
                "prompt": "视频工作区不能保存为图片配方",
                "reverse_snapshot_v3": {"version": 3, "target": "video"},
            },
        },
        headers=headers,
    )
    assert target_mismatch.status_code == 422, target_mismatch.text

    recipe = _create_recipe(client, headers)
    mismatched_version = client.post(
        f"/api/recipes/{recipe['id']}/versions",
        json={
            "payload": {
                "schema_version": "creation-recipe.v1",
                "prompt": "视频版本",
                "reverse_snapshot_v3": {"version": 3, "target": "video"},
            },
        },
        headers=headers,
    )
    assert mismatched_version.status_code == 422, mismatched_version.text


def test_recipe_moderation_queue_gates_public_discovery_and_invalidates_on_version(
    client,
    make_user,
    auth,
):
    make_user("13710000981", balance=100)
    make_user("13710000982", balance=100)
    make_user("13710000983", balance=100, admin=True)
    owner_headers = auth("13710000981")
    viewer_headers = auth("13710000982")
    admin_headers = auth("13710000983")
    recipe = _create_recipe(
        client,
        owner_headers,
        title="需要正式审核的配方",
        visibility="public",
    )
    assert recipe["moderation_status"] == "draft"
    assert client.get("/api/recipes/public?q=正式审核").json() == []
    assert client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={},
        headers=viewer_headers,
    ).status_code == 404

    submitted = client.post(
        f"/api/recipes/{recipe['id']}/submit-review",
        headers=owner_headers,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["moderation_status"] == "pending"
    assert client.get(
        "/api/admin/recipes/reviews",
        headers=viewer_headers,
    ).status_code == 403
    queue = client.get("/api/admin/recipes/reviews", headers=admin_headers)
    assert queue.status_code == 200, queue.text
    assert recipe["id"] in [item["id"] for item in queue.json()]
    assert client.post(
        f"/api/admin/recipes/{recipe['id']}/review",
        json={"action": "reject"},
        headers=admin_headers,
    ).status_code == 422
    rejected = client.post(
        f"/api/admin/recipes/{recipe['id']}/review",
        json={"action": "reject", "note": "封面与配方内容不一致"},
        headers=admin_headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["moderation_status"] == "rejected"
    assert client.get(f"/api/recipes/public/{recipe['id']}").status_code == 404

    approved = _submit_and_approve_recipe(
        client,
        recipe["id"],
        owner_headers,
        admin_headers,
    )
    assert approved["approved_version"] == recipe["current_version"]
    db = SessionLocal()
    try:
        review_actions = {
            row.action
            for row in db.query(AuditLog).filter_by(
                biz_type="creation_recipe",
                biz_id=recipe["id"],
            )
        }
        assert {
            "reject_creation_recipe",
            "approve_creation_recipe",
        } <= review_actions
    finally:
        db.close()
    assert client.get(f"/api/recipes/public/{recipe['id']}").status_code == 200
    assert client.post(
        f"/api/admin/recipes/{recipe['id']}/review",
        json={"action": "approve"},
        headers=admin_headers,
    ).status_code == 409

    next_version = client.post(
        f"/api/recipes/{recipe['id']}/versions",
        json={"payload": _payload("审核后新增的版本")},
        headers=owner_headers,
    )
    assert next_version.status_code == 200, next_version.text
    owner_detail = client.get(f"/api/recipes/{recipe['id']}", headers=owner_headers)
    assert owner_detail.json()["moderation_status"] == "draft"
    assert owner_detail.json()["approved_version"] is None
    assert client.get(f"/api/recipes/public/{recipe['id']}").status_code == 404


def test_recipe_shares_require_current_approval_and_fail_closed_after_rejection(
    client,
    make_user,
    auth,
):
    make_user("13710000960", balance=100)
    make_user("13710000961", balance=100)
    make_user("13710000962", balance=100, admin=True)
    owner_headers = auth("13710000960")
    viewer_headers = auth("13710000961")
    admin_headers = auth("13710000962")

    recipe = _create_recipe(client, owner_headers, visibility="private")
    assert client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    ).status_code == 409
    candidate = client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"visibility": "public"},
        headers=owner_headers,
    )
    assert candidate.status_code == 200, candidate.text
    assert candidate.json()["moderation_status"] == "draft"
    assert client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    ).status_code == 409

    _submit_and_approve_recipe(
        client,
        recipe["id"],
        owner_headers,
        admin_headers,
    )
    created = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    )
    assert created.status_code == 200, created.text
    share = created.json()
    assert client.get(f"/api/recipes/shared/{share['slug']}").status_code == 200

    changed = client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"title": "修改后需要重新审核的配方"},
        headers=owner_headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["moderation_status"] == "draft"
    assert client.get(f"/api/recipes/shared/{share['slug']}").status_code == 404
    listed = client.get(
        f"/api/recipes/{recipe['id']}/shares",
        headers=owner_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["status"] == "revoked"
    assert listed.json()[0]["revoked_at"] is not None

    submitted = client.post(
        f"/api/recipes/{recipe['id']}/submit-review",
        headers=owner_headers,
    )
    assert submitted.status_code == 200, submitted.text
    rejected = client.post(
        f"/api/admin/recipes/{recipe['id']}/review",
        json={"action": "reject", "note": "公开内容不符合要求"},
        headers=admin_headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["moderation_status"] == "rejected"
    assert client.get(f"/api/recipes/shared/{share['slug']}").status_code == 404
    assert client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={"share_slug": share["slug"]},
        headers=viewer_headers,
    ).status_code == 404
    assert client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    ).status_code == 409


def test_backfilled_public_recipe_remains_shareable_without_leaking_private_assets(
    client,
    make_user,
    auth,
):
    make_user("13710000963", balance=100)
    owner_headers = auth("13710000963")
    payload = _payload("历史公开配方仍可复用")
    payload["reverse_snapshot_v3"] = {
        "version": 3,
        "target": "image",
        "selected": {
            "type": "image",
            "url": "/api/uploads/private-legacy-source.png",
        },
        "final_text": "历史公开配方仍可复用",
    }
    recipe = _create_recipe(
        client,
        owner_headers,
        title="/api/uploads/private-legacy-title.png",
        visibility="public",
        payload=payload,
    )
    db = SessionLocal()
    try:
        row = db.get(CreationRecipe, recipe["id"])
        # Migration 0054 backfills legacy public rows to this exact state.
        row.moderation_status = "approved"
        row.approved_version = row.current_version
        row.submitted_at = row.updated_at
        row.reviewed_at = row.updated_at
        db.commit()
    finally:
        db.close()

    created = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    )
    assert created.status_code == 200, created.text
    share = created.json()
    resolved = client.get(f"/api/recipes/shared/{share['slug']}")
    assert resolved.status_code == 200, resolved.text
    public_recipe = resolved.json()["recipe"]
    assert public_recipe["title"] == "公开创作配方"
    assert public_recipe["cover_asset_url"] is None
    assert public_recipe["version"]["payload"]["reverse_snapshot_v3"]["selected"] == {
        "type": "image"
    }
    assert "private-legacy" not in json.dumps(public_recipe, ensure_ascii=False)

    next_version = client.post(
        f"/api/recipes/{recipe['id']}/versions",
        json={"payload": _payload("历史配方的新草稿")},
        headers=owner_headers,
    )
    assert next_version.status_code == 200, next_version.text
    assert client.get(f"/api/recipes/shared/{share['slug']}").status_code == 404


def test_recipe_share_slug_expiration_revocation_and_share_attribution(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000984", balance=100)
    viewer_id = make_user("13710000985", balance=100)
    make_user("13710000988", balance=100, admin=True)
    owner_headers = auth("13710000984")
    viewer_headers = auth("13710000985")
    admin_headers = auth("13710000988")
    payload = _payload("可通过独立链接分享的配方")
    payload["reverse_snapshot_v3"] = {
        "version": 3,
        "target": "image",
        "selected": {"type": "image", "url": "/api/uploads/private-source.png"},
        "final_text": "可通过独立链接分享的配方",
    }
    recipe = _create_recipe(
        client,
        owner_headers,
        visibility="public",
        payload=payload,
    )
    expiration = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    blocked_draft_share = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={"version": 1, "expires_at": expiration},
        headers=owner_headers,
    )
    assert blocked_draft_share.status_code == 409, blocked_draft_share.text
    _submit_and_approve_recipe(
        client,
        recipe["id"],
        owner_headers,
        admin_headers,
    )
    created = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={"version": 1, "expires_at": expiration},
        headers=owner_headers,
    )
    assert created.status_code == 200, created.text
    share = created.json()
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", share["slug"])
    assert str(recipe["id"]) != share["slug"]
    assert share["share_url"].endswith(f"/recipes/shared/{share['slug']}")
    assert client.get(
        f"/api/recipes/{recipe['id']}/shares",
        headers=viewer_headers,
    ).status_code == 404

    client.cookies.clear()
    resolved = client.get(f"/api/recipes/shared/{share['slug']}")
    assert resolved.status_code == 200, resolved.text
    resolved_payload = resolved.json()["recipe"]["version"]["payload"]
    assert resolved_payload["prompt"] == "可通过独立链接分享的配方"
    assert resolved_payload["reverse_snapshot_v3"]["selected"] == {"type": "image"}

    cloned = client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={"share_slug": share["slug"], "title": "来自分享的派生配方"},
        headers=viewer_headers,
    )
    assert cloned.status_code == 200, cloned.text
    assert cloned.json()["visibility"] == "private"
    usage = client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "apply",
            "share_slug": share["slug"],
            "client_event_id": "shared-apply-event-0001",
        },
        headers=viewer_headers,
    )
    assert usage.status_code == 200, usage.text
    replay = client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "apply",
            "share_slug": share["slug"],
            "client_event_id": "shared-apply-event-0001",
        },
        headers=viewer_headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == usage.json()["id"]

    db = SessionLocal()
    try:
        events = db.query(CreationRecipeUsageEvent).filter_by(recipe_id=recipe["id"]).all()
        clone_event = next(event for event in events if event.event_type == "clone")
        assert clone_event.source == "share"
        assert clone_event.share_id == share["id"]
        assert clone_event.derived_recipe_id == cloned.json()["id"]
        assert {event.user_id for event in events} == {viewer_id}
    finally:
        db.close()

    summary = client.get(
        f"/api/recipes/{recipe['id']}/usage",
        headers=owner_headers,
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["by_event"] == {"apply": 1, "clone": 1}
    assert summary.json()["unique_users"] == 1

    revoked = client.delete(
        f"/api/recipes/{recipe['id']}/shares/{share['id']}",
        headers=owner_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert client.get(f"/api/recipes/shared/{share['slug']}").status_code == 404
    assert client.post(
        f"/api/recipes/{recipe['id']}/clone",
        json={"share_slug": share["slug"]},
        headers=viewer_headers,
    ).status_code == 404

    expiring = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    )
    assert expiring.status_code == 200, expiring.text
    source_conflict = client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "apply",
            "share_slug": expiring.json()["slug"],
            "client_event_id": "shared-apply-event-0001",
        },
        headers=viewer_headers,
    )
    assert source_conflict.status_code == 409, source_conflict.text
    db = SessionLocal()
    try:
        row = db.get(CreationRecipeShare, expiring.json()["id"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert row.owner_user_id == owner_id
    finally:
        db.close()
    assert client.get(
        f"/api/recipes/shared/{expiring.json()['slug']}",
    ).status_code == 404


def test_generation_quote_and_task_preserve_owner_recipe_attribution_atomically(
    client,
    make_user,
    auth,
):
    user_id = make_user("13710000970", balance=100)
    headers = auth("13710000970")
    recipe = _create_recipe(client, headers)
    payload = {
        "client_request_id": "owner-recipe-generation-0001",
        "category": "image",
        "stage": "preview",
        "instruction": "银色香水瓶主视觉",
        "params": {"n": 1, "size": "1024x1024"},
        "creation_recipe_id": recipe["id"],
        "creation_recipe_version": recipe["current_version"],
    }

    quoted = client.post("/api/quotes", json=payload, headers=headers)
    assert quoted.status_code == 201, quoted.text
    quote = quoted.json()
    assert quote["creation_recipe_id"] == recipe["id"]
    assert quote["creation_recipe_version"] == recipe["current_version"]
    assert quote["creation_recipe_source"] == "owner"
    assert quote["creation_recipe_share_id"] is None

    # Removing the attribution changes the server fingerprint and cannot consume
    # a quote that was prepared for the recipe-backed request.
    tampered = client.post(
        "/api/generate",
        json={
            key: value
            for key, value in {**payload, "quote_id": quote["quote_id"]}.items()
            if not key.startswith("creation_recipe_")
        },
        headers=headers,
    )
    assert tampered.status_code == 409, tampered.text
    assert tampered.json()["detail"]["code"] == "QUOTE_MISMATCH"

    generated = client.post(
        "/api/generate",
        json={**payload, "quote_id": quote["quote_id"]},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text
    task_out = generated.json()
    assert task_out["creation_recipe_id"] == recipe["id"]
    assert task_out["creation_recipe_version"] == recipe["current_version"]
    assert task_out["creation_recipe_source"] == "owner"
    assert task_out["creation_recipe_share_id"] is None

    db = SessionLocal()
    try:
        task = db.get(GenTask, task_out["id"])
        persisted_quote = db.get(GenerationQuote, quote["quote_id"])
        assert task is not None and persisted_quote is not None
        expected = {
            "recipe_id": recipe["id"],
            "recipe_version": recipe["current_version"],
            "source": "owner",
            "share_id": None,
        }
        assert task.user_id == user_id
        assert task.params["_recipe_attribution"] == expected
        assert persisted_quote.request_snapshot["creation_recipe"] == expected
        events = (
            db.query(CreationRecipeUsageEvent)
            .filter_by(recipe_id=recipe["id"], user_id=user_id)
            .order_by(CreationRecipeUsageEvent.id)
            .all()
        )
        assert [event.event_type for event in events] == [
            "generation_prepare",
            "generation_submit",
        ]
        assert events[0].generation_task_id is None
        assert events[1].generation_task_id == task.id
        assert all(event.source == "owner" for event in events)
    finally:
        db.close()


def test_generation_recipe_access_is_authoritative_for_private_share_and_public(
    client,
    make_user,
    auth,
):
    make_user("13710000971", balance=200)
    viewer_id = make_user("13710000972", balance=200)
    make_user("13710000973", balance=200, admin=True)
    owner_headers = auth("13710000971")
    viewer_headers = auth("13710000972")
    admin_headers = auth("13710000973")
    recipe = _create_recipe(client, owner_headers, visibility="private")
    base = {
        "category": "image",
        "stage": "preview",
        "instruction": "通过分享恢复的香水广告",
        "params": {"n": 1, "size": "1024x1024"},
        "creation_recipe_id": recipe["id"],
        "creation_recipe_version": recipe["current_version"],
    }

    denied = client.post("/api/quotes", json=base, headers=viewer_headers)
    assert denied.status_code == 404, denied.text

    blocked_private_share = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    )
    assert blocked_private_share.status_code == 409, blocked_private_share.text
    made_public = client.patch(
        f"/api/recipes/{recipe['id']}",
        json={"visibility": "public"},
        headers=owner_headers,
    )
    assert made_public.status_code == 200, made_public.text
    _submit_and_approve_recipe(
        client,
        recipe["id"],
        owner_headers,
        admin_headers,
    )

    shared = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    )
    assert shared.status_code == 200, shared.text
    share = shared.json()
    shared_payload = {
        **base,
        "creation_recipe_version": share["version"],
        "client_request_id": "shared-recipe-generation-0001",
        "creation_recipe_share_slug": share["slug"],
    }
    quoted = client.post("/api/quotes", json=shared_payload, headers=viewer_headers)
    assert quoted.status_code == 201, quoted.text
    quote = quoted.json()
    assert quote["creation_recipe_source"] == "share"
    assert quote["creation_recipe_share_id"] == share["id"]

    revoked = client.delete(
        f"/api/recipes/{recipe['id']}/shares/{share['id']}",
        headers=owner_headers,
    )
    assert revoked.status_code == 200, revoked.text
    blocked = client.post(
        "/api/generate",
        json={**shared_payload, "quote_id": quote["quote_id"]},
        headers=viewer_headers,
    )
    assert blocked.status_code == 404, blocked.text
    db = SessionLocal()
    try:
        persisted_quote = db.get(GenerationQuote, quote["quote_id"])
        assert persisted_quote is not None and persisted_quote.status == "active"
        assert db.query(GenTask).filter_by(quote_id=quote["quote_id"]).count() == 0
    finally:
        db.close()

    active_share = client.post(
        f"/api/recipes/{recipe['id']}/shares",
        json={},
        headers=owner_headers,
    ).json()
    active_payload = {
        **base,
        "creation_recipe_version": active_share["version"],
        "client_request_id": "shared-recipe-generation-0002",
        "creation_recipe_share_slug": active_share["slug"],
    }
    active_quote = client.post("/api/quotes", json=active_payload, headers=viewer_headers)
    assert active_quote.status_code == 201, active_quote.text
    generated = client.post(
        "/api/generate",
        json={**active_payload, "quote_id": active_quote.json()["quote_id"]},
        headers=viewer_headers,
    )
    assert generated.status_code == 200, generated.text
    assert generated.json()["creation_recipe_source"] == "share"
    assert generated.json()["creation_recipe_share_id"] == active_share["id"]

    public_recipe = _create_recipe(
        client,
        owner_headers,
        title="审核通过的公开生成配方",
        visibility="public",
    )
    _submit_and_approve_recipe(
        client,
        public_recipe["id"],
        owner_headers,
        admin_headers,
    )
    public_payload = {
        **base,
        "client_request_id": "public-recipe-generation-0001",
        "creation_recipe_id": public_recipe["id"],
        "creation_recipe_version": public_recipe["current_version"],
    }
    public_quote = client.post("/api/quotes", json=public_payload, headers=viewer_headers)
    assert public_quote.status_code == 201, public_quote.text
    assert public_quote.json()["creation_recipe_source"] == "public"

    db = SessionLocal()
    try:
        share_events = (
            db.query(CreationRecipeUsageEvent)
            .filter_by(recipe_id=recipe["id"], user_id=viewer_id)
            .all()
        )
        assert [event.event_type for event in share_events].count("generation_prepare") == 2
        assert [event.event_type for event in share_events].count("generation_submit") == 1
    finally:
        db.close()


def test_recipe_usage_links_generation_and_rejects_cross_user_or_key_reuse(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000986", balance=100)
    other_id = make_user("13710000987", balance=100)
    owner_headers = auth("13710000986")
    other_headers = auth("13710000987")
    recipe = _create_recipe(client, owner_headers)
    db = SessionLocal()
    try:
        own_task = GenTask(user_id=owner_id, category="image", status="queued")
        other_task = GenTask(user_id=other_id, category="image", status="queued")
        wrong_category_task = GenTask(
            user_id=owner_id,
            category="video",
            status="queued",
        )
        db.add_all([own_task, other_task, wrong_category_task])
        db.commit()
        db.refresh(own_task)
        db.refresh(other_task)
        db.refresh(wrong_category_task)
        own_task_id = int(own_task.id)
        other_task_id = int(other_task.id)
        wrong_category_task_id = int(wrong_category_task.id)
    finally:
        db.close()

    assert client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={"event_type": "generation_submit"},
        headers=owner_headers,
    ).status_code == 422
    linked = client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "generation_submit",
            "generation_task_id": own_task_id,
            "client_event_id": "generation-submit-event-0001",
        },
        headers=owner_headers,
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["generation_task_id"] == own_task_id
    assert client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "generation_submit",
            "generation_task_id": other_task_id,
            "client_event_id": "generation-submit-event-0002",
        },
        headers=owner_headers,
    ).status_code == 404
    assert client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "generation_submit",
            "generation_task_id": wrong_category_task_id,
            "client_event_id": "generation-submit-event-0003",
        },
        headers=owner_headers,
    ).status_code == 422
    conflict = client.post(
        f"/api/recipes/{recipe['id']}/usage",
        json={
            "event_type": "generation_prepare",
            "client_event_id": "generation-submit-event-0001",
        },
        headers=owner_headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert client.get(
        f"/api/recipes/{recipe['id']}/usage",
        headers=other_headers,
    ).status_code == 404


def test_public_recipe_sanitizer_scales_and_preserves_long_safe_prompts():
    timings = []
    for length in (10_000, 50_000, 200_000, _MAX_PUBLIC_STRING_LENGTH):
        prompt = "x" * length
        started_at = perf_counter()
        assert _contains_url_uri_or_secret(prompt) is False
        timings.append(perf_counter() - started_at)

        projected = _public_recipe_payload({"prompt": prompt})
        assert projected == {"prompt": prompt}

    slash_prompt_unit = "use /imagine for image/video at 16/9; input / output. "
    slash_prompt = (
        slash_prompt_unit
        * (_MAX_PUBLIC_STRING_LENGTH // len(slash_prompt_unit) + 1)
    )[:_MAX_PUBLIC_STRING_LENGTH]
    started_at = perf_counter()
    assert _contains_url_uri_or_secret(slash_prompt) is False
    timings.append(perf_counter() - started_at)
    assert _public_recipe_payload({"prompt": slash_prompt}) == {"prompt": slash_prompt}

    # This is intentionally loose enough for slow CI hosts while catching the
    # former quadratic authority regex (50k characters took several seconds).
    assert max(timings) < 3.0

    dangerous_tail = "x" * (_MAX_PUBLIC_STRING_LENGTH - 64)
    dangerous_tail += "https://cdn.example.com/private/owner.png"
    started_at = perf_counter()
    assert _contains_url_uri_or_secret(dangerous_tail) is True
    assert perf_counter() - started_at < 3.0
    projected = _public_recipe_payload({
        "prompt": "core replay prompt",
        "reference": dangerous_tail,
    })
    assert projected["prompt"] == "core replay prompt"
    assert "reference" not in projected
    assert projected["public_asset_access"]["status"] == "unavailable"


def test_public_recipe_sanitizer_resource_limits_fail_closed():
    too_long = "x" * (_MAX_PUBLIC_STRING_LENGTH + 1)
    projected = _public_recipe_payload({
        "prompt": "core replay prompt",
        "oversized": too_long,
    })
    assert projected["prompt"] == "core replay prompt"
    assert "oversized" not in projected

    deep: dict = {"private": "https://cdn.example.com/private/deep.png"}
    for index in range(_MAX_PUBLIC_PAYLOAD_DEPTH + 100):
        deep = {f"level_{index}": deep}
    projected = _public_recipe_payload({"prompt": "core replay prompt", "deep": deep})
    serialized = json.dumps(projected)
    assert projected["prompt"] == "core replay prompt"
    assert "cdn.example.com" not in serialized
    assert projected["public_asset_access"]["status"] == "unavailable"

    oversized_collection = ["safe"] * (_MAX_PUBLIC_COLLECTION_ITEMS + 1)
    projected = _public_recipe_payload({
        "prompt": "core replay prompt",
        "oversized_collection": oversized_collection,
    })
    assert projected["prompt"] == "core replay prompt"
    assert "oversized_collection" not in projected

    entries = _MAX_PUBLIC_PAYLOAD_NODES // 6 + 1
    node_heavy = [
        {"a": 1, "b": 2, "c": 3, "d": 4, "e": "safe"}
        for _ in range(entries)
    ]
    node_heavy[-1]["private"] = "https://cdn.example.com/private/tail.png"
    projected = _public_recipe_payload({
        "prompt": "core replay prompt",
        "node_heavy": node_heavy,
    })
    serialized = json.dumps(projected)
    assert projected["prompt"] == "core replay prompt"
    assert "cdn.example.com" not in serialized
    assert len(projected["node_heavy"]) < len(node_heavy)
    assert projected["public_asset_access"]["status"] == "unavailable"


def _bulk_insert_recipes(user_id: int, count: int, *, public: bool = False) -> list[int]:
    """直接批量写入配方与首个版本，避免 200+ 次 API 往返拖慢测试。"""
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        rows = []
        for index in range(count):
            rows.append(CreationRecipe(
                user_id=user_id,
                title=f"批量配方 {index:03d}",
                category="image",
                visibility="public" if public else "private",
                moderation_status="approved" if public else "draft",
                favorite=False,
                current_version=1,
                approved_version=1 if public else None,
                created_at=now - timedelta(seconds=index),
                updated_at=now - timedelta(seconds=index),
            ))
        db.add_all(rows)
        db.flush()
        for row in rows:
            db.add(CreationRecipeVersion(
                recipe_id=row.id,
                version=1,
                schema_version="creation-recipe.v1",
                payload=_payload(f"批量配方提示词 {row.title}"),
                metadata_snapshot={},
            ))
        db.commit()
        return [int(row.id) for row in rows]
    finally:
        db.close()


def test_list_recipes_offset_pagination_and_title_search(client, make_user, auth):
    user_id = make_user("13710000871", balance=100)
    headers = auth("13710000871")
    _bulk_insert_recipes(user_id, 5)

    first_page = client.get("/api/recipes?limit=2&offset=0", headers=headers)
    assert first_page.status_code == 200, first_page.text
    second_page = client.get("/api/recipes?limit=2&offset=2", headers=headers)
    assert second_page.status_code == 200, second_page.text
    all_rows = client.get("/api/recipes?limit=10&offset=0", headers=headers)
    assert all_rows.status_code == 200, all_rows.text
    first_ids = [row["id"] for row in first_page.json()]
    second_ids = [row["id"] for row in second_page.json()]
    assert len(first_ids) == 2 and len(second_ids) == 2
    # offset 生效:两页互不重叠，且与整体排序逐段一致
    assert not set(first_ids) & set(second_ids)
    assert first_ids + second_ids == [row["id"] for row in all_rows.json()][:4]
    # 越过末尾的 offset 返回空列表而不是报错
    tail = client.get("/api/recipes?limit=10&offset=5", headers=headers)
    assert tail.status_code == 200 and tail.json() == []

    # q 过滤:命中标题子串，且不区分他人配方
    searched = client.get("/api/recipes?q=%E9%85%8D%E6%96%B9%20003", headers=headers)
    assert searched.status_code == 200, searched.text
    assert [row["title"] for row in searched.json()] == ["批量配方 003"]
    missed = client.get("/api/recipes?q=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E6%A0%87%E9%A2%98", headers=headers)
    assert missed.status_code == 200 and missed.json() == []


def test_list_recipes_limit_clamped_to_200(client, make_user, auth):
    user_id = make_user("13710000872", balance=100)
    headers = auth("13710000872")
    _bulk_insert_recipes(user_id, 205, public=True)

    # 我的配方列表:limit 超上限被夹到 200
    mine = client.get("/api/recipes?limit=999", headers=headers)
    assert mine.status_code == 200, mine.text
    assert len(mine.json()) == 200
    # 第二页可以取到剩余的 5 条，历史数据不会永远够不到
    mine_tail = client.get("/api/recipes?limit=999&offset=200", headers=headers)
    assert mine_tail.status_code == 200 and len(mine_tail.json()) == 5

    # 公开发现列表同样夹到 200，翻页可达剩余数据（用 q 隔离其他用例的公开配方）
    public = client.get("/api/recipes/public?limit=999&q=%E6%89%B9%E9%87%8F%E9%85%8D%E6%96%B9")
    assert public.status_code == 200, public.text
    assert len(public.json()) == 200
    public_tail = client.get(
        "/api/recipes/public?limit=999&offset=200&q=%E6%89%B9%E9%87%8F%E9%85%8D%E6%96%B9"
    )
    assert public_tail.status_code == 200 and len(public_tail.json()) == 5
