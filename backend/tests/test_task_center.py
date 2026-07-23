from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.db import SessionLocal
from app.models import (
    GenAsset,
    GenerationDispatch,
    GenTask,
    MediaProject,
    MediaProjectTask,
    ModelConfig,
    ModelRoute,
    ParseRecord,
    ReverseOperation,
    ToolDefinition,
    ToolNodeRun,
    ToolRun,
    ToolVersion,
    WorkflowRun,
)
from app.services import reverse_operations
from app.services import task_center as task_center_service
from app.services.model_gateway_config import encrypt_api_key


def _candidate_pricing(cost: int) -> dict:
    return {
        "image": {"1k": cost, "2k": cost, "4k": cost},
        "image_edit": {"1k": cost, "2k": cost, "4k": cost},
        "video_preview_cost": cost,
        "video_per_second": {"480p": cost, "720p": cost, "1080p": cost},
    }


def _candidate_model(
    db,
    *,
    use: str,
    suffix: str,
    capabilities: dict,
    cost: int = 13,
    enabled: bool = True,
    provider: str = "custom_openai",
    gateway_format: str = "openai",
) -> ModelConfig:
    row = ModelConfig(
        use=use,
        model_id=f"task-center-{use}-{suffix}",
        display_name=f"Task center {suffix}",
        is_default=False,
        sort_order=900,
        provider=provider,
        base_url=f"https://task-center-{suffix}.example.com/v1",
        api_key_encrypted=encrypt_api_key(f"task-center-secret-{suffix}"),
        gateway_format=gateway_format,
        cost_credits=cost,
        unlock_cost=0,
        enabled=enabled,
        extra={
            "capabilities": capabilities,
            "credit_pricing": _candidate_pricing(cost),
            "reverse_pricing": {
                "image_cost": cost,
                "video_preset_costs": {"fast": cost + 10, "standard": cost + 12, "fine": cost + 14},
                "audio_surcharge": 4,
            },
        },
    )
    db.add(row)
    db.flush()
    return row


def _delete_candidate_models(
    model_ids: list[int],
    *,
    generation_task_ids: list[int] | None = None,
    reverse_operation_ids: list[int] | None = None,
) -> None:
    with SessionLocal() as db:
        if generation_task_ids:
            db.query(GenTask).filter(GenTask.id.in_(generation_task_ids)).delete(
                synchronize_session=False
            )
        if reverse_operation_ids:
            db.query(ReverseOperation).filter(
                ReverseOperation.id.in_(reverse_operation_ids)
            ).delete(synchronize_session=False)
        db.query(ModelRoute).filter(ModelRoute.model_config_id.in_(model_ids)).delete(
            synchronize_session=False
        )
        db.query(ModelConfig).filter(ModelConfig.id.in_(model_ids)).delete(
            synchronize_session=False
        )
        db.commit()


def test_task_center_unifies_kinds_cursor_projects_and_results(client, make_user, auth):
    user_id = make_user("13971200001", balance=100)
    other_user_id = make_user("13971200002", balance=100)
    headers = auth("13971200001")
    now = datetime.now(timezone.utc).replace(microsecond=0)

    with SessionLocal() as db:
        generation = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            prompt={"final_text": "white ceramic product photo"},
            model_use="image",
            params={"n": 1, "_model_snapshot": {"model_id": "image-test"}},
            status="succeeded",
            cost_frozen=8,
            cost_settled=8,
            created_at=now,
            finished_at=now + timedelta(seconds=5),
        )
        reverse = ReverseOperation(
            user_id=user_id,
            request_fingerprint="a" * 64,
            target="video",
            asset_url="https://example.com/source.mp4",
            analysis_focus="camera_motion",
            status="running",
            progress=45,
            cost_frozen=12,
            cost_settled=0,
            created_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(seconds=20),
        )
        parsed = ParseRecord(
            user_id=user_id,
            url="https://example.com/post",
            status="done",
            assets=[],
            created_at=now - timedelta(minutes=2),
        )
        hidden = ParseRecord(
            user_id=other_user_id,
            url="https://example.com/other-user",
            status="failed",
            error="request timed out",
            created_at=now + timedelta(minutes=1),
        )
        project = MediaProject(
            user_id=user_id,
            title="Catalog launch",
            project_type="mixed",
            status="active",
        )
        db.add_all([generation, reverse, parsed, hidden, project])
        db.flush()
        asset = GenAsset(
            task_id=generation.id,
            user_id=user_id,
            type="image",
            preview_url="/media/preview/task-center.png",
            watermarked=True,
            unlocked=False,
        )
        db.add(asset)
        db.add(MediaProjectTask(
            project_id=project.id,
            task_kind="generation",
            task_id=generation.id,
        ))
        db.commit()
        generation_id = generation.id
        reverse_id = reverse.id
        parse_id = parsed.id
        project_id = project.id
        asset_id = asset.id

    first = client.get("/api/task-center?limit=2", headers=headers)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["total"] == 3
    assert first_body["counts"] == {
        "active": 1,
        "succeeded": 2,
        "failed": 0,
        "canceled": 0,
        "needs_attention": 0,
        "all": 3,
    }
    assert [item["key"] for item in first_body["items"]] == [
        f"generation:{generation_id}",
        f"reverse:{reverse_id}",
    ]
    generation_item = first_body["items"][0]
    assert generation_item["project_ids"] == [project_id]
    assert generation_item["result_refs"] == [f"g.{asset_id}"]
    assert generation_item["cost_settled"] == 8
    assert "create_similar" in generation_item["available_actions"]
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second = client.get(
        "/api/task-center",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert [item["key"] for item in second_body["items"]] == [f"parse:{parse_id}"]
    assert second_body["has_more"] is False
    assert second_body["next_cursor"] is None


def test_task_center_filters_attention_and_rejects_invalid_cursor(client, make_user, auth):
    user_id = make_user("13971200003", balance=100)
    headers = auth("13971200003")
    now = datetime.now(timezone.utc).replace(microsecond=0)

    with SessionLocal() as db:
        db.add_all([
            GenTask(
                user_id=user_id,
                category="image",
                stage="preview",
                prompt={"final_text": "review result"},
                model_use="image",
                params={"_error_type": "provider_error"},
                status="needs_review",
                cost_frozen=8,
                cost_settled=0,
                error="provider state unknown",
                created_at=now,
            ),
            ReverseOperation(
                user_id=user_id,
                request_fingerprint="b" * 64,
                target="video",
                asset_url="https://example.com/confirm.mp4",
                status="needs_confirmation",
                progress=35,
                cost_frozen=10,
                cost_settled=0,
                created_at=now - timedelta(seconds=1),
            ),
            ReverseOperation(
                user_id=user_id,
                request_fingerprint="c" * 64,
                target="image",
                asset_url="https://example.com/retry.png",
                status="failed",
                progress=10,
                error="provider failed",
                created_at=now - timedelta(seconds=2),
            ),
        ])
        db.commit()

    attention = client.get(
        "/api/task-center?status=needs_attention&category=video",
        headers=headers,
    )
    assert attention.status_code == 200, attention.text
    body = attention.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "reverse"
    assert body["items"][0]["available_actions"] == ["view", "confirm", "cancel"]
    assert body["items"][0]["error_type"] is None

    generation_attention = client.get(
        "/api/task-center?status=needs_attention&category=image",
        headers=headers,
    )
    assert generation_attention.status_code == 200, generation_attention.text
    generation_item = generation_attention.json()["items"][0]
    assert generation_item["status"] == "needs_review"
    assert generation_item["progress"] == 100

    failed_reverse = client.get(
        "/api/task-center?kind=reverse&status=failed",
        headers=headers,
    )
    assert failed_reverse.status_code == 200, failed_reverse.text
    reverse_actions = failed_reverse.json()["items"][0]["available_actions"]
    assert reverse_actions[:2] == ["view", "retry"]
    assert set(reverse_actions).issubset({"view", "retry", "retry_compatible_model"})
    assert len(reverse_actions) == len(set(reverse_actions))

    invalid = client.get(
        "/api/task-center?cursor=not-a-valid-cursor",
        headers=headers,
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["detail"]["code"] == "INVALID_TASK_CURSOR"


def test_task_center_distinguishes_generation_retry_modes_and_hides_unreconciled_retry(
    client,
    make_user,
    auth,
):
    user_id = make_user("13971200004", balance=100)
    headers = auth("13971200004")
    now = datetime.now(timezone.utc).replace(microsecond=0)

    with SessionLocal() as db:
        source = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            prompt={"final_text": "source generation"},
            model_use="image",
            params={},
            status="failed",
            cost_frozen=8,
            cost_settled=0,
            error="provider failed before submission",
            created_at=now - timedelta(seconds=3),
            finished_at=now - timedelta(seconds=2),
        )
        db.add(source)
        db.flush()
        retried = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            prompt={"final_text": "requote retry"},
            model_use="image",
            params={},
            status="failed",
            retry_of_task_id=source.id,
            cost_frozen=11,
            cost_settled=0,
            error="known provider failure",
            created_at=now - timedelta(seconds=1),
            finished_at=now,
        )
        unreconciled = GenTask(
            user_id=user_id,
            category="video",
            stage="preview",
            prompt={"final_text": "dispatch reconciliation"},
            model_use="video",
            params={},
            status="failed",
            cost_frozen=20,
            cost_settled=0,
            error="dispatch state unknown",
            created_at=now,
            finished_at=now,
        )
        db.add_all([retried, unreconciled])
        db.flush()
        db.add(GenerationDispatch(
            task_id=unreconciled.id,
            attempt=1,
            task_name="generate_video_task",
            celery_task_id="task-center-unreconciled-1",
            status="unknown",
        ))
        db.commit()
        source_id = int(source.id)
        retried_id = int(retried.id)
        unreconciled_id = int(unreconciled.id)

    response = client.get("/api/task-center?kind=generation&status=failed", headers=headers)
    assert response.status_code == 200, response.text
    items = {int(item["id"]): item for item in response.json()["items"]}

    for retryable_id in (source_id, retried_id):
        actions = items[retryable_id]["available_actions"]
        assert actions[:2] == ["view", "retry_requote"]
        assert "retry" not in actions
        assert actions == list(dict.fromkeys(actions))
        if items[retryable_id]["compatible_retry_models"]:
            assert actions[-1] == "retry_compatible_model"
            assert items[retryable_id]["failure_suggestion"]["recommended_action"] == (
                "retry_compatible_model"
            )
    assert int(items[retried_id]["retry_of_task_id"]) == source_id
    assert items[unreconciled_id]["available_actions"] == ["view"]
    assert items[unreconciled_id]["dispatch_reconciliation_required"] is True
    assert items[unreconciled_id]["compatible_retry_models"] == []
    assert items[unreconciled_id]["failure_suggestion"]["recommended_action"] == "view"


def test_task_center_filters_and_prices_compatible_generation_retry_models(
    client,
    make_user,
    auth,
):
    user_id = make_user("13971200014", balance=100)
    headers = auth("13971200014")
    capabilities = {
        "image_to_image": True,
        "image_edit": True,
        "reference_image": True,
        "multi_reference": True,
    }
    model_ids: list[int] = []
    with SessionLocal() as db:
        current = _candidate_model(
            db,
            use="image",
            suffix="generation-current",
            capabilities=capabilities,
        )
        compatible = _candidate_model(
            db,
            use="image",
            suffix="generation-compatible",
            capabilities=capabilities,
            cost=13,
        )
        incompatible = _candidate_model(
            db,
            use="image",
            suffix="generation-incompatible",
            capabilities={**capabilities, "multi_reference": False},
        )
        disabled = _candidate_model(
            db,
            use="image",
            suffix="generation-disabled",
            capabilities=capabilities,
            enabled=False,
        )
        unavailable = _candidate_model(
            db,
            use="image",
            suffix="generation-unavailable",
            capabilities=capabilities,
        )
        db.add(ModelRoute(
            model_config_id=int(unavailable.id),
            route_key="only-route",
            name="Unavailable route",
            model_id=unavailable.model_id,
            provider="custom_openai",
            base_url=unavailable.base_url,
            api_key_encrypted=unavailable.api_key_encrypted,
            gateway_format="openai",
            extra={},
            priority=10,
            enabled=True,
            managed_by_model_config=False,
            health_status="open",
            cooldown_until=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        task = GenTask(
            user_id=user_id,
            model_config_id=int(current.id),
            source_asset_url="https://example.com/product-primary.png",
            source_type="image",
            category="image",
            stage="preview",
            prompt={"final_text": "preserve the product"},
            model_use="image",
            params={
                "n": 2,
                "size": "1024x1024",
                "reference_image_url": "https://example.com/product-reference.png",
                "product_detail_images": ["https://example.com/product-detail.png"],
                "_model_snapshot": {
                    "model_config_id": int(current.id),
                    "model_id": current.model_id,
                },
                "_worker_attempt": 7,
            },
            status="failed",
            cost_frozen=9,
            error="request timed out",
            finished_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        task_id = int(task.id)
        compatible_id = int(compatible.id)
        excluded_ids = {int(current.id), int(incompatible.id), int(disabled.id), int(unavailable.id)}
        model_ids = list(excluded_ids | {compatible_id})

    try:
        response = client.get("/api/task-center?kind=generation&status=failed", headers=headers)
        assert response.status_code == 200, response.text
        item = next(item for item in response.json()["items"] if int(item["id"]) == task_id)
        candidates = {
            int(candidate["model_config_id"]): candidate
            for candidate in item["compatible_retry_models"]
        }
        assert compatible_id in candidates
        assert excluded_ids.isdisjoint(candidates)
        assert candidates[compatible_id] == {
            "model_config_id": compatible_id,
            "display_name": "Task center generation-compatible",
            "model_id": "task-center-image-generation-compatible",
            "provider": "custom_openai",
            "estimated_credits": 26,
            "route_status": "available",
            "reason": "支持当前图片参考与编辑参数",
        }
        assert item["available_actions"] == [
            "view",
            "retry_requote",
            "retry_compatible_model",
        ]
        assert item["failure_suggestion"]["error_type"] == "provider_timeout"
        assert item["failure_suggestion"]["recommended_action"] == "retry_compatible_model"
    finally:
        _delete_candidate_models(model_ids, generation_task_ids=[task_id])


def test_task_center_tolerates_legacy_non_object_generation_params(client, make_user, auth):
    user_id = make_user("13971200015", balance=100)
    headers = auth("13971200015")
    with SessionLocal() as db:
        task = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            prompt={"final_text": "legacy params"},
            model_use="image",
            params=["legacy", "invalid"],
            status="failed",
            cost_frozen=0,
            error="request timed out",
            finished_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()
        task_id = int(task.id)

    try:
        response = client.get("/api/task-center?kind=generation&status=failed", headers=headers)
        assert response.status_code == 200, response.text
        item = next(item for item in response.json()["items"] if int(item["id"]) == task_id)
        assert item["available_actions"][0:2] == ["view", "retry_requote"]
        assert item["failure_suggestion"]["error_type"] == "provider_timeout"
    finally:
        with SessionLocal() as db:
            db.query(GenTask).filter(GenTask.id == task_id).delete(synchronize_session=False)
            db.commit()


def test_reverse_quote_failure_does_not_poison_shared_runtime_cache(make_user, monkeypatch):
    user_id = make_user("13971200016", balance=100)
    capabilities = {
        "image_analysis": True,
        "image_input": True,
    }
    model_ids: list[int] = []
    operation_ids: list[int] = []
    with SessionLocal() as db:
        current = _candidate_model(
            db,
            use="vision",
            suffix="cache-current",
            capabilities=capabilities,
        )
        compatible = _candidate_model(
            db,
            use="vision",
            suffix="cache-compatible",
            capabilities=capabilities,
            cost=19,
        )

        def operation(asset_name: str, fingerprint: str) -> ReverseOperation:
            asset_url = f"https://example.com/{asset_name}.png"
            row = ReverseOperation(
                user_id=user_id,
                model_config_id=int(current.id),
                request_fingerprint=fingerprint,
                target="image",
                asset_url=asset_url,
                analysis_focus="comprehensive",
                analysis_precision="standard",
                output_purpose="generation",
                request_context={
                    "client_request_id": f"task-center-{asset_name}",
                    "asset_url": asset_url,
                    "sources": [{
                        "asset_url": asset_url,
                        "source_type": "image",
                        "role": "primary",
                    }],
                    "target": "image",
                    "source_type": "image",
                    "analysis_focus": "comprehensive",
                    "analysis_precision": "standard",
                    "output_purpose": "generation",
                    "model_config_id": int(current.id),
                },
                model_snapshot={
                    "model_config_id": int(current.id),
                    "model_id": current.model_id,
                },
                status="failed",
                progress=100,
                error="request timed out",
                finished_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.flush()
            return row

        invalid = operation("cache-invalid", "a" * 64)
        valid = operation("cache-valid", "b" * 64)
        db.commit()
        model_ids = [int(current.id), int(compatible.id)]
        operation_ids = [int(invalid.id), int(valid.id)]

        original_prepare = reverse_operations.prepare_reverse_quote

        def prepare_with_one_invalid(db_session, *, user_id, body):
            if str(body.asset_url).endswith("cache-invalid.png"):
                raise HTTPException(400, "task-specific quote rejection")
            return original_prepare(db_session, user_id=user_id, body=body)

        monkeypatch.setattr(
            reverse_operations,
            "prepare_reverse_quote",
            prepare_with_one_invalid,
        )
        runtime_cache: dict[int, object | None] = {}
        invalid_candidates = task_center_service._reverse_retry_models(
            db,
            invalid,
            models=[current, compatible],
            runtime_cache=runtime_cache,
        )
        assert invalid_candidates == []
        assert runtime_cache[int(compatible.id)] is not None

        valid_candidates = task_center_service._reverse_retry_models(
            db,
            valid,
            models=[current, compatible],
            runtime_cache=runtime_cache,
        )
        assert [item["model_config_id"] for item in valid_candidates] == [int(compatible.id)]

    _delete_candidate_models(model_ids, reverse_operation_ids=operation_ids)


@pytest.mark.parametrize(
    ("case_index", "target", "source_type", "include_audio", "reason", "expected_cost"),
    [
        (1, "image", "image", False, "支持图片证据反推", 17),
        (2, "video", "image", False, "支持图片到视频提示词分析", 29),
        (3, "video", "video", True, "支持视频证据反推", 33),
        (4, "product_profile", "image", False, "支持商品档案识别", 17),
        (5, "portrait_profile", "image", False, "支持人物档案识别", 17),
    ],
)
def test_task_center_filters_and_prices_compatible_reverse_retry_models(
    client,
    make_user,
    auth,
    case_index,
    target,
    source_type,
    include_audio,
    reason,
    expected_cost,
):
    phone = f"139712001{case_index:02d}"
    user_id = make_user(phone, balance=100)
    headers = auth(phone)
    capabilities = {
        "image_analysis": True,
        "image_input": True,
        "video_analysis": True,
        "video_input": True,
        "product_profile": True,
        "portrait_profile": True,
    }
    suffix = f"reverse-{case_index}"
    asset_url = (
        f"https://example.com/{suffix}.mp4"
        if source_type == "video"
        else f"https://example.com/{suffix}.png"
    )
    model_ids: list[int] = []
    with SessionLocal() as db:
        current = _candidate_model(
            db,
            use="vision",
            suffix=f"{suffix}-current",
            capabilities=capabilities,
            cost=11,
        )
        compatible = _candidate_model(
            db,
            use="vision",
            suffix=f"{suffix}-compatible",
            capabilities=capabilities,
            cost=17,
        )
        incompatible_capabilities = dict(capabilities)
        if target == "product_profile":
            incompatible_capabilities["product_profile"] = False
        elif target == "portrait_profile":
            incompatible_capabilities["portrait_profile"] = False
        elif source_type == "video":
            incompatible_capabilities.update({"video_analysis": False, "video_input": False})
        else:
            incompatible_capabilities.update({"image_analysis": False, "image_input": False})
        incompatible = _candidate_model(
            db,
            use="vision",
            suffix=f"{suffix}-incompatible",
            capabilities=incompatible_capabilities,
        )
        anthropic = _candidate_model(
            db,
            use="vision",
            suffix=f"{suffix}-anthropic",
            capabilities=capabilities,
            provider="anthropic",
            gateway_format="anthropic",
        )
        disabled = _candidate_model(
            db,
            use="vision",
            suffix=f"{suffix}-disabled",
            capabilities=capabilities,
            enabled=False,
        )
        request_context = {
            "client_request_id": f"task-center-reverse-{case_index}",
            "asset_url": asset_url,
            "sources": [{
                "asset_url": asset_url,
                "source_type": source_type,
                "role": "primary",
            }],
            "target": target,
            "source_type": source_type,
            "analysis_focus": "comprehensive",
            "analysis_precision": "standard",
            "video_analysis_preset": "standard",
            "output_purpose": "generation",
            "include_audio": include_audio,
            "model_config_id": int(current.id),
        }
        operation = ReverseOperation(
            user_id=user_id,
            model_config_id=int(current.id),
            request_fingerprint=(f"{case_index}" * 64)[:64],
            target=target,
            asset_url=asset_url,
            analysis_focus="comprehensive",
            analysis_precision="standard",
            output_purpose="generation",
            include_audio=include_audio,
            request_context=request_context,
            model_snapshot={
                "model_config_id": int(current.id),
                "model_id": current.model_id,
            },
            status="failed",
            progress=100,
            cost_frozen=5,
            cost_settled=0,
            error="provider timeout",
            finished_at=datetime.now(timezone.utc),
        )
        db.add(operation)
        db.commit()
        operation_id = int(operation.id)
        compatible_id = int(compatible.id)
        anthropic_id = int(anthropic.id)
        excluded_ids = {int(current.id), int(incompatible.id), int(disabled.id)}
        model_ids = list(excluded_ids | {compatible_id, anthropic_id})

    try:
        response = client.get("/api/task-center?kind=reverse&status=failed", headers=headers)
        assert response.status_code == 200, response.text
        item = next(item for item in response.json()["items"] if int(item["id"]) == operation_id)
        candidates = {
            int(candidate["model_config_id"]): candidate
            for candidate in item["compatible_retry_models"]
        }
        assert compatible_id in candidates
        assert anthropic_id in candidates
        assert candidates[anthropic_id]["provider"] == "anthropic"
        assert excluded_ids.isdisjoint(candidates)
        assert candidates[compatible_id]["estimated_credits"] == expected_cost
        assert candidates[compatible_id]["route_status"] == "configured"
        assert candidates[compatible_id]["reason"] == reason
        assert item["available_actions"] == ["view", "retry", "retry_compatible_model"]
        assert item["failure_suggestion"]["recommended_action"] == "retry_compatible_model"
    finally:
        _delete_candidate_models(model_ids, reverse_operation_ids=[operation_id])


def test_task_center_projects_workflow_nodes_actions_and_output_assets(
    client,
    make_user,
    auth,
):
    user_id = make_user("13971200005", balance=100)
    headers = auth("13971200005")
    now = datetime.now(timezone.utc).replace(microsecond=0)

    with SessionLocal() as db:
        tool = ToolDefinition(
            slug="task-center-workflow",
            name="分镜合成与导出",
            description="把镜头合成为最终视频",
            category="workflow",
            renderer="studio",
            entry_path="/?workflow=task-center-workflow",
            enabled=True,
        )
        db.add(tool)
        db.flush()
        version = ToolVersion(
            tool_definition_id=tool.id,
            version=1,
            schema_version="tool.v1",
            input_schema={},
            workflow={
                "type": "workflow.v1",
                "nodes": [
                    {"key": "prepare", "type": "parse", "depends_on": []},
                    {
                        "key": "review",
                        "type": "manual_review",
                        "depends_on": ["prepare"],
                    },
                    {"key": "export", "type": "export", "depends_on": ["review"]},
                ],
                "output_node": "export",
            },
            pricing_policy={"type": "server_quote"},
            capabilities={},
            is_active=True,
        )
        db.add(version)
        db.flush()

        output_task = GenTask(
            user_id=user_id,
            category="video",
            stage="final",
            status="succeeded",
            cost_frozen=14,
            cost_settled=12,
            created_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4),
        )
        db.add(output_task)
        db.flush()
        output_asset = GenAsset(
            task_id=output_task.id,
            user_id=user_id,
            type="video",
            preview_url="/media/video_preview/workflow.mp4",
            hd_url="/media/video_hd/workflow.mp4",
            watermarked=False,
            unlocked=True,
        )
        db.add(output_asset)
        db.flush()
        output_ref = f"g.{output_asset.id}"

        def add_run(
            *,
            suffix: str,
            status: str,
            created_at: datetime,
            node_statuses: list[tuple[str, str, str]],
            output: dict | None = None,
            current_node_key: str | None = None,
            error: str | None = None,
            cost_frozen: int = 0,
            cost_settled: int = 0,
        ) -> WorkflowRun:
            tool_run = ToolRun(
                user_id=user_id,
                tool_definition_id=tool.id,
                tool_version_id=version.id,
                client_request_id=f"task-center-workflow-{suffix}",
                request_fingerprint=suffix * 64,
                status=status,
                input_snapshot={"composition": {"title": f"项目 {suffix}"}},
                cost_frozen=cost_frozen,
                cost_settled=cost_settled,
                output=output,
                error=error,
                created_at=created_at,
            )
            db.add(tool_run)
            db.flush()
            run = WorkflowRun(
                tool_run_id=tool_run.id,
                user_id=user_id,
                workflow_schema_version="workflow.v1",
                workflow_snapshot=version.workflow,
                status=status,
                current_node_key=current_node_key,
                error=error,
                created_at=created_at,
            )
            db.add(run)
            db.flush()
            for index, (key, node_type, node_status) in enumerate(node_statuses):
                db.add(ToolNodeRun(
                    workflow_run_id=run.id,
                    node_key=key,
                    node_type=node_type,
                    topological_index=index,
                    depends_on=[] if index == 0 else [node_statuses[index - 1][0]],
                    config_snapshot={},
                    status=node_status,
                    attempt_count=1 if node_status != "queued" else 0,
                    max_attempts=2,
                    compensation_status=(
                        "running" if status == "compensating" and index == 0 else "none"
                    ),
                ))
            return run

        waiting = add_run(
            suffix="w",
            status="waiting_review",
            created_at=now,
            node_statuses=[
                ("prepare", "parse", "succeeded"),
                ("review", "manual_review", "waiting_review"),
                ("export", "export", "queued"),
            ],
            current_node_key="review",
        )
        compensating = add_run(
            suffix="c",
            status="compensating",
            created_at=now - timedelta(seconds=1),
            node_statuses=[
                ("prepare", "compose", "succeeded"),
                ("export", "export", "canceled"),
            ],
        )
        failed = add_run(
            suffix="f",
            status="failed",
            created_at=now - timedelta(seconds=2),
            node_statuses=[
                ("prepare", "compose", "failed"),
                ("export", "export", "canceled"),
            ],
            error="compose failed",
        )
        paid_failed = add_run(
            suffix="p",
            status="failed",
            created_at=now - timedelta(seconds=3),
            node_statuses=[
                ("prepare", "compose", "failed"),
                ("export", "export", "canceled"),
            ],
            error="paid compose failed",
            cost_frozen=5,
        )
        succeeded = add_run(
            suffix="s",
            status="succeeded",
            created_at=now - timedelta(seconds=4),
            node_statuses=[
                ("prepare", "compose", "succeeded"),
                ("export", "export", "succeeded"),
            ],
            output={
                "asset_ref": output_ref,
                "manifest": {"outputs": [{"asset_ref": output_ref}]},
            },
            cost_frozen=3,
            cost_settled=3,
        )
        project = MediaProject(
            user_id=user_id,
            title="Workflow project",
            project_type="video",
            status="active",
        )
        db.add(project)
        db.flush()
        db.add(MediaProjectTask(
            project_id=project.id,
            task_kind="workflow",
            task_id=succeeded.id,
        ))
        db.commit()
        waiting_id = int(waiting.id)
        compensating_id = int(compensating.id)
        failed_id = int(failed.id)
        paid_failed_id = int(paid_failed.id)
        succeeded_id = int(succeeded.id)
        project_id = int(project.id)

    response = client.get("/api/task-center?kind=workflow", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {
        "active": 1,
        "succeeded": 1,
        "failed": 2,
        "canceled": 0,
        "needs_attention": 1,
        "all": 5,
    }
    items = {int(item["id"]): item for item in body["items"]}

    waiting_item = items[waiting_id]
    assert waiting_item["status_group"] == "needs_attention"
    assert waiting_item["progress"] == 33
    assert waiting_item["workflow_current_node_key"] == "review"
    assert waiting_item["available_actions"] == ["view", "cancel", "review"]
    assert [node["key"] for node in waiting_item["workflow_nodes"]] == [
        "prepare",
        "review",
        "export",
    ]

    compensating_item = items[compensating_id]
    assert compensating_item["status_group"] == "active"
    assert compensating_item["available_actions"] == ["view", "cancel", "resume"]

    failed_item = items[failed_id]
    assert failed_item["workflow_failed_node_key"] == "prepare"
    assert failed_item["available_actions"] == ["view", "retry"]

    paid_failed_item = items[paid_failed_id]
    assert paid_failed_item["workflow_failed_node_key"] == "prepare"
    assert paid_failed_item["available_actions"] == ["view"]
    assert paid_failed_item["cost_frozen"] == 5

    succeeded_item = items[succeeded_id]
    assert succeeded_item["title"] == "分镜合成与导出"
    assert succeeded_item["workflow_tool_slug"] == "task-center-workflow"
    assert succeeded_item["result_refs"] == [output_ref]
    assert succeeded_item["cost_frozen"] == 17
    assert succeeded_item["cost_settled"] == 15
    assert succeeded_item["project_ids"] == [project_id]
    assert succeeded_item["available_actions"] == ["view", "download"]

    attention = client.get(
        "/api/task-center?kind=workflow&status=needs_attention",
        headers=headers,
    )
    assert attention.status_code == 200, attention.text
    assert [item["id"] for item in attention.json()["items"]] == [waiting_id]
