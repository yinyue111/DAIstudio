"""Security and lifecycle coverage for immutable reverse-result revisions."""
from __future__ import annotations

import base64
import hashlib
import io
from copy import deepcopy

import pytest
from fastapi import HTTPException
from PIL import Image

from app.db import SessionLocal
from app.models import GenerationQuote, GenTask, ReverseOperation, ReverseResultRevision
from app.routers import prompt as prompt_router
from app.services import reverse_lineage
from app.services.generation_image_evidence import (
    ReviewedEvidenceMaskError,
    ReviewedEvidencePlan,
    ReviewedEvidenceRegion,
    rasterize_reviewed_evidence_mask,
)


def _base_payload(*, image_evidence: list[dict] | None = None) -> dict:
    payload = {
        "final_text": "a silver perfume bottle on a clean studio background",
        "structured": {"subject": "silver perfume bottle"},
    }
    if image_evidence is not None:
        payload["image_evidence"] = deepcopy(image_evidence)
    return payload


def _reviewed_evidence() -> list[dict]:
    return [{
        "evidence_id": "editable-background",
        "evidence_type": "visual_field",
        "bbox": {"x": 0.0, "y": 0.0, "width": 0.25, "height": 1.0},
        "field_key": "scene_background",
        "evidence_text": "plain studio background",
        "confidence": 0.96,
        "source_index": 1,
        "fact_status": "visible",
        "protected": False,
        "editable": True,
        "review_status": "confirmed",
    }]


def _seed_operation(
    user_id: int,
    *,
    payload: dict | None = None,
    through: str | None = "normalized",
    lineage_status: str = reverse_lineage.VERIFIED,
    suffix: str = "default",
) -> tuple[int, dict[str, int]]:
    normalized_payload = deepcopy(payload or _base_payload())
    asset_url = f"http://example.com/lineage-{user_id}-{suffix}.png"
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=hashlib.sha256(
                f"{user_id}:{suffix}:{asset_url}".encode()
            ).hexdigest(),
            target="image",
            asset_url=asset_url,
            output_purpose="generation",
            request_context={
                "source_type": "image",
                "output_purpose": "generation",
                "sources": [{
                    "asset_url": asset_url,
                    "source_type": "image",
                    "role": "primary",
                }],
            },
            status="succeeded",
            progress=100,
            result=deepcopy(normalized_payload),
            normalized_result=deepcopy(normalized_payload),
        )
        db.add(operation)
        db.flush()
        ids: dict[str, int] = {}
        if through is not None:
            fingerprint = reverse_lineage.build_source_fingerprint(
                source_index=1,
                content_hash=reverse_lineage.bytes_content_hash(
                    f"source-bytes:{user_id}:{suffix}".encode()
                ),
                locator=asset_url,
                method="test_source_sha256",
            )
            fingerprints = [fingerprint]
            source_hash = reverse_lineage.source_content_hash(fingerprints)
            parent_id = None
            sources = ("provider_raw", "normalized", "user_edit", "applied")
            for version, source in enumerate(sources, start=1):
                revision_payload = (
                    {"provider": "test", "result": deepcopy(normalized_payload)}
                    if source == "provider_raw"
                    else deepcopy(normalized_payload)
                )
                verified = lineage_status == reverse_lineage.VERIFIED
                revision = ReverseResultRevision(
                    operation_id=operation.id,
                    user_id=user_id,
                    version=version,
                    source=source,
                    payload=revision_payload,
                    parent_revision_id=parent_id,
                    source_content_hash=source_hash if verified else None,
                    source_fingerprints=deepcopy(fingerprints) if verified else None,
                    payload_hash=(
                        reverse_lineage.canonical_payload_hash(revision_payload)
                        if verified else None
                    ),
                    lineage_status=lineage_status,
                    evidence_review_action=(
                        "updated" if source in {"normalized", "user_edit"}
                        and "image_evidence" in revision_payload
                        else "not_applicable"
                    ),
                )
                db.add(revision)
                db.flush()
                ids[source] = int(revision.id)
                parent_id = int(revision.id)
                if source == through:
                    break
        db.commit()
        return int(operation.id), ids


def _post_revision(client, headers, operation_id: int, **body):
    return client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json=body,
        headers=headers,
    )


def _post_apply(client, headers, operation_id: int, **body):
    return client.post(
        f"/api/prompt/reverse-operations/{operation_id}/apply",
        json=body,
        headers=headers,
    )


def _generation_request(operation_id: int, revision_id: int, request_id: str) -> dict:
    return {
        "client_request_id": request_id,
        "reverse_operation_id": operation_id,
        "reverse_revision_id": revision_id,
        "category": "image",
        "stage": "preview",
        "instruction": "a silver perfume bottle on a clean studio background",
        "params": {"n": 1, "size": "1024x1024"},
    }


@pytest.mark.parametrize("source", ["user_edit", "applied"])
def test_empty_succeeded_operation_cannot_forge_client_revision(
    client, make_user, auth, source
):
    user_id = make_user("13973000001")
    headers = auth("13973000001")
    operation_id, _ = _seed_operation(user_id, through=None, suffix=source)

    response = _post_revision(
        client,
        headers,
        operation_id,
        source=source,
        payload=_base_payload(),
    )

    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.query(ReverseResultRevision).filter_by(operation_id=operation_id).count() == 0


def test_client_revision_rejects_cross_operation_parent(client, make_user, auth):
    user_id = make_user("13973000002")
    headers = auth("13973000002")
    operation_id, _ = _seed_operation(user_id, suffix="cross-parent-child")
    _, foreign_ids = _seed_operation(user_id, suffix="cross-parent-foreign")

    response = _post_revision(
        client,
        headers,
        operation_id,
        source="user_edit",
        parent_revision_id=foreign_ids["normalized"],
        payload=_base_payload(),
    )

    assert response.status_code == 409, response.text
    assert "同一反推任务" in response.text


@pytest.mark.parametrize(
    ("source", "parent_source"),
    [
        ("user_edit", "provider_raw"),
        ("user_edit", "applied"),
        ("applied", "normalized"),
    ],
)
def test_client_revision_rejects_illegal_transition(
    client, make_user, auth, source, parent_source
):
    user_id = make_user("13973000003")
    headers = auth("13973000003")
    operation_id, revision_ids = _seed_operation(
        user_id,
        through="applied" if parent_source == "applied" else "normalized",
        suffix=f"illegal-{source}-{parent_source}",
    )

    response = _post_revision(
        client,
        headers,
        operation_id,
        source=source,
        parent_revision_id=revision_ids[parent_source],
        payload=_base_payload(),
    )

    assert response.status_code == 409, response.text


@pytest.mark.parametrize(
    "reserved_key",
    [
        "parent_revision_id",
        "payload_hash",
        "source_content_hash",
        "source_fingerprints",
        "lineage_status",
        "evidence_review_action",
        "_lineage",
    ],
)
def test_client_payload_cannot_forge_reserved_lineage_fields(
    client, make_user, auth, reserved_key
):
    user_id = make_user("13973000004")
    headers = auth("13973000004")
    operation_id, revision_ids = _seed_operation(
        user_id,
        suffix=f"reserved-{reserved_key}",
    )
    payload = _base_payload()
    payload[reserved_key] = "forged"

    response = _post_revision(
        client,
        headers,
        operation_id,
        source="user_edit",
        parent_revision_id=revision_ids["normalized"],
        payload=payload,
    )

    assert response.status_code == 422, response.text
    assert "血缘字段由服务端生成" in response.text


def test_atomic_apply_creates_user_edit_and_applied_and_updates_operation(
    client, make_user, auth
):
    user_id = make_user("13973000011")
    headers = auth("13973000011")
    operation_id, revision_ids = _seed_operation(
        user_id, suffix="atomic-apply-success"
    )
    payload = _base_payload()
    payload["final_text"] = "a reviewed silver perfume bottle with cool rim lighting"

    response = _post_apply(
        client,
        headers,
        operation_id,
        parent_revision_id=revision_ids["normalized"],
        payload=payload,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_edit"]["source"] == "user_edit"
    assert body["applied"]["source"] == "applied"
    assert body["applied"]["parent_revision_id"] == body["user_edit"]["id"]
    assert body["user_edit"]["payload"] == body["applied"]["payload"]
    with SessionLocal() as db:
        revisions = (
            db.query(ReverseResultRevision)
            .filter_by(operation_id=operation_id)
            .order_by(ReverseResultRevision.version)
            .all()
        )
        operation = db.get(ReverseOperation, operation_id)
        assert [row.source for row in revisions] == [
            "provider_raw", "normalized", "user_edit", "applied",
        ]
        assert operation is not None
        assert operation.applied_result_version == body["applied"]["version"]


def test_atomic_apply_rejects_stale_parent_without_partial_revision(
    client, make_user, auth
):
    user_id = make_user("13973000012")
    headers = auth("13973000012")
    operation_id, revision_ids = _seed_operation(
        user_id, suffix="atomic-apply-stale"
    )
    first = _post_apply(
        client,
        headers,
        operation_id,
        parent_revision_id=revision_ids["normalized"],
        payload={**_base_payload(), "final_text": "first reviewed edit"},
    )
    assert first.status_code == 200, first.text

    stale = _post_apply(
        client,
        headers,
        operation_id,
        parent_revision_id=revision_ids["normalized"],
        payload={**_base_payload(), "final_text": "stale concurrent edit"},
    )

    assert stale.status_code == 409, stale.text
    assert "已更新" in stale.text
    with SessionLocal() as db:
        rows = db.query(ReverseResultRevision).filter_by(operation_id=operation_id).all()
        assert len(rows) == 4


def test_atomic_apply_rolls_back_user_edit_when_applied_evidence_is_invalid(
    client, make_user, auth
):
    user_id = make_user("13973000013")
    headers = auth("13973000013")
    original = _base_payload(image_evidence=_reviewed_evidence())
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=original,
        suffix="atomic-apply-invalid-evidence",
    )
    pending = _reviewed_evidence()
    pending[0]["review_status"] = "pending"

    response = _post_apply(
        client,
        headers,
        operation_id,
        parent_revision_id=revision_ids["normalized"],
        payload=_base_payload(image_evidence=pending),
    )

    assert response.status_code == 422, response.text
    with SessionLocal() as db:
        rows = db.query(ReverseResultRevision).filter_by(operation_id=operation_id).all()
        operation = db.get(ReverseOperation, operation_id)
        assert len(rows) == 2
        assert operation is not None
        assert operation.applied_result_version is None


def test_atomic_apply_content_rejection_has_zero_side_effects(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13973000014")
    headers = auth("13973000014")
    operation_id, revision_ids = _seed_operation(
        user_id, suffix="atomic-apply-content-rejection"
    )

    def reject(*_args, **_kwargs):
        raise HTTPException(400, "内容安全拦截")

    monkeypatch.setattr(prompt_router, "_assert_text_allowed", reject)
    response = _post_apply(
        client,
        headers,
        operation_id,
        parent_revision_id=revision_ids["normalized"],
        payload=_base_payload(),
    )

    assert response.status_code == 400, response.text
    with SessionLocal() as db:
        rows = db.query(ReverseResultRevision).filter_by(operation_id=operation_id).all()
        operation = db.get(ReverseOperation, operation_id)
        assert len(rows) == 2
        assert operation is not None
        assert operation.applied_result_version is None


@pytest.mark.parametrize("source", ["normalized", "user_edit"])
@pytest.mark.parametrize("endpoint", ["/api/quotes", "/api/generate"])
def test_generation_contract_rejects_non_applied_reverse_revision(
    client, make_user, auth, quote_generation, source, endpoint
):
    suffix = f"reject-non-applied-{source}-{endpoint.rsplit('/', 1)[-1]}"
    phone = f"13974{abs(hash(suffix)) % 1000000:06d}"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    operation_id, revision_ids = _seed_operation(
        user_id,
        through=source,
        suffix=suffix,
    )

    request = _generation_request(
        operation_id,
        revision_ids[source],
        f"non-applied-{source}-{endpoint.rsplit('/', 1)[-1]}",
    )
    expected_quotes = 0
    if endpoint == "/api/generate":
        baseline = {
            key: value
            for key, value in request.items()
            if key not in {"reverse_operation_id", "reverse_revision_id"}
        }
        baseline["client_request_id"] = f"baseline-{source}-generate-001"
        quote = quote_generation(baseline, headers=headers)
        request["quote_id"] = quote["quote_id"]
        expected_quotes = 1

    response = client.post(endpoint, json=request, headers=headers)

    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        quotes = db.query(GenerationQuote).filter_by(user_id=user_id).all()
        assert len(quotes) == expected_quotes
        if quotes:
            assert quotes[0].status == "active"
            assert quotes[0].task_id is None
        assert db.query(GenTask).filter_by(user_id=user_id).count() == 0


def test_valid_client_chain_can_quote_and_generate(client, make_user, auth):
    user_id = make_user("13973000005", balance=1000)
    headers = auth("13973000005")
    operation_id, revision_ids = _seed_operation(user_id, suffix="valid-full-chain")
    edited_payload = _base_payload()
    edited_payload["final_text"] = "a silver perfume bottle with cool rim lighting"

    edited = _post_revision(
        client,
        headers,
        operation_id,
        source="user_edit",
        parent_revision_id=revision_ids["normalized"],
        payload=edited_payload,
    )
    assert edited.status_code == 200, edited.text
    applied = _post_revision(
        client,
        headers,
        operation_id,
        source="applied",
        parent_revision_id=edited.json()["id"],
        payload={},
    )
    assert applied.status_code == 200, applied.text
    request = _generation_request(
        operation_id,
        applied.json()["id"],
        "reverse-full-chain-generate-001",
    )

    quoted = client.post("/api/quotes", json=request, headers=headers)
    assert quoted.status_code == 201, quoted.text
    generated = client.post(
        "/api/generate",
        json={**request, "quote_id": quoted.json()["quote_id"]},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text

    with SessionLocal() as db:
        sources = [
            row.source
            for row in db.query(ReverseResultRevision)
            .filter_by(operation_id=operation_id)
            .order_by(ReverseResultRevision.version)
        ]
        assert sources == [
            "provider_raw",
            "normalized",
            "user_edit",
            "applied",
            "model_compiled",
            "generation",
        ]


def test_image_evidence_inherits_and_only_user_edit_can_clear_explicitly(
    client, make_user, auth
):
    user_id = make_user("13973000006")
    headers = auth("13973000006")
    original_payload = _base_payload(image_evidence=_reviewed_evidence())
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=original_payload,
        suffix="evidence-lifecycle",
    )

    inherited = _post_revision(
        client,
        headers,
        operation_id,
        source="user_edit",
        parent_revision_id=revision_ids["normalized"],
        payload={"final_text": "edited", "structured": {}},
    )
    assert inherited.status_code == 200, inherited.text
    pending_evidence = _reviewed_evidence()
    pending_evidence[0]["review_status"] = "pending"
    assert inherited.json()["payload"]["image_evidence"] == pending_evidence
    assert inherited.json()["evidence_review_action"] == "inherited"

    silent_apply_clear = _post_revision(
        client,
        headers,
        operation_id,
        source="applied",
        parent_revision_id=inherited.json()["id"],
        payload={"final_text": "edited", "structured": {}, "image_evidence": []},
    )
    assert silent_apply_clear.status_code == 422, silent_apply_clear.text
    assert "applied" in silent_apply_clear.text

    implicit_clear = _post_revision(
        client,
        headers,
        operation_id,
        source="user_edit",
        parent_revision_id=revision_ids["normalized"],
        payload={"final_text": "edited", "structured": {}, "image_evidence": []},
    )
    assert implicit_clear.status_code == 422, implicit_clear.text
    assert "clear_image_evidence" in implicit_clear.text

    cleared = _post_revision(
        client,
        headers,
        operation_id,
        source="user_edit",
        parent_revision_id=revision_ids["normalized"],
        payload={"final_text": "edited", "structured": {}, "image_evidence": []},
        clear_image_evidence=True,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["payload"]["image_evidence"] == []
    assert cleared.json()["evidence_review_action"] == "cleared"

    applied = _post_revision(
        client,
        headers,
        operation_id,
        source="applied",
        parent_revision_id=cleared.json()["id"],
        payload={},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["payload"]["image_evidence"] == []
    assert applied.json()["evidence_review_action"] == "inherited"


def test_atomic_apply_filters_to_confirmed_image_evidence_and_can_clear_it(
    client, make_user, auth
):
    user_id = make_user("13973000015")
    headers = auth("13973000015")
    confirmed = _reviewed_evidence()[0]
    pending = deepcopy(confirmed)
    pending.update({
        "evidence_id": "pending-caption",
        "field_key": "caption",
        "evidence_text": "unreviewed caption",
        "review_status": "pending",
    })
    operation_id, revision_ids = _seed_operation(
        user_id,
        payload=_base_payload(image_evidence=[confirmed, pending]),
        suffix="atomic-evidence-selection",
    )

    selected = _post_apply(
        client,
        headers,
        operation_id,
        parent_revision_id=revision_ids["normalized"],
        payload={
            **_base_payload(image_evidence=[confirmed]),
            "application_selection": {"prompt": True, "imageEvidence": True},
            "changed_fields": ["prompt", "image_evidence"],
        },
    )

    assert selected.status_code == 200, selected.text
    assert selected.json()["user_edit"]["payload"]["image_evidence"] == [confirmed]
    assert selected.json()["applied"]["payload"]["image_evidence"] == [confirmed]
    assert selected.json()["user_edit"]["evidence_review_action"] == "updated"

    cleared = _post_apply(
        client,
        headers,
        operation_id,
        parent_revision_id=selected.json()["user_edit"]["id"],
        payload={
            **_base_payload(image_evidence=[]),
            "application_selection": {"prompt": True, "imageEvidence": False},
            "changed_fields": ["prompt"],
        },
        clear_image_evidence=True,
    )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["user_edit"]["payload"]["image_evidence"] == []
    assert cleared.json()["applied"]["payload"]["image_evidence"] == []
    assert cleared.json()["user_edit"]["evidence_review_action"] == "cleared"


@pytest.mark.parametrize("endpoint", ["/api/quotes", "/api/generate"])
def test_legacy_unverified_applied_revision_cannot_execute(
    client, make_user, auth, quote_generation, endpoint
):
    phone = "13973100007" if endpoint == "/api/quotes" else "13973100017"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    with SessionLocal() as db:
        baseline_quote_ids = {
            int(row.id) for row in db.query(GenerationQuote).filter_by(user_id=user_id)
        }
        baseline_task_ids = {
            int(row.id) for row in db.query(GenTask).filter_by(user_id=user_id)
        }
    operation_id, revision_ids = _seed_operation(
        user_id,
        through="applied",
        lineage_status=reverse_lineage.LEGACY_UNVERIFIED,
        suffix=f"legacy-{endpoint.rsplit('/', 1)[-1]}",
    )

    request = _generation_request(
        operation_id,
        revision_ids["applied"],
        f"legacy-unverified-{endpoint.rsplit('/', 1)[-1]}-001",
    )
    expected_quotes = 0
    if endpoint == "/api/generate":
        baseline = {
            key: value
            for key, value in request.items()
            if key not in {"reverse_operation_id", "reverse_revision_id"}
        }
        baseline["client_request_id"] = "baseline-legacy-generate-001"
        quote = quote_generation(baseline, headers=headers)
        request["quote_id"] = quote["quote_id"]
        expected_quotes = 1

    response = client.post(endpoint, json=request, headers=headers)

    assert response.status_code == 409, response.text
    assert "未完成血缘验证" in response.text
    with SessionLocal() as db:
        quotes = [
            row for row in db.query(GenerationQuote).filter_by(user_id=user_id)
            if int(row.id) not in baseline_quote_ids
        ]
        tasks = [
            row for row in db.query(GenTask).filter_by(user_id=user_id)
            if int(row.id) not in baseline_task_ids
        ]
        assert len(quotes) == expected_quotes
        if quotes:
            assert quotes[0].status == "active"
            assert quotes[0].task_id is None
        assert not tasks


@pytest.mark.parametrize("tamper", ["payload", "source_fingerprint"])
def test_quote_and_generation_reject_revision_hash_inconsistency(
    client, make_user, auth, quote_generation, tamper
):
    phone = "13973100008" if tamper == "payload" else "13973100009"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    with SessionLocal() as db:
        baseline_quote_ids = {
            int(row.id) for row in db.query(GenerationQuote).filter_by(user_id=user_id)
        }
        baseline_task_ids = {
            int(row.id) for row in db.query(GenTask).filter_by(user_id=user_id)
        }
    operation_id, revision_ids = _seed_operation(
        user_id,
        through="applied",
        suffix=f"tamper-{tamper}",
    )
    request = _generation_request(
        operation_id,
        revision_ids["applied"],
        f"tampered-{tamper}-001",
    )
    original_quote = quote_generation(request, headers=headers)

    with SessionLocal() as db:
        revision = db.get(ReverseResultRevision, revision_ids["applied"])
        assert revision is not None
        if tamper == "payload":
            revision.payload = {**revision.payload, "final_text": "tampered after save"}
        else:
            fingerprints = deepcopy(revision.source_fingerprints)
            fingerprints[0]["content_sha256"] = "f" * 64
            revision.source_fingerprints = fingerprints
        db.commit()

    quoted = client.post("/api/quotes", json=request, headers=headers)
    generated = client.post(
        "/api/generate",
        json={**request, "quote_id": original_quote["quote_id"]},
        headers=headers,
    )

    assert quoted.status_code == 409, quoted.text
    assert generated.status_code == 409, generated.text
    with SessionLocal() as db:
        quote = db.get(GenerationQuote, original_quote["quote_id"])
        assert quote is not None
        assert quote.status == "active"
        assert quote.task_id is None
        assert {
            int(row.id) for row in db.query(GenerationQuote).filter_by(user_id=user_id)
        } - baseline_quote_ids == {int(original_quote["quote_id"])}
        assert {
            int(row.id) for row in db.query(GenTask).filter_by(user_id=user_id)
        } == baseline_task_ids


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(output, format="PNG")
    return output.getvalue()


def test_same_source_url_with_changed_bytes_cannot_reuse_evidence_mask():
    original = _png_bytes((20, 40, 60))
    changed = _png_bytes((60, 40, 20))
    plan = ReviewedEvidencePlan(
        operation_id=1,
        revision_id=2,
        revision_version=4,
        source_index=1,
        source_url="http://example.com/same-url.png",
        protected_regions=(),
        editable_regions=(ReviewedEvidenceRegion(
            evidence_id="editable-background",
            source_index=1,
            protected=False,
            bbox=(0.0, 0.0, 0.25, 1.0),
            polygon=None,
        ),),
        source_content_hash=reverse_lineage.bytes_content_hash(original),
    )
    changed_data_uri = (
        "data:image/png;base64," + base64.b64encode(changed).decode("ascii")
    )

    with pytest.raises(ReviewedEvidenceMaskError, match="内容已变更"):
        rasterize_reviewed_evidence_mask(
            plan,
            reference_data_uri=changed_data_uri,
            reference_content_hash=reverse_lineage.bytes_content_hash(changed),
        )
