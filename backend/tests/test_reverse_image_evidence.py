from __future__ import annotations

from copy import deepcopy

import pytest

from app.db import SessionLocal
from app.models import ReverseOperation, ReverseResultRevision
from app.services import reverse_lineage, reverse_operations
from app.services.gateway_prompting import (
    ReverseResultValidationError,
    reverse_template,
    validate_reverse_result,
)


def _evidence_payload() -> dict:
    return {
        "主体": "画面中央的银色香水瓶",
        "商品服装": "银色矩形瓶身，正面有 ACME Logo",
        "文字版式": "瓶身中部为 ACME，底部小字无法辨认",
        "一致性约束": "保持瓶身形状、Logo 位置和包装比例",
        "image_evidence": [
            {
                "evidence_type": "ocr",
                "bbox": {"x": 0.31, "y": 0.39, "width": 0.2, "height": 0.08},
                "field_key": "  文字版式  ",
                "evidence_text": "  ACME  ",
                "confidence": 0.97,
                "source_index": 1,
                "fact_status": "visible",
                "protected": True,
                "editable": False,
            },
            {
                "evidence_type": "logo",
                "bbox": {"x": 0.3, "y": 0.36, "width": 0.23, "height": 0.13},
                "field_key": "商品服装",
                "evidence_text": "ACME Logo 字样可见",
                "confidence": 0.94,
                "source_index": 1,
                "fact_status": "visible",
                "protected": True,
                "editable": False,
            },
            {
                "evidence_type": "packaging",
                "bbox": {"x": 0.22, "y": 0.17, "width": 0.4, "height": 0.7},
                "field_key": "商品服装",
                "evidence_text": "银色矩形玻璃瓶包装",
                "confidence": 0.91,
                "source_index": 1,
                "fact_status": "visible",
                "protected": True,
                "editable": False,
            },
            {
                "evidence_type": "subject_protection",
                "bbox": {"x": 0.2, "y": 0.12, "width": 0.44, "height": 0.78},
                "field_key": "主体",
                "evidence_text": "香水瓶完整主体保护区",
                "confidence": 0.98,
                "source_index": 1,
                "fact_status": "visible",
                "protected": True,
                "editable": False,
            },
            {
                "evidence_type": "visual_field",
                "bbox": None,
                "field_key": "一致性约束",
                "evidence_text": "第二张参考图的材质细节无法确认",
                "confidence": 0.15,
                "source_index": 2,
                "fact_status": "unknown",
                "protected": False,
                "editable": False,
            },
        ],
        "final_text": "银色香水瓶商业摄影",
    }


def test_reverse_v3_image_evidence_normalizes_all_region_types():
    result = validate_reverse_result(_evidence_payload(), "image", source_count=2)

    assert {item["evidence_type"] for item in result["image_evidence"]} == {
        "ocr",
        "logo",
        "packaging",
        "subject_protection",
        "visual_field",
    }
    assert result["image_evidence"][0]["field_key"] == "文字版式"
    assert result["image_evidence"][0]["evidence_text"] == "ACME"
    assert result["image_evidence"][0]["bbox"] == {
        "x": 0.31,
        "y": 0.39,
        "width": 0.2,
        "height": 0.08,
    }
    assert result["image_evidence"][-1]["bbox"] is None
    assert "image_evidence" not in result["structured"]
    assert "香水瓶完整主体保护区" not in result["final_text"]

    settled = reverse_operations.validate_reverse_result(
        result,
        "image",
        source_count=2,
    )
    assert settled["image_evidence"] == result["image_evidence"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda item: item.update(
                bbox={"x": 0.8, "y": 0.2, "width": 0.3, "height": 0.2}
            ),
            "bbox",
        ),
        (lambda item: item.update(confidence=1.2), "confidence"),
        (lambda item: item.update(confidence="0.9"), "confidence"),
        (lambda item: item.update(source_index=3), "source_index"),
        (lambda item: item.update(field_key="__proto__"), "field_key"),
        (lambda item: item.update(protected=True, editable=True), "protected"),
        (lambda item: item.update(bbox=None), "bbox"),
        (lambda item: item.update({"constructor": {"polluted": True}}), "危险字段"),
    ],
)
def test_reverse_v3_image_evidence_rejects_invalid_provider_regions(mutate, message):
    payload = _evidence_payload()
    payload["image_evidence"] = [deepcopy(payload["image_evidence"][0])]
    mutate(payload["image_evidence"][0])

    with pytest.raises(ReverseResultValidationError, match=message):
        validate_reverse_result(payload, "image", source_count=2)


def test_reverse_v2_image_result_remains_compatible_with_empty_v3_evidence():
    legacy = validate_reverse_result(
        {"主体": "红色杯子", "场景背景": "白色背景", "final_text": "红色杯子"},
        "image",
        source_count=1,
    )

    assert legacy["image_evidence"] == []
    assert legacy["structured"] == {"主体": "红色杯子", "场景背景": "白色背景"}
    assert reverse_operations.validate_reverse_result(legacy, "image")["image_evidence"] == []
    assert "image_evidence" in reverse_template("image")
    assert "不得猜测" in reverse_template("product_profile")


def _seed_review_operation(
    user_id: int,
    *,
    source_count: int = 2,
    normalized_image_evidence: list[dict] | None = None,
) -> int:
    sources = [
        {
            "asset_url": f"http://example.com/review-source-{user_id}-{index}.png",
            "source_type": "image",
            "role": "primary" if index == 1 else "style",
        }
        for index in range(1, source_count + 1)
    ]
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=f"{user_id:064d}"[-64:],
            target="image",
            asset_url=sources[0]["asset_url"],
            output_purpose="generation",
            request_context={"source_type": "image", "sources": sources},
            status="succeeded",
            progress=100,
            result={"final_text": "reviewed image", "structured": {}},
            normalized_result={"final_text": "reviewed image", "structured": {}},
        )
        db.add(operation)
        db.flush()
        fingerprints = [
            reverse_lineage.build_source_fingerprint(
                source_index=index,
                content_hash=reverse_lineage.bytes_content_hash(
                    f"review-source:{user_id}:{index}".encode()
                ),
                locator=source["asset_url"],
                method="test_source_sha256",
            )
            for index, source in enumerate(sources, start=1)
        ]
        content_hash = reverse_lineage.source_content_hash(fingerprints)
        normalized_payload = {
            "final_text": "reviewed image",
            "structured": {},
        }
        if normalized_image_evidence is not None:
            normalized_payload["image_evidence"] = deepcopy(normalized_image_evidence)
        provider_payload = {"provider": "test", "result": deepcopy(normalized_payload)}
        provider_revision = ReverseResultRevision(
            operation_id=operation.id,
            user_id=user_id,
            version=1,
            source="provider_raw",
            payload=provider_payload,
            source_content_hash=content_hash,
            source_fingerprints=fingerprints,
            payload_hash=reverse_lineage.canonical_payload_hash(provider_payload),
            lineage_status=reverse_lineage.VERIFIED,
            evidence_review_action="not_applicable",
        )
        db.add(provider_revision)
        db.flush()
        db.add(ReverseResultRevision(
            operation_id=operation.id,
            user_id=user_id,
            version=2,
            source="normalized",
            payload=normalized_payload,
            parent_revision_id=provider_revision.id,
            source_content_hash=content_hash,
            source_fingerprints=fingerprints,
            payload_hash=reverse_lineage.canonical_payload_hash(normalized_payload),
            lineage_status=reverse_lineage.VERIFIED,
            evidence_review_action="not_applicable",
        ))
        db.commit()
        return int(operation.id)


def _saved_review_payload() -> dict:
    return {
        "final_text": "reviewed image",
        "structured": {},
        "image_evidence": [
            {
                "evidence_id": "subject-primary-001",
                "evidence_type": "subject_protection",
                "bbox": {"x": 0.2, "y": 0.1, "width": 0.4, "height": 0.7},
                "field_key": "主体",
                "evidence_text": "完整商品主体",
                "confidence": 0.98,
                "source_index": 1,
                "fact_status": "visible",
                "protected": True,
                "editable": False,
                "review_status": "confirmed",
            },
            {
                "evidence_id": "background-primary-001",
                "evidence_type": "visual_field",
                "bbox": None,
                "polygon": [
                    {"x": 0.0, "y": 0.0},
                    {"x": 1.0, "y": 0.0},
                    {"x": 1.0, "y": 1.0},
                    {"x": 0.0, "y": 1.0},
                ],
                "field_key": "场景背景",
                "evidence_text": "背景区域",
                "confidence": 0.9,
                "source_index": 1,
                "fact_status": "visible",
                "protected": False,
                "editable": True,
                "review_status": "confirmed",
            },
        ],
    }


def test_user_edit_inheritance_normalizes_provider_evidence_to_pending_review(
    client, make_user, auth
):
    user_id = make_user("13973000005", balance=100)
    headers = auth("13973000005")
    inherited_candidates = deepcopy(_saved_review_payload()["image_evidence"])
    inherited_candidates[0].pop("review_status")
    inherited_candidates[1]["review_status"] = "approved"
    inherited_candidates[1].pop("evidence_id")
    operation_id = _seed_review_operation(
        user_id,
        normalized_image_evidence=inherited_candidates,
    )

    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "user_edit",
            "payload": {"final_text": "prompt-only edit", "structured": {}},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    evidence = response.json()["payload"]["image_evidence"]
    assert [item["review_status"] for item in evidence] == ["pending", "pending"]
    assert [item["evidence_id"] for item in evidence] == [
        "subject-primary-001",
        "evidence-1-2",
    ]
    assert evidence[0]["bbox"] == inherited_candidates[0]["bbox"]
    assert evidence[1]["polygon"] == inherited_candidates[1]["polygon"]
    assert response.json()["evidence_review_action"] == "inherited"
    with SessionLocal() as db:
        normalized = db.query(ReverseResultRevision).filter_by(
            operation_id=operation_id,
            source="normalized",
        ).one()
        parent_evidence = normalized.payload["image_evidence"]
        assert "review_status" not in parent_evidence[0]
        assert parent_evidence[1]["review_status"] == "approved"
        assert "evidence_id" not in parent_evidence[1]


def test_applied_rejects_pending_evidence_from_explicit_or_inherited_payload(
    client, make_user, auth
):
    user_id = make_user("13973000006", balance=100)
    headers = auth("13973000006")
    operation_id = _seed_review_operation(user_id)
    pending_payload = _saved_review_payload()
    pending_payload["image_evidence"][0]["review_status"] = "pending"
    user_edit = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={"source": "user_edit", "payload": pending_payload},
        headers=headers,
    )
    assert user_edit.status_code == 200, user_edit.text

    for payload in (user_edit.json()["payload"], {}):
        applied = client.post(
            f"/api/prompt/reverse-operations/{operation_id}/revisions",
            json={
                "source": "applied",
                "parent_revision_id": user_edit.json()["id"],
                "payload": payload,
            },
            headers=headers,
        )
        assert applied.status_code == 422, applied.text
        assert "applied" in applied.text
        assert "尚未明确确认" in applied.text

    with SessionLocal() as db:
        assert db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count() == 3


@pytest.mark.parametrize("source", ["user_edit", "applied"])
def test_revision_save_accepts_strict_reviewed_image_evidence(
    client, make_user, auth, source
):
    user_id = make_user("13973000001", balance=100)
    headers = auth("13973000001")
    operation_id = _seed_review_operation(user_id)

    edit = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={"source": "user_edit", "payload": _saved_review_payload()},
        headers=headers,
    )
    assert edit.status_code == 200, edit.text
    response = edit if source == "user_edit" else client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "applied",
            "parent_revision_id": edit.json()["id"],
            "payload": edit.json()["payload"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["source"] == source
    assert response.json()["payload"]["image_evidence"][1]["polygon"][2] == {
        "x": 1.0,
        "y": 1.0,
    }


def test_revision_save_accepts_manual_evidence_when_provider_returned_none(
    client, make_user, auth
):
    user_id = make_user("13973000007", balance=100)
    headers = auth("13973000007")
    operation_id = _seed_review_operation(user_id, source_count=1)
    manual_evidence = {
        "evidence_id": "manual:1:1",
        "evidence_type": "ocr",
        "bbox": {"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6},
        "field_key": "标题",
        "evidence_text": "人工校正文字",
        "confidence": 1.0,
        "source_index": 1,
        "fact_status": "visible",
        "protected": False,
        "editable": True,
        "review_status": "confirmed",
        "analyzer": "人工标注",
        "analyzer_source": "manual",
        "analyzer_status": "analyzed",
        "analyzer_version": "manual.v1",
        "evidence_source": "manual",
        "analysis_status": "ready",
        "degraded_reason": None,
        "conflict_group": None,
        "conflict_status": "none",
        "conflicts_with": [],
    }
    payload = {
        "final_text": "manual evidence prompt",
        "structured": {},
        "image_evidence": [manual_evidence],
    }

    edit = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={"source": "user_edit", "payload": payload},
        headers=headers,
    )
    assert edit.status_code == 200, edit.text
    saved = edit.json()["payload"]["image_evidence"][0]
    assert saved["evidence_id"] == "manual:1:1"
    assert saved["analyzer_source"] == "manual"
    assert saved["editable"] is True

    applied = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "applied",
            "parent_revision_id": edit.json()["id"],
            "payload": edit.json()["payload"],
        },
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["payload"]["image_evidence"][0] == saved


@pytest.mark.parametrize("source", ["user_edit", "applied"])
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["image_evidence"][0].pop("evidence_id"),
            "evidence_id",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(evidence_id=123),
            "evidence_id",
        ),
        (
            lambda payload: payload["image_evidence"][0].pop("review_status"),
            "review_status",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(
                evidence_id="background-primary-001"
            ),
            "重复",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(source_index=3),
            "不存在的源图",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(
                bbox={"x": 0.8, "y": 0.2, "width": 0.4, "height": 0.2}
            ),
            "bbox",
        ),
        (
            lambda payload: payload["image_evidence"][1].update(
                polygon=[
                    {"x": 0.1, "y": 0.1},
                    {"x": 0.2, "y": 0.2},
                    {"x": 0.3, "y": 0.3},
                ]
            ),
            "polygon",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(
                polygon=[
                    {"x": 0.2, "y": 0.1},
                    {"x": 0.6, "y": 0.1},
                    {"x": 0.6, "y": 0.8},
                ]
            ),
            "bbox 和 polygon",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(review_status="approved"),
            "review_status",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(review_status=None),
            "review_status",
        ),
        (
            lambda payload: payload["image_evidence"][0].update(
                fact_status="inferred"
            ),
            "可见事实",
        ),
        (
            lambda payload: payload["image_evidence"][1].update(
                protected=False,
                editable=False,
            ),
            "必须且只能",
        ),
        (
            lambda payload: payload["image_evidence"][1].update(
                protected=True,
                editable=True,
            ),
            "不能同时",
        ),
    ],
)
def test_revision_save_rejects_invalid_reviewed_image_evidence_before_insert(
    client,
    make_user,
    auth,
    source,
    mutate,
    message,
):
    user_id = make_user("13973000002", balance=100)
    headers = auth("13973000002")
    operation_id = _seed_review_operation(user_id)
    payload = _saved_review_payload()
    mutate(payload)

    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={"source": source, "payload": payload},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert "图片证据审阅数据无效" in response.text
    assert message in response.text
    with SessionLocal() as db:
        assert db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count() == 2


def test_revision_save_rejects_explicit_null_image_evidence(client, make_user, auth):
    user_id = make_user("13973000003", balance=100)
    headers = auth("13973000003")
    operation_id = _seed_review_operation(user_id)

    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "user_edit",
            "payload": {"final_text": "reviewed image", "image_evidence": None},
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert "image_evidence 必须是数组" in response.text
