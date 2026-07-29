import hashlib
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from PIL import Image
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AssetFolderItem,
    AuditLog,
    GenAsset,
    GenTask,
    MediaProject,
    ReverseOperation,
    ToolDefinition,
    ToolNodeRun,
    ToolRun,
    ToolVersion,
    WorkflowRun,
)
from app.services import project_collection, storage, user_assets


def _generated_asset(user_id: int) -> tuple[int, int, str]:
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=user_id,
            category="image",
            stage="preview",
            status="succeeded",
            cost_frozen=0,
            cost_settled=0,
            created_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=task.id,
            user_id=user_id,
            type="image",
            preview_url="https://cdn.example.com/project.png",
            hd_url="https://cdn.example.com/project-hd.png",
            watermarked=False,
            unlocked=True,
            moderation_status="active",
        )
        db.add(asset)
        db.commit()
        db.refresh(task)
        db.refresh(asset)
        return int(task.id), int(asset.id), user_assets.generated_asset_ref(asset.id)
    finally:
        db.close()


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _stored_generated_asset(
    user_id: int,
    data: bytes,
    *,
    asset_type: str = "image",
    unlocked: bool = True,
    created_at: datetime | None = None,
    cost_frozen: int = 0,
    cost_settled: int = 0,
) -> tuple[int, int, str, str]:
    suffix = "mp4" if asset_type == "video" else "png"
    subdir = "video_preview" if asset_type == "video" else "hd"
    key = storage.save_bytes_named(
        data,
        subdir,
        f"project-{uuid.uuid4().hex}.{suffix}",
    )
    created = created_at or datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        task = GenTask(
            user_id=user_id,
            category=asset_type,
            stage="preview",
            status="succeeded",
            prompt={"final_text": "项目导出提示词"},
            params={"ratio": "1:1"},
            cost_frozen=cost_frozen,
            cost_settled=cost_settled,
            created_at=created,
            finished_at=created,
        )
        db.add(task)
        db.flush()
        asset = GenAsset(
            task_id=task.id,
            user_id=user_id,
            type=asset_type,
            preview_url=storage.public_url(key),
            hd_url=storage.public_url(key),
            watermarked=not unlocked,
            unlocked=unlocked,
            moderation_status="active",
            created_at=created,
        )
        db.add(asset)
        db.commit()
        db.refresh(task)
        db.refresh(asset)
        return (
            int(task.id),
            int(asset.id),
            user_assets.generated_asset_ref(int(asset.id)),
            key,
        )
    finally:
        db.close()


def _create_project(client, headers, title: str = "素材治理项目") -> int:
    response = client.post(
        "/api/projects",
        headers=headers,
        json={"title": title, "project_type": "mixed"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _recipe(client, headers) -> dict:
    response = client.post(
        "/api/recipes",
        headers=headers,
        json={
            "title": "项目配方",
            "category": "image",
            "payload": {
                "schema_version": "creation-recipe.v1",
                "prompt": "商品主视觉",
                "generation_params": {"ratio": "1:1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_project_aggregates_assets_recipes_and_tasks_without_owning_media(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000801", balance=100)
    make_user("13710000802", balance=100)
    owner_headers = auth("13710000801")
    other_headers = auth("13710000802")
    task_id, asset_id, asset_ref = _generated_asset(owner_id)
    recipe = _recipe(client, owner_headers)

    created = client.post(
        "/api/projects",
        headers=owner_headers,
        json={
            "title": "春季新品项目",
            "description": "商品图片与视频统一归集",
            "project_type": "mixed",
            "cover_asset_ref": asset_ref,
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    linked_asset = client.post(
        f"/api/projects/{project_id}/assets",
        headers=owner_headers,
        json={"asset_refs": [asset_ref], "role": "product", "note": "商品主图"},
    )
    assert linked_asset.status_code == 200, linked_asset.text
    assert linked_asset.json()["asset_count"] == 1
    assert linked_asset.json()["assets"][0]["role"] == "product"

    linked_recipe = client.post(
        f"/api/projects/{project_id}/recipes",
        headers=owner_headers,
        json={"recipe_ids": [recipe["id"]]},
    )
    assert linked_recipe.status_code == 200, linked_recipe.text
    linked_task = client.post(
        f"/api/projects/{project_id}/tasks",
        headers=owner_headers,
        json={"task_kind": "generation", "task_ids": [task_id]},
    )
    assert linked_task.status_code == 200, linked_task.text
    assert linked_task.json()["task_count"] == 1

    detail = client.get(f"/api/projects/{project_id}", headers=owner_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["asset_count"] == 1
    assert detail.json()["recipe_count"] == 1
    assert detail.json()["task_count"] == 1
    assert client.get(f"/api/projects/{project_id}", headers=other_headers).status_code == 404

    removed = client.request(
        "DELETE",
        f"/api/projects/{project_id}/assets",
        headers=owner_headers,
        json={"asset_refs": [asset_ref]},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["asset_count"] == 0
    assert removed.json()["cover_asset_ref"] is None

    deleted = client.delete(f"/api/projects/{project_id}", headers=owner_headers)
    assert deleted.status_code == 204, deleted.text
    db = SessionLocal()
    try:
        assert db.get(MediaProject, project_id) is None
        assert db.get(GenAsset, asset_id) is not None
        assert db.get(GenTask, task_id) is not None
        actions = set(db.scalars(
            select(AuditLog.action).where(
                AuditLog.user_id == owner_id,
                AuditLog.biz_type == "media_project",
                AuditLog.biz_id == project_id,
            )
        ))
        assert {
            "create_media_project",
            "add_media_project_assets",
            "add_media_project_recipes",
            "add_media_project_tasks",
            "remove_media_project_assets",
            "delete_media_project",
        } <= actions
    finally:
        db.close()


def test_project_links_and_exports_owned_workflow_run_with_result_media(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000809", balance=100)
    other_id = make_user("13710000810", balance=100)
    headers = auth("13710000809")
    _, output_asset_id, output_ref, _ = _stored_generated_asset(
        owner_id,
        b"workflow-project-video",
        asset_type="video",
        cost_frozen=21,
        cost_settled=18,
    )
    project_id = _create_project(client, headers, "分镜成片项目")

    with SessionLocal() as db:
        tool = ToolDefinition(
            slug="project-workflow-export",
            name="分镜合成与导出",
            description="项目工作流导出验证",
            category="workflow",
            renderer="studio",
            entry_path="/?workflow=project-workflow-export",
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
                    {"key": "compose", "type": "compose", "depends_on": []},
                    {"key": "export", "type": "export", "depends_on": ["compose"]},
                ],
                "output_node": "export",
            },
            pricing_policy={"type": "server_quote", "credits": 0},
            capabilities={"export": True},
            is_active=True,
        )
        db.add(version)
        db.flush()

        def add_run(user_id: int, suffix: str) -> WorkflowRun:
            tool_run = ToolRun(
                user_id=user_id,
                tool_definition_id=tool.id,
                tool_version_id=version.id,
                client_request_id=f"project-workflow-{suffix}",
                request_fingerprint=suffix * 64,
                status="succeeded",
                input_snapshot={"composition": {"title": "成片 A"}},
                output={
                    "schema_version": "video-composition-export.v1",
                    "asset_ref": output_ref,
                    "download_url": f"/api/me/assets/download?asset_ref={output_ref}",
                },
            )
            db.add(tool_run)
            db.flush()
            run = WorkflowRun(
                tool_run_id=tool_run.id,
                user_id=user_id,
                workflow_schema_version="workflow.v1",
                workflow_snapshot=version.workflow,
                status="succeeded",
                finished_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.flush()
            db.add_all([
                ToolNodeRun(
                    workflow_run_id=run.id,
                    node_key="compose",
                    node_type="compose",
                    topological_index=0,
                    depends_on=[],
                    config_snapshot={},
                    output={"asset_ref": output_ref},
                    status="succeeded",
                    attempt_count=1,
                    max_attempts=2,
                    compensation_status="none",
                ),
                ToolNodeRun(
                    workflow_run_id=run.id,
                    node_key="export",
                    node_type="export",
                    topological_index=1,
                    depends_on=["compose"],
                    config_snapshot={},
                    output={"asset_ref": output_ref},
                    status="succeeded",
                    attempt_count=1,
                    max_attempts=2,
                    compensation_status="none",
                ),
            ])
            return run

        owned_run = add_run(owner_id, "o")
        foreign_run = add_run(other_id, "x")
        db.commit()
        owned_run_id = int(owned_run.id)
        foreign_run_id = int(foreign_run.id)

    foreign_link = client.post(
        f"/api/projects/{project_id}/tasks",
        headers=headers,
        json={"task_kind": "workflow", "task_ids": [foreign_run_id]},
    )
    assert foreign_link.status_code == 404, foreign_link.text

    linked = client.post(
        f"/api/projects/{project_id}/tasks",
        headers=headers,
        json={"task_kind": "workflow", "task_ids": [owned_run_id]},
    )
    assert linked.status_code == 200, linked.text
    payload = linked.json()
    assert payload["task_count"] == 1
    assert payload["asset_count"] == 1
    assert payload["assets"][0]["asset_ref"] == output_ref
    assert payload["assets"][0]["role"] == "output"
    assert payload["cost_frozen"] == 21
    assert payload["cost_settled"] == 18
    task = payload["tasks"][0]
    assert task["task_kind"] == "workflow"
    assert task["title"] == "分镜合成与导出"
    assert task["workflow_tool_slug"] == "project-workflow-export"
    assert task["progress"] == 100
    assert task["result_refs"] == [output_ref]
    assert [node["key"] for node in task["workflow_nodes"]] == ["compose", "export"]

    response = client.get(
        f"/api/projects/{project_id}/export?include_media=true",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        tasks = json.loads(archive.read("tasks/tasks.json"))
        assert len(tasks) == 1
        exported = tasks[0]
        assert exported["kind"] == "workflow"
        assert exported["id"] == owned_run_id
        assert exported["tool_definition"]["name"] == "分镜合成与导出"
        assert exported["result_refs"] == [output_ref]
        assert exported["output"]["asset_ref"] == output_ref
        assert [node["key"] for node in exported["nodes"]] == ["compose", "export"]
        assert exported["cost_settled"] == 18
        manifest = json.loads(archive.read("assets/manifest.json"))
        assert manifest[0]["asset_ref"] == output_ref
        assert manifest[0]["media_included"] is True
        assert any(name.startswith("media/") for name in archive.namelist())

    db = SessionLocal()
    try:
        assert db.get(GenAsset, output_asset_id) is not None
    finally:
        db.close()


def test_asset_folder_move_cycle_and_delete_preserve_assets(client, make_user, auth):
    owner_id = make_user("13710000803", balance=100)
    headers = auth("13710000803")
    _, asset_id, asset_ref = _generated_asset(owner_id)

    root = client.post(
        "/api/asset-folders",
        headers=headers,
        json={"name": "商品素材"},
    )
    assert root.status_code == 201, root.text
    child = client.post(
        "/api/asset-folders",
        headers=headers,
        json={"name": "主图", "parent_id": root.json()["id"]},
    )
    assert child.status_code == 201, child.text

    cycle = client.patch(
        f"/api/asset-folders/{root.json()['id']}",
        headers=headers,
        json={"parent_id": child.json()["id"]},
    )
    assert cycle.status_code == 409, cycle.text

    moved = client.post(
        f"/api/asset-folders/{child.json()['id']}/assets",
        headers=headers,
        json={"asset_refs": [asset_ref]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["item_count"] == 1

    moved_again = client.post(
        f"/api/asset-folders/{root.json()['id']}/assets",
        headers=headers,
        json={"asset_refs": [asset_ref]},
    )
    assert moved_again.status_code == 200, moved_again.text
    assert moved_again.json()["item_count"] == 1
    assert client.get(
        f"/api/asset-folders/{child.json()['id']}",
        headers=headers,
    ).json()["item_count"] == 0

    deleted = client.delete(f"/api/asset-folders/{root.json()['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    db = SessionLocal()
    try:
        assert db.get(GenAsset, asset_id) is not None
        folder_actions = list(db.scalars(
            select(AuditLog.action).where(
                AuditLog.user_id == owner_id,
                AuditLog.biz_type == "asset_folder",
            )
        ))
        assert folder_actions.count("create_asset_folder") == 2
        assert folder_actions.count("move_assets_to_folder") == 2
        assert folder_actions.count("delete_asset_folder") == 1
    finally:
        db.close()


def test_asset_folder_detail_items_expose_readable_media_fields(client, make_user, auth):
    owner_id = make_user("13710000811", balance=100)
    headers = auth("13710000811")
    _, _asset_id, asset_ref = _generated_asset(owner_id)

    created = client.post(
        "/api/asset-folders",
        headers=headers,
        json={"name": "可读展示"},
    )
    assert created.status_code == 201, created.text
    folder_id = int(created.json()["id"])

    moved = client.post(
        f"/api/asset-folders/{folder_id}/assets",
        headers=headers,
        json={"asset_refs": [asset_ref]},
    )
    assert moved.status_code == 200, moved.text

    # 项目/文件夹链接可能比素材保留期活得更久：直接落一条失效引用
    dangling_ref = user_assets.generated_asset_ref(999_999)
    db = SessionLocal()
    try:
        db.add(AssetFolderItem(folder_id=folder_id, user_id=owner_id, asset_ref=dangling_ref))
        db.commit()
    finally:
        db.close()

    detail = client.get(f"/api/asset-folders/{folder_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["item_count"] == 2
    by_ref = {row["asset_ref"]: row for row in payload["items"]}
    assert set(by_ref) == {asset_ref, dangling_ref}

    valid = by_ref[asset_ref]
    assert valid["type"] == "image"
    assert valid["available"] is True
    assert valid["url"] == "https://cdn.example.com/project-hd.png"
    assert valid["preview_url"] == "https://cdn.example.com/project.png"
    assert valid["thumb"] == "https://cdn.example.com/project.png"
    assert valid["created_at"] is not None

    dangling = by_ref[dangling_ref]
    assert dangling["available"] is False
    assert dangling["type"] is None
    assert dangling["url"] is None
    assert dangling["thumb"] is None

    # 列表视图仍是轻量结构：不带条目明细，但计数完整
    listed = client.get("/api/asset-folders", headers=headers)
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if int(item["id"]) == folder_id)
    assert row["items"] == []
    assert row["item_count"] == 2


def test_project_asset_tags_exact_duplicates_and_image_similarity(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000804", balance=100)
    make_user("13710000805", balance=100)
    headers = auth("13710000804")
    other_headers = auth("13710000805")
    first_bytes = _png_bytes((20, 80, 180))
    _, _, first_ref, _ = _stored_generated_asset(owner_id, first_bytes)
    _, _, exact_ref, _ = _stored_generated_asset(owner_id, first_bytes)
    _, _, similar_ref, _ = _stored_generated_asset(owner_id, _png_bytes((180, 80, 20)))
    project_id = _create_project(client, headers)
    linked = client.post(
        f"/api/projects/{project_id}/assets",
        headers=headers,
        json={"asset_refs": [first_ref, exact_ref, similar_ref], "role": "source"},
    )
    assert linked.status_code == 200, linked.text

    updated = client.put(
        f"/api/projects/{project_id}/assets/{first_ref}/tags",
        headers=headers,
        json={"tags": [" 商品 ", "#主图", "商品"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["商品", "主图"]
    assert client.put(
        f"/api/projects/{project_id}/assets/{first_ref}/tags",
        headers=other_headers,
        json={"tags": ["越权"]},
    ).status_code == 404

    analyzed = client.post(
        f"/api/projects/{project_id}/assets/{first_ref}/similar?max_distance=8",
        headers=headers,
    )
    assert analyzed.status_code == 200, analyzed.text
    payload = analyzed.json()
    assert payload["status"] == "ready"
    assert payload["exact_available"] is True
    assert payload["perceptual_available"] is True
    matches = {row["asset_ref"]: row for row in payload["matches"]}
    assert matches[exact_ref]["match_type"] == "exact"
    assert matches[exact_ref]["metadata"]["content_sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert matches[similar_ref]["match_type"] == "similar"
    assert matches[similar_ref]["hamming_distance"] == 0

    detail = client.get(f"/api/projects/{project_id}", headers=headers).json()
    by_ref = {row["asset_ref"]: row for row in detail["assets"]}
    assert by_ref[first_ref]["metadata"]["tags"] == ["商品", "主图"]
    assert by_ref[first_ref]["duplicate_count"] == 1
    assert by_ref[first_ref]["similar_count"] == 1


def test_project_asset_similarity_reports_external_and_video_degradation(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000806", balance=100)
    headers = auth("13710000806")
    _, _, external_ref = _generated_asset(owner_id)
    video_bytes = b"project-video-content"
    _, _, video_ref, _ = _stored_generated_asset(
        owner_id,
        video_bytes,
        asset_type="video",
    )
    _, _, video_duplicate_ref, _ = _stored_generated_asset(
        owner_id,
        video_bytes,
        asset_type="video",
    )
    project_id = _create_project(client, headers, "降级检测项目")
    linked = client.post(
        f"/api/projects/{project_id}/assets",
        headers=headers,
        json={
            "asset_refs": [external_ref, video_ref, video_duplicate_ref],
            "role": "source",
        },
    )
    assert linked.status_code == 200, linked.text

    external = client.post(
        f"/api/projects/{project_id}/assets/{external_ref}/similar",
        headers=headers,
    ).json()
    assert external["status"] == "degraded"
    assert external["exact_available"] is False
    assert "受控存储" in external["message"]

    video = client.post(
        f"/api/projects/{project_id}/assets/{video_ref}/similar",
        headers=headers,
    ).json()
    assert video["status"] == "degraded"
    assert video["exact_available"] is True
    assert video["perceptual_available"] is False
    # 视频感知哈希已实现（会真的尝试抽帧算指纹），抽帧不可用时降级为精确匹配。
    # 文案从早期的"暂不支持感知相似"改为说明"为什么不可用"，这里跟随实现。
    assert "视频感知指纹不可用" in video["message"]
    assert "SHA-256 精确重复检测" in video["message"]
    assert any(
        row["asset_ref"] == video_duplicate_ref and row["match_type"] == "exact"
        for row in video["matches"]
    )


def test_project_cost_totals_and_auto_archive_boundary(client, make_user, auth):
    owner_id = make_user("13710000807", balance=100)
    headers = auth("13710000807")
    generation_id, _, _, _ = _stored_generated_asset(
        owner_id,
        _png_bytes((10, 20, 30)),
        cost_frozen=17,
        cost_settled=11,
    )
    db = SessionLocal()
    try:
        reverse = ReverseOperation(
            user_id=owner_id,
            request_fingerprint=uuid.uuid4().hex,
            target="image",
            asset_url="https://cdn.example.com/source.png",
            status="succeeded",
            progress=100,
            cost_frozen=9,
            cost_settled=7,
        )
        db.add(reverse)
        db.commit()
        db.refresh(reverse)
        reverse_id = int(reverse.id)
    finally:
        db.close()
    project_id = _create_project(client, headers, "成本项目")
    assert client.post(
        f"/api/projects/{project_id}/tasks",
        headers=headers,
        json={"task_kind": "generation", "task_ids": [generation_id]},
    ).status_code == 200
    linked = client.post(
        f"/api/projects/{project_id}/tasks",
        headers=headers,
        json={"task_kind": "reverse", "task_ids": [reverse_id]},
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["cost_frozen"] == 26
    assert linked.json()["cost_settled"] == 18

    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        at_boundary = MediaProject(
            user_id=owner_id,
            title="到期项目",
            project_type="mixed",
            status="active",
            auto_archive_after_days=30,
            created_at=now - timedelta(days=40),
            updated_at=now - timedelta(days=30),
        )
        still_active = MediaProject(
            user_id=owner_id,
            title="未到期项目",
            project_type="mixed",
            status="active",
            auto_archive_after_days=30,
            created_at=now - timedelta(days=40),
            updated_at=now - timedelta(days=30) + timedelta(seconds=1),
        )
        db.add_all([at_boundary, still_active])
        db.commit()
        boundary_id = int(at_boundary.id)
        active_id = int(still_active.id)
        result = project_collection.archive_inactive_projects(db, now=now)
        assert boundary_id in result["project_ids"]
        assert active_id not in result["project_ids"]
        assert db.get(MediaProject, boundary_id).status == "archived"
        assert db.get(MediaProject, active_id).status == "active"
    finally:
        db.close()


def test_project_zip_export_contains_snapshots_and_excludes_unauthorized_media(
    client,
    make_user,
    auth,
):
    owner_id = make_user("13710000808", balance=100)
    headers = auth("13710000808")
    allowed_task_id, _, allowed_ref, _ = _stored_generated_asset(
        owner_id,
        _png_bytes((90, 120, 150)),
        unlocked=True,
        cost_frozen=5,
        cost_settled=5,
    )
    _, _, locked_ref, _ = _stored_generated_asset(
        owner_id,
        _png_bytes((150, 120, 90)),
        unlocked=False,
        cost_frozen=3,
    )
    _, _, expired_ref, _ = _stored_generated_asset(
        owner_id,
        _png_bytes((80, 80, 80)),
        unlocked=True,
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    project_id = _create_project(client, headers, "安全导出项目")
    assert client.post(
        f"/api/projects/{project_id}/assets",
        headers=headers,
        json={"asset_refs": [allowed_ref, locked_ref, expired_ref], "role": "source"},
    ).status_code == 200
    recipe = _recipe(client, headers)
    assert client.post(
        f"/api/projects/{project_id}/recipes",
        headers=headers,
        json={"recipe_ids": [recipe["id"]]},
    ).status_code == 200
    assert client.post(
        f"/api/projects/{project_id}/tasks",
        headers=headers,
        json={"task_kind": "generation", "task_ids": [allowed_task_id]},
    ).status_code == 200
    assert client.put(
        f"/api/me/drafts/project-{project_id}",
        headers=headers,
        json={"payload": {"content": "导出草稿"}},
    ).status_code == 200

    response = client.get(
        f"/api/projects/{project_id}/export?include_media=true",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "project.json",
            "assets/manifest.json",
            "recipes/index.json",
            f"recipes/recipe-{recipe['id']}.json",
            "tasks/tasks.json",
            "drafts/project-draft.json",
        } <= names
        assert len([name for name in names if name.startswith("media/")]) == 1
        manifest = json.loads(archive.read("assets/manifest.json"))
        by_ref = {row["asset_ref"]: row for row in manifest}
        assert by_ref[allowed_ref]["media_included"] is True
        assert by_ref[locked_ref]["media_included"] is False
        assert "未解锁" in by_ref[locked_ref]["media_exclusion_reason"]
        assert by_ref[expired_ref]["media_included"] is False
        assert "保留期" in by_ref[expired_ref]["media_exclusion_reason"]
        tasks = json.loads(archive.read("tasks/tasks.json"))
        assert tasks[0]["prompt"]["final_text"] == "项目导出提示词"
        assert tasks[0]["cost_settled"] == 5
        draft = json.loads(archive.read("drafts/project-draft.json"))
        assert draft["payload"]["content"] == "导出草稿"

    db = SessionLocal()
    try:
        audit = db.query(AuditLog).filter(
            AuditLog.user_id == owner_id,
            AuditLog.action == "export_media_project",
        ).order_by(AuditLog.id.desc()).first()
        assert audit is not None
        assert set(audit.detail) == {
            "asset_count",
            "included_media_count",
            "total_media_bytes",
        }
    finally:
        db.close()
