from __future__ import annotations

import base64
import io
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image

from app.db import SessionLocal
from app.models import GenTask, ReverseOperation, ReverseResultRevision
from app.services import gateway, reverse_lineage, reverse_operations, storage
from app.services.generation_image_evidence import (
    ReviewedEvidenceMaskError,
    ReviewedEvidencePlan,
    ReviewedEvidenceRegion,
    image_mask_readiness_for_operation,
    rasterize_reviewed_evidence_mask,
    resolve_reviewed_evidence_plan,
)


def _image_bytes(size=(20, 10), color=(120, 160, 200)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _data_uri(size=(20, 10)) -> str:
    return f"data:image/png;base64,{base64.b64encode(_image_bytes(size)).decode('ascii')}"


def _evidence(
    evidence_id: str,
    *,
    source_index: int = 1,
    protected: bool = False,
    editable: bool = True,
    review_status: str = "confirmed",
    bbox: dict | None = None,
) -> dict:
    return {
        "evidence_id": evidence_id,
        "evidence_type": "visual_field",
        "bbox": bbox or {"x": 0.25, "y": 0.2, "width": 0.5, "height": 0.4},
        "field_key": "background",
        "evidence_text": "background region",
        "confidence": 0.9,
        "source_index": source_index,
        "fact_status": "visible",
        "protected": protected,
        "editable": editable,
        "review_status": review_status,
    }


class _FakeDb:
    def __init__(self, operation, revision):
        self.operation = operation
        self.revision = revision

    def get(self, model, row_id):
        if model is ReverseOperation and int(row_id) == int(self.operation.id):
            return self.operation
        if model is ReverseResultRevision and int(row_id) == int(self.revision.id):
            return self.revision
        return None


def test_plan_uses_only_confirmed_visible_regions_from_the_actual_edit_source():
    primary = "http://example.com/primary.png"
    style = "http://example.com/style.png"
    operation = SimpleNamespace(
        id=11,
        user_id=7,
        asset_url=primary,
        request_context={
            "sources": [
                {"asset_url": primary, "role": "primary"},
                {"asset_url": style, "role": "style"},
            ]
        },
    )
    revision = SimpleNamespace(
        id=22,
        operation_id=11,
        user_id=7,
        version=3,
        source="applied",
        payload={
            "image_evidence": [
                _evidence("edit-primary"),
                _evidence("pending-style", source_index=2, review_status="pending"),
            ]
        },
    )
    task = SimpleNamespace(
        reverse_operation_id=11,
        source_revision_id=22,
        user_id=7,
    )

    plan = resolve_reviewed_evidence_plan(_FakeDb(operation, revision), task, edit_source_url=primary)

    assert plan is not None
    assert plan.source_index == 1
    assert [region.evidence_id for region in plan.editable_regions] == ["edit-primary"]
    assert plan.protected_regions == ()


def test_plan_does_not_build_a_mask_from_pending_applied_evidence():
    primary = "http://example.com/pending-primary.png"
    operation = SimpleNamespace(
        id=14,
        user_id=10,
        asset_url=primary,
        request_context={"sources": [{"asset_url": primary}]},
    )
    revision = SimpleNamespace(
        id=25,
        operation_id=14,
        user_id=10,
        version=4,
        source="applied",
        payload={
            "image_evidence": [
                _evidence("pending-edit", review_status="pending")
            ]
        },
    )
    task = SimpleNamespace(
        reverse_operation_id=14,
        source_revision_id=25,
        user_id=10,
    )

    assert resolve_reviewed_evidence_plan(
        _FakeDb(operation, revision),
        task,
        edit_source_url=primary,
    ) is None


def test_plan_rejects_confirmed_regions_for_a_secondary_reference():
    primary = "http://example.com/primary.png"
    style = "http://example.com/style.png"
    operation = SimpleNamespace(
        id=12,
        user_id=8,
        asset_url=primary,
        request_context={"sources": [{"asset_url": primary}, {"asset_url": style}]},
    )
    revision = SimpleNamespace(
        id=23,
        operation_id=12,
        user_id=8,
        version=3,
        source="user_edit",
        payload={"image_evidence": [_evidence("style-mask", source_index=2)]},
    )
    task = SimpleNamespace(reverse_operation_id=12, source_revision_id=23, user_id=8)

    with pytest.raises(ReviewedEvidenceMaskError, match="单蒙版"):
        resolve_reviewed_evidence_plan(_FakeDb(operation, revision), task, edit_source_url=primary)


def test_plan_reads_legacy_unreviewed_candidates_leniently_without_executing_them():
    primary = "http://example.com/legacy-primary.png"
    legacy_candidate = _evidence("legacy-candidate")
    legacy_candidate.pop("evidence_id")
    legacy_candidate.pop("review_status")
    operation = SimpleNamespace(
        id=13,
        user_id=9,
        asset_url=primary,
        request_context={"sources": [{"asset_url": primary}]},
    )
    revision = SimpleNamespace(
        id=24,
        operation_id=13,
        user_id=9,
        version=1,
        source="applied",
        payload={"image_evidence": [legacy_candidate]},
    )
    task = SimpleNamespace(reverse_operation_id=13, source_revision_id=24, user_id=9)

    assert (
        resolve_reviewed_evidence_plan(
            _FakeDb(operation, revision),
            task,
            edit_source_url=primary,
        )
        is None
    )


def test_rasterizer_defaults_to_protection_when_editable_regions_exist():
    editable = ReviewedEvidenceRegion(
        evidence_id="editable",
        source_index=1,
        protected=False,
        bbox=(0.25, 0.2, 0.5, 0.4),
        polygon=None,
    )
    protected = ReviewedEvidenceRegion(
        evidence_id="protected",
        source_index=1,
        protected=True,
        bbox=(0.45, 0.3, 0.1, 0.2),
        polygon=None,
    )
    plan = ReviewedEvidencePlan(
        operation_id=1,
        revision_id=2,
        revision_version=3,
        source_index=1,
        source_url="http://example.com/source.png",
        protected_regions=(protected,),
        editable_regions=(editable,),
    )

    result = rasterize_reviewed_evidence_mask(plan, reference_data_uri=_data_uri())
    raw = base64.b64decode(result.data_uri.split(",", 1)[1])
    alpha = Image.open(io.BytesIO(raw)).getchannel("A")

    assert alpha.getpixel((0, 0)) == 255
    assert alpha.getpixel((6, 3)) == 0
    assert alpha.getpixel((9, 3)) == 255
    assert len(result.mask_hash) == 64
    assert result.metadata["_evidence_mask_revision_id"] == 2


def test_rasterizer_defaults_to_editable_when_only_protected_regions_exist():
    plan = ReviewedEvidencePlan(
        operation_id=1,
        revision_id=2,
        revision_version=3,
        source_index=1,
        source_url="http://example.com/source.png",
        protected_regions=(ReviewedEvidenceRegion(
            evidence_id="logo",
            source_index=1,
            protected=True,
            bbox=(0.25, 0.2, 0.5, 0.4),
            polygon=None,
        ),),
        editable_regions=(),
    )

    result = rasterize_reviewed_evidence_mask(plan, reference_data_uri=_data_uri())
    raw = base64.b64decode(result.data_uri.split(",", 1)[1])
    alpha = Image.open(io.BytesIO(raw)).getchannel("A")

    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((6, 3)) == 255


def test_generation_sends_server_rasterized_reviewed_evidence_mask(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    user_id = make_user("13972000001", balance=1000)
    headers = auth("13972000001")
    source_bytes = _image_bytes((80, 40))
    upload = client.post(
        "/api/uploads/image",
        files={"file": ("source.png", source_bytes, "image/png")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    asset_url = upload.json()["url"]

    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=f"{user_id:064d}"[-64:],
            target="image",
            asset_url=asset_url,
            status="succeeded",
            progress=100,
            request_context={
                "source_type": "image",
                "sources": [{"asset_url": asset_url, "source_type": "image", "role": "primary"}],
            },
            result={"final_text": "replace the background", "structured": {}},
            normalized_result={"final_text": "replace the background", "structured": {}},
        )
        db.add(operation)
        db.flush()
        payload = {
            "final_text": "replace the background",
            "structured": {},
            "image_evidence": [_evidence("editable-background")],
        }
        fingerprint = reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(source_bytes),
            locator=asset_url,
            method="test_upload_sha256",
        )
        fingerprints = [fingerprint]
        content_hash = reverse_lineage.source_content_hash(fingerprints)
        revisions = []
        for version, source, revision_payload in (
            (1, "provider_raw", {"provider": "test", "result": payload}),
            (2, "normalized", payload),
            (3, "user_edit", payload),
            (4, "applied", payload),
        ):
            revision = ReverseResultRevision(
                operation_id=operation.id,
                user_id=user_id,
                version=version,
                source=source,
                payload=revision_payload,
                parent_revision_id=revisions[-1].id if revisions else None,
                source_content_hash=content_hash,
                source_fingerprints=fingerprints,
                payload_hash=reverse_lineage.canonical_payload_hash(revision_payload),
                lineage_status=reverse_lineage.VERIFIED,
                evidence_review_action=(
                    "updated" if source in {"normalized", "user_edit"} else "inherited"
                ),
            )
            db.add(revision)
            db.flush()
            revisions.append(revision)
        db.commit()
        operation_id = int(operation.id)
        revision_id = int(revisions[-1].id)

    seen = {}

    def fake_gen_image(*_args, **kwargs):
        seen["mask"] = (kwargs.get("extra_payload") or {}).get("mask")
        return [gateway._mock_image("reviewed evidence mask", "1024x1024", 0)]

    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)
    response = quote_and_generate(
        {
            "client_request_id": "reviewed-evidence-generation-001",
            "reverse_operation_id": operation_id,
            "reverse_revision_id": revision_id,
            "source_asset_url": asset_url,
            "source_type": "image",
            "category": "image",
            "stage": "preview",
            "instruction": "replace the background",
            "params": {"n": 1, "size": "1024x1024"},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert str(seen.get("mask") or "").startswith("data:image/png;base64,")

    with SessionLocal() as db:
        task = db.get(GenTask, response.json()["id"])
        assert task is not None
        assert task.status == "succeeded"
        assert task.params["_edit_mask_mode"] == "reviewed_evidence"
        assert task.params["_edit_mask_sent"] is True
        assert task.params["_evidence_mask_revision_id"] == revision_id
        generation_revision = db.get(ReverseResultRevision, task.generation_revision_id)
        assert generation_revision.payload["image_mask"]["source_revision_id"] == revision_id
        assert generation_revision.payload["image_mask"]["mask_hash"] == task.params["_evidence_mask_hash"]


# ---------------------------------------------------------------------------
# 服务端蒙版就绪状态（image_mask_readiness）随 result/operation 回传
# ---------------------------------------------------------------------------


def _readiness_operation(db, user_id: int, asset_url: str, *, target: str = "image"):
    operation = ReverseOperation(
        user_id=user_id,
        request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        target=target,
        asset_url=asset_url,
        status="succeeded",
        progress=100,
        request_context={
            "source_type": "image",
            "sources": [{"asset_url": asset_url, "source_type": "image", "role": "primary"}],
        },
        result={"final_text": "prompt", "structured": {}},
        normalized_result={"final_text": "prompt", "structured": {}},
    )
    db.add(operation)
    db.flush()
    return operation


def _reviewed_revision(
    db,
    operation,
    payload: dict,
    *,
    fingerprints=None,
    lineage_status=reverse_lineage.VERIFIED,
    source: str = "user_edit",
    version: int = 1,
):
    revision = ReverseResultRevision(
        operation_id=operation.id,
        user_id=operation.user_id,
        version=version,
        source=source,
        payload=payload,
        source_content_hash=(
            reverse_lineage.source_content_hash(fingerprints) if fingerprints else None
        ),
        source_fingerprints=fingerprints,
        payload_hash=reverse_lineage.canonical_payload_hash(payload),
        lineage_status=lineage_status,
        evidence_review_action="updated",
    )
    db.add(revision)
    db.flush()
    return revision


def _uploaded_asset(client, headers):
    upload = client.post(
        "/api/uploads/image",
        files={"file": ("readiness.png", _image_bytes((32, 16)), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    asset_url = upload.json()["url"]
    stored = storage.local_path(storage.key_from_url(asset_url)).read_bytes()
    return asset_url, stored


def _serialized_readiness(operation):
    serialized = reverse_operations.serialize_operation(operation)
    readiness = serialized["result"]["image_mask_readiness"]
    assert readiness == image_mask_readiness_for_operation(
        operation, supported=operation.target in reverse_operations.IMAGE_EVIDENCE_TARGETS
    )
    return readiness


def test_mask_readiness_reaches_ready_and_hash_changed_states(client, make_user, auth):
    user_id = make_user("13972100001", balance=100)
    headers = auth("13972100001")
    asset_url, stored = _uploaded_asset(client, headers)

    with SessionLocal() as db:
        operation = _readiness_operation(db, user_id, asset_url)

        # 尚未保存审阅版本 → 待校验
        assert _serialized_readiness(operation)["status"] == "pending"

        fingerprint = reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(stored),
            locator=asset_url,
        )
        revision = _reviewed_revision(
            db,
            operation,
            {
                "final_text": "prompt",
                "structured": {},
                "image_evidence": [_evidence("readiness-bg")],
            },
            fingerprints=[fingerprint],
        )

        # 已确认区域 + 血缘验证 + 指纹匹配 → ready
        readiness = _serialized_readiness(operation)
        assert readiness["status"] == "ready", readiness
        assert readiness["revision_id"] == int(revision.id)
        assert readiness["revision_version"] == 1

        # 源素材内容被替换 → source_hash_changed
        path = storage.local_path(storage.key_from_url(asset_url))
        path.write_bytes(_image_bytes((48, 48), color=(9, 9, 9)))
        assert _serialized_readiness(operation)["status"] == "source_hash_changed"

        # 源素材文件不可读 → source_expired
        path.unlink()
        assert _serialized_readiness(operation)["status"] == "source_expired"


def test_mask_readiness_reports_pending_degraded_and_unsupported(client, make_user, auth):
    user_id = make_user("13972100002", balance=100)
    headers = auth("13972100002")
    asset_url, stored = _uploaded_asset(client, headers)
    fingerprint = reverse_lineage.build_source_fingerprint(
        source_index=1,
        content_hash=reverse_lineage.bytes_content_hash(stored),
        locator=asset_url,
    )

    with SessionLocal() as db:
        # 审阅版本存在但未确认任何区域 → 仍是 pending
        unconfirmed = _readiness_operation(db, user_id, asset_url)
        _reviewed_revision(
            db,
            unconfirmed,
            {"image_evidence": [_evidence("still-pending", review_status="pending")]},
            fingerprints=[fingerprint],
        )
        assert _serialized_readiness(unconfirmed)["status"] == "pending"

        # 已确认区域但血缘未验证 → degraded
        legacy = _readiness_operation(db, user_id, asset_url)
        _reviewed_revision(
            db,
            legacy,
            {"image_evidence": [_evidence("legacy-confirmed")]},
            fingerprints=None,
            lineage_status=reverse_lineage.LEGACY_UNVERIFIED,
        )
        assert _serialized_readiness(legacy)["status"] == "degraded"

        # 审阅 payload 结构损坏 → degraded（不抛错、不阻塞序列化）
        broken = _readiness_operation(db, user_id, asset_url)
        _reviewed_revision(db, broken, {"image_evidence": {"bad": 1}}, fingerprints=None)
        assert _serialized_readiness(broken)["status"] == "degraded"

        # 不支持证据蒙版的反推目标 → unsupported
        video = _readiness_operation(db, user_id, asset_url, target="video")
        assert _serialized_readiness(video)["status"] == "unsupported"

        # 结果已过期 → source_expired
        expired = _readiness_operation(db, user_id, asset_url)
        expired.error_code = "RESULT_EXPIRED"
        assert _serialized_readiness(expired)["status"] == "source_expired"


def test_mask_readiness_stays_pending_without_a_session():
    operation = SimpleNamespace(
        id=None,
        user_id=1,
        target="image",
        error_code=None,
        asset_url="http://example.com/a.png",
        request_context={},
    )
    assert image_mask_readiness_for_operation(operation)["status"] == "pending"
    operation.error_code = "RESULT_EXPIRED"
    assert image_mask_readiness_for_operation(operation)["status"] == "source_expired"
    operation.error_code = None
    assert (
        image_mask_readiness_for_operation(operation, supported=False)["status"]
        == "unsupported"
    )
