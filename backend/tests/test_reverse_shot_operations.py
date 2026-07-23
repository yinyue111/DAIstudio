from __future__ import annotations

from copy import deepcopy

import pytest

from app.db import SessionLocal
from app.models import GenTask, ModelConfig, ReverseOperation, ReverseResultRevision
from app.services import reverse_lineage, reverse_operations


def _seed_video_operation(user_id: int) -> int:
    asset_url = f"https://cdn.example.com/timeline-{user_id}.mp4"
    payload = {
        "structured": {"主题": "商品片"},
        "final_text": "商品广告",
        "video_analysis": {
            "sampled_frames": [
                {"index": 1, "absolute_timestamp_seconds": 10.5, "source_segment_index": 1},
                {"index": 2, "absolute_timestamp_seconds": 12.5, "source_segment_index": 1},
                {"index": 3, "absolute_timestamp_seconds": 14.5, "source_segment_index": 1},
            ],
            "shots": [
                {
                    "shot_id": "shot-one", "start_seconds": 10.0, "end_seconds": 12.5,
                    "source_segment_index": 1, "visual": "开场", "action": "旋转",
                    "camera": "pan", "lighting": "柔光", "transition": "cut", "ocr": "",
                    "audio_cue": "", "evidence_frame_indices": [1, 2], "ocr_track_refs": [],
                    "audio_refs": [], "confidence": .9, "locked": False,
                },
                {
                    "shot_id": "shot-two", "start_seconds": 12.5, "end_seconds": 15.0,
                    "source_segment_index": 1, "visual": "结尾", "action": "静止",
                    "camera": "static", "lighting": "柔光", "transition": "", "ocr": "",
                    "audio_cue": "", "evidence_frame_indices": [2, 3], "ocr_track_refs": [],
                    "audio_refs": [], "confidence": .8, "locked": False,
                },
            ],
        },
    }
    with SessionLocal() as db:
        operation = ReverseOperation(
            user_id=user_id,
            request_fingerprint=f"{user_id:064d}"[-64:],
            target="video",
            asset_url=asset_url,
            analysis_focus="storyboard",
            analysis_precision="standard",
            output_purpose="generation",
            include_audio=False,
            source_ranges=[{"start_seconds": 10, "end_seconds": 15}],
            request_context={
                "client_request_id": f"seed-video-{user_id}",
                "asset_url": asset_url,
                "sources": [{"asset_url": asset_url, "source_type": "video", "role": "primary"}],
                "source_type": "video", "target": "video", "analysis_focus": "storyboard",
                "analysis_precision": "standard", "video_analysis_preset": "standard",
                "output_purpose": "generation", "include_audio": False,
                "source_ranges": [{"start_seconds": 10, "end_seconds": 15}],
            },
            status="succeeded", progress=100,
            result=deepcopy(payload), normalized_result=deepcopy(payload),
        )
        db.add(operation)
        db.flush()
        fingerprints = [reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(f"video:{user_id}".encode()),
            locator=asset_url,
            method="test_source_sha256",
        )]
        content_hash = reverse_lineage.source_content_hash(fingerprints)
        provider_payload = {"provider": "fixture"}
        provider = ReverseResultRevision(
            operation_id=operation.id, user_id=user_id, version=1, source="provider_raw",
            payload=provider_payload, source_content_hash=content_hash,
            source_fingerprints=fingerprints,
            payload_hash=reverse_lineage.canonical_payload_hash(provider_payload),
            lineage_status=reverse_lineage.VERIFIED,
        )
        db.add(provider)
        db.flush()
        db.add(ReverseResultRevision(
            operation_id=operation.id, user_id=user_id, version=2, source="normalized",
            payload=payload, parent_revision_id=provider.id, source_content_hash=content_hash,
            source_fingerprints=fingerprints,
            payload_hash=reverse_lineage.canonical_payload_hash(payload),
            lineage_status=reverse_lineage.VERIFIED,
        ))
        db.commit()
        return int(operation.id)


def test_applied_external_video_reverse_generates_as_analysis_only_source(
    client,
    make_user,
    auth,
    quote_and_generate,
):
    user_id = make_user("13730000000", balance=1000)
    headers = auth("13730000000")
    operation_id = _seed_video_operation(user_id)
    with SessionLocal() as db:
        operation = db.get(ReverseOperation, operation_id)
        normalized = db.query(ReverseResultRevision).filter_by(
            operation_id=operation_id,
            source="normalized",
        ).one()
        source_url = operation.asset_url
        payload = deepcopy(normalized.payload)
        normalized_id = int(normalized.id)
        model = db.query(ModelConfig).filter_by(use="video", enabled=True).first()
        assert model is not None
        model_config_id = int(model.id)

    edited = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "user_edit",
            "parent_revision_id": normalized_id,
            "payload": payload,
        },
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    applied = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "applied",
            "parent_revision_id": edited.json()["id"],
            "payload": payload,
        },
        headers=headers,
    )
    assert applied.status_code == 200, applied.text

    request = {
        "client_request_id": "video-analysis-only-generate-001",
        "reverse_operation_id": operation_id,
        "reverse_revision_id": applied.json()["id"],
        "source_asset_url": source_url,
        "source_type": "video",
        "category": "video",
        "stage": "preview",
        "prompt": {"final_text": payload["final_text"]},
        "params": {"duration": 5, "resolution": "480p", "ratio": "16:9"},
        "model_config_id": model_config_id,
    }
    generated = quote_and_generate(request, headers=headers)
    assert generated.status_code == 200, generated.text

    with SessionLocal() as db:
        task = db.get(GenTask, generated.json()["id"])
        assert task is not None
        assert task.source_asset_url == source_url
        assert task.source_type == "video"
        assert task.params["_video_reference_roles"] == [{
            "role": "motion_analysis",
            "source": "source_asset_url",
            "mode": "analysis_only",
        }]
        assert "first_frame_image" not in task.params

    mismatched = client.post(
        "/api/quotes",
        json={
            **request,
            "client_request_id": "video-analysis-only-mismatch-001",
            "source_asset_url": "https://cdn.example.com/other-video.mp4",
        },
        headers=headers,
    )
    assert mismatched.status_code == 409, mismatched.text
    assert "反推源视频不一致" in mismatched.text


def test_shot_split_is_persisted_idempotent_and_owner_scoped(client, make_user, auth):
    user_id = make_user("13730000001", balance=100)
    make_user("13730000002", balance=100)
    operation_id = _seed_video_operation(user_id)
    body = {
        "client_request_id": "shot-split-request-001",
        "action": "split", "shot_id": "shot-one", "split_seconds": 11.25,
    }
    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json=body, headers=auth("13730000001"),
    )

    assert response.status_code == 200, response.text
    revision = response.json()
    shots = revision["payload"]["video_analysis"]["shots"]
    assert len(shots) == 3
    assert shots[0]["end_seconds"] == 11.25
    assert shots[1]["start_seconds"] == 11.25
    replay = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json=body, headers=auth("13730000001"),
    )
    assert replay.json()["id"] == revision["id"]
    assert client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json=body, headers=auth("13730000002"),
    ).status_code == 404


def test_shot_boundary_edit_is_atomic_and_redistributes_frame_evidence(
    client, make_user, auth,
):
    user_id = make_user("13730000009", balance=100)
    operation_id = _seed_video_operation(user_id)
    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "shot-boundary-request-001",
            "action": "boundary",
            "shot_ids": ["shot-one", "shot-two"],
            "boundary_seconds": 13.5,
        },
        headers=auth("13730000009"),
    )

    assert response.status_code == 200, response.text
    shots = response.json()["payload"]["video_analysis"]["shots"]
    assert shots[0]["end_seconds"] == 13.5
    assert shots[1]["start_seconds"] == 13.5
    assert shots[0]["evidence_frame_indices"] == [1, 2]
    assert shots[1]["evidence_frame_indices"] == [3]


def test_shot_merge_persists_aggregated_timeline_evidence(client, make_user, auth):
    user_id = make_user("13730000011", balance=100)
    operation_id = _seed_video_operation(user_id)
    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "shot-merge-request-001",
            "action": "merge",
            "shot_ids": ["shot-one", "shot-two"],
        },
        headers=auth("13730000011"),
    )

    assert response.status_code == 200, response.text
    revision = response.json()
    shots = revision["payload"]["video_analysis"]["shots"]
    assert len(shots) == 1
    merged = shots[0]
    assert merged["shot_id"].startswith("shot-merge-")
    assert merged["start_seconds"] == 10.0
    assert merged["end_seconds"] == 15.0
    assert merged["evidence_frame_indices"] == [1, 2, 3]
    assert merged["visual"] == "开场；结尾"
    assert merged["action"] == "旋转；静止"
    assert merged["confidence"] == 0.85
    assert revision["payload"]["timeline_edit"]["action"] == "merge"

    with SessionLocal() as db:
        persisted = db.get(ReverseResultRevision, revision["id"])
        assert persisted is not None
        assert persisted.payload["video_analysis"]["shots"] == shots


def test_shot_reorder_requires_complete_ids_and_persists_order(client, make_user, auth):
    user_id = make_user("13730000012", balance=100)
    operation_id = _seed_video_operation(user_id)
    headers = auth("13730000012")
    response = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "shot-reorder-request-001",
            "action": "reorder",
            "ordered_shot_ids": ["shot-two", "shot-one"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    revision = response.json()
    assert [
        shot["shot_id"]
        for shot in revision["payload"]["video_analysis"]["shots"]
    ] == ["shot-two", "shot-one"]
    assert revision["payload"]["timeline_edit"]["action"] == "reorder"

    invalid = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "shot-reorder-request-002",
            "action": "reorder",
            "ordered_shot_ids": ["shot-one", "shot-one"],
        },
        headers=headers,
    )
    assert invalid.status_code == 422
    assert "必须完整且不能重复" in invalid.text


def test_completed_shot_reanalysis_merges_into_unchanged_parent_revision(
    client,
    make_user,
):
    user_id = make_user("13730000010", balance=100)
    parent_operation_id = _seed_video_operation(user_id)
    with SessionLocal() as db:
        parent_revision = db.query(ReverseResultRevision).filter_by(
            operation_id=parent_operation_id,
            source="normalized",
        ).one()
        child = ReverseOperation(
            user_id=user_id,
            request_fingerprint="a" * 64,
            target="video",
            asset_url=f"https://cdn.example.com/timeline-{user_id}.mp4",
            analysis_focus="storyboard",
            analysis_precision="standard",
            output_purpose="generation",
            include_audio=False,
            request_context={
                "shot_reanalysis": {
                    "contract_version": 1,
                    "parent_operation_id": parent_operation_id,
                    "parent_revision_id": int(parent_revision.id),
                    "parent_shot_id": "shot-one",
                    "source_segment_index": 1,
                    "source_range": {"start_seconds": 10, "end_seconds": 12.5},
                },
            },
            status="running",
            progress=80,
            cost_frozen=0,
        )
        db.add(child)
        db.commit()
        child_id = int(child.id)
        source_fingerprints = [reverse_lineage.build_source_fingerprint(
            source_index=1,
            content_hash=reverse_lineage.bytes_content_hash(b"shot-reanalysis"),
            locator=child.asset_url,
            method="test_source_sha256",
        )]
        finished = reverse_operations._finish_success(
            db,
            child_id,
            result={
                "final_text": "重新分析后的镜头",
                "video_analysis": {
                    "shots": [{
                        "start_seconds": 0,
                        "end_seconds": 2.5,
                        "visual": "新商品特写",
                        "action": "缓慢抬起",
                        "camera": "zoom",
                        "lighting": "轮廓光",
                        "transition": "cut",
                        "confidence": 0.95,
                    }],
                },
            },
            source_fingerprints=source_fingerprints,
            reference_count=1,
            real_cost=0,
        )
        assert finished is not None

    with SessionLocal() as db:
        child = db.get(ReverseOperation, child_id)
        assert child.result["shot_reanalysis_merge"]["status"] == "merged"
        merged = db.query(ReverseResultRevision).filter_by(
            operation_id=parent_operation_id,
            source="user_edit",
        ).order_by(ReverseResultRevision.version.desc()).first()
        assert merged is not None
        shot = merged.payload["video_analysis"]["shots"][0]
        assert shot["shot_id"] == "shot-one"
        assert shot["start_seconds"] == 10.0
        assert shot["end_seconds"] == 12.5
        assert shot["visual"] == "新商品特写"
        assert shot["camera"] == "zoom"
        assert shot["evidence_frame_indices"] == [1, 2]
        assert shot["reanalysis_evidence"]["operation_id"] == child_id


def test_shot_lock_blocks_reanalysis_and_prepare_uses_applied_lineage(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13730000003", balance=100)
    operation_id = _seed_video_operation(user_id)
    headers = auth("13730000003")
    locked = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "shot-lock-request-001", "action": "lock",
            "shot_id": "shot-one", "locked": True,
        },
        headers=headers,
    )
    assert locked.status_code == 200, locked.text
    blocked = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/reanalyze",
        json={"client_request_id": "shot-reanalyze-001", "shot_id": "shot-one"},
        headers=headers,
    )
    assert blocked.status_code == 409

    applied = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "applied", "parent_revision_id": locked.json()["id"],
            "payload": locked.json()["payload"],
        }, headers=headers,
    )
    assert applied.status_code == 200, applied.text
    with SessionLocal() as db:
        video_model = db.query(ModelConfig).filter_by(use="video", enabled=True).first()
        assert video_model is not None
        video_model_id = int(video_model.id)
    prepared = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/prepare-generation",
        json={
            "client_request_id": "shot-generate-request-001", "shot_id": "shot-one",
            "revision_id": applied.json()["id"], "model_config_id": video_model_id,
        }, headers=headers,
    )
    assert prepared.status_code == 200, prepared.text
    payload = prepared.json()
    assert payload["quote_endpoint"] == "/api/quotes"
    assert "source_asset_url" not in payload["request"]
    assert "source_type" not in payload["request"]
    assert payload["request"]["reverse_revision_id"] == applied.json()["id"]
    assert payload["request"]["params"].get("shot_id") is None
    assert payload["request"]["params"].get("source_range") is None
    assert payload["request"]["params"].get("first_frame_image") is None
    assert payload["request"]["shot_context"] == {
        "contract_version": 1,
        "shot_id": "shot-one",
        "source_segment_index": 1,
        "source_range": {"start_seconds": 10.0, "end_seconds": 12.5},
    }

    quoted = client.post("/api/quotes", json=payload["request"], headers=headers)
    assert quoted.status_code == 201, quoted.text
    generated = client.post(
        "/api/generate",
        json={**payload["request"], "quote_id": quoted.json()["quote_id"]},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text
    with SessionLocal() as db:
        task = db.get(GenTask, generated.json()["id"])
        assert task is not None
        assert task.source_asset_url is None
        assert task.reverse_operation_id == operation_id
        assert task.source_revision_id == applied.json()["id"]
        assert task.params["_shot_context"] == payload["request"]["shot_context"]
        assert "first_frame_image" not in task.params
        assert "shot_id" not in {
            key: value for key, value in task.params.items() if not key.startswith("_")
        }


def test_consecutive_shot_edits_follow_actual_user_edit_parent_and_detect_tamper(
    client, make_user, auth
):
    user_id = make_user("13730000004", balance=100)
    operation_id = _seed_video_operation(user_id)
    headers = auth("13730000004")

    split = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "shot-chain-split-001",
            "action": "split",
            "shot_id": "shot-one",
            "split_seconds": 11.25,
        },
        headers=headers,
    )
    assert split.status_code == 200, split.text
    locked = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "shot-chain-lock-001",
            "action": "lock",
            "shot_id": "shot-two",
            "locked": True,
        },
        headers=headers,
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["parent_revision_id"] == split.json()["id"]
    assert len(locked.json()["payload"]["video_analysis"]["shots"]) == 3

    applied = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/revisions",
        json={
            "source": "applied",
            "parent_revision_id": locked.json()["id"],
            "payload": {},
        },
        headers=headers,
    )
    assert applied.status_code == 200, applied.text

    with SessionLocal() as db:
        applied_revision = db.get(ReverseResultRevision, applied.json()["id"])
        chain = reverse_lineage.validate_revision_chain(
            db, applied_revision, terminal_source="applied",
        )
        assert [row.source for row in chain] == [
            "applied", "user_edit", "user_edit", "normalized", "provider_raw",
        ]

        first_edit = db.get(ReverseResultRevision, split.json()["id"])
        first_edit.payload_hash = "0" * 64
        db.commit()
        db.refresh(applied_revision)
        with pytest.raises(reverse_lineage.ReverseLineageError, match="内容哈希不一致"):
            reverse_lineage.validate_revision_chain(
                db, applied_revision, terminal_source="applied",
            )


def test_shot_edit_continues_from_latest_optimized_user_edit(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13730000005", balance=100)
    operation_id = _seed_video_operation(user_id)
    headers = auth("13730000005")
    with SessionLocal() as db:
        normalized = db.query(ReverseResultRevision).filter_by(
            operation_id=operation_id, source="normalized",
        ).one()
        normalized_id = int(normalized.id)
        target_model = db.query(ModelConfig).filter_by(use="video", enabled=True).first()
        assert target_model is not None
        target_model_id = int(target_model.id)

    monkeypatch.setattr(
        "app.services.prompt_optimization._invoke_optimizer",
        lambda *_args, **_kwargs: {
            "prompt": "已优化的商品广告提示词",
            "field_suggestions": {},
            "constraint_coverage": [],
            "usage": None,
            "latency_ms": 1,
        },
    )

    proposal_body = {
        "prompt": None,
        "category": None,
        "reverse_operation_id": operation_id,
        "reverse_revision_id": normalized_id,
        "target_model_config_id": target_model_id,
        "mode": "faithful",
        "idempotency_key": "optimized-shot-proposal-001",
    }
    quote = client.post(
        "/api/quotes",
        json={
            "kind": "prompt_optimization",
            "client_request_id": proposal_body["idempotency_key"],
            "request": proposal_body,
        },
        headers=headers,
    )
    assert quote.status_code == 201, quote.text
    proposed = client.post(
        "/api/studio/prompt-optimizations",
        json={**proposal_body, "quote_id": quote.json()["quote_id"]},
        headers=headers,
    )
    assert proposed.status_code == 201, proposed.text
    proposal = proposed.json()
    optimized = client.post(
        f"/api/studio/prompt-optimizations/{proposal['proposal_id']}/accept",
        json={
            "proposal_version": proposal["proposal_version"],
            "idempotency_key": "optimized-shot-decision-001",
            "accepted_segment_ids": [],
            "rejected_segment_ids": [],
        },
        headers=headers,
    )
    assert optimized.status_code == 200, optimized.text
    optimized_user_edit_id = optimized.json()["revision"]["user_edit_revision_id"]

    edited = client.post(
        f"/api/prompt/reverse-operations/{operation_id}/shots/edit",
        json={
            "client_request_id": "optimized-shot-lock-001",
            "action": "lock",
            "shot_id": "shot-one",
            "locked": True,
        },
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["parent_revision_id"] == optimized_user_edit_id
    assert edited.json()["payload"]["final_text"] == "已优化的商品广告提示词"
