from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (
    CreditTransaction,
    ReverseOperation,
    ReverseResultRevision,
    User,
    UserPrompt,
)
from app.routers import prompt
from app.services import credits, gateway, reverse_lineage, reverse_operations
from tests.reverse_helpers import post_reverse, post_reverse_retry


def _operation_events(caplog):
    return [
        json.loads(record.getMessage().split("=", 1)[1])
        for record in caplog.records
        if record.getMessage().startswith("reverse_operation_event=")
    ]


def _stub_reverse_dependencies(monkeypatch, *, result=None):
    payload = result or {
        "structured": {"主体": "红色杯子", "场景背景": "白色背景"},
        "final_text": "红色杯子,白色背景",
    }
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        prompt, "_gateway_ref", lambda *_args, **_kwargs: "data:image/jpeg;base64,YQ=="
    )
    monkeypatch.setattr(reverse_operations, "assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_operations.usage, "record_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_operations, "_remember_history", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway, "reverse_prompt", lambda *_args, **_kwargs: dict(payload))


def _stub_quoted_provider_cost(monkeypatch, policy: dict) -> None:
    build_snapshot = reverse_operations.reverse_pricing_snapshot

    def with_provider_cost(body, reverse_pricing):
        snapshot = build_snapshot(body, reverse_pricing)
        snapshot["provider_cost_credits"] = dict(policy)
        return snapshot

    monkeypatch.setattr(reverse_operations, "reverse_pricing_snapshot", with_provider_cost)


def test_workspace_snapshot_preserves_video_duration():
    snapshot = reverse_operations.sanitize_workspace_snapshot(
        {
            "selected": {
                "type": "video",
                "url": "https://cdn.example.com/source.mp4",
                "duration": 6.016,
            },
        },
        version=3,
    )

    assert snapshot["selected"]["duration"] == 6.016


def test_provider_cost_detail_uses_actual_visual_path_and_audio_evidence():
    snapshot = {
        "provider_cost_credits": {
            "image_cost": 2,
            "video_preset_costs": {"standard": 4, "fine": 6},
            "audio_surcharge": 1,
        }
    }

    assert reverse_operations._provider_cost_call_detail(
        snapshot,
        contract_target="image_to_video",
        preset="fine",
        audio_evidence=False,
    ) == {
        "provider_cost_status": "complete",
        "provider_cost_credits": 2,
    }
    assert reverse_operations._provider_cost_call_detail(
        snapshot,
        contract_target="video",
        preset="fine",
        audio_evidence=True,
    ) == {
        "provider_cost_status": "complete",
        "provider_cost_credits": 7,
    }
    assert reverse_operations._provider_cost_call_detail(
        {
            "provider_cost_credits": {
                "video_preset_costs": {"fine": 6},
            }
        },
        contract_target="video",
        preset="fine",
        audio_evidence=True,
    ) == {
        "provider_cost_status": "partial",
        "provider_cost_known_credits": 6,
    }
    assert reverse_operations._provider_cost_call_detail(
        {"provider_cost_credits": {"image_cost": 0}},
        contract_target="image",
        preset="standard",
        audio_evidence=False,
    ) == {
        "provider_cost_status": "complete",
        "provider_cost_credits": 0,
    }
    assert reverse_operations._provider_cost_call_detail(
        {},
        contract_target="image",
        preset="standard",
        audio_evidence=False,
    ) == {"provider_cost_status": "unavailable"}


def test_local_video_range_is_rejected_before_operation_and_credit_freeze(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000998", balance=100)
    headers = auth("13710000998")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(prompt.storage, "key_from_url", lambda _url: "upload_video/local.mp4")
    monkeypatch.setattr(
        prompt.asset_refs,
        "generated_video_reference_path",
        lambda *_args, **_kwargs: "/tmp/local-video.mp4",
    )
    monkeypatch.setattr(
        prompt.video_frames,
        "probe_media",
        lambda _path: {"duration_seconds": 6.016, "width": 960, "height": 540},
    )
    body = {
        "client_request_id": "local-video-invalid-range-001",
        "asset_url": "http://testserver/api/uploads/upload_video/local.mp4",
        "target": "video",
        "source_type": "video",
        "source_ranges": [
            {"start_seconds": 0, "end_seconds": 2},
            {"start_seconds": 3, "end_seconds": 7},
        ],
    }

    response = post_reverse(client, body, headers=headers)

    assert response.status_code == 400, response.text
    assert "结束时间 7 秒超过素材时长" in response.text
    with SessionLocal() as db:
        assert (
            db.query(ReverseOperation)
            .filter_by(client_request_id=body["client_request_id"])
            .count()
            == 0
        )
        user = db.get(User, uid)
        assert user.balance_credits == 100
        assert user.frozen_credits == 0


def test_async_reverse_success_freezes_then_settles_and_replays(
    client, make_user, auth, monkeypatch, quote_reverse
):
    uid = make_user("13710000001", balance=100)
    headers = auth("13710000001")
    _stub_reverse_dependencies(monkeypatch)
    body = {
        "client_request_id": "async-reverse-success-001",
        "asset_url": "https://cdn.example.com/cup.jpg",
        "target": "image",
        "source_type": "image",
        "workspace_snapshot_v2": {
            "version": 2,
            "creation_mode": "image",
            "subject_mode": "general",
            "selected": {"type": "image", "url": "https://cdn.example.com/cup.jpg"},
            "structured": {"主体": "工作区杯子"},
            "final_text": "工作区手工提示词",
            "video_analysis": {"analysis_mode": "image_motion"},
            "portrait_profile": {"脸型五官": "椭圆脸"},
        },
    }

    quote = quote_reverse(body, headers=headers)
    quoted_body = {**body, "quote_id": quote["quote_id"]}
    first = client.post("/api/prompt/reverse-operations", json=quoted_body, headers=headers)
    assert first.status_code == 202, first.text
    data = first.json()
    assert data["status"] == "succeeded"
    assert data["cost_frozen"] == 0
    assert data["cost_settled"] == 5
    assert data["charged_credits"] == 5
    assert data["result_schema_version"] == "reverse.v3"
    assert data["result"]["final_text"] == "红色杯子；白色背景"
    assert data["result"]["provider_final_text"] == "红色杯子,白色背景"
    assert data["result"]["image_evidence"] == []
    assert data["timestamps"] == {
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "started_at": data["started_at"],
        "finished_at": data["finished_at"],
    }
    assert all(data["timestamps"].values())
    assert "structured" not in data["workspace_snapshot_v2"]
    assert "final_text" not in data["workspace_snapshot_v2"]
    assert "video_analysis" not in data["workspace_snapshot_v2"]
    assert data["workspace_snapshot_v2"]["portrait_profile"] == {"脸型五官": "椭圆脸"}

    replay = client.post("/api/prompt/reverse-operations", json=quoted_body, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["id"] == data["id"]

    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, data["id"])
        assert operation.status == "succeeded"
        assert "base_url" not in operation.model_snapshot
        assert "key_fingerprint" not in operation.model_snapshot
        assert "IMAGE_REVERSE_TEMPLATE" not in str(operation.template_snapshot)
        assert operation.template_snapshot["templates"]["image"]
        transactions = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation.id,
            )
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [transaction.type for transaction in transactions] == ["freeze", "settle"]
        assert db.get(User, uid).balance_credits == 95
        assert db.get(User, uid).frozen_credits == 0
    finally:
        db.close()


def test_legacy_reverse_success_and_replay_preserve_image_evidence(
    client, make_user, auth, monkeypatch
):
    make_user("13710000031", balance=100)
    headers = auth("13710000031")
    evidence = {
        "evidence_type": "ocr",
        "bbox": {"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.1},
        "field_key": "文字版式",
        "evidence_text": "ACME",
        "confidence": 0.96,
        "source_index": 1,
        "fact_status": "visible",
        "protected": True,
        "editable": False,
    }
    _stub_reverse_dependencies(
        monkeypatch,
        result={
            "structured": {"主体": "白色包装盒"},
            "final_text": "白色包装盒，正面 ACME 字样",
            "image_evidence": [evidence],
        },
    )
    body = {
        "client_request_id": "legacy-reverse-image-evidence-001",
        "asset_url": "https://cdn.example.com/legacy-evidence.jpg",
        "target": "image",
        "source_type": "image",
    }

    first = client.post("/api/prompt/reverse", json=body, headers=headers)
    replay = client.post("/api/prompt/reverse", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["image_evidence"] == [evidence]
    assert replay.json()["image_evidence"] == [evidence]


def test_create_result_revision_converts_unique_version_race_to_conflict(make_user, monkeypatch):
    user_id = make_user("13710000032", balance=100)
    asset_url = "https://cdn.example.com/revision-race.jpg"
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=f"{user_id:064d}"[-64:],
            target="image",
            asset_url=asset_url,
            request_context={
                "source_type": "image",
                "sources": [{"asset_url": asset_url, "source_type": "image"}],
            },
            status="succeeded",
            progress=100,
        )
        db.add(operation)
        db.flush()
        fingerprint = reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(b"revision-race-source"),
            locator=asset_url,
            method="test_source_sha256",
        )
        fingerprints = [fingerprint]
        source_content_hash = reverse_lineage.source_content_hash(fingerprints)
        normalized_payload = {"final_text": "race", "structured": {}}
        provider_payload = {"provider": "test", "result": normalized_payload}
        parent_id = None
        for version, source, revision_payload in (
            (1, "provider_raw", provider_payload),
            (2, "normalized", normalized_payload),
            (3, "user_edit", normalized_payload),
        ):
            revision = ReverseResultRevision(
                operation_id=operation.id,
                user_id=user_id,
                version=version,
                source=source,
                payload=revision_payload,
                parent_revision_id=parent_id,
                source_content_hash=source_content_hash,
                source_fingerprints=fingerprints,
                payload_hash=reverse_lineage.canonical_payload_hash(revision_payload),
                lineage_status=reverse_lineage.VERIFIED,
                evidence_review_action="not_applicable",
            )
            db.add(revision)
            db.flush()
            parent_id = int(revision.id)
        db.commit()
        operation_id = int(operation.id)
        user_edit_id = int(parent_id)

    with SessionLocal() as db:
        real_flush = db.flush

        def conflict_flush(*_args, **_kwargs):
            raise IntegrityError(
                "INSERT INTO reverse_result_revisions",
                {},
                RuntimeError("uq_reverse_result_revisions_operation_version"),
            )

        monkeypatch.setattr(db, "flush", conflict_flush)
        with pytest.raises(
            reverse_operations.ReverseOperationConflict,
            match="并发更新",
        ):
            reverse_operations.create_result_revision(
                db,
                operation_id=operation_id,
                user_id=user_id,
                source="applied",
                payload={},
                parent_revision_id=user_edit_id,
            )

        monkeypatch.setattr(db, "flush", real_flush)
        assert db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count() == 3
        assert db.get(ReverseOperation, operation_id).applied_result_version is None


def test_reverse_v3_multi_reference_intent_versions_feedback_and_retry(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000991", balance=200)
    headers = auth("13710000991")
    seen = {}

    def reverse_stub(refs, *_args, **kwargs):
        seen["refs"] = list(refs)
        seen["reference_context"] = kwargs.get("reference_context")
        seen["template_override"] = kwargs.get("template_override")
        return {
            "structured": {"主体": "银色香水瓶", "风格": "冷白商业摄影"},
            "final_text": "银色香水瓶，冷白商业摄影",
            "image_evidence": [
                {
                    "evidence_type": "subject_protection",
                    "bbox": {"x": 0.25, "y": 0.1, "width": 0.5, "height": 0.8},
                    "field_key": "主体",
                    "evidence_text": "银色香水瓶完整主体",
                    "confidence": 0.98,
                    "source_index": 1,
                    "fact_status": "visible",
                    "protected": True,
                    "editable": False,
                }
            ],
        }

    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(gateway, "reverse_prompt", reverse_stub)
    body = {
        "client_request_id": "reverse-v3-complete-001",
        "target": "image",
        "sources": [
            {"asset_url": "https://cdn.example.com/main.jpg", "role": "primary"},
            {"asset_url": "https://cdn.example.com/style.jpg", "role": "style"},
            {"asset_url": "https://cdn.example.com/layout.jpg", "role": "composition"},
        ],
        "analysis_focus": "product_ad",
        "analysis_precision": "fine",
        "output_purpose": "generation",
        "custom_instruction": "重点识别瓶身材质和高光位置",
        "workspace_snapshot_v3": {
            "version": 3,
            "creation_mode": "image",
            "analysis_focus": "product_ad",
        },
    }
    response = post_reverse(client, body, headers=headers)
    assert response.status_code == 202, response.text
    operation = response.json()
    assert operation["analysis_focus"] == "product_ad"
    assert operation["analysis_precision"] == "fine"
    assert operation["workspace_snapshot_v3"]["version"] == 3
    assert operation["result_schema_version"] == "reverse.v3"
    assert operation["result"]["image_evidence"][0]["evidence_type"] == "subject_protection"
    assert operation["result"]["image_evidence"][0]["source_index"] == 1
    assert len(seen["refs"]) == 3
    assert [item["role"] for item in seen["reference_context"]] == [
        "primary",
        "style",
        "composition",
    ]
    assert "用户补充关注点" in seen["template_override"]
    assert "不得改变JSON契约" in seen["template_override"]

    conflict = post_reverse(
        client,
        {**body, "analysis_focus": "style"},
        headers=headers,
    )
    assert conflict.status_code == 409

    revisions = client.get(
        f"/api/prompt/reverse-operations/{operation['id']}/revisions", headers=headers
    )
    assert revisions.status_code == 200
    assert [(item["version"], item["source"]) for item in revisions.json()] == [
        (1, "provider_raw"),
        (2, "normalized"),
    ]
    assert revisions.json()[1]["payload"]["image_evidence"] == operation["result"]["image_evidence"]
    reviewed_evidence = [
        {
            **item,
            "evidence_id": f"evidence-{item['source_index']}-{index}",
            "review_status": "confirmed",
        }
        for index, item in enumerate(operation["result"]["image_evidence"], start=1)
    ]
    edited = client.post(
        f"/api/prompt/reverse-operations/{operation['id']}/revisions",
        json={
            "source": "user_edit",
            "payload": {
                "final_text": "用户修改稿",
                "structured": {},
                "image_evidence": reviewed_evidence,
            },
        },
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 3
    applied = client.post(
        f"/api/prompt/reverse-operations/{operation['id']}/revisions",
        json={
            "source": "applied",
            "parent_revision_id": edited.json()["id"],
            "payload": {},
        },
        headers=headers,
    )
    assert applied.status_code == 200
    assert applied.json()["version"] == 4
    for server_owned_source in ("provider_raw", "normalized", "model_compiled", "generation"):
        forged = client.post(
            f"/api/prompt/reverse-operations/{operation['id']}/revisions",
            json={"source": server_owned_source, "payload": {"final_text": "伪造版本"}},
            headers=headers,
        )
        assert forged.status_code == 422, (server_owned_source, forged.text)
    unchanged_revisions = client.get(
        f"/api/prompt/reverse-operations/{operation['id']}/revisions", headers=headers
    )
    assert [item["source"] for item in unchanged_revisions.json()] == [
        "provider_raw",
        "normalized",
        "user_edit",
        "applied",
    ]
    refreshed = client.get(
        f"/api/prompt/reverse-operations/{operation['id']}", headers=headers
    ).json()
    assert refreshed["applied_result_version"] == 4

    feedback = client.put(
        f"/api/prompt/reverse-operations/{operation['id']}/feedback",
        json={"rating": "not_useful", "issue_types": ["text_error"], "note": "瓶身文字识别错误"},
        headers=headers,
    )
    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["issue_types"] == ["text_error"]
    restored_feedback = client.get(
        f"/api/prompt/reverse-operations/{operation['id']}/feedback",
        headers=headers,
    )
    assert restored_feedback.status_code == 200, restored_feedback.text
    assert restored_feedback.json() == feedback.json()

    retried = post_reverse_retry(
        client,
        operation["id"],
        {"client_request_id": "reverse-v3-complete-retry-001"},
        headers=headers,
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["retry_of_operation_id"] == operation["id"]
    assert retried.json()["id"] != operation["id"]

    db = SessionLocal()
    try:
        stored = db.get(ReverseOperation, operation["id"])
        assert stored.user_id == uid
        assert stored.normalized_result["final_text"] == "银色香水瓶；冷白商业摄影"
        assert stored.raw_provider_result is not None
        assert stored.custom_instruction == "重点识别瓶身材质和高光位置"
    finally:
        db.close()

    db = SessionLocal()
    try:
        stored = db.get(ReverseOperation, operation["id"])
        stored.error_code = "RESULT_EXPIRED"
        stored.result = None
        stored.normalized_result = None
        db.commit()
    finally:
        db.close()
    expired_replay = post_reverse(client, body, headers=headers)
    assert expired_replay.status_code == 409
    assert "超过保留期" in expired_replay.text


def test_async_reverse_replay_bypasses_rate_limit(client, make_user, auth, monkeypatch):
    make_user("13710000019", balance=100)
    headers = auth("13710000019")
    _stub_reverse_dependencies(monkeypatch)
    rate_calls = []
    monkeypatch.setattr(
        prompt,
        "incr_window",
        lambda *_args, **_kwargs: rate_calls.append(1) or 1,
    )
    body = {
        "client_request_id": "async-reverse-rate-replay-001",
        "asset_url": "https://cdn.example.com/cup.jpg",
        "target": "image",
    }

    first = post_reverse(client, body, headers=headers)
    assert first.status_code == 202, first.text
    monkeypatch.setattr(prompt, "incr_window", lambda *_args, **_kwargs: 999)
    replay = post_reverse(client, body, headers=headers)

    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert rate_calls == [1]


@pytest.mark.parametrize(
    ("endpoint", "phone", "request_id", "expected_status"),
    [
        (
            "/api/prompt/reverse-operations",
            "13710000022",
            "async-reverse-policy-replay-001",
            202,
        ),
        (
            "/api/prompt/reverse",
            "13710000023",
            "legacy-reverse-policy-replay-001",
            200,
        ),
    ],
)
def test_exact_reverse_replay_precedes_later_asset_policy_validation(
    client,
    make_user,
    auth,
    monkeypatch,
    endpoint,
    phone,
    request_id,
    expected_status,
):
    make_user(phone, balance=100)
    headers = auth(phone)
    _stub_reverse_dependencies(monkeypatch)
    body = {
        "client_request_id": request_id,
        "asset_url": "https://cdn.example.com/policy-version-1.jpg",
        "target": "image",
    }

    first = (
        post_reverse(client, body, headers=headers)
        if endpoint == "/api/prompt/reverse-operations"
        else client.post(endpoint, json=body, headers=headers)
    )
    assert first.status_code == expected_status, first.text

    validation_calls = []

    def changed_asset_policy(*_args, **_kwargs):
        validation_calls.append(1)
        raise AssertionError("an exact replay must bypass the new asset policy")

    monkeypatch.setattr(prompt, "_validate_reverse_asset_request", changed_asset_policy)
    replay = (
        post_reverse(client, body, headers=headers)
        if endpoint == "/api/prompt/reverse-operations"
        else client.post(endpoint, json=body, headers=headers)
    )

    assert replay.status_code == expected_status, replay.text
    assert replay.json() == first.json()
    assert validation_calls == []


def test_async_reverse_client_request_id_conflict(client, make_user, auth, monkeypatch):
    make_user("13710000002", balance=100)
    headers = auth("13710000002")
    _stub_reverse_dependencies(monkeypatch)
    body = {
        "client_request_id": "async-reverse-conflict-001",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }
    assert post_reverse(client, body, headers=headers).status_code == 202
    conflict = post_reverse(
        client,
        {**body, "asset_url": "https://cdn.example.com/b.jpg"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert "client_request_id" in conflict.text


def test_async_reverse_normalizes_request_id_and_default_preset(
    client, make_user, auth, monkeypatch
):
    make_user("13710000015", balance=100)
    headers = auth("13710000015")
    _stub_reverse_dependencies(monkeypatch)
    body = {
        "client_request_id": "  normalized-request-001  ",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }

    first = post_reverse(client, body, headers=headers)
    replay = post_reverse(
        client,
        {
            **body,
            "client_request_id": "normalized-request-001",
            "video_analysis_preset": "standard",
        },
        headers=headers,
    )

    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]


def test_async_reverse_rejects_whitespace_request_id_without_freezing(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000016", balance=100)
    headers = auth("13710000016")
    _stub_reverse_dependencies(monkeypatch)

    response = post_reverse(
        client,
        {
            "client_request_id": "        ",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 422
    db = SessionLocal()
    try:
        assert db.query(ReverseOperation).filter_by(user_id=uid).count() == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
    finally:
        db.close()


def test_async_reverse_invalid_result_refunds_before_success(client, make_user, auth, monkeypatch):
    uid = make_user("13710000003", balance=100)
    headers = auth("13710000003")
    _stub_reverse_dependencies(
        monkeypatch,
        result={"structured": {"主体": "x"}, "final_text": {"invalid": True}},
    )
    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-invalid-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "failed"
    assert data["error_code"] == "INVALID_REVERSE_RESULT"
    assert data["cost_frozen"] == 0
    assert data["cost_settled"] == 0
    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=data["id"],
                type="settle",
            )
            .count()
            == 0
        )
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=data["id"],
                type="refund",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_async_reverse_missing_target_core_field_never_settles(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000013", balance=100)
    headers = auth("13710000013")
    _stub_reverse_dependencies(
        monkeypatch,
        result={"structured": {"光线": "柔和棚拍光"}, "final_text": "柔和棚拍画面"},
    )

    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-contract-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "failed"
    assert data["error_code"] == "INVALID_REVERSE_RESULT"
    assert "image 契约" in data["error"]
    assert data["cost_frozen"] == 0
    assert data["cost_settled"] == 0
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, data["id"])
        assert operation.status == "failed"
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=data["id"],
                type="settle",
            )
            .count()
            == 0
        )
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=data["id"],
                type="refund",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_settlement_boundary_rebuilds_visual_prompt_and_preserves_provider_text():
    provider_text = "女声轻声说欢迎回来；屏幕中央浮出限时优惠；OCR: BUY NOW"
    result = reverse_operations.validate_reverse_result(
        {
            "structured": {
                "主体": "红色产品",
                "光线": "柔和侧光",
                "未知审计字段": "证据帧中有BUY NOW",
            },
            "final_text": provider_text,
            "shots": [
                {
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "visual": "产品居中",
                    "ocr": "BUY NOW",
                    "audio_cue": "女声轻声说欢迎回来",
                    "evidence_frame_indices": [1],
                    "confidence": 0.9,
                }
            ],
        },
        "video",
    )

    assert result["provider_final_text"] == provider_text
    assert result["final_text"] == "红色产品；柔和侧光；镜头1（0.000-1.000秒）：产品居中"
    assert result["structured"]["未知审计字段"] == "证据帧中有BUY NOW"
    for forbidden in ("欢迎回来", "限时优惠", "BUY NOW", "OCR", "证据帧"):
        assert forbidden not in result["final_text"]


def test_enqueue_operation_persists_task_id_before_publish_and_reuses_it(
    client, make_user, auth, monkeypatch
):
    from app import tasks

    make_user("13710000030", balance=100)
    headers = auth("13710000030")
    _stub_reverse_dependencies(monkeypatch)
    enqueue_operation = reverse_operations.enqueue_operation
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args: "queued")
    created = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-persist-task-id-001",
            "asset_url": "https://cdn.example.com/persist-task-id.jpg",
            "target": "image",
        },
        headers=headers,
    )
    assert created.status_code == 202, created.text
    operation_id = created.json()["id"]
    published = []

    def observe_publish(_task, published_operation_id, *, task_id=None, **_kwargs):
        db = SessionLocal()
        try:
            operation = db.get(ReverseOperation, published_operation_id)
            published.append(
                {
                    "operation_id": published_operation_id,
                    "task_id": task_id,
                    "persisted_task_id": operation.celery_task_id,
                    "status": operation.status,
                    "cost_frozen": operation.cost_frozen,
                }
            )
        finally:
            db.close()
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(tasks, "enqueue_with_request_context", observe_publish)

    first_task_id = enqueue_operation(operation_id)
    second_task_id = enqueue_operation(operation_id)

    assert first_task_id
    assert second_task_id == first_task_id
    assert published == [
        {
            "operation_id": operation_id,
            "task_id": first_task_id,
            "persisted_task_id": first_task_id,
            "status": "queued",
            "cost_frozen": 5,
        },
        {
            "operation_id": operation_id,
            "task_id": first_task_id,
            "persisted_task_id": first_task_id,
            "status": "queued",
            "cost_frozen": 5,
        },
    ]


def test_async_reverse_broker_failure_returns_503_and_refunds(client, make_user, auth, monkeypatch):
    from app import tasks

    uid = make_user("13710000004", balance=100)
    headers = auth("13710000004")
    _stub_reverse_dependencies(monkeypatch)
    publish_attempts = []

    def reject_publish(_task, operation_id, *, task_id=None, **_kwargs):
        db = SessionLocal()
        try:
            operation = db.get(ReverseOperation, operation_id)
            user = db.get(User, uid)
            publish_attempts.append(
                {
                    "task_id": task_id,
                    "persisted_task_id": operation.celery_task_id,
                    "status": operation.status,
                    "cost_frozen": operation.cost_frozen,
                    "balance": user.balance_credits,
                    "frozen": user.frozen_credits,
                }
            )
        finally:
            db.close()
        raise RuntimeError("broker down")

    monkeypatch.setattr(tasks, "enqueue_with_request_context", reject_publish)
    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-broker-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    )
    assert response.status_code == 503
    assert len(publish_attempts) == 1
    reserved_task_id = publish_attempts[0]["task_id"]
    assert reserved_task_id
    assert publish_attempts == [
        {
            "task_id": reserved_task_id,
            "persisted_task_id": reserved_task_id,
            "status": "queued",
            "cost_frozen": 5,
            "balance": 95,
            "frozen": 5,
        }
    ]
    db = SessionLocal()
    try:
        operation = (
            db.query(ReverseOperation).filter_by(client_request_id="async-reverse-broker-001").one()
        )
        assert operation.status == "failed"
        assert operation.error_code == "BROKER_UNAVAILABLE"
        assert operation.celery_task_id == reserved_task_id
        assert operation.cost_frozen == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
        transactions = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation.id,
            )
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [transaction.type for transaction in transactions] == ["freeze", "refund"]
    finally:
        db.close()


def test_async_reverse_broker_error_after_worker_claim_returns_existing_operation(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000024", balance=100)
    headers = auth("13710000024")
    _stub_reverse_dependencies(monkeypatch)
    gateway_calls = []

    def fake_reverse(*_args, **_kwargs):
        gateway_calls.append(1)
        return {
            "structured": {"主体": "已由 worker 完成的杯子"},
            "final_text": "已由 worker 完成的杯子,白色背景",
        }

    def accepted_then_publish_response_failed(operation_id):
        reverse_operations.run_operation(operation_id)
        raise RuntimeError("broker accepted the task but publish response was lost")

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    monkeypatch.setattr(
        reverse_operations,
        "enqueue_operation",
        accepted_then_publish_response_failed,
    )

    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-broker-accepted-001",
            "asset_url": "https://cdn.example.com/accepted.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "succeeded"
    assert data["cost_frozen"] == 0
    assert data["cost_settled"] == 5
    assert gateway_calls == [1]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, data["id"])
        assert operation.status == "succeeded"
        assert operation.error_code is None
        assert db.get(User, uid).balance_credits == 95
        transactions = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation.id,
            )
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [transaction.type for transaction in transactions] == ["freeze", "settle"]
    finally:
        db.close()


def test_duplicate_reverse_task_delivery_invokes_gateway_once(client, make_user, auth, monkeypatch):
    from app.tasks import reverse_operation_task

    uid = make_user("13710000025", balance=100)
    headers = auth("13710000025")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args: "queued")
    gateway_calls = []

    def fake_reverse(*_args, **_kwargs):
        gateway_calls.append(1)
        return {
            "structured": {"主体": "只分析一次的杯子"},
            "final_text": "只分析一次的杯子,白色背景",
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    created = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-redelivery-001",
            "asset_url": "https://cdn.example.com/redelivery.jpg",
            "target": "image",
        },
        headers=headers,
    )
    assert created.status_code == 202, created.text
    operation_id = created.json()["id"]

    reverse_operation_task.run(operation_id)
    reverse_operation_task.run(operation_id)

    assert gateway_calls == [1]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "succeeded"
        assert operation.attempt_count == 1
        assert db.get(User, uid).balance_credits == 95
        transactions = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation_id,
            )
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [transaction.type for transaction in transactions] == ["freeze", "settle"]
    finally:
        db.close()


def test_async_reverse_queued_cancel_refunds_once(client, make_user, auth, monkeypatch):
    uid = make_user("13710000005", balance=100)
    headers = auth("13710000005")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args, **_kwargs: "queued")
    created = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-cancel-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    ).json()
    assert created["status"] == "queued"
    assert created["cost_frozen"] == 5

    first = client.post(f"/api/prompt/reverse-operations/{created['id']}/cancel", headers=headers)
    second = client.post(f"/api/prompt/reverse-operations/{created['id']}/cancel", headers=headers)
    assert first.json()["status"] == "canceled"
    assert second.json()["status"] == "canceled"
    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=created["id"],
                type="refund",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_async_reverse_cancel_wins_settlement_race(client, make_user, auth, monkeypatch, caplog):
    uid = make_user("13710000006", balance=100)
    headers = auth("13710000006")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args, **_kwargs: "queued")
    operation_id = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-race-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    ).json()["id"]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        operation.status = "running"
        db.commit()
    finally:
        db.close()
    requested = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/cancel", headers=headers
    ).json()
    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True

    caplog.set_level(logging.INFO, logger="reverse_operations")
    caplog.clear()
    db = SessionLocal()
    try:
        assert (
            reverse_operations._finish_success(
                db,
                operation_id,
                result={"structured": {"主体": "x"}, "final_text": "x"},
                reference_count=1,
                real_cost=5,
            )
            is None
        )
    finally:
        db.close()
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "canceled"
        assert operation.cost_settled == 0
        assert db.get(User, uid).balance_credits == 100
    finally:
        db.close()
    canceled = [
        event
        for event in _operation_events(caplog)
        if event["event"] == "reverse_operation_canceled"
    ]
    assert canceled == [
        {
            "event": "reverse_operation_canceled",
            "operation_id": operation_id,
            "status": "canceled",
            "phase": None,
            "error_code": "CANCELED",
        }
    ]


def test_async_reverse_cancel_wins_gateway_failure(client, make_user, auth, monkeypatch):
    uid = make_user("13710000014", balance=100)
    headers = auth("13710000014")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args, **_kwargs: "queued")
    operation_id = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-cancel-failure-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    ).json()["id"]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        operation.status = "running"
        operation.phase = "calling_model"
        db.commit()
    finally:
        db.close()

    requested = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/cancel",
        headers=headers,
    ).json()
    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True
    assert (
        reverse_operations.fail_operation(
            operation_id,
            code="GATEWAY_ERROR",
            error="upstream timeout",
        )
        is True
    )

    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "canceled"
        assert operation.error_code == "CANCELED"
        assert operation.error is None
        assert operation.phase is None
        assert operation.cost_frozen == 0
        assert operation.cost_settled == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=operation_id,
                type="settle",
            )
            .count()
            == 0
        )
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=operation_id,
                type="refund",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_async_reverse_refund_failure_rolls_back_operation_state(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000021", balance=100)
    headers = auth("13710000021")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args: "queued")
    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-refund-failure-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    operation_id = response.json()["id"]

    def fail_refund(*_args, **_kwargs):
        raise RuntimeError("refund unavailable")

    monkeypatch.setattr(reverse_operations.credits, "refund", fail_refund)
    with pytest.raises(RuntimeError, match="refund unavailable"):
        reverse_operations.fail_operation(
            operation_id,
            code="GATEWAY_ERROR",
            error="gateway failed",
        )

    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "queued"
        assert operation.cost_frozen == 5
        user = db.get(User, uid)
        assert user.balance_credits == 95
        assert user.frozen_credits == 5
        transactions = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation_id,
            )
            .all()
        )
        assert [transaction.type for transaction in transactions] == ["freeze"]
    finally:
        db.close()


def test_async_reverse_records_successful_gateway_call_when_canceled_after_response(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000020", balance=100)
    headers = auth("13710000020")
    _stub_reverse_dependencies(monkeypatch)
    _stub_quoted_provider_cost(monkeypatch, {"image_cost": 2})
    recorded = []
    monkeypatch.setattr(
        reverse_operations.usage,
        "record_call",
        lambda *_args, **kwargs: recorded.append(kwargs),
    )

    def cancel_after_response(*_args, **kwargs):
        operation_id = kwargs["audit_context"]["operation_id"]
        cancel_db = SessionLocal()
        try:
            operation = cancel_db.get(ReverseOperation, operation_id)
            operation.cancel_requested = True
            cancel_db.commit()
        finally:
            cancel_db.close()
        return {
            "structured": {"主体": "红色杯子"},
            "final_text": "红色杯子,白色背景",
            "usage": {"total_tokens": 12},
            "latency_ms": 25,
        }

    monkeypatch.setattr(gateway, "reverse_prompt", cancel_after_response)
    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-cancel-after-response-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "canceled"
    assert data["cost_frozen"] == 0
    assert data["cost_settled"] == 0
    assert len(recorded) == 1
    gateway_call = recorded[0]
    assert gateway_call["status"] == "ok"
    assert gateway_call["usage"] == {"total_tokens": 12}
    assert gateway_call["detail"]["operation_id"] == data["id"]
    assert gateway_call["detail"]["cost_credits"] == 5
    assert gateway_call["detail"]["provider_cost_status"] == "complete"
    assert gateway_call["detail"]["provider_cost_credits"] == 2
    assert gateway_call["detail"]["phase"] == "gateway_completed"

    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
        assert (
            db.query(CreditTransaction)
            .filter_by(
                biz_type="reverse_operation",
                biz_ref=data["id"],
                type="settle",
            )
            .count()
            == 0
        )
    finally:
        db.close()


def test_async_reverse_rechecks_cancel_before_repair_and_audits_failure(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000017", balance=100)
    headers = auth("13710000017")
    _stub_reverse_dependencies(monkeypatch)
    _stub_quoted_provider_cost(monkeypatch, {"image_cost": 3})
    recorded = []
    monkeypatch.setattr(
        reverse_operations.usage,
        "record_call",
        lambda *_args, **kwargs: recorded.append(kwargs),
    )

    def cancel_before_repair(*_args, **kwargs):
        audit = kwargs["audit_context"]
        assert audit["operation_id"]
        assert audit["cost_credits"] == 5
        assert kwargs["template_override"]
        cancel_db = SessionLocal()
        try:
            operation = cancel_db.get(ReverseOperation, audit["operation_id"])
            operation.cancel_requested = True
            cancel_db.commit()
        finally:
            cancel_db.close()
        assert kwargs["before_repair"]() is False
        raise gateway.GatewayError(
            "反推任务已在文本修复前取消",
            error_code="CANCELED",
            phase="repairing",
        )

    monkeypatch.setattr(gateway, "reverse_prompt", cancel_before_repair)
    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-repair-cancel-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "canceled"
    assert data["cost_frozen"] == 0
    assert data["cost_settled"] == 0
    failure = next(call for call in recorded if call["status"] == "failed")
    assert failure["detail"]["operation_id"] == data["id"]
    assert failure["detail"]["phase"] == "repairing"
    assert failure["detail"]["error_code"] == "CANCELED"
    assert failure["detail"]["repair_attempted"] is True
    assert failure["detail"]["provider_cost_status"] == "complete"
    assert failure["detail"]["provider_cost_credits"] == 3
    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("preset", "frozen_cost", "phone"),
    [
        ("fast", 5, "13710000033"),
        ("standard", 5, "13710000034"),
        ("fine", 5, "13710000035"),
    ],
)
def test_video_cover_confirmation_settles_reverse_price(
    client, make_user, auth, monkeypatch, preset, frozen_cost, phone
):
    uid = make_user(phone, balance=100)
    headers = auth(phone)
    _stub_reverse_dependencies(monkeypatch)
    seen_targets = []
    monkeypatch.setattr(
        prompt,
        "_collect_refs",
        lambda *_args, **_kwargs: (
            ["data:image/jpeg;base64,YQ=="],
            {
                "analysis_mode": "cover_fallback",
                "degraded_reason": "抽帧失败",
                "sampled_frames": [],
            },
        ),
    )

    def fake_reverse(*_args, **kwargs):
        seen_targets.append(kwargs.get("target"))
        return {
            "structured": {
                "静态观察": "产品居中",
                "主体运动设计": "产品缓慢旋转",
                "镜头运动设计": "镜头缓慢环绕",
            },
            "final_text": "缓慢环绕产品",
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    created = post_reverse(
        client,
        {
            "client_request_id": f"async-reverse-cover-{preset}-001",
            "asset_url": "https://cdn.example.com/source.mp4",
            "source_type": "video",
            "target": "video",
            "video_analysis_preset": preset,
            "fallback_image": "https://cdn.example.com/cover.jpg",
        },
        headers=headers,
    )
    assert created.status_code == 202, created.text
    pending = created.json()
    assert pending["status"] == "needs_confirmation"
    assert pending["cost_frozen"] == frozen_cost
    assert pending["confirmation_expires_at"]
    assert seen_targets == []

    confirmed = client.post(
        f"/api/prompt/reverse-operations/{pending['id']}/confirm-cover",
        json={},
        headers=headers,
    )
    assert confirmed.status_code == 202, confirmed.text
    done = confirmed.json()
    assert done["status"] == "succeeded"
    assert done["cost_settled"] == 5
    assert done["cost_frozen"] == 0
    assert seen_targets == ["image_to_video"]
    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 95
        assert db.get(User, uid).frozen_credits == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("preset", "frozen_cost", "phone"),
    [
        ("fast", 5, "13710000036"),
        ("standard", 5, "13710000037"),
        ("fine", 5, "13710000038"),
    ],
)
def test_image_source_video_target_freezes_tier_and_settles_single_image_cost(
    client,
    make_user,
    auth,
    monkeypatch,
    preset,
    frozen_cost,
    phone,
):
    uid = make_user(phone, balance=100)
    headers = auth(phone)
    _stub_reverse_dependencies(monkeypatch)
    gateway_targets = []

    def fake_reverse(*_args, **kwargs):
        gateway_targets.append(kwargs.get("target"))
        return {
            "structured": {
                "静态观察": "产品居中",
                "主体运动设计": "新设计:产品轻微呼吸式微动",
                "镜头运动设计": "新设计:镜头缓慢推近",
            },
            "final_text": "保持产品外观稳定,设计轻微微动与缓慢推近",
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)

    def assert_frozen_then_run(operation_id):
        db = SessionLocal()
        try:
            operation = db.get(ReverseOperation, operation_id)
            user = db.get(User, uid)
            assert operation.status == "queued"
            assert operation.cost_frozen == frozen_cost
            assert user.balance_credits == 100 - frozen_cost
            assert user.frozen_credits == frozen_cost
        finally:
            db.close()
        reverse_operations.run_operation(operation_id)

    monkeypatch.setattr(reverse_operations, "enqueue_operation", assert_frozen_then_run)
    response = post_reverse(
        client,
        {
            "client_request_id": f"image-to-video-{preset}-001",
            "asset_url": "https://cdn.example.com/source-image.jpg",
            "source_type": "image",
            "target": "video",
            "video_analysis_preset": preset,
        },
        headers=headers,
    )

    assert response.status_code == 202, response.text
    done = response.json()
    assert done["status"] == "succeeded"
    assert done["cost_frozen"] == 0
    assert done["cost_settled"] == 5
    assert gateway_targets == ["image_to_video"]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, done["id"])
        user = db.get(User, uid)
        assert user.balance_credits == 95
        assert user.frozen_credits == 0
        transactions = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation.id,
            )
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [transaction.type for transaction in transactions] == ["freeze", "settle"]
        assert transactions[-1].reserved_amount == frozen_cost
        assert transactions[-1].real_cost == 5
    finally:
        db.close()


def test_mock_video_without_cover_pauses_for_confirmation(client, make_user, auth, monkeypatch):
    uid = make_user("13710000028", balance=100)
    headers = auth("13710000028")
    _stub_reverse_dependencies(monkeypatch)
    gateway_calls = []
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: gateway_calls.append(1),
    )

    response = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-mock-video-no-cover-001",
            "asset_url": "https://cdn.example.com/mock-no-cover.mp4",
            "source_type": "video",
            "target": "video",
            "video_analysis_preset": "standard",
        },
        headers=headers,
    )

    assert response.status_code == 202, response.text
    pending = response.json()
    assert pending["status"] == "needs_confirmation"
    assert pending["phase"] == "awaiting_cover_confirmation"
    assert pending["error_code"] == "VIDEO_FRAMES_UNAVAILABLE"
    assert pending["confirmation_expires_at"]
    assert pending["cost_frozen"] == 5
    assert pending["cost_settled"] == 0
    assert gateway_calls == []
    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 95
        assert db.get(User, uid).frozen_credits == 5
    finally:
        db.close()


def test_cover_confirmation_broker_error_after_worker_claim_keeps_worker_result(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000027", balance=100)
    headers = auth("13710000027")
    _stub_reverse_dependencies(monkeypatch)
    gateway_calls = []
    monkeypatch.setattr(
        prompt,
        "_collect_refs",
        lambda *_args, **_kwargs: (
            ["data:image/jpeg;base64,YQ=="],
            {
                "analysis_mode": "cover_fallback",
                "degraded_reason": "抽帧失败",
                "sampled_frames": [],
            },
        ),
    )

    def fake_reverse(*_args, **_kwargs):
        gateway_calls.append(1)
        return {
            "structured": {
                "静态观察": "产品居中",
                "主体运动设计": "产品缓慢旋转",
                "镜头运动设计": "镜头缓慢环绕",
            },
            "final_text": "镜头缓慢环绕居中的产品",
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    created = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-confirm-broker-accepted-001",
            "asset_url": "https://cdn.example.com/confirm-source.mp4",
            "source_type": "video",
            "target": "video",
            "video_analysis_preset": "standard",
            "fallback_image": "https://cdn.example.com/confirm-cover.jpg",
        },
        headers=headers,
    )
    assert created.status_code == 202, created.text
    pending = created.json()
    assert pending["status"] == "needs_confirmation"
    assert pending["cost_frozen"] == 5

    def accepted_then_publish_response_failed(operation_id):
        reverse_operations.run_operation(operation_id)
        raise RuntimeError("broker accepted the task but publish response was lost")

    monkeypatch.setattr(
        reverse_operations,
        "enqueue_operation",
        accepted_then_publish_response_failed,
    )
    confirmed = client.post(
        f"/api/prompt/reverse-operations/{pending['id']}/confirm-cover",
        json={},
        headers=headers,
    )

    assert confirmed.status_code == 202, confirmed.text
    done = confirmed.json()
    assert done["status"] == "succeeded"
    assert done["error_code"] is None
    assert done["cost_frozen"] == 0
    assert done["cost_settled"] == 5
    assert gateway_calls == [1]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, pending["id"])
        assert operation.status == "succeeded"
        assert operation.error_code is None
        assert db.get(User, uid).balance_credits == 95
        assert db.get(User, uid).frozen_credits == 0
        transactions = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation.id,
            )
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [transaction.type for transaction in transactions] == ["freeze", "settle"]
    finally:
        db.close()


def test_cover_confirmation_requeues_with_new_persisted_task_id(
    client, make_user, auth, monkeypatch
):
    from app import tasks

    uid = make_user("13710000039", balance=100)
    headers = auth("13710000039")
    _stub_reverse_dependencies(monkeypatch)
    enqueue_operation = reverse_operations.enqueue_operation
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args: "queued")
    created = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-confirm-new-task-id-001",
            "asset_url": "https://cdn.example.com/confirm-new-task-id.mp4",
            "fallback_image": "https://cdn.example.com/confirm-new-task-id.jpg",
            "source_type": "video",
            "target": "video",
            "video_analysis_preset": "standard",
        },
        headers=headers,
    )
    assert created.status_code == 202, created.text
    operation_id = created.json()["id"]
    old_task_id = "old-cover-analysis-task-id"
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        operation.status = "needs_confirmation"
        operation.phase = "awaiting_cover_confirmation"
        operation.progress = 45
        operation.celery_task_id = old_task_id
        operation.confirmation_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        db.commit()
    finally:
        db.close()

    publish_attempts = []

    def observe_publish(_task, published_operation_id, *, task_id=None, **_kwargs):
        db = SessionLocal()
        try:
            operation = db.get(ReverseOperation, published_operation_id)
            publish_attempts.append(
                {
                    "task_id": task_id,
                    "persisted_task_id": operation.celery_task_id,
                    "previous_task_id": operation.request_context.get("previous_celery_task_id"),
                    "status": operation.status,
                }
            )
        finally:
            db.close()
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(reverse_operations, "enqueue_operation", enqueue_operation)
    monkeypatch.setattr(tasks, "enqueue_with_request_context", observe_publish)
    confirmed = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/confirm-cover",
        json={},
        headers=headers,
    )

    assert confirmed.status_code == 202, confirmed.text
    assert confirmed.json()["status"] == "queued"
    assert len(publish_attempts) == 1
    new_task_id = publish_attempts[0]["task_id"]
    assert new_task_id
    assert new_task_id != old_task_id
    assert publish_attempts == [
        {
            "task_id": new_task_id,
            "persisted_task_id": new_task_id,
            "previous_task_id": old_task_id,
            "status": "queued",
        }
    ]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.celery_task_id == new_task_id
        assert operation.request_context["previous_celery_task_id"] == old_task_id
        assert operation.cost_frozen == 5
        assert db.get(User, uid).balance_credits == 95
        assert db.get(User, uid).frozen_credits == 5
    finally:
        db.close()


def test_confirmation_expiry_cancels_and_refunds(client, make_user, auth, monkeypatch, caplog):
    uid = make_user("13710000008", balance=100)
    headers = auth("13710000008")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args, **_kwargs: "queued")
    operation_id = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-expiry-001",
            "asset_url": "https://cdn.example.com/a.jpg",
            "target": "image",
        },
        headers=headers,
    ).json()["id"]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        operation.status = "needs_confirmation"
        operation.confirmation_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    caplog.set_level(logging.INFO, logger="reverse_operations")
    caplog.clear()
    result = reverse_operations.reap_operations()
    assert result["expired"] == 1
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "canceled"
        assert operation.error_code == "CONFIRMATION_EXPIRED"
        assert operation.cost_frozen == 0
        assert db.get(User, uid).balance_credits == 100
    finally:
        db.close()
    expired = [
        event
        for event in _operation_events(caplog)
        if event["event"] == "reverse_operation_confirmation_expired"
    ]
    assert expired == [
        {
            "event": "reverse_operation_confirmation_expired",
            "operation_id": operation_id,
            "status": "canceled",
            "phase": None,
            "error_code": "CONFIRMATION_EXPIRED",
        }
    ]


def test_confirm_cover_after_expiry_logs_closure(client, make_user, auth, monkeypatch, caplog):
    uid = make_user("13710000052", balance=100)
    headers = auth("13710000052")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args: "queued")
    operation_id = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-confirm-expired-001",
            "asset_url": "https://cdn.example.com/expired.mp4",
            "fallback_image": "https://cdn.example.com/expired.jpg",
            "source_type": "video",
            "target": "video",
        },
        headers=headers,
    ).json()["id"]
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        operation.status = "needs_confirmation"
        operation.phase = "awaiting_cover_confirmation"
        operation.confirmation_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    caplog.set_level(logging.INFO, logger="reverse_operations")
    caplog.clear()
    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/confirm-cover",
        json={},
        headers=headers,
    )

    assert response.status_code == 409
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "canceled"
        assert operation.error_code == "CONFIRMATION_EXPIRED"
        assert operation.error == "封面降级确认已过期,积分已全额退回"
        assert operation.cost_frozen == 0
        assert db.get(User, uid).balance_credits == 100
    finally:
        db.close()
    expired = [
        event
        for event in _operation_events(caplog)
        if event["event"] == "reverse_operation_confirmation_expired"
    ]
    assert expired == [
        {
            "event": "reverse_operation_confirmation_expired",
            "operation_id": operation_id,
            "status": "canceled",
            "phase": None,
            "error_code": "CONFIRMATION_EXPIRED",
        }
    ]


def test_legacy_reverse_returns_deprecated_409_for_cover_confirmation(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000009", balance=100)
    headers = auth("13710000009")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(prompt.settings, "mock_mode", False)
    monkeypatch.setattr(
        prompt,
        "_collect_refs",
        lambda *_args, **_kwargs: (
            ["data:image/jpeg;base64,YQ=="],
            {
                "analysis_mode": "cover_fallback",
                "degraded_reason": "抽帧失败",
                "sampled_frames": [],
            },
        ),
    )
    response = client.post(
        "/api/prompt/reverse",
        json={
            "client_request_id": "legacy-reverse-cover-001",
            "asset_url": "https://cdn.example.com/source.mp4",
            "source_type": "video",
            "target": "video",
            "fallback_image": "https://cdn.example.com/cover.jpg",
        },
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "needs_confirmation"
    operation_id = response.json()["detail"]["operation_id"]
    assert response.headers["deprecation"] == "true"
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "needs_confirmation"
        assert operation.cost_frozen == 5
        assert operation.charged_credits == 0
        assert db.get(User, uid).balance_credits == 95
        assert db.get(User, uid).frozen_credits == 5
    finally:
        db.close()
    canceled = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/cancel",
        headers=headers,
    )
    assert canceled.status_code == 200
    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
    finally:
        db.close()


def test_reaper_republishes_queued_and_refunds_stale_running(client, make_user, auth, monkeypatch):
    queued_uid = make_user("13710000010", balance=100)
    running_uid = make_user("13710000011", balance=100)
    legacy_uid = make_user("13710000018", balance=100)
    queued_headers = auth("13710000010")
    running_headers = auth("13710000011")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args, **_kwargs: "queued")

    def create(headers, request_id):
        return post_reverse(
            client,
            {
                "client_request_id": request_id,
                "asset_url": "https://cdn.example.com/a.jpg",
                "target": "image",
            },
            headers=headers,
        ).json()["id"]

    queued_id = create(queued_headers, "async-reverse-republish-001")
    running_id = create(running_headers, "async-reverse-timeout-001")
    db = SessionLocal()
    try:
        queued = db.get(ReverseOperation, queued_id)
        queued.updated_at = datetime.now(timezone.utc) - timedelta(minutes=3)
        running = db.get(ReverseOperation, running_id)
        running.status = "running"
        running.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        legacy = ReverseOperation(
            user_id=legacy_uid,
            client_request_id=None,
            request_fingerprint="9" * 64,
            target="image",
            asset_url="https://cdn.example.com/legacy.jpg",
            status="running",
            charged_credits=2,
            cost_frozen=0,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
        credits.consume(db, legacy_uid, 2, biz_type="reverse", biz_ref=legacy_id)
    finally:
        db.close()

    republished = []
    monkeypatch.setattr(reverse_operations, "enqueue_operation", republished.append)
    result = reverse_operations.reap_operations()
    assert queued_id in republished
    assert result["republished"] >= 1
    assert result["failed"] >= 1
    assert result["legacy_failed"] >= 1
    db = SessionLocal()
    try:
        running = db.get(ReverseOperation, running_id)
        assert running.status == "failed"
        assert running.error_code == "OPERATION_TIMEOUT"
        assert running.cost_frozen == 0
        assert db.get(User, running_uid).balance_credits == 100
        legacy = db.get(ReverseOperation, legacy_id)
        assert legacy.status == "failed"
        assert legacy.charged_credits == 0
        assert db.get(User, legacy_uid).balance_credits == 100
        queued = reverse_operations.request_cancel(
            db,
            operation_id=queued_id,
            user_id=queued_uid,
        )
        assert queued.status == "canceled"
    finally:
        db.close()


def test_stale_running_candidate_refreshed_before_claim_is_not_refunded(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000026", balance=100)
    headers = auth("13710000026")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args: "queued")
    created = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-stale-refresh-001",
            "asset_url": "https://cdn.example.com/stale-refresh.jpg",
            "target": "image",
        },
        headers=headers,
    )
    assert created.status_code == 202, created.text
    operation_id = created.json()["id"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=30)

    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        operation.status = "running"
        operation.phase = "calling_model"
        operation.updated_at = cutoff - timedelta(seconds=1)
        db.commit()
        candidate_operation_id = operation.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, candidate_operation_id)
        operation.updated_at = now
        db.commit()
    finally:
        db.close()

    assert (
        reverse_operations._fail_stale_running(
            candidate_operation_id,
            cutoff=cutoff,
            now=now,
        )
        is False
    )
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "running"
        assert operation.phase == "calling_model"
        assert operation.error_code is None
        assert operation.cost_frozen == 5
        user = db.get(User, uid)
        assert user.balance_credits == 95
        assert user.frozen_credits == 5
        assert (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation_id,
                type="refund",
            )
            .count()
            == 0
        )
    finally:
        db.close()


def test_stale_running_cancel_request_finishes_canceled_and_refunds(
    client, make_user, auth, monkeypatch
):
    uid = make_user("13710000049", balance=100)
    headers = auth("13710000049")
    _stub_reverse_dependencies(monkeypatch)
    monkeypatch.setattr(reverse_operations, "enqueue_operation", lambda *_args: "queued")
    created = post_reverse(
        client,
        {
            "client_request_id": "async-reverse-stale-cancel-001",
            "asset_url": "https://cdn.example.com/stale-cancel.jpg",
            "target": "image",
        },
        headers=headers,
    )
    assert created.status_code == 202, created.text
    operation_id = created.json()["id"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=30)

    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        operation.status = "running"
        operation.phase = "calling_model"
        operation.cancel_requested = True
        operation.updated_at = cutoff - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    assert (
        reverse_operations._fail_stale_running(
            operation_id,
            cutoff=cutoff,
            now=now,
        )
        is True
    )
    db = SessionLocal()
    try:
        operation = db.get(ReverseOperation, operation_id)
        assert operation.status == "canceled"
        assert operation.error_code == "CANCELED"
        assert operation.error is None
        assert operation.cost_frozen == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.get(User, uid).frozen_credits == 0
        assert (
            db.query(CreditTransaction)
            .filter_by(
                user_id=uid,
                biz_type="reverse_operation",
                biz_ref=operation_id,
                type="refund",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_async_history_snapshot_flattens_workspace_for_v2_restore(make_user):
    uid = make_user("13710000012", balance=100)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id="async-reverse-history-001",
            request_fingerprint="a" * 64,
            target="image",
            asset_url="https://cdn.example.com/history.jpg",
            status="succeeded",
            request_context={
                "source_type": "image",
                "workspace_snapshot_v2": {
                    "version": 2,
                    "creation_mode": "image",
                    "subject_mode": "portrait",
                    "source_signature": "image|history.jpg",
                    "selected": {
                        "type": "image",
                        "url": "https://cdn.example.com/history.jpg",
                    },
                    "assets": [],
                    "subject_profile": {"发型": "黑色长发"},
                },
            },
            result={"structured": {"主体": "人物"}, "final_text": "最终人像提示词"},
            reference_count=1,
            cost_settled=2,
            charged_credits=2,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)
        db.expunge(operation)
    finally:
        db.close()

    reverse_operations._remember_history(
        operation,
        {
            "structured": {"主体": "人物", "光线": "柔光"},
            "final_text": "最终人像提示词",
            "video_analysis": None,
        },
    )
    db = SessionLocal()
    try:
        history = db.query(UserPrompt).filter_by(user_id=uid, source="reverse").one()
        snapshot = history.params["reverse_snapshot_v2"]
        assert snapshot["creation_mode"] == "image"
        assert snapshot["subject_mode"] == "portrait"
        assert snapshot["source_signature"] == "image|history.jpg"
        assert snapshot["selected"]["url"].endswith("history.jpg")
        assert snapshot["subject_profile"] == {"发型": "黑色长发"}
        assert snapshot["structured"] == {"主体": "人物", "光线": "柔光"}
        assert snapshot["final_text"] == "最终人像提示词"
        assert snapshot["workspace_snapshot_v2"]["creation_mode"] == "image"
    finally:
        db.close()


def test_cover_result_merge_preserves_gateway_analysis_evidence():
    result = {
        "structured": {"静态观察": "产品居中"},
        "final_text": "基于封面的运动设计",
        "shots": [],
        "analysis_gaps": [{"message": "封面单帧无法覆盖源视频时间线"}],
        "video_analysis": {
            "analysis_mode": "cover_fallback",
            "source": {"source_type": "video_cover", "audio_analyzed": False},
            "sampled_frames": [{"index": 1, "timestamp_seconds": 0}],
            "degraded_reason": "用户确认使用封面",
        },
    }

    merged = reverse_operations._merge_video_analysis_result(
        result,
        {
            "analysis_mode": "cover_fallback",
            "source": {"duration_seconds": 8.5},
            "degraded_reason": "抽帧失败",
        },
    )

    assert merged["source"] == {
        "duration_seconds": 8.5,
        "source_type": "video_cover",
        "audio_analyzed": False,
    }
    assert merged["sampled_frames"] == [{"index": 1, "timestamp_seconds": 0}]
    assert merged["analysis_gaps"] == [{"message": "封面单帧无法覆盖源视频时间线"}]
    assert merged["shots"] == []
    assert merged["degraded_reason"] == "用户确认使用封面"
    assert "video_analysis" not in result
    assert "shots" not in result
    assert "analysis_gaps" not in result


def test_evidence_bound_video_result_rebuilds_prompt_after_temporal_gating():
    result = {
        "structured": {
            "主体": "深蓝玻璃精华瓶",
            "主体动作": "产品快速旋转",
            "镜头运动": "镜头环绕一周",
            "剪辑节奏": "快速跳切",
            "转场": "闪白转场",
        },
        "final_text": "包含未验证动作的旧提示词",
        "video_analysis": {
            "shots": [{
                "start_seconds": 0.0,
                "end_seconds": 4.0,
                "visual": "直接可见事实：产品正面居中。",
                "lighting": "冷白轮廓光",
                "action": "产品快速旋转",
                "camera": "镜头缓慢推进",
                "transition": "闪白转场",
                "action_evidence_refs": [],
                "camera_motion_evidence_refs": ["motion-1"],
                "transition_evidence_refs": [],
                "analyzer_status": {
                    "action": "unsupported",
                    "camera_motion": "analyzed",
                    "transition": "unsupported",
                },
            }],
        },
    }

    reverse_operations._finalize_evidence_bound_video_result(result)

    shot = result["video_analysis"]["shots"][0]
    assert shot["visual"] == "产品正面居中"
    assert shot["action"] == ""
    assert shot["camera"] == "镜头缓慢推进"
    assert shot["transition"] == ""
    assert "主体动作" not in result["structured"]
    assert result["structured"]["镜头运动"] == "镜头缓慢推进"
    assert "转场" not in result["structured"]
    assert "快速旋转" not in result["final_text"]
    assert "闪白转场" not in result["final_text"]
    assert "镜头缓慢推进" in result["final_text"]


@pytest.mark.parametrize(
    ("target", "subject_mode", "profile_key", "profile", "phone"),
    [
        (
            "product_profile",
            "product",
            "product_profile",
            {"产品品类": "精华液", "包装结构": "透明玻璃瓶"},
            "13710000031",
        ),
        (
            "portrait_profile",
            "portrait",
            "portrait_profile",
            {"脸型五官": "椭圆脸", "妆发": "黑色短发"},
            "13710000032",
        ),
    ],
)
def test_profile_history_writes_target_profile_and_asset_association(
    make_user,
    target,
    subject_mode,
    profile_key,
    profile,
    phone,
):
    uid = make_user(phone, balance=100)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id=f"history-{target}",
            request_fingerprint="b" * 64,
            target=target,
            asset_url=f"https://cdn.example.com/{target}.jpg",
            status="succeeded",
            request_context={
                "source_type": "image",
                "workspace_snapshot_v2": {
                    "version": 2,
                    "creation_mode": "image",
                    "target": target,
                },
            },
            result={"structured": profile, "final_text": "身份锁定提示词"},
            reference_count=1,
            cost_settled=2,
            charged_credits=2,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)
        db.expunge(operation)
    finally:
        db.close()

    reverse_operations._remember_history(
        operation,
        {"structured": profile, "final_text": "身份锁定提示词"},
    )

    db = SessionLocal()
    try:
        history = db.query(UserPrompt).filter_by(user_id=uid, source="reverse").one()
        snapshot = history.params["reverse_snapshot_v2"]
        assert snapshot["subject_mode"] == subject_mode
        expected_profile = {
            "structured": profile,
            "final_text": "身份锁定提示词",
        }
        assert snapshot["subject_profile"] == expected_profile
        assert snapshot[profile_key] == expected_profile
        assert snapshot["product_asset"] == {
            "type": "image",
            "url": f"https://cdn.example.com/{target}.jpg",
        }
    finally:
        db.close()
