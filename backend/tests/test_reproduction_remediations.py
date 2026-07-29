"""Focused remediation-plan API coverage."""
from __future__ import annotations

import base64
import hashlib
import uuid

import pytest
from PIL import Image

from app.db import SessionLocal
from app.models import (
    GenAsset,
    GenTask,
    ModelConfig,
    ReproductionAssessment,
    ReproductionFinding,
    ReproductionRemediation,
)
from app.reproduction_schemas import ReproductionRemediationCreateIn
from app.services import asset_refs, reproduction_remediation, storage
from app.services.user_assets import resolve_asset_ref
from tests.test_reproduction_assessments import (
    _create_assessment,
    _jpeg_bytes,
    _seed_assets,
    _seed_reverse_revision,
)


def _image_model() -> int:
    with SessionLocal() as db:
        row = db.query(ModelConfig).filter(
            ModelConfig.use == "image",
            ModelConfig.is_default.is_(True),
        ).one()
        return int(row.id)


def _video_model() -> int:
    with SessionLocal() as db:
        row = db.query(ModelConfig).filter(
            ModelConfig.use == "video",
            ModelConfig.is_default.is_(True),
        ).one()
        return int(row.id)


def test_image_remediation_plan_is_idempotent_and_request_immutable(client, make_user, auth):
    user_id = make_user("13977119901")
    headers = auth("13977119901")
    source_ref, generated_ref, _task_id = _seed_assets(user_id)
    operation_id, revision_id, _payload = _seed_reverse_revision(user_id)
    assessment = _create_assessment(
        client,
        headers,
        source_ref,
        generated_ref,
        reverse_operation_id=operation_id,
        reverse_revision_id=revision_id,
    )
    assert assessment.status_code == 201, assessment.text
    findings = [item for item in assessment.json()["findings"] if item["bbox"]]
    assert findings
    payload = {
        "idempotency_key": "remediation-" + uuid.uuid4().hex,
        "parent_revision_id": revision_id,
        "selected_finding_ids": [findings[0]["id"]],
        "mode": "image_inpaint",
        "model_config_id": _image_model(),
        "params": {},
    }
    path = f"/api/reproduction-assessments/{assessment.json()['id']}/remediations"
    created = client.post(path, headers=headers, json=payload)
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["status"] == "planned"
    assert plan["applied_revision_id"] > revision_id
    assert len(plan["plan_snapshot"]) == len(plan["plan_items"]) == 1
    request = plan["plan_snapshot"][0]["request"]
    assert all(not key.startswith("_") for key in request)

    replay = client.post(path, headers=headers, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == plan["id"]

    with SessionLocal() as db:
        context = reproduction_remediation.validate_generation_request(
            db,
            user_id=user_id,
            remediation_id=plan["id"],
            plan_item_id=plan["plan_snapshot"][0]["item_id"],
            request_payload=request,
        )
        assert context["plan_hash"] == plan["plan_hash"]
        altered = {**request, "client_request_id": "remediation-altered"}
        with pytest.raises(reproduction_remediation.ReproductionRemediationConflict):
            reproduction_remediation.validate_generation_request(
                db,
                user_id=user_id,
                remediation_id=plan["id"],
                plan_item_id=plan["plan_snapshot"][0]["item_id"],
                request_payload=altered,
            )

        target_id = int(plan["target_asset_ref"].split(".", 1)[1])
        target_asset = db.get(GenAsset, target_id)
        assert target_asset is not None
        preview_key = storage.key_from_url(str(target_asset.preview_url))
        assert preview_key is not None
        model_ref_bytes = _jpeg_bytes((10, 200, 90))
        model_ref_key = storage.save_bytes_named(
            model_ref_bytes,
            "model_ref",
            preview_key.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".jpg",
        )
        assert model_ref_key == preview_key.replace("preview/", "model_ref/", 1).rsplit(".", 1)[0] + ".jpg"
        preview_hash = storage.object_sha256(preview_key)
        model_ref_hash = hashlib.sha256(model_ref_bytes).hexdigest()
        assert preview_hash != model_ref_hash

        target = resolve_asset_ref(db, user_id, plan["target_asset_ref"])
        assert asset_refs.generated_asset_reference_path(db, user_id, preview_key).read_bytes() == model_ref_bytes
        assert reproduction_remediation._target_asset_content_hash(db, target) == model_ref_hash

        context = {
            "remediation_id": int(plan["id"]),
            "plan_item_id": plan["plan_snapshot"][0]["item_id"],
            "plan_hash": plan["plan_hash"],
        }
        task = GenTask(
            user_id=user_id,
            client_request_id=request["client_request_id"],
            category="image",
            stage="preview",
            status="queued",
            source_asset_url=request["source_asset_url"],
            source_type="image",
            prompt=request["prompt"],
            params={"_reproduction_context": context},
        )
        db.add(task)
        db.flush()
        reference_data_uri = "data:image/jpeg;base64," + base64.b64encode(model_ref_bytes).decode("ascii")
        mask = reproduction_remediation.rasterize_image_remediation_mask(
            db,
            task=task,
            reference_data_uri=reference_data_uri,
            reference_content_hash=model_ref_hash,
        )
        assert mask.source_content_hash == model_ref_hash
        assert mask.editable_fraction > 0


def test_video_composition_replaces_only_selected_shot(client, make_user):
    user_id = make_user("13977119902")
    source_ref, generated_ref, _generation_task_id = _seed_assets(user_id, media_type="video")
    operation_id, revision_id, _payload = _seed_reverse_revision(
        user_id,
        target="video",
        payload={
            "final_text": "two-shot fixture",
            "video_analysis": {
                "shots": [
                    {"shot_id": "shot-a", "start_seconds": 0, "end_seconds": 4},
                    {"shot_id": "shot-b", "start_seconds": 4, "end_seconds": 8},
                ]
            },
        },
    )
    with SessionLocal() as db:
        assessment = ReproductionAssessment(
            user_id=user_id,
            idempotency_key=f"assessment-{uuid.uuid4().hex}",
            request_fingerprint="a" * 64,
            source_asset_ref=source_ref,
            generated_asset_ref=generated_ref,
            media_type="video",
            reverse_operation_id=operation_id,
            reverse_revision_id=revision_id,
            status="succeeded",
            progress=100,
            cost_credits=0,
            asset_snapshot={},
            lineage_snapshot={},
            metrics={},
            analyzers={},
            warnings=[],
        )
        db.add(assessment)
        db.flush()
        finding = ReproductionFinding(
            assessment_id=int(assessment.id),
            finding_key="shot-a-difference",
            position=1,
            dimension="action",
            kind="semantic_difference",
            severity="high",
            confidence=0.9,
            message="fix shot a",
            shot_id="shot-a",
            time_range={"start_seconds": 0, "end_seconds": 4},
            evidence={},
            metrics={},
        )
        db.add(finding)
        db.commit()
        body = ReproductionRemediationCreateIn(
            idempotency_key="video-remediation-" + uuid.uuid4().hex,
            parent_revision_id=revision_id,
            selected_finding_ids=[int(finding.id)],
            selected_shot_ids=["shot-a"],
            mode="video_shot_regenerate",
            model_config_id=_video_model(),
            params={},
        )
        plan, created = reproduction_remediation.create_remediation(
            db, assessment_id=int(assessment.id), user_id=user_id, body=body
        )
        assert created
        task = GenTask(
            user_id=user_id,
            client_request_id=plan["plan_snapshot"][0]["request"]["client_request_id"],
            category="video",
            stage="preview",
            status="succeeded",
            prompt={"final_text": "fixed shot"},
            params={
                "_reproduction_context": {
                    "remediation_id": int(plan["id"]),
                    "plan_item_id": plan["plan_snapshot"][0]["item_id"],
                    "plan_hash": plan["plan_hash"],
                }
            },
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=int(task.id), user_id=user_id, type="video", preview_url="http://example.com/new.mp4"
        )
        db.add(asset)
        db.flush()
        reproduction_remediation.bind_generation_task(
            db,
            user_id=user_id,
            remediation_id=int(plan["id"]),
            plan_item_id=plan["plan_snapshot"][0]["item_id"],
            task_id=int(task.id),
        )
        remediation = db.get(ReproductionRemediation, int(plan["id"]))
        assert remediation is not None
        payload = reproduction_remediation._video_composition_payload(
            db, row=remediation, tasks=[task]
        )
        by_shot = {item["shot_id"]: item for item in payload["shots"]}
        assert by_shot["shot-a"]["asset_ref"] == f"g.{int(asset.id)}"
        assert "source_start_seconds" not in by_shot["shot-a"]
        assert by_shot["shot-b"]["asset_ref"] == generated_ref
        assert by_shot["shot-b"]["source_start_seconds"] == 4
