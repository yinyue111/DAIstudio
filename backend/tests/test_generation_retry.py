"""Retry must re-enter the same live policy boundary as a new generation."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone

import pytest

import app.services.generation_model_runtime as generation_runtime
import app.tasks as generation_jobs
from app.db import SessionLocal
from app.models import (
    CreditTransaction,
    GatewayCall,
    GenerationDispatch,
    GenerationQuote,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    ModelRoute,
    ModelRouteHealthEvent,
    ModelRouteVersion,
    ReverseOperation,
    ReverseResultRevision,
    UploadedAsset,
    User,
)
from app.services import reverse_lineage, storage
from app.services.config_store import get_setting, set_setting
from app.services.generation_model_runtime import model_snapshot
from app.services.model_gateway_config import encrypt_api_key

_IMAGE_CAPABILITIES = {
    "text_to_image": True,
    "image_to_image": True,
    "image_edit": True,
    "reference_image": True,
    "multi_reference": True,
}
_VIDEO_CAPABILITIES = {
    "text_to_video": True,
    "image_to_video": True,
    "video_to_video": True,
    "multi_reference": True,
    "max_reference_images": 10,
}


@pytest.fixture(autouse=True)
def _restore_retry_model_overrides(client):
    with SessionLocal() as db:
        original = {
            int(row.id): {
                "model_id": row.model_id,
                "display_name": row.display_name,
                "provider": row.provider,
                "base_url": row.base_url,
                "api_key_encrypted": row.api_key_encrypted,
                "gateway_format": row.gateway_format,
                "enabled": bool(row.enabled),
                "cost_credits": int(row.cost_credits),
                "unlock_cost": int(row.unlock_cost),
                "extra": deepcopy(row.extra),
            }
            for row in db.query(ModelConfig).filter(ModelConfig.use.in_(("image", "video"))).all()
        }
    yield
    with SessionLocal() as db:
        for model_id, state in original.items():
            row = db.get(ModelConfig, model_id)
            if row is None:
                continue
            row.model_id = state["model_id"]
            row.display_name = state["display_name"]
            row.provider = state["provider"]
            row.base_url = state["base_url"]
            row.api_key_encrypted = state["api_key_encrypted"]
            row.gateway_format = state["gateway_format"]
            row.enabled = state["enabled"]
            row.cost_credits = state["cost_credits"]
            row.unlock_cost = state["unlock_cost"]
            row.extra = deepcopy(state["extra"])
        db.commit()


def _configure_model(use: str, *, capabilities: dict | None = None) -> int:
    with SessionLocal() as db:
        row = (
            db.query(ModelConfig)
            .filter(ModelConfig.use == use, ModelConfig.is_default.is_(True))
            .order_by(ModelConfig.id)
            .first()
        )
        assert row is not None, f"missing default {use} model"
        extra = deepcopy(row.extra) if isinstance(row.extra, dict) else {}
        extra["capabilities"] = dict(
            capabilities
            if capabilities is not None
            else (_IMAGE_CAPABILITIES if use == "image" else _VIDEO_CAPABILITIES)
        )
        row.enabled = True
        row.cost_credits = 7
        row.extra = extra
        db.commit()
        db.refresh(row)
        return int(row.id)


def _set_image_catalog_state(
    model_config_id: int,
    label: str,
    *,
    api_key: str = "quote-snapshot-stable-secret",
    text_to_image: bool = True,
    cost: int = 13,
    enabled: bool = True,
    provider: str = "custom_openai",
    gateway_format: str = "openai",
) -> None:
    with SessionLocal() as db:
        row = db.get(ModelConfig, model_config_id)
        assert row is not None
        row.model_id = f"quote-snapshot-model-{label.lower()}"
        row.display_name = f"Quote snapshot {label}"
        row.provider = provider
        row.base_url = f"https://quote-{label.lower()}.example.com/v1"
        row.api_key_encrypted = encrypt_api_key(api_key)
        row.gateway_format = gateway_format
        row.enabled = enabled
        row.cost_credits = cost
        row.unlock_cost = 0
        row.extra = {
            "capabilities": {
                **_IMAGE_CAPABILITIES,
                "text_to_image": text_to_image,
                "catalog_marker": label,
            },
            "generation_path": f"/v1/{label.lower()}/images/generations",
            "edit_path": f"/v1/{label.lower()}/images/edits",
            "image_payload": {
                "field_map": {
                    "prompt": f"{label.lower()}_prompt",
                    "model": f"{label.lower()}_model",
                },
                "catalog_marker": label,
            },
            "prompt_profile": {
                "name": f"profile-{label.lower()}",
                "prefix": f"{label}-PREFIX",
            },
            "credit_pricing": {
                "image": {"1k": cost, "2k": cost, "4k": cost},
                "image_edit": {"1k": cost, "2k": cost, "4k": cost},
            },
        }
        db.commit()


def _create_temporary_image_model(label: str, *, cost: int) -> int:
    with SessionLocal() as db:
        row = ModelConfig(
            use="image",
            model_id=f"retry-switch-{label.lower()}",
            display_name=f"Retry switch {label}",
            is_default=False,
            sort_order=990,
            provider="custom_openai",
            base_url=f"https://retry-switch-{label.lower()}.example.com/v1",
            api_key_encrypted=encrypt_api_key(f"retry-switch-secret-{label.lower()}"),
            gateway_format="openai",
            cost_credits=cost,
            unlock_cost=0,
            enabled=True,
            extra={
                "capabilities": dict(_IMAGE_CAPABILITIES),
                "credit_pricing": {
                    "image": {"1k": cost, "2k": cost, "4k": cost},
                    "image_edit": {"1k": cost, "2k": cost, "4k": cost},
                },
                "prompt_profile": {
                    "name": f"retry-switch-{label.lower()}",
                    "prefix": f"{label}-PREFIX",
                },
            },
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return int(row.id)


def _delete_temporary_model(model_config_id: int) -> None:
    """Remove a test catalog row after detaching durable test history from its FKs."""
    with SessionLocal() as db:
        db.query(GenTask).filter(GenTask.model_config_id == model_config_id).update(
            {GenTask.model_config_id: None},
            synchronize_session=False,
        )
        db.query(ReverseOperation).filter(
            ReverseOperation.model_config_id == model_config_id
        ).update(
            {ReverseOperation.model_config_id: None},
            synchronize_session=False,
        )
        db.query(GatewayCall).filter(GatewayCall.model_config_id == model_config_id).update(
            {GatewayCall.model_config_id: None},
            synchronize_session=False,
        )
        db.query(GenerationQuote).filter(
            GenerationQuote.model_config_id == model_config_id
        ).update(
            {
                GenerationQuote.model_config_id: None,
                GenerationQuote.capability_version_id: None,
                GenerationQuote.price_version_id: None,
            },
            synchronize_session=False,
        )
        route_ids = [
            int(route_id)
            for (route_id,) in db.query(ModelRoute.id).filter(
                ModelRoute.model_config_id == model_config_id
            )
        ]
        if route_ids:
            db.query(ModelRouteHealthEvent).filter(
                ModelRouteHealthEvent.route_id.in_(route_ids)
            ).delete(synchronize_session=False)
            db.query(ModelRouteVersion).filter(
                ModelRouteVersion.route_id.in_(route_ids)
            ).delete(synchronize_session=False)
            db.query(ModelRoute).filter(ModelRoute.id.in_(route_ids)).delete(
                synchronize_session=False
            )
        db.query(ModelCapabilityVersion).filter_by(model_config_id=model_config_id).delete(
            synchronize_session=False
        )
        db.query(ModelPriceVersion).filter_by(model_config_id=model_config_id).delete(
            synchronize_session=False
        )
        db.query(ModelConfig).filter_by(id=model_config_id).delete(synchronize_session=False)
        db.commit()


def _upload(user_id: int, suffix: str) -> tuple[str, str]:
    key = storage.save_bytes_named(b"retry-policy-image", "upload", f"{suffix}.png")
    with SessionLocal() as db:
        db.add(
            UploadedAsset(
                key=key,
                user_id=user_id,
                mime="image/png",
                bytes=18,
                original_filename=f"{suffix}.png",
            )
        )
        db.commit()
    return storage.upload_api_url(key), key


def _failed_task(
    *,
    user_id: int,
    model_config_id: int,
    category: str,
    prompt: dict | None = None,
    params: dict | None = None,
    source_asset_url: str | None = None,
    source_type: str = "image",
) -> int:
    with SessionLocal() as db:
        model = db.get(ModelConfig, model_config_id)
        public_params = (
            {"n": 1, "size": "256x256"}
            if category == "image"
            else {"duration": 2, "resolution": "720p", "ratio": "16:9"}
        )
        public_params.update(params or {})
        public_params["_model_snapshot"] = model_snapshot(model)
        # A policy rejection must not strip or increment existing worker state.
        public_params["_video_download_attempts"] = 4
        row = GenTask(
            user_id=user_id,
            model_config_id=model_config_id,
            source_asset_url=source_asset_url,
            source_type=source_type,
            category=category,
            stage="preview",
            prompt=prompt or {"final_text": "safe retry policy prompt"},
            model_use=category,
            params=public_params,
            status="failed",
            error="provider rejected before completion",
            cost_frozen=7,
            cost_settled=0,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return int(row.id)


def _seed_verified_reverse_result(
    user_id: int,
    suffix: str,
    *,
    user_edit_count: int = 1,
) -> tuple[int, int]:
    assert user_edit_count >= 1
    payload = {
        "final_text": f"verified reverse prompt {suffix}",
        "structured": {"subject": "verified reverse subject"},
    }
    asset_url = f"http://example.com/retry-lineage-{user_id}-{suffix}.png"
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=hashlib.sha256(
                f"retry-lineage:{user_id}:{suffix}".encode()
            ).hexdigest(),
            target="image",
            asset_url=asset_url,
            output_purpose="generation",
            request_context={"output_purpose": "generation"},
            status="succeeded",
            progress=100,
            result=deepcopy(payload),
            normalized_result=deepcopy(payload),
        )
        db.add(operation)
        db.flush()
        fingerprint = reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(
                f"retry-lineage-source:{user_id}:{suffix}".encode()
            ),
            locator=asset_url,
            method="test_source_sha256",
        )
        fingerprints = [fingerprint]
        source_hash = reverse_lineage.source_content_hash(fingerprints)
        parent_id = None
        applied_id = None
        sources = (
            "provider_raw",
            "normalized",
            *("user_edit" for _ in range(user_edit_count)),
            "applied",
        )
        for version, source in enumerate(sources, start=1):
            revision_payload = (
                {"provider": "test", "result": deepcopy(payload)}
                if source == "provider_raw"
                else deepcopy(payload)
            )
            revision = ReverseResultRevision(
                operation_id=operation.id,
                user_id=user_id,
                version=version,
                source=source,
                payload=revision_payload,
                parent_revision_id=parent_id,
                source_content_hash=source_hash,
                source_fingerprints=deepcopy(fingerprints),
                payload_hash=reverse_lineage.canonical_payload_hash(revision_payload),
                lineage_status=reverse_lineage.VERIFIED,
                evidence_review_action="not_applicable",
            )
            db.add(revision)
            db.flush()
            parent_id = int(revision.id)
            if source == "applied":
                applied_id = int(revision.id)
        db.commit()
        assert applied_id is not None
        return int(operation.id), applied_id


def _failed_verified_reverse_task(
    quote_and_generate,
    headers: dict,
    *,
    user_id: int,
    model_config_id: int,
    suffix: str,
    user_edit_count: int = 1,
) -> int:
    operation_id, applied_id = _seed_verified_reverse_result(
        user_id,
        suffix,
        user_edit_count=user_edit_count,
    )
    created = quote_and_generate(
        headers=headers,
        payload={
            "client_request_id": f"retry-lineage-{suffix}",
            "reverse_operation_id": operation_id,
            "reverse_revision_id": applied_id,
            "category": "image",
            "stage": "preview",
            "instruction": f"verified reverse retry {suffix}",
            "params": {"n": 1, "size": "256x256"},
            "model_config_id": model_config_id,
        },
    )
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        params = deepcopy(task.params)
        params["_video_download_attempts"] = 4
        task.params = params
        task.status = "failed"
        task.error = "known failure before reverse retry"
        task.finished_at = datetime.now(timezone.utc)
        db.commit()
    return task_id


def _retry_state(task_id: int) -> dict:
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        user = db.get(User, task.user_id)
        return {
            "status": task.status,
            "phase": task.phase,
            "error": task.error,
            "params": deepcopy(task.params),
            "cost_frozen": int(task.cost_frozen),
            "cost_settled": int(task.cost_settled),
            "created_at": task.created_at,
            "finished_at": task.finished_at,
            "external_task_id": task.external_task_id,
            "external_submitted_at": task.external_submitted_at,
            "reverse_operation_id": task.reverse_operation_id,
            "source_revision_id": task.source_revision_id,
            "compiled_revision_id": task.compiled_revision_id,
            "generation_revision_id": task.generation_revision_id,
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "credit_rows": db.query(CreditTransaction)
            .filter_by(biz_type="gen_task", biz_ref=task_id)
            .count(),
            "ledger_rows": db.query(CreditTransaction).filter_by(user_id=task.user_id).count(),
            "gateway_calls": db.query(GatewayCall).filter_by(task_id=task_id).count(),
        }


def _post_rejected_retry(
    client,
    headers: dict,
    monkeypatch,
    task_id: int,
    *,
    status: int,
    message: str,
):
    published: list[tuple] = []
    monkeypatch.setattr(
        generation_jobs,
        "enqueue_with_request_context",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    before = _retry_state(task_id)

    response = client.post(f"/api/tasks/{task_id}/retry", headers=headers)

    assert response.status_code == status, response.text
    assert message in response.text
    assert _retry_state(task_id) == before
    if "_video_download_attempts" in before["params"]:
        assert before["params"]["_video_download_attempts"] == 4
    assert published == []
    return response


def test_retry_rechecks_current_content_policy_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13981000001", balance=1000)
    headers = auth("13981000001")
    model_id = _configure_model("image")
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="image",
        prompt={"final_text": "contains retry-policy-blocked-term"},
    )
    with SessionLocal() as db:
        old_enabled = get_setting(db, "content_safety_enabled", False)
        old_terms = get_setting(db, "content_safety_banned_terms", "")
    try:
        with SessionLocal() as db:
            set_setting(db, "content_safety_enabled", True)
            set_setting(db, "content_safety_banned_terms", "retry-policy-blocked-term")
        _post_rejected_retry(
            client,
            headers,
            monkeypatch,
            task_id,
            status=400,
            message="内容安全拦截",
        )
    finally:
        with SessionLocal() as db:
            set_setting(db, "content_safety_enabled", old_enabled)
            set_setting(db, "content_safety_banned_terms", old_terms)


def test_retry_rechecks_current_model_capability_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13981000002", balance=1000)
    headers = auth("13981000002")
    model_id = _configure_model("image", capabilities={"text_to_image": False})
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="image",
    )

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=400,
        message="不支持文生图",
    )


@pytest.mark.parametrize(
    ("field", "category", "case_id", "case_number"),
    [
        ("source_asset_url", "image", "source", 1),
        ("reference_image_url", "image", "reference", 2),
        ("mask_image_url", "image", "mask", 3),
        ("product_reference_image", "video", "product", 4),
        ("first_frame_image", "video", "first", 5),
        ("last_frame_image", "video", "last", 6),
        ("style_reference_image", "video", "style", 7),
        ("character_reference_image", "video", "character", 8),
        ("product_detail_images", "video", "detail", 9),
    ],
)
def test_retry_rechecks_ssrf_for_every_forwarded_reference_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
    field,
    category,
    case_id,
    case_number,
):
    phone = f"1398110{case_number:04d}"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model(category)
    source_url = None
    params: dict = {}
    blocked = "http://127.0.0.1/retry-policy-private.png"
    if field == "source_asset_url":
        source_url = blocked
    elif field == "product_detail_images":
        product_url, _ = _upload(user_id, f"ssrf-detail-product-{case_id}")
        params.update(product_reference_image=product_url, product_detail_images=[blocked])
    else:
        params[field] = blocked
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category=category,
        params=params,
        source_asset_url=source_url,
        source_type="image",
    )

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=400,
        message="安全策略拦截",
    )


@pytest.mark.parametrize(
    ("field", "category", "case_id", "case_number"),
    [
        ("source_asset_url", "image", "source", 1),
        ("reference_image_url", "image", "reference", 2),
        ("mask_image_url", "image", "mask", 3),
        ("product_reference_image", "video", "product", 4),
        ("first_frame_image", "video", "first", 5),
        ("last_frame_image", "video", "last", 6),
        ("style_reference_image", "video", "style", 7),
        ("character_reference_image", "video", "character", 8),
        ("product_detail_images", "video", "detail", 9),
    ],
)
def test_retry_rechecks_owner_for_every_local_reference_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
    field,
    category,
    case_id,
    case_number,
):
    owner_id = make_user(f"1398120{case_number:04d}", balance=1000)
    retry_phone = f"1398130{case_number:04d}"
    retry_user_id = make_user(retry_phone, balance=1000)
    headers = auth(retry_phone)
    model_id = _configure_model(category)
    other_url, _ = _upload(owner_id, f"owner-other-{case_id}")
    source_url = None
    params: dict = {}
    if field == "source_asset_url":
        source_url = other_url
    elif field == "product_detail_images":
        product_url, _ = _upload(retry_user_id, f"owner-detail-product-{case_id}")
        params.update(product_reference_image=product_url, product_detail_images=[other_url])
    else:
        params[field] = other_url
    task_id = _failed_task(
        user_id=retry_user_id,
        model_config_id=model_id,
        category=category,
        params=params,
        source_asset_url=source_url,
        source_type="image",
    )

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=404,
        message="上传素材不存在",
    )


@pytest.mark.parametrize("field", ["product_reference_image", "product_detail_images"])
def test_retry_rechecks_product_reference_liveness_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
    field,
):
    suffix = "theme" if field == "product_reference_image" else "detail"
    phone = "13981000401" if suffix == "theme" else "13981000402"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("video")
    dead_url, dead_key = _upload(user_id, f"dead-{suffix}")
    params: dict = {}
    if field == "product_reference_image":
        params[field] = dead_url
    else:
        product_url, _ = _upload(user_id, "live-detail-product")
        params.update(product_reference_image=product_url, product_detail_images=[dead_url])
    storage.local_path(dead_key).unlink()
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="video",
        params=params,
    )

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=410,
        message="文件已失效",
    )


def test_retry_rechecks_product_detail_contract_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13981000403", balance=1000)
    headers = auth("13981000403")
    model_id = _configure_model("video")
    detail_url, _ = _upload(user_id, "detail-without-theme")
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="video",
        params={"product_detail_images": [detail_url]},
    )

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=400,
        message="必须先提供产品主题图",
    )


def test_retry_rechecks_video_reference_actionability_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13981000404", balance=1000)
    headers = auth("13981000404")
    model_id = _configure_model("video")
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="video",
        source_asset_url="https://example.com/raw-reference.mp4",
        source_type="video",
    )

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=400,
        message="视频参考缺少可用封面",
    )


@pytest.mark.parametrize(
    ("category", "marker", "phone"),
    [
        ("image", "_image_submit_state_unknown", "13981000405"),
        ("video", "_video_submit_state_unknown", "13981000406"),
    ],
)
def test_retry_never_resubmits_legacy_unknown_upstream_submission(
    client,
    make_user,
    auth,
    monkeypatch,
    category,
    marker,
    phone,
):
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model(category)
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category=category,
        params={marker: True},
    )

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=409,
        message="上游提交状态未知",
    )


def test_requote_retry_blocks_unknown_submission_without_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
):
    phone = "13981000416"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="image",
        params={"_image_submit_state_unknown": True},
    )
    published: list[tuple] = []
    monkeypatch.setattr(
        generation_jobs,
        "enqueue_with_request_context",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )

    def state() -> tuple[int, int, int, int, int]:
        with SessionLocal() as db:
            return (
                db.query(GenTask).filter_by(user_id=user_id).count(),
                db.query(GenerationQuote).filter_by(user_id=user_id).count(),
                db.query(GenerationDispatch)
                .join(GenTask, GenTask.id == GenerationDispatch.task_id)
                .filter(GenTask.user_id == user_id)
                .count(),
                db.query(CreditTransaction).filter_by(user_id=user_id).count(),
                db.query(ReverseResultRevision).filter_by(user_id=user_id).count(),
            )

    before = state()
    response = client.post(
        f"/api/tasks/{task_id}/retry/requote",
        headers=headers,
        json={"client_request_id": "requote-unknown-request-001"},
    )
    assert response.status_code == 409, response.text
    assert "上游提交状态未知" in response.text
    assert state() == before
    assert published == []


def test_requote_retry_blocks_unreconciled_dispatch_before_any_side_effect(
    client,
    make_user,
    auth,
    monkeypatch,
):
    phone = "13981000418"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="image",
    )
    with SessionLocal() as db:
        db.add(
            GenerationDispatch(
                task_id=task_id,
                attempt=1,
                task_name="generate.image",
                celery_task_id=f"unreconciled-requote-{task_id}",
                status="unknown",
                publish_attempts=2,
                next_attempt_at=datetime.now(timezone.utc),
                last_error_code="BROKER_ACK_UNKNOWN",
            )
        )
        db.commit()

    published: list[tuple] = []
    monkeypatch.setattr(
        generation_jobs,
        "enqueue_with_request_context",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )

    def state() -> dict:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            dispatch = db.scalar(
                db.query(GenerationDispatch)
                .filter_by(task_id=task_id)
                .order_by(GenerationDispatch.attempt.desc())
                .statement
            )
            routes = (
                db.query(ModelRoute)
                .filter_by(model_config_id=model_id)
                .order_by(ModelRoute.id)
                .all()
            )
            return {
                "retry_state": _retry_state(task_id),
                "task_count": db.query(GenTask).filter_by(user_id=user_id).count(),
                "quote_count": db.query(GenerationQuote).filter_by(user_id=user_id).count(),
                "dispatch_count": (
                    db.query(GenerationDispatch)
                    .join(GenTask, GenTask.id == GenerationDispatch.task_id)
                    .filter(GenTask.user_id == user_id)
                    .count()
                ),
                "dispatch": (
                    dispatch.status,
                    dispatch.publish_attempts,
                    dispatch.next_attempt_at,
                    dispatch.last_error_code,
                ),
                "revision_count": (
                    db.query(ReverseResultRevision).filter_by(user_id=user_id).count()
                ),
                "balance": int(user.balance_credits),
                "frozen": int(user.frozen_credits),
                "routes": [
                    (
                        int(route.id),
                        route.health_status,
                        route.last_probe_at,
                        route.half_open_claimed_until,
                    )
                    for route in routes
                ],
            }

    before = state()
    response = client.post(
        f"/api/tasks/{task_id}/retry/requote",
        headers=headers,
        json={"client_request_id": "requote-unreconciled-dispatch-001"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RETRY_SOURCE_UNRECONCILED"
    assert state() == before
    assert published == []


def test_normal_retry_keeps_original_quote_price_and_model_snapshot(
    client,
    make_user,
    auth,
    quote_and_generate,
):
    make_user("13981000407", balance=1000)
    headers = auth("13981000407")
    model_id = _configure_model("image")
    created = quote_and_generate(
        headers=headers,
        payload={
            "category": "image",
            "stage": "preview",
            "instruction": "quote-bound retry succeeds",
            "params": {"n": 1, "size": "256x256"},
            "model_config_id": model_id,
        },
    )
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])

    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        quote = db.get(GenerationQuote, task.quote_id)
        original_cost = int(quote.estimated_credits)
        original_snapshot = deepcopy(task.params["_model_snapshot"])
        original_quote_meta = deepcopy(task.params["_quote"])
        assert int(task.cost_frozen) == original_cost
        task.status = "failed"
        task.error = "retry after a known pre-submit failure"
        task.finished_at = datetime.now(timezone.utc)
        model = db.get(ModelConfig, model_id)
        model.cost_credits = 999
        model.extra = {
            "capabilities": dict(_IMAGE_CAPABILITIES),
            "credit_pricing": {
                "image": {"1k": 999, "2k": 999, "4k": 999},
                "image_edit": {"1k": 999, "2k": 999, "4k": 999},
            },
        }
        db.commit()

    retried = client.post(f"/api/tasks/{task_id}/retry", headers=headers)

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] in {"queued", "succeeded"}
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        assert task.quote_id is not None
        quote = db.get(GenerationQuote, task.quote_id)
        assert int(task.cost_frozen) == original_cost
        assert int(task.cost_settled) == original_cost
        assert int(quote.estimated_credits) == original_cost
        assert task.params["_model_snapshot"] == original_snapshot
        assert task.params["_quote"] == original_quote_meta
        retry_freezes = (
            db.query(CreditTransaction)
            .filter_by(
                type="freeze",
                biz_type="gen_task",
                biz_ref=task_id,
            )
            .all()
        )
        assert int(retry_freezes[-1].reserved_amount or -retry_freezes[-1].change) == original_cost


def test_requote_retry_creates_new_task_with_current_price_and_is_idempotent(
    client,
    make_user,
    auth,
    quote_and_generate,
):
    phone = "13981000417"
    user_id = make_user(phone, balance=2000)
    headers = auth(phone)
    model_id = _configure_model("image")
    _set_image_catalog_state(model_id, "RequoteA", cost=13)
    created = quote_and_generate(
        headers=headers,
        payload={
            "client_request_id": "requote-source-request-001",
            "category": "image",
            "stage": "preview",
            "instruction": "requote uses the current catalog price",
            "params": {"n": 1, "size": "1024x1024"},
            "model_config_id": model_id,
        },
    )
    assert created.status_code == 200, created.text
    source_task_id = int(created.json()["id"])

    with SessionLocal() as db:
        source = db.get(GenTask, source_task_id)
        source_quote = db.get(GenerationQuote, source.quote_id)
        original_quote_id = int(source_quote.id)
        original_price_version_id = int(source_quote.price_version_id)
        original_snapshot = deepcopy(source.params["_model_snapshot"])
        original_quote_snapshot = deepcopy(source_quote.request_snapshot)
        assert int(source_quote.estimated_credits) == 13
        source.status = "failed"
        source.error = "known provider failure eligible for requote"
        source.finished_at = datetime.now(timezone.utc)
        db.commit()

    _set_image_catalog_state(model_id, "RequoteB", cost=29)
    retry_request = {"client_request_id": "requote-new-request-001"}
    retried = client.post(
        f"/api/tasks/{source_task_id}/retry/requote",
        headers=headers,
        json=retry_request,
    )
    assert retried.status_code == 200, retried.text
    retried_payload = retried.json()
    new_task_id = int(retried_payload["id"])
    assert new_task_id != source_task_id
    assert int(retried_payload["retry_of_task_id"]) == source_task_id

    with SessionLocal() as db:
        source = db.get(GenTask, source_task_id)
        source_quote = db.get(GenerationQuote, original_quote_id)
        retried_task = db.get(GenTask, new_task_id)
        new_quote = db.get(GenerationQuote, retried_task.quote_id)
        assert source.status == "failed"
        assert int(source.quote_id) == original_quote_id
        assert source.params["_model_snapshot"] == original_snapshot
        assert source_quote.request_snapshot == original_quote_snapshot
        assert int(source_quote.estimated_credits) == 13
        assert int(source_quote.price_version_id) == original_price_version_id

        assert int(retried_task.retry_of_task_id) == source_task_id
        assert int(new_quote.id) != original_quote_id
        assert int(new_quote.estimated_credits) == 29
        assert int(new_quote.price_version_id) != original_price_version_id
        assert new_quote.status == "consumed"
        assert new_quote.request_snapshot["retry"]["source_task_id"] == source_task_id
        assert new_quote.request_snapshot["retry"]["source_quote_id"] == original_quote_id
        assert new_quote.request_snapshot["retry"]["selected_quote_id"] == int(new_quote.id)
        assert retried_task.params["_retry"] == new_quote.request_snapshot["retry"]
        task_count = db.query(GenTask).filter_by(user_id=user_id).count()
        quote_count = db.query(GenerationQuote).filter_by(user_id=user_id).count()

    replay = client.post(
        f"/api/tasks/{source_task_id}/retry/requote",
        headers=headers,
        json=retry_request,
    )
    assert replay.status_code == 200, replay.text
    assert int(replay.json()["id"]) == new_task_id
    with SessionLocal() as db:
        assert db.query(GenTask).filter_by(user_id=user_id).count() == task_count
        assert db.query(GenerationQuote).filter_by(user_id=user_id).count() == quote_count


def test_requote_retry_switches_model_with_new_quote_lineage_and_immutable_source(
    client,
    make_user,
    auth,
    quote_and_generate,
):
    phone = "13981000418"
    user_id = make_user(phone, balance=5000)
    headers = auth(phone)
    source_model_id = _configure_model("image")
    _set_image_catalog_state(source_model_id, "SwitchSource", cost=13)
    target_model_id = _create_temporary_image_model("SwitchTarget", cost=31)
    try:
        source_task_id = _failed_verified_reverse_task(
            quote_and_generate,
            headers,
            user_id=user_id,
            model_config_id=source_model_id,
            suffix="requote-model-switch-001",
            user_edit_count=2,
        )
        with SessionLocal() as db:
            source = db.get(GenTask, source_task_id)
            source.parent_task_id = 987654321
            db.commit()
            db.refresh(source)
            source_quote = db.get(GenerationQuote, source.quote_id)
            original = {
                "params": deepcopy(source.params),
                "status": source.status,
                "parent_task_id": source.parent_task_id,
                "quote_id": int(source.quote_id),
                "quote_snapshot": deepcopy(source_quote.request_snapshot),
                "quote_estimated_credits": int(source_quote.estimated_credits),
                "operation_id": int(source.reverse_operation_id),
                "source_revision_id": int(source.source_revision_id),
                "compiled_revision_id": int(source.compiled_revision_id),
                "generation_revision_id": int(source.generation_revision_id),
            }
            revision_count = db.query(ReverseResultRevision).filter_by(
                operation_id=original["operation_id"]
            ).count()

        retry_request = {
            "client_request_id": "requote-model-switch-new-001",
            "model_config_id": target_model_id,
        }
        retried = client.post(
            f"/api/tasks/{source_task_id}/retry/requote",
            headers=headers,
            json=retry_request,
        )
        assert retried.status_code == 200, retried.text
        retry_task_id = int(retried.json()["id"])
        assert retry_task_id != source_task_id

        with SessionLocal() as db:
            source = db.get(GenTask, source_task_id)
            retry = db.get(GenTask, retry_task_id)
            source_quote = db.get(GenerationQuote, original["quote_id"])
            retry_quote = db.get(GenerationQuote, retry.quote_id)

            assert source.status == original["status"] == "failed"
            assert source.parent_task_id == original["parent_task_id"]
            assert source.params == original["params"]
            assert int(source.quote_id) == original["quote_id"]
            assert source_quote.request_snapshot == original["quote_snapshot"]
            assert int(source_quote.estimated_credits) == original["quote_estimated_credits"]
            assert int(source.compiled_revision_id) == original["compiled_revision_id"]
            assert int(source.generation_revision_id) == original["generation_revision_id"]

            assert int(retry.retry_of_task_id) == source_task_id
            assert int(retry.model_config_id) == target_model_id
            assert retry.parent_task_id is None
            assert int(retry.quote_id) != original["quote_id"]
            assert int(retry_quote.model_config_id) == target_model_id
            assert int(retry_quote.estimated_credits) == 31
            assert int(retry.reverse_operation_id) == original["operation_id"]
            assert int(retry.source_revision_id) == original["source_revision_id"]
            assert int(retry.compiled_revision_id) != original["compiled_revision_id"]
            assert int(retry.generation_revision_id) != original["generation_revision_id"]

            retry_meta = retry.params["_retry"]
            assert retry_meta == retry_quote.request_snapshot["retry"]
            assert retry_meta["source_task_id"] == source_task_id
            assert retry_meta["source_model_config_id"] == source_model_id
            assert retry_meta["selected_model_config_id"] == target_model_id
            assert retry_meta["model_switched"] is True
            assert retry_meta["source_quote_id"] == original["quote_id"]
            assert retry_meta["selected_quote_id"] == int(retry_quote.id)
            assert retry_meta["source_estimated_credits"] == 13
            assert retry_meta["selected_estimated_credits"] == 31
            assert retry_meta["source_route_id"] is not None
            assert retry_meta["selected_route_id"] is not None
            assert retry.params["_model_snapshot"]["model_config_id"] == target_model_id
            assert retry_quote.model_snapshot["model_config_id"] == target_model_id

            compiled = db.get(ReverseResultRevision, retry.compiled_revision_id)
            generation_revision = db.get(ReverseResultRevision, retry.generation_revision_id)
            assert int(compiled.parent_revision_id) == original["source_revision_id"]
            assert int(compiled.payload["generation_task_id"]) == retry_task_id
            assert int(compiled.payload["retry_of_task_id"]) == source_task_id
            assert int(generation_revision.parent_revision_id) == int(compiled.id)
            assert int(generation_revision.payload["generation"]["task_id"]) == retry_task_id
            assert (
                int(generation_revision.payload["generation"]["retry_of_task_id"])
                == source_task_id
            )
            assert db.query(ReverseResultRevision).filter_by(
                operation_id=original["operation_id"]
            ).count() == revision_count + 2
            task_count = db.query(GenTask).filter_by(user_id=user_id).count()
            quote_count = db.query(GenerationQuote).filter_by(user_id=user_id).count()

        conflict = client.post(
            f"/api/tasks/{source_task_id}/retry/requote",
            headers=headers,
            json={
                "client_request_id": retry_request["client_request_id"],
                "model_config_id": source_model_id,
            },
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
        with SessionLocal() as db:
            assert db.query(GenTask).filter_by(user_id=user_id).count() == task_count
            assert db.query(GenerationQuote).filter_by(user_id=user_id).count() == quote_count
    finally:
        _delete_temporary_model(target_model_id)


def test_requote_retry_preserves_multi_hop_chain_with_independent_quotes_and_freezes(
    client,
    make_user,
    auth,
    quote_and_generate,
):
    phone = "13981000419"
    user_id = make_user(phone, balance=5000)
    headers = auth(phone)
    model_id = _configure_model("image")
    _set_image_catalog_state(model_id, "ChainA", cost=13)
    created = quote_and_generate(
        headers=headers,
        payload={
            "client_request_id": "requote-chain-source-001",
            "category": "image",
            "stage": "preview",
            "instruction": "multi hop requote chain",
            "params": {"n": 1, "size": "1024x1024"},
            "model_config_id": model_id,
        },
    )
    assert created.status_code == 200, created.text
    source_id = int(created.json()["id"])
    with SessionLocal() as db:
        source = db.get(GenTask, source_id)
        source.status = "failed"
        source.error = "known source failure"
        source.finished_at = datetime.now(timezone.utc)
        db.commit()

    _set_image_catalog_state(model_id, "ChainB", cost=17)
    first_retry = client.post(
        f"/api/tasks/{source_id}/retry/requote",
        headers=headers,
        json={"client_request_id": "requote-chain-first-001"},
    )
    assert first_retry.status_code == 200, first_retry.text
    first_retry_id = int(first_retry.json()["id"])
    with SessionLocal() as db:
        first = db.get(GenTask, first_retry_id)
        first.status = "failed"
        first.error = "known first retry failure"
        first.finished_at = datetime.now(timezone.utc)
        db.commit()

    _set_image_catalog_state(model_id, "ChainC", cost=23)
    second_retry = client.post(
        f"/api/tasks/{first_retry_id}/retry/requote",
        headers=headers,
        json={"client_request_id": "requote-chain-second-001"},
    )
    assert second_retry.status_code == 200, second_retry.text
    second_retry_id = int(second_retry.json()["id"])

    with SessionLocal() as db:
        tasks = {
            task_id: db.get(GenTask, task_id)
            for task_id in (source_id, first_retry_id, second_retry_id)
        }
        assert tasks[source_id].retry_of_task_id is None
        assert int(tasks[first_retry_id].retry_of_task_id) == source_id
        assert int(tasks[second_retry_id].retry_of_task_id) == first_retry_id

        traversed: list[int] = []
        current = tasks[second_retry_id]
        while current is not None:
            traversed.append(int(current.id))
            current = (
                db.get(GenTask, int(current.retry_of_task_id))
                if current.retry_of_task_id is not None
                else None
            )
        assert traversed == [second_retry_id, first_retry_id, source_id]

        quotes = [db.get(GenerationQuote, tasks[task_id].quote_id) for task_id in traversed[::-1]]
        assert [int(quote.estimated_credits) for quote in quotes] == [13, 17, 23]
        assert len({int(quote.id) for quote in quotes}) == 3
        assert len({int(quote.price_version_id) for quote in quotes}) == 3
        assert all(quote.status == "consumed" for quote in quotes)
        assert [int(quote.task_id) for quote in quotes] == [
            source_id,
            first_retry_id,
            second_retry_id,
        ]
        assert [int(tasks[task_id].cost_frozen) for task_id in traversed[::-1]] == [13, 17, 23]

        freeze_rows = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.user_id == user_id,
                CreditTransaction.type == "freeze",
                CreditTransaction.biz_type == "gen_task",
                CreditTransaction.biz_ref.in_(traversed),
            )
            .all()
        )
        frozen_by_task = {
            int(row.biz_ref): int(row.frozen_delta or -row.change)
            for row in freeze_rows
        }
        assert frozen_by_task == {
            source_id: 13,
            first_retry_id: 17,
            second_retry_id: 23,
        }


def test_quote_snapshot_survives_catalog_drift_through_submit_and_retry(
    client,
    make_user,
    auth,
    monkeypatch,
):
    phone = "13981000408"
    make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    _set_image_catalog_state(model_id, "A", cost=13)
    payload = {
        "category": "image",
        "stage": "preview",
        "instruction": "immutable quote snapshot",
        "params": {"n": 1, "size": "1024x1024"},
        "model_config_id": model_id,
    }
    quoted = client.post("/api/quotes", headers=headers, json=payload)
    assert quoted.status_code == 201, quoted.text
    quote_id = int(quoted.json()["quote_id"])
    with SessionLocal() as db:
        quote = db.get(GenerationQuote, quote_id)
        snapshot_a = deepcopy(quote.model_snapshot)
        capability_version_id = int(quote.capability_version_id)
        price_version_id = int(quote.price_version_id)
    assert snapshot_a["model_id"] == "quote-snapshot-model-a"
    assert snapshot_a["extra"]["capabilities"]["catalog_marker"] == "A"
    assert snapshot_a["extra"]["generation_path"] == "/v1/a/images/generations"
    assert snapshot_a["extra"]["image_payload"]["field_map"]["prompt"] == "a_prompt"
    assert snapshot_a["extra"]["prompt_profile"]["name"] == "profile-a"
    assert snapshot_a["capability_version_id"] == capability_version_id
    assert snapshot_a["price_version_id"] == price_version_id

    seen: list[dict] = []
    monkeypatch.setattr(generation_runtime.gateway.settings, "mock_mode", False)

    def capture_image_post(path, outbound_payload, n, config=None):
        seen.append(
            {
                "path": path,
                "payload": deepcopy(outbound_payload),
                "provider": config.provider,
                "base_url": config.base_url,
                "gateway_format": config.gateway_format,
                "source": config.source,
            }
        )
        return generation_runtime.gateway.ImageBatchResult(
            [
                generation_runtime.gateway._mock_image("captured", "1024x1024", index)
                for index in range(n)
            ]
        )

    monkeypatch.setattr(
        generation_runtime.gateway,
        "_post_single_image_repeated",
        capture_image_post,
    )
    _set_image_catalog_state(
        model_id,
        "B",
        text_to_image=False,
        cost=97,
        provider="openrouter",
        gateway_format="anthropic",
    )
    submitted = client.post(
        "/api/generate",
        headers=headers,
        json={**payload, "quote_id": quote_id},
    )
    assert submitted.status_code == 200, submitted.text
    task_id = int(submitted.json()["id"])
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        assert task.params["_model_snapshot"] == snapshot_a
        assert task.params["_quote"]["capability_version_id"] == capability_version_id
        assert task.params["_quote"]["price_version_id"] == price_version_id
        assert int(task.cost_frozen) == 13
        task.status = "failed"
        task.error = "known failure after quote A submission"
        task.finished_at = datetime.now(timezone.utc)
        db.commit()

    _set_image_catalog_state(
        model_id,
        "C",
        text_to_image=False,
        cost=131,
        provider="grok",
        gateway_format="ark",
    )
    retried = client.post(f"/api/tasks/{task_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        assert task.params["_model_snapshot"] == snapshot_a
        assert task.params["_quote"]["capability_version_id"] == capability_version_id
        assert task.params["_quote"]["price_version_id"] == price_version_id
        assert int(task.cost_frozen) == 13
        assert int(task.cost_settled) == 13

    assert len(seen) == 2
    for call in seen:
        assert call["path"] == "/v1/a/images/generations"
        assert call["provider"] == "custom_openai"
        assert call["base_url"] == "https://quote-a.example.com/v1"
        assert call["gateway_format"] == "openai"
        assert call["source"] == "model"
        assert call["payload"]["a_model"] == "quote-snapshot-model-a"
        assert call["payload"]["a_prompt"].startswith("A-PREFIX\n")
        assert "immutable quote snapshot" in call["payload"]["a_prompt"]
        assert call["payload"]["catalog_marker"] == "A"
        assert "model" not in call["payload"]
        assert "prompt" not in call["payload"]
        assert "field_map" not in call["payload"]


@pytest.mark.parametrize("tamper", ["path", "field_map"])
def test_invalid_frozen_adapter_quote_has_zero_submission_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
    tamper,
):
    phone = "13981000413" if tamper == "path" else "13981000414"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    _set_image_catalog_state(model_id, "A", cost=19)
    payload = {
        "category": "image",
        "stage": "preview",
        "instruction": "invalid frozen outbound adapter",
        "params": {"n": 1, "size": "1024x1024"},
        "model_config_id": model_id,
    }
    quoted = client.post("/api/quotes", headers=headers, json=payload)
    assert quoted.status_code == 201, quoted.text
    quote_id = int(quoted.json()["quote_id"])
    with SessionLocal() as db:
        quote = db.get(GenerationQuote, quote_id)
        snapshot = deepcopy(quote.model_snapshot)
        if tamper == "path":
            snapshot["extra"]["generation_path"] = "https://evil.example.com/submit"
        else:
            snapshot["extra"]["image_payload"]["field_map"]["prompt"] = ""
        quote.model_snapshot = snapshot
        db.commit()

    published = []
    monkeypatch.setattr(
        generation_jobs,
        "enqueue_with_request_context",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        before = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
            "gateway_calls": db.query(GatewayCall).filter_by(user_id=user_id).count(),
        }

    response = client.post(
        "/api/generate",
        headers=headers,
        json={**payload, "quote_id": quote_id},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "QUOTE_SNAPSHOT_INVALID"
    with SessionLocal() as db:
        user = db.get(User, user_id)
        quote = db.get(GenerationQuote, quote_id)
        after = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
            "gateway_calls": db.query(GatewayCall).filter_by(user_id=user_id).count(),
        }
        assert quote.status == "active"
        assert quote.task_id is None
    assert after == before
    assert published == []


@pytest.mark.parametrize("collision", ["configured", "unmapped"])
def test_catalog_field_map_collision_has_zero_quote_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
    collision,
):
    variant = {
        "configured": "1",
        "unmapped": "2",
    }[collision]
    phone = f"1398100050{variant}"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    _set_image_catalog_state(model_id, "A", cost=23)
    with SessionLocal() as db:
        model = db.get(ModelConfig, model_id)
        extra = deepcopy(model.extra)
        payload_config = deepcopy(extra["image_payload"])
        if collision == "configured":
            payload_config["seed"] = 7
            payload_config["field_map"] = {"prompt": "seed"}
        else:
            payload_config["field_map"] = {"model": "prompt"}
        extra["image_payload"] = payload_config
        model.extra = extra
        db.commit()

    published = []
    monkeypatch.setattr(
        generation_jobs,
        "enqueue_with_request_context",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        before = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "quotes": db.query(GenerationQuote).filter_by(user_id=user_id).count(),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
            "gateway_calls": db.query(GatewayCall).filter_by(user_id=user_id).count(),
        }
    payload = {
        "category": "image",
        "stage": "preview",
        "instruction": "reject colliding adapter before side effects",
        "params": {"n": 1, "size": "1024x1024"},
        "model_config_id": model_id,
    }

    response = client.post("/api/quotes", headers=headers, json=payload)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "QUOTE_SNAPSHOT_INVALID"
    with SessionLocal() as db:
        user = db.get(User, user_id)
        after = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "quotes": db.query(GenerationQuote).filter_by(user_id=user_id).count(),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
            "gateway_calls": db.query(GatewayCall).filter_by(user_id=user_id).count(),
        }
    assert after == before
    assert published == []


def test_unsafe_video_request_query_path_has_zero_quote_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
):
    phone = "13981000505"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("video")
    with SessionLocal() as db:
        model = db.get(ModelConfig, model_id)
        extra = deepcopy(model.extra) if isinstance(model.extra, dict) else {}
        extra["request_query_path"] = "https://evil.example.com/tasks/{request_id}"
        model.extra = extra
        db.commit()
    published = []
    monkeypatch.setattr(
        generation_jobs,
        "enqueue_with_request_context",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        before = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "quotes": db.query(GenerationQuote).filter_by(user_id=user_id).count(),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
        }

    response = client.post(
        "/api/quotes",
        headers=headers,
        json={
            "category": "video",
            "stage": "preview",
            "instruction": "reject unsafe frozen request lookup route",
            "params": {"duration": 2, "resolution": "720p", "ratio": "16:9"},
            "model_config_id": model_id,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "QUOTE_SNAPSHOT_INVALID"
    with SessionLocal() as db:
        user = db.get(User, user_id)
        after = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "quotes": db.query(GenerationQuote).filter_by(user_id=user_id).count(),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
        }
    assert after == before
    assert published == []


@pytest.mark.parametrize("unavailable", ["secret", "disabled"])
def test_quote_rejects_unexecutable_frozen_gateway_without_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
    unavailable,
):
    phone = "13981000409" if unavailable == "secret" else "13981000410"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    _set_image_catalog_state(model_id, "A", cost=17)
    payload = {
        "category": "image",
        "stage": "preview",
        "instruction": "reject unavailable quote gateway",
        "params": {"n": 1, "size": "1024x1024"},
        "model_config_id": model_id,
    }
    quoted = client.post("/api/quotes", headers=headers, json=payload)
    assert quoted.status_code == 201, quoted.text
    quote_id = int(quoted.json()["quote_id"])
    _set_image_catalog_state(
        model_id,
        "B",
        api_key=(
            "quote-snapshot-rotated-secret"
            if unavailable == "secret"
            else "quote-snapshot-stable-secret"
        ),
        enabled=unavailable != "disabled",
        cost=73,
    )
    published: list[tuple] = []
    monkeypatch.setattr(
        generation_jobs,
        "enqueue_with_request_context",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        before = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
            "gateway_calls": db.query(GatewayCall).filter_by(user_id=user_id).count(),
        }

    response = client.post(
        "/api/generate",
        headers=headers,
        json={**payload, "quote_id": quote_id},
    )
    assert response.status_code == 409, response.text
    expected_code = (
        "MODEL_GATEWAY_SNAPSHOT_STALE" if unavailable == "secret" else "QUOTE_GATEWAY_UNAVAILABLE"
    )
    assert response.json()["detail"]["code"] == expected_code
    with SessionLocal() as db:
        user = db.get(User, user_id)
        quote = db.get(GenerationQuote, quote_id)
        after = {
            "balance": int(user.balance_credits),
            "frozen": int(user.frozen_credits),
            "tasks": db.query(GenTask).filter_by(user_id=user_id).count(),
            "credits": db.query(CreditTransaction).filter_by(user_id=user_id).count(),
            "gateway_calls": db.query(GatewayCall).filter_by(user_id=user_id).count(),
        }
        assert quote.status == "active"
        assert quote.task_id is None
    assert after == before
    assert published == []


@pytest.mark.parametrize("tamper", ["capability_version", "snapshot_capabilities"])
def test_retry_rejects_quote_task_version_drift_without_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
    tamper,
    quote_and_generate,
):
    phone = "13981000411" if tamper == "capability_version" else "13981000412"
    make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    created = quote_and_generate(
        headers=headers,
        payload={
            "category": "image",
            "stage": "preview",
            "instruction": "quote task version integrity",
            "params": {"n": 1, "size": "256x256"},
            "model_config_id": model_id,
        },
    )
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        params = deepcopy(task.params)
        if tamper == "capability_version":
            params["_quote"]["capability_version_id"] += 1000
        else:
            params["_model_snapshot"]["extra"]["capabilities"]["text_to_image"] = False
        task.params = params
        task.status = "failed"
        task.error = "known failure with tampered quote metadata"
        task.finished_at = datetime.now(timezone.utc)
        db.commit()

    _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=409,
        message="报价元数据与冻结模型快照不一致",
    )


@pytest.mark.parametrize(
    ("tamper", "phone"),
    [
        ("legacy_unverified", "13981000601"),
        ("missing_hash", "13981000602"),
        ("payload", "13981000603"),
        ("broken_chain", "13981000604"),
        ("owner", "13981000605"),
    ],
)
def test_reverse_retry_revalidates_persisted_lineage_without_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
    tamper,
    phone,
    quote_and_generate,
):
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    task_id = _failed_verified_reverse_task(
        quote_and_generate,
        headers,
        user_id=user_id,
        model_config_id=model_id,
        suffix=f"reject-{tamper}-001",
    )

    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        revisions = (
            db.query(ReverseResultRevision)
            .filter_by(operation_id=task.reverse_operation_id)
            .order_by(ReverseResultRevision.version)
            .all()
        )
        by_source = {revision.source: revision for revision in revisions}
        if tamper == "legacy_unverified":
            for revision in revisions:
                revision.lineage_status = reverse_lineage.LEGACY_UNVERIFIED
                revision.source_content_hash = None
                revision.source_fingerprints = None
                revision.payload_hash = None
        elif tamper == "missing_hash":
            by_source["applied"].payload_hash = None
        elif tamper == "payload":
            by_source["applied"].payload = {
                **by_source["applied"].payload,
                "final_text": "tampered after generation",
            }
        elif tamper == "broken_chain":
            by_source["model_compiled"].parent_revision_id = by_source["provider_raw"].id
        else:
            other_user_id = make_user("13981000699", balance=1000)
            operation = db.get(ReverseOperation, task.reverse_operation_id)
            operation.user_id = other_user_id
        db.commit()

    response = _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=409,
        message="任务反推血缘无效",
    )
    assert response.json()["detail"]["code"] == "RETRY_LINEAGE_INVALID"


def test_retry_rejects_param_only_migrated_reverse_lineage_without_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
):
    phone = "13981000606"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    operation_id, applied_id = _seed_verified_reverse_result(
        user_id,
        "param-only-migrated-001",
    )
    task_id = _failed_task(
        user_id=user_id,
        model_config_id=model_id,
        category="image",
        params={
            "_reverse_lineage": {
                "operation_id": operation_id,
                "source_revision_id": applied_id,
                "compiled_revision_id": applied_id,
                "generation_revision_id": applied_id,
            },
        },
    )

    response = _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        task_id,
        status=409,
        message="缺少持久化的完整反推版本链",
    )
    assert response.json()["detail"]["code"] == "RETRY_LINEAGE_INVALID"


def test_retry_rejects_cross_operation_chain_even_when_task_params_match(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    phone = "13981000607"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    first_task_id = _failed_verified_reverse_task(
        quote_and_generate,
        headers,
        user_id=user_id,
        model_config_id=model_id,
        suffix="cross-operation-first-001",
    )
    second_task_id = _failed_verified_reverse_task(
        quote_and_generate,
        headers,
        user_id=user_id,
        model_config_id=model_id,
        suffix="cross-operation-second-001",
    )

    with SessionLocal() as db:
        first = db.get(GenTask, first_task_id)
        second = db.get(GenTask, second_task_id)
        second_compiled_id = int(second.compiled_revision_id)
        second_generation_id = int(second.generation_revision_id)
        second.reverse_operation_id = None
        second.source_revision_id = None
        second.compiled_revision_id = None
        second.generation_revision_id = None
        db.flush()

        first.compiled_revision_id = second_compiled_id
        first.generation_revision_id = second_generation_id
        params = deepcopy(first.params)
        params["_reverse_lineage"] = {
            "operation_id": int(first.reverse_operation_id),
            "source_revision_id": int(first.source_revision_id),
            "compiled_revision_id": second_compiled_id,
            "generation_revision_id": second_generation_id,
        }
        first.params = params
        db.commit()

    response = _post_rejected_retry(
        client,
        headers,
        monkeypatch,
        first_task_id,
        status=409,
        message="任务反推血缘无效",
    )
    assert response.json()["detail"]["code"] == "RETRY_LINEAGE_INVALID"


def test_verified_reverse_retry_preserves_lineage_and_continues(
    client,
    make_user,
    auth,
    quote_and_generate,
):
    phone = "13981000608"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    model_id = _configure_model("image")
    task_id = _failed_verified_reverse_task(
        quote_and_generate,
        headers,
        user_id=user_id,
        model_config_id=model_id,
        suffix="verified-continues-001",
    )
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        expected_ids = {
            "operation_id": int(task.reverse_operation_id),
            "source_revision_id": int(task.source_revision_id),
            "compiled_revision_id": int(task.compiled_revision_id),
            "generation_revision_id": int(task.generation_revision_id),
        }

    retried = client.post(f"/api/tasks/{task_id}/retry", headers=headers)

    assert retried.status_code == 200, retried.text
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        assert task.params["_reverse_lineage"] == expected_ids
        assert int(task.reverse_operation_id) == expected_ids["operation_id"]
        assert int(task.source_revision_id) == expected_ids["source_revision_id"]
        assert int(task.compiled_revision_id) == expected_ids["compiled_revision_id"]
        assert int(task.generation_revision_id) == expected_ids["generation_revision_id"]
        generation_revision = db.get(
            ReverseResultRevision,
            expected_ids["generation_revision_id"],
        )
        chain = reverse_lineage.validate_revision_chain(
            db,
            generation_revision,
            terminal_source="generation",
        )
        assert [revision.source for revision in chain] == [
            "generation",
            "model_compiled",
            "applied",
            "user_edit",
            "normalized",
            "provider_raw",
        ]


def test_requote_retry_creates_fresh_reverse_generation_lineage_after_consecutive_edits(
    client,
    make_user,
    auth,
    quote_and_generate,
):
    phone = "13981000609"
    user_id = make_user(phone, balance=2000)
    headers = auth(phone)
    model_id = _configure_model("image")
    source_task_id = _failed_verified_reverse_task(
        quote_and_generate,
        headers,
        user_id=user_id,
        model_config_id=model_id,
        suffix="requote-consecutive-edits-001",
        user_edit_count=2,
    )
    with SessionLocal() as db:
        source = db.get(GenTask, source_task_id)
        original_ids = {
            "operation_id": int(source.reverse_operation_id),
            "source_revision_id": int(source.source_revision_id),
            "compiled_revision_id": int(source.compiled_revision_id),
            "generation_revision_id": int(source.generation_revision_id),
            "quote_id": int(source.quote_id),
        }
        revision_count = (
            db.query(ReverseResultRevision)
            .filter_by(operation_id=original_ids["operation_id"])
            .count()
        )

    retried = client.post(
        f"/api/tasks/{source_task_id}/retry/requote",
        headers=headers,
        json={"client_request_id": "requote-consecutive-edits-new-001"},
    )
    assert retried.status_code == 200, retried.text
    retried_task_id = int(retried.json()["id"])
    assert retried_task_id != source_task_id

    with SessionLocal() as db:
        source = db.get(GenTask, source_task_id)
        retry = db.get(GenTask, retried_task_id)
        assert int(retry.retry_of_task_id) == source_task_id
        assert int(retry.reverse_operation_id) == original_ids["operation_id"]
        assert int(retry.source_revision_id) == original_ids["source_revision_id"]
        assert int(retry.compiled_revision_id) != original_ids["compiled_revision_id"]
        assert int(retry.generation_revision_id) != original_ids["generation_revision_id"]
        assert int(retry.quote_id) != original_ids["quote_id"]
        assert int(source.compiled_revision_id) == original_ids["compiled_revision_id"]
        assert int(source.generation_revision_id) == original_ids["generation_revision_id"]
        assert (
            db.query(ReverseResultRevision)
            .filter_by(operation_id=original_ids["operation_id"])
            .count()
            == revision_count + 2
        )

        compiled = db.get(ReverseResultRevision, retry.compiled_revision_id)
        generation_revision = db.get(ReverseResultRevision, retry.generation_revision_id)
        assert compiled.source == "model_compiled"
        assert int(compiled.parent_revision_id) == original_ids["source_revision_id"]
        assert int(compiled.payload["generation_task_id"]) == retried_task_id
        assert int(compiled.payload["retry_of_task_id"]) == source_task_id
        assert generation_revision.source == "generation"
        assert int(generation_revision.parent_revision_id) == int(compiled.id)
        assert int(generation_revision.payload["generation"]["task_id"]) == retried_task_id
        assert (
            int(generation_revision.payload["generation"]["retry_of_task_id"])
            == source_task_id
        )

        chain = reverse_lineage.validate_revision_chain(
            db,
            generation_revision,
            terminal_source="generation",
        )
        assert [revision.source for revision in chain] == [
            "generation",
            "model_compiled",
            "applied",
            "user_edit",
            "user_edit",
            "normalized",
            "provider_raw",
        ]
