"""Generation-result reproduction assessment API coverage."""
from __future__ import annotations

import hashlib
import io
import uuid
from copy import deepcopy

from PIL import Image

from app.db import SessionLocal
from app.models import GenAsset, GenTask, ReverseOperation, ReverseResultRevision, UploadedAsset
from app.services import reverse_lineage, reverse_operations, storage
from app.services.user_assets import generated_asset_ref, uploaded_asset_ref
from app.services.video_frames import (
    SampledVideoFrame,
    VideoMetadata,
    VideoSample,
)


def _png_bytes(color: tuple[int, int, int], *, accent: bool = False) -> bytes:
    image = Image.new("RGB", (96, 64), color)
    if accent:
        for x in range(16, 80):
            for y in range(20, 44):
                image.putpixel((x, y), (245, 245, 245))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="JPEG")
    return output.getvalue()


def _seed_assets(
    user_id: int,
    *,
    media_type: str = "image",
    source_bytes: bytes | None = None,
    generated_bytes: bytes | None = None,
) -> tuple[str, str, int]:
    suffix = uuid.uuid4().hex
    if media_type == "image":
        source_key = storage.save_bytes_named(
            source_bytes or _png_bytes((15, 25, 35), accent=True),
            "upload",
            f"reproduction-source-{suffix}.png",
        )
        generated_key = storage.save_bytes_named(
            generated_bytes or _png_bytes((220, 35, 60)),
            "preview",
            f"reproduction-generated-{suffix}.png",
        )
        mime = "image/png"
        duration = None
    else:
        source_key = storage.save_bytes_named(
            source_bytes or b"source-video-fixture",
            "upload_video",
            f"reproduction-source-{suffix}.mp4",
        )
        generated_key = storage.save_bytes_named(
            generated_bytes or b"generated-video-fixture",
            "video_preview",
            f"reproduction-generated-{suffix}.mp4",
        )
        mime = "video/mp4"
        duration = 12

    with SessionLocal() as db:
        db.add(
            UploadedAsset(
                key=source_key,
                user_id=user_id,
                mime=mime,
                duration=duration,
                bytes=len(source_bytes or b"fixture"),
                original_filename=source_key.rsplit("/", 1)[-1],
            )
        )
        task = GenTask(
            user_id=user_id,
            source_asset_url=storage.upload_api_url(source_key),
            source_type=media_type,
            category=media_type,
            stage="preview",
            prompt={"final_text": "reproduce the supplied source"},
            params={
                "_model_snapshot": {
                    "model_config_id": 77,
                    "model_id": "fixture-model",
                    "capability_version_id": 88,
                    "price_version_id": 99,
                    "provider": "fixture",
                }
            },
            status="succeeded",
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=task.id,
            user_id=user_id,
            type=media_type,
            preview_url=storage.public_url(generated_key),
            unlocked=False,
            watermarked=True,
            duration=duration,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return uploaded_asset_ref(source_key), generated_asset_ref(asset.id), int(task.id)


def _seed_reverse_revision(
    user_id: int,
    *,
    target: str = "image",
    payload: dict | None = None,
) -> tuple[int, int, dict]:
    base_payload = deepcopy(
        payload
        or {
            "final_text": "a silver product on a clean studio background",
            "structured": {"subject": "silver product", "lighting": "softbox"},
        }
    )
    source_url = f"http://example.com/reproduction-lineage-{uuid.uuid4().hex}.png"
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=hashlib.sha256(source_url.encode()).hexdigest(),
            target=target,
            asset_url=source_url,
            output_purpose="generation",
            request_context={"source_type": target},
            status="succeeded",
            progress=100,
            result=deepcopy(base_payload),
            normalized_result=deepcopy(base_payload),
        )
        db.add(operation)
        db.flush()
        fingerprint = reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(source_url.encode()),
            locator=source_url,
            method="test_source_sha256",
        )
        source_fingerprints = [fingerprint]
        source_content_hash = reverse_lineage.source_content_hash(source_fingerprints)
        provider_payload = {"provider": "fixture", "result": deepcopy(base_payload)}
        provider = ReverseResultRevision(
            operation_id=operation.id,
            user_id=user_id,
            version=1,
            source="provider_raw",
            payload=provider_payload,
            source_content_hash=source_content_hash,
            source_fingerprints=source_fingerprints,
            payload_hash=reverse_lineage.canonical_payload_hash(provider_payload),
            lineage_status=reverse_lineage.VERIFIED,
            evidence_review_action="not_applicable",
        )
        db.add(provider)
        db.flush()
        normalized = ReverseResultRevision(
            operation_id=operation.id,
            user_id=user_id,
            version=2,
            source="normalized",
            payload=deepcopy(base_payload),
            parent_revision_id=provider.id,
            source_content_hash=source_content_hash,
            source_fingerprints=source_fingerprints,
            payload_hash=reverse_lineage.canonical_payload_hash(base_payload),
            lineage_status=reverse_lineage.VERIFIED,
            evidence_review_action="not_applicable",
        )
        db.add(normalized)
        db.commit()
        return int(operation.id), int(normalized.id), base_payload


def _create_assessment(client, headers, source_ref: str, generated_ref: str, **extra):
    return client.post(
        "/api/reproduction-assessments",
        headers=headers,
        json={
            "source_asset_ref": source_ref,
            "generated_asset_ref": generated_ref,
            "idempotency_key": f"assessment-{uuid.uuid4().hex}",
            **extra,
        },
    )


def test_image_assessment_uses_real_pixels_and_persists_explainable_findings(
    client, make_user, auth
):
    user_id = make_user("13977110001")
    headers = auth("13977110001")
    source_ref, generated_ref, generation_task_id = _seed_assets(user_id)

    response = _create_assessment(client, headers, source_ref, generated_ref)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] in {"succeeded", "partial"}
    assert body["media_type"] == "image"
    assert body["cost_credits"] == 0
    assert body["source_asset_ref"] == source_ref
    assert body["generated_asset_ref"] == generated_ref
    assert body["lineage"]["generation_task_id"] == generation_task_id
    assert body["lineage"]["model_snapshot"]["model_id"] == "fixture-model"
    dimensions = body["metrics"]["dimensions"]
    assert dimensions["structure_layout"]["status"] == "analyzed"
    assert 0 <= dimensions["structure_layout"]["score"] < 0.8
    assert dimensions["color_light"]["status"] == "analyzed"
    assert body["metrics"]["heatmap"]["width"] > 0
    assert body["analyzers"]["opencv"]["status"] == "analyzed"
    assert body["analyzers"]["opencv"]["version"]
    assert any(
        finding["dimension"] == "structure_layout"
        and finding["bbox"]
        and finding["time_range"] is None
        for finding in body["findings"]
    )
    assert any("预览" in warning for warning in body["warnings"])

    detail = client.get(
        f"/api/reproduction-assessments/{body['id']}", headers=headers
    )
    assert detail.status_code == 200
    assert detail.json() == body


def test_image_assessment_compares_configured_detector_and_segmenter_evidence(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13977110010")
    headers = auth("13977110010")
    source_ref, generated_ref, _generation_task_id = _seed_assets(user_id)

    def fake_region_provider(image: Image.Image, *, capability: str) -> dict:
        is_source = sum(image.getpixel((0, 0))) < 150
        label = "product" if is_source else "person"
        bbox = (
            {"x": 0.15, "y": 0.2, "width": 0.7, "height": 0.6}
            if is_source
            else {"x": 0.55, "y": 0.1, "width": 0.35, "height": 0.8}
        )
        return {
            "status": "analyzed",
            "analyzer": f"fixture_{capability}",
            "analyzer_version": "2026.07",
            "evidence_count": 1,
            "degraded_reason": None,
            "evidence": [
                {
                    "label": label,
                    "confidence": 0.95,
                    "bbox": bbox,
                }
            ],
        }

    monkeypatch.setattr(
        "app.services.image_evidence_analysis.http_region_provider",
        fake_region_provider,
    )

    response = _create_assessment(client, headers, source_ref, generated_ref)

    assert response.status_code == 201, response.text
    body = response.json()
    subject = body["metrics"]["dimensions"]["subject_semantics"]
    assert subject["status"] == "analyzed"
    assert subject["score"] == 0
    assert body["analyzers"]["subject_semantics"]["status"] == "analyzed"
    assert body["analyzers"]["subject_semantics"]["capabilities"]["detector"][
        "source_evidence_count"
    ] == 1
    finding = next(
        item for item in body["findings"] if item["dimension"] == "subject_semantics"
    )
    assert finding["kind"] == "subject_mismatch"
    assert finding["bbox"] == {"x": 0.15, "y": 0.2, "width": 0.7, "height": 0.6}
    assert finding["evidence"]["capabilities"]["detector"] == {
        "source_labels": ["product"],
        "generated_labels": ["person"],
    }


def test_assessment_enforces_owner_generated_origin_and_matching_media(
    client, make_user, auth
):
    owner_id = make_user("13977110002")
    other_id = make_user("13977110003")
    owner_headers = auth("13977110002")
    other_headers = auth("13977110003")
    source_ref, generated_ref, _task_id = _seed_assets(owner_id)

    hidden = _create_assessment(client, other_headers, source_ref, generated_ref)
    assert hidden.status_code == 404

    uploaded_as_result = _create_assessment(
        client,
        owner_headers,
        source_ref,
        source_ref,
    )
    assert uploaded_as_result.status_code == 422
    assert "GenAsset" in uploaded_as_result.text or "生成资产" in uploaded_as_result.text

    video_source_ref, video_generated_ref, _video_task_id = _seed_assets(
        owner_id, media_type="video"
    )
    mismatch = _create_assessment(
        client,
        owner_headers,
        source_ref,
        video_generated_ref,
    )
    assert mismatch.status_code == 422
    assert "媒体类型" in mismatch.text

    # The generated result also remains hidden from a different authenticated owner.
    other_source, _other_generated, _ = _seed_assets(other_id)
    hidden_result = _create_assessment(
        client,
        other_headers,
        other_source,
        generated_ref,
    )
    assert hidden_result.status_code == 404
    assert video_source_ref


def test_video_assessment_aligns_normalized_frames_and_emits_time_range_findings(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13977110004")
    headers = auth("13977110004")
    source_ref, generated_ref, _task_id = _seed_assets(user_id, media_type="video")
    source_jpeg = _jpeg_bytes((10, 20, 30))
    generated_jpeg = _jpeg_bytes((235, 220, 50))

    def fake_probe(path: str) -> dict:
        duration = 12.0 if "reproduction-source" in path else 9.0
        return {
            "width": 64,
            "height": 64,
            "duration_seconds": duration,
            "duration": duration,
            "fps": 24.0,
            "has_audio": False,
        }

    def fake_sample(path: str, n: int = 4, **kwargs) -> VideoSample:
        duration = fake_probe(path)["duration_seconds"]
        timestamps = kwargs.get("custom_timestamps") or [duration * index / (n - 1) for index in range(n)]
        jpeg = source_jpeg if "reproduction-source" in path else generated_jpeg
        return VideoSample(
            frames=tuple(
                SampledVideoFrame(jpeg=jpeg, timestamp_seconds=float(timestamp))
                for timestamp in timestamps
            ),
            source=VideoMetadata(
                width=64,
                height=64,
                duration_seconds=duration,
                total_duration_seconds=duration,
                fps=24.0,
                has_audio=False,
            ),
        )

    monkeypatch.setattr("app.services.video_frames.probe_media", fake_probe)
    monkeypatch.setattr("app.services.video_frames.sample_video_from_path", fake_sample)
    monkeypatch.setattr(
        "app.services.video_audio.analyze_video_audio_from_path",
        lambda *_args, **_kwargs: {"status": "no_audio", "reason": "no audio track"},
    )

    def fake_semantic_provider(images: list[Image.Image], frames: list[dict]) -> dict:
        is_source = sum(images[0].getpixel((0, 0))) < 200
        subject_label = "product" if is_source else "person"
        action_label = "rotate" if is_source else "walk"
        transition_label = "hard_cut" if is_source else "dissolve"
        start = float(frames[0]["timestamp_seconds"])
        end = float(frames[-1]["timestamp_seconds"])
        middle = float(frames[len(frames) // 2]["timestamp_seconds"])
        analyzer = {
            "status": "analyzed",
            "analyzer": "fixture_video_semantic",
            "analyzer_version": "2026.07",
            "evidence_count": 1,
            "degraded_reason": None,
        }
        return {
            "contract_version": "video-semantic-evidence.v1",
            "subject_tracking": {
                **analyzer,
                "tracks": [
                    {
                        "label": subject_label,
                        "start_seconds": start,
                        "end_seconds": end,
                        "observations": [
                            {
                                "bbox": {
                                    "x": 0.1 if is_source else 0.6,
                                    "y": 0.1,
                                    "width": 0.3,
                                    "height": 0.8,
                                }
                            }
                        ],
                    }
                ],
            },
            "pose": {
                **analyzer,
                "status": "unsupported",
                "evidence_count": 0,
                "degraded_reason": "fixture pose unavailable",
                "observations": [],
            },
            "action": {
                **analyzer,
                "events": [
                    {
                        "label": action_label,
                        "start_seconds": start,
                        "end_seconds": end,
                    }
                ],
            },
            "transition": {
                **analyzer,
                "events": [{"label": transition_label, "timestamp_seconds": middle}],
            },
        }

    def fake_camera_motion(images: list[Image.Image], frames: list[dict]) -> dict:
        is_source = sum(images[0].getpixel((0, 0))) < 200
        samples = []
        for index in range(1, len(frames)):
            scores = (
                {"pan": 0.9, "tilt": 0.05, "zoom": 0.05, "static": 0.1}
                if is_source
                else {"pan": 0.05, "tilt": 0.05, "zoom": 0.05, "static": 0.95}
            )
            samples.append(
                {
                    "start_seconds": frames[index - 1]["timestamp_seconds"],
                    "end_seconds": frames[index]["timestamp_seconds"],
                    "camera": "pan" if is_source else "static",
                    "camera_scores": scores,
                }
            )
        return {
            "status": "analyzed",
            "analyzer": "fixture_camera_motion",
            "analyzer_version": "2026.07",
            "samples": samples,
            "degraded_reason": None,
        }

    monkeypatch.setattr(
        "app.services.video_evidence_analysis.http_semantic_provider",
        fake_semantic_provider,
    )
    monkeypatch.setattr(
        "app.services.video_evidence_analysis.cv2_motion_analysis",
        fake_camera_motion,
    )

    reverse_payload = {
        "final_text": "three product shots",
        "structured": {},
        "video_analysis": {
            "shots": [
                {"shot_id": "shot-a", "start_seconds": 0.0, "end_seconds": 6.0},
                {"shot_id": "shot-b", "start_seconds": 6.0, "end_seconds": 12.0},
            ]
        },
    }
    operation_id, revision_id, _ = _seed_reverse_revision(
        user_id, target="video", payload=reverse_payload
    )

    response = _create_assessment(
        client,
        headers,
        source_ref,
        generated_ref,
        reverse_operation_id=operation_id,
        reverse_revision_id=revision_id,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["media_type"] == "video"
    assert body["status"] == "partial"
    assert body["metrics"]["dimensions"]["duration_pacing"]["status"] == "analyzed"
    assert body["metrics"]["dimensions"]["frame_structure"]["status"] == "analyzed"
    assert len(body["metrics"]["frame_pairs"]) >= 4
    assert body["analyzers"]["audio"]["status"] == "no_audio"
    assert body["analyzers"]["subject_semantics"]["status"] == "analyzed"
    assert body["analyzers"]["action_semantics"]["status"] == "analyzed"
    assert body["analyzers"]["camera_motion"]["status"] == "analyzed"
    assert body["metrics"]["dimensions"]["subject_semantics"]["score"] == 0
    assert body["metrics"]["dimensions"]["action_semantics"]["score"] == 0
    assert body["metrics"]["dimensions"]["transition_semantics"]["score"] == 0
    assert body["metrics"]["dimensions"]["camera_motion"]["score"] < 0.8
    assert {
        "subject_semantics",
        "action_semantics",
        "transition_semantics",
        "camera_motion",
    }.issubset({item["dimension"] for item in body["findings"]})
    assert any(
        finding["time_range"]
        and finding["time_range"]["end_seconds"] > finding["time_range"]["start_seconds"]
        and finding["shot_id"] in {"shot-a", "shot-b"}
        for finding in body["findings"]
    )


def test_queued_assessment_can_be_canceled_without_running_analysis(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13977110005")
    headers = auth("13977110005")
    source_ref, generated_ref, _task_id = _seed_assets(user_id)
    monkeypatch.setattr(
        "app.services.reproduction_assessment.enqueue_assessment",
        lambda _assessment_id: None,
    )

    created = _create_assessment(client, headers, source_ref, generated_ref)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "queued"

    canceled = client.post(
        f"/api/reproduction-assessments/{created.json()['id']}/cancel",
        headers=headers,
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["cancel_requested"] is True
    assert canceled.json()["finished_at"]


def test_enqueue_failure_is_terminal_and_never_leaves_a_queued_orphan(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13977110008")
    headers = auth("13977110008")
    source_ref, generated_ref, _task_id = _seed_assets(user_id)

    def fail_enqueue(_assessment_id: int) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "app.services.reproduction_assessment.enqueue_assessment",
        fail_enqueue,
    )
    response = _create_assessment(client, headers, source_ref, generated_ref)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["phase"] == "enqueue_failed"
    assert body["error_code"] == "ASSESSMENT_ENQUEUE_FAILED"
    assert body["finished_at"]


def test_correction_creates_new_revision_without_overwriting_source_and_apply_is_explicit(
    client, make_user, auth
):
    user_id = make_user("13977110006")
    headers = auth("13977110006")
    source_ref, generated_ref, _task_id = _seed_assets(user_id)
    operation_id, normalized_revision_id, original_payload = _seed_reverse_revision(user_id)
    assessed = _create_assessment(
        client,
        headers,
        source_ref,
        generated_ref,
        reverse_operation_id=operation_id,
        reverse_revision_id=normalized_revision_id,
    )
    assert assessed.status_code == 201, assessed.text
    finding_id = assessed.json()["findings"][0]["id"]
    correction_request_id = f"correction-{uuid.uuid4().hex}"
    correction_payload = {
        "idempotency_key": correction_request_id,
        "parent_revision_id": normalized_revision_id,
        "selected_finding_ids": [finding_id],
        "structured_patch": {"lighting": "cool rim light", "layout": "centered"},
        "prompt_patch": "a centered silver product with cool rim light",
        "negative_prompt_patch": "warped text, off-center product",
        "mask_patch": {
            "mode": "editable_regions",
            "regions": [{"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}],
        },
        "apply": False,
    }

    corrected = client.post(
        f"/api/reproduction-assessments/{assessed.json()['id']}/corrections",
        headers=headers,
        json=correction_payload,
    )
    assert corrected.status_code == 201, corrected.text
    correction = corrected.json()
    assert correction["edited_revision"]["source"] == "user_edit"
    assert correction["edited_revision"]["parent_revision_id"] == normalized_revision_id
    assert correction["applied_revision"] is None
    assert correction["edited_revision"]["payload"]["structured"]["lighting"] == "cool rim light"
    assert correction["edited_revision"]["payload"]["final_text"] == correction_payload["prompt_patch"]
    assert correction["edited_revision"]["payload"]["negative_prompt"] == correction_payload[
        "negative_prompt_patch"
    ]
    assert correction["edited_revision"]["payload"]["mask_patch"] == correction_payload[
        "mask_patch"
    ]
    assert correction["selected_finding_ids"] == [finding_id]

    replay = client.post(
        f"/api/reproduction-assessments/{assessed.json()['id']}/corrections",
        headers=headers,
        json=correction_payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == correction["id"]

    with SessionLocal() as db:
        original = db.get(ReverseResultRevision, normalized_revision_id)
        operation = db.get(ReverseOperation, operation_id)
        assert original.payload == original_payload
        assert operation.applied_result_version is None

    applied_payload = {
        **correction_payload,
        "idempotency_key": f"correction-apply-{uuid.uuid4().hex}",
        "parent_revision_id": correction["edited_revision"]["id"],
        "prompt_patch": "approved centered silver product with cool rim light",
        "apply": True,
    }
    applied = client.post(
        f"/api/reproduction-assessments/{assessed.json()['id']}/corrections",
        headers=headers,
        json=applied_payload,
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["edited_revision"]["source"] == "user_edit"
    assert applied.json()["applied_revision"]["source"] == "applied"
    assert applied.json()["applied_revision"]["parent_revision_id"] == applied.json()[
        "edited_revision"
    ]["id"]


def test_correction_derives_patch_when_only_findings_are_selected(client, make_user, auth):
    user_id = make_user("13977110007")
    headers = auth("13977110007")
    source_ref, generated_ref, _task_id = _seed_assets(user_id)
    operation_id, normalized_revision_id, original_payload = _seed_reverse_revision(user_id)
    assessed = _create_assessment(
        client,
        headers,
        source_ref,
        generated_ref,
        reverse_operation_id=operation_id,
        reverse_revision_id=normalized_revision_id,
    )
    assert assessed.status_code == 201, assessed.text

    correction = client.post(
        f"/api/reproduction-assessments/{assessed.json()['id']}/corrections",
        headers=headers,
        json={
            "idempotency_key": f"finding-only-{uuid.uuid4().hex}",
            "parent_revision_id": normalized_revision_id,
            "selected_finding_ids": [assessed.json()["findings"][0]["id"]],
        },
    )

    assert correction.status_code == 201, correction.text
    body = correction.json()
    assert body["structured_patch"]["reproduction_corrections"][0]["finding_id"] == body[
        "selected_finding_ids"
    ][0]
    assert body["edited_revision"]["payload"]["structured"]["reproduction_corrections"]
    with SessionLocal() as db:
        assert db.get(ReverseResultRevision, normalized_revision_id).payload == original_payload


def test_generation_task_lineage_uses_editable_source_revision_for_correction(
    client, make_user, auth
):
    user_id = make_user("13977110009")
    headers = auth("13977110009")
    source_ref, generated_ref, generation_task_id = _seed_assets(user_id)
    operation_id, normalized_revision_id, original_payload = _seed_reverse_revision(user_id)
    with SessionLocal() as db:
        _edited, applied = reverse_operations.apply_result_revision(
            db,
            operation_id=operation_id,
            user_id=user_id,
            payload=deepcopy(original_payload),
            parent_revision_id=normalized_revision_id,
        )
        applied_revision_id = int(applied.id)
        compiled = ReverseResultRevision(
            operation_id=operation_id,
            user_id=user_id,
            version=int(applied.version) + 1,
            source="model_compiled",
            payload={"compiled_prompt": "fixture"},
            parent_revision_id=applied.id,
            source_content_hash=applied.source_content_hash,
            source_fingerprints=deepcopy(applied.source_fingerprints),
            payload_hash=hashlib.sha256(b"compiled-fixture").hexdigest(),
            lineage_status="verified",
            evidence_review_action="not_applicable",
        )
        db.add(compiled)
        db.flush()
        generation = ReverseResultRevision(
            operation_id=operation_id,
            user_id=user_id,
            version=int(compiled.version) + 1,
            source="generation",
            payload={"generation": "fixture"},
            parent_revision_id=compiled.id,
            source_content_hash=compiled.source_content_hash,
            source_fingerprints=deepcopy(compiled.source_fingerprints),
            payload_hash=hashlib.sha256(b"generation-fixture").hexdigest(),
            lineage_status="verified",
            evidence_review_action="not_applicable",
        )
        db.add(generation)
        db.flush()
        task = db.get(GenTask, generation_task_id)
        task.reverse_operation_id = operation_id
        task.source_revision_id = applied_revision_id
        # This represents a compiled/generation provenance revision and must
        # never become the correction parent.
        task.compiled_revision_id = compiled.id
        task.generation_revision_id = generation.id
        db.commit()

    assessed = _create_assessment(
        client,
        headers,
        source_ref,
        generated_ref,
        generation_task_id=generation_task_id,
    )
    assert assessed.status_code == 201, assessed.text
    assert assessed.json()["lineage"]["reverse_revision_id"] == applied_revision_id
    correction = client.post(
        f"/api/reproduction-assessments/{assessed.json()['id']}/corrections",
        headers=headers,
        json={
            "idempotency_key": f"task-lineage-{uuid.uuid4().hex}",
            "parent_revision_id": applied_revision_id,
            "selected_finding_ids": [assessed.json()["findings"][0]["id"]],
        },
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["edited_revision"]["parent_revision_id"] == applied_revision_id

    applied_correction = client.post(
        f"/api/reproduction-assessments/{assessed.json()['id']}/corrections",
        headers=headers,
        json={
            "idempotency_key": f"applied-parent-{uuid.uuid4().hex}",
            "parent_revision_id": applied_revision_id,
            "selected_finding_ids": [assessed.json()["findings"][0]["id"]],
            "apply": True,
        },
    )
    assert applied_correction.status_code == 201, applied_correction.text
    assert applied_correction.json()["edited_revision"]["parent_revision_id"] == applied_revision_id
    assert applied_correction.json()["applied_revision"]["source"] == "applied"

    mismatched_revision = client.post(
        "/api/reproduction-assessments",
        headers=headers,
        json={
            "source_asset_ref": source_ref,
            "generated_asset_ref": generated_ref,
            "generation_task_id": generation_task_id,
            "reverse_operation_id": operation_id,
            "reverse_revision_id": normalized_revision_id,
            "idempotency_key": f"wrong-source-revision-{uuid.uuid4().hex}",
        },
    )
    assert mismatched_revision.status_code == 422
    assert "生成任务血缘" in mismatched_revision.text
