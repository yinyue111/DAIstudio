"""管理端配置对象软删：模型路由与工具目录可删、可重建，历史引用保留。"""

from app.db import SessionLocal
from app.models import ModelRoute, ToolDefinition, ToolRun, ToolVersion


def _pricing(cost: int = 9) -> dict:
    return {
        "image": {"1k": cost, "2k": cost, "4k": cost},
        "image_edit": {"1k": cost, "2k": cost, "4k": cost},
        "video_preview_cost": 50,
        "video_per_second": {"480p": 10, "720p": 10, "1080p": 10},
    }


def _model_body(suffix: str) -> dict:
    return {
        "use": "image",
        "model_id": f"soft-delete-model-{suffix}",
        "display_name": f"软删测试模型 {suffix}",
        "is_default": False,
        "provider": "custom_openai",
        "base_url": f"https://soft-delete-{suffix}.example.com/v1",
        "api_key": f"soft-delete-secret-{suffix}",
        "gateway_format": "openai",
        "cost_credits": 9,
        "unlock_cost": 0,
        "enabled": True,
        "extra": {
            "capabilities": {"text_to_image": True},
            "credit_pricing": _pricing(),
        },
    }


def _route_body(key: str) -> dict:
    return {
        "route_key": key,
        "name": f"软删测试路由 {key}",
        "model_id": f"soft-delete-provider-{key}",
        "provider": "custom_openai",
        "base_url": f"https://route-{key}.example.com/v1",
        "api_key": f"route-secret-{key}",
        "gateway_format": "openai",
        "priority": 1,
        "failure_threshold": 2,
        "window_seconds": 60,
        "cooldown_seconds": 30,
    }


def _tool_body(slug: str) -> dict:
    return {
        "slug": slug,
        "name": f"软删测试工具 {slug}",
        "description": "软删测试",
        "category": "workflow",
        "renderer": "studio",
        "entry_path": f"/?workflow={slug}",
        "icon": "wand-sparkles",
        "featured": True,
        "initial_version": {
            "input_schema": {"fields": [{"name": "prompt", "type": "text"}]},
            "workflow": {"type": "studio_preset", "creation_mode": "image_edit"},
            "pricing_policy": {"type": "server_quote"},
            "capabilities": {"reverse": True},
        },
    }


def test_model_route_soft_delete_and_key_reuse(client, make_user, auth):
    make_user("13975100001", admin=True)
    headers = auth("13975100001")
    model = client.post("/api/admin/models", headers=headers, json=_model_body("route"))
    assert model.status_code == 201, model.text
    model_id = int(model.json()["model"]["id"])

    created = client.post(
        f"/api/admin/models/{model_id}/routes", headers=headers, json=_route_body("dup-key")
    )
    assert created.status_code == 201, created.text
    route_id = int(created.json()["id"])

    deleted = client.delete(
        f"/api/admin/models/{model_id}/routes/{route_id}", headers=headers
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}

    # 已删路由从管理列表消失，且不能再被编辑 / 重复删除
    listed = client.get(f"/api/admin/models/{model_id}/routes", headers=headers)
    assert listed.status_code == 200, listed.text
    assert route_id not in [row["id"] for row in listed.json()["items"]]
    patched = client.patch(
        f"/api/admin/models/{model_id}/routes/{route_id}",
        headers=headers,
        json={"name": "不应命中"},
    )
    assert patched.status_code == 404, patched.text
    again = client.delete(
        f"/api/admin/models/{model_id}/routes/{route_id}", headers=headers
    )
    assert again.status_code == 404, again.text

    # 软删：历史行保留（deleted_at 置位、enabled=False），route_key 立即可复用
    with SessionLocal() as db:
        row = db.get(ModelRoute, route_id)
        assert row is not None
        assert row.deleted_at is not None
        assert row.enabled is False
    recreated = client.post(
        f"/api/admin/models/{model_id}/routes", headers=headers, json=_route_body("dup-key")
    )
    assert recreated.status_code == 201, recreated.text
    assert int(recreated.json()["id"]) != route_id


def test_legacy_default_route_cannot_be_deleted(client, make_user, auth):
    make_user("13975100002", admin=True)
    headers = auth("13975100002")
    model = client.post("/api/admin/models", headers=headers, json=_model_body("legacy"))
    assert model.status_code == 201, model.text
    model_id = int(model.json()["model"]["id"])
    with SessionLocal() as db:
        route = ModelRoute(
            model_config_id=model_id,
            route_key="legacy-default",
            name="默认兼容路由",
            priority=100,
            enabled=True,
            managed_by_model_config=True,
            extra={},
        )
        db.add(route)
        db.commit()
        legacy_id = int(route.id)

    rejected = client.delete(
        f"/api/admin/models/{model_id}/routes/{legacy_id}", headers=headers
    )
    assert rejected.status_code == 409, rejected.text
    assert "默认兼容路由不能删除" in rejected.text
    with SessionLocal() as db:
        row = db.get(ModelRoute, legacy_id)
        assert row.deleted_at is None
        assert row.enabled is True


def test_tool_soft_delete_hides_catalog_and_frees_slug(client, make_user, auth):
    make_user("13975100003", admin=True)
    make_user("13975100004")
    admin_headers = auth("13975100003")
    user_headers = auth("13975100004")

    created = client.post(
        "/api/admin/tools", headers=admin_headers, json=_tool_body("soft-delete-tool")
    )
    assert created.status_code == 201, created.text
    tool_id = int(created.json()["id"])
    public = client.get("/api/tools", headers=user_headers)
    assert tool_id in [row["id"] for row in public.json()["items"]]

    deleted = client.delete(f"/api/admin/tools/{tool_id}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True}

    # 管理列表 / 版本详情 / 用户目录全部不可见
    admin_list = client.get("/api/admin/tools", headers=admin_headers)
    assert tool_id not in [row["id"] for row in admin_list.json()["items"]]
    versions = client.get(f"/api/admin/tools/{tool_id}/versions", headers=admin_headers)
    assert versions.status_code == 404, versions.text
    patched = client.patch(
        f"/api/admin/tools/{tool_id}", headers=admin_headers, json={"name": "不应命中"}
    )
    assert patched.status_code == 404, patched.text
    public_after = client.get("/api/tools", headers=user_headers)
    assert tool_id not in [row["id"] for row in public_after.json()["items"]]

    # 历史行与版本保留，但活跃版本已停用，slug 立即可复用
    with SessionLocal() as db:
        tool = db.get(ToolDefinition, tool_id)
        assert tool is not None
        assert tool.deleted_at is not None
        assert tool.enabled is False
        active = (
            db.query(ToolVersion)
            .filter(ToolVersion.tool_definition_id == tool_id, ToolVersion.is_active.is_(True))
            .first()
        )
        assert active is None
    recreated = client.post(
        "/api/admin/tools", headers=admin_headers, json=_tool_body("soft-delete-tool")
    )
    assert recreated.status_code == 201, recreated.text
    assert int(recreated.json()["id"]) != tool_id


def test_tool_delete_blocked_by_active_run(client, make_user, auth):
    make_user("13975100005", admin=True)
    user_id = make_user("13975100006")
    admin_headers = auth("13975100005")

    created = client.post(
        "/api/admin/tools", headers=admin_headers, json=_tool_body("soft-delete-busy-tool")
    )
    assert created.status_code == 201, created.text
    tool_id = int(created.json()["id"])
    version_id = int(created.json()["active_version"]["id"])

    with SessionLocal() as db:
        db.add(ToolRun(
            user_id=user_id,
            tool_definition_id=tool_id,
            tool_version_id=version_id,
            client_request_id="soft-delete-busy-run",
            request_fingerprint="f" * 64,
            status="running",
            input_snapshot={},
            pricing_snapshot={},
        ))
        db.commit()

    rejected = client.delete(f"/api/admin/tools/{tool_id}", headers=admin_headers)
    assert rejected.status_code == 409, rejected.text
    assert "进行中的任务" in rejected.text
    with SessionLocal() as db:
        assert db.get(ToolDefinition, tool_id).deleted_at is None

    with SessionLocal() as db:
        run = db.query(ToolRun).filter_by(client_request_id="soft-delete-busy-run").one()
        run.status = "succeeded"
        db.commit()
    allowed = client.delete(f"/api/admin/tools/{tool_id}", headers=admin_headers)
    assert allowed.status_code == 200, allowed.text
    # 历史运行记录仍指向被软删的工具定义
    with SessionLocal() as db:
        run = db.query(ToolRun).filter_by(client_request_id="soft-delete-busy-run").one()
        assert int(run.tool_definition_id) == tool_id
