"""配方使用埋点的聚合展示：_serialize 补 usage 字段且列表页无 N+1。"""

from sqlalchemy import event

from app.db import SessionLocal, engine
from app.models import CreationRecipe
from app.routers.recipes import _serialize, _usage_stats_map


def _payload(prompt: str) -> dict:
    return {
        "schema_version": "creation-recipe.v1",
        "analysis_focus": "product_ad",
        "prompt": prompt,
        "structured": {"主体": "银色香水瓶"},
        "generation_params": {"ratio": "3:4", "quality": "2k"},
    }


def _create_recipe(client, headers, *, title: str) -> dict:
    response = client.post(
        "/api/recipes",
        json={
            "title": title,
            "category": "image",
            "visibility": "private",
            "payload": _payload(f"{title} 提示词"),
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _record_usage(client, headers, recipe_id: int, event_type: str, event_id: str):
    response = client.post(
        f"/api/recipes/{recipe_id}/usage",
        json={"event_type": event_type, "client_event_id": event_id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_serialize_includes_usage_stats(client, make_user, auth):
    make_user("13975200001")
    headers = auth("13975200001")
    hot = _create_recipe(client, headers, title="usage-热门配方")
    cold = _create_recipe(client, headers, title="usage-冷门配方")

    _record_usage(client, headers, hot["id"], "apply", "usage-evt-apply-1")
    _record_usage(client, headers, hot["id"], "apply", "usage-evt-apply-2")
    _record_usage(client, headers, hot["id"], "generation_prepare", "usage-evt-prepare-1")

    with SessionLocal() as db:
        hot_row = db.get(CreationRecipe, hot["id"])
        cold_row = db.get(CreationRecipe, cold["id"])

        data = _serialize(db, hot_row)
        assert data["usage"]["total"] == 3
        assert data["usage"]["unique_users"] == 1
        assert data["usage"]["by_event"] == {"apply": 2, "generation_prepare": 1}
        assert data["usage"]["last_used_at"] is not None

        # 公开序列化同样携带热度聚合（仅聚合数字，不含使用者身份）
        public = _serialize(db, hot_row, public=True)
        assert public["usage"]["total"] == 3
        assert "user_id" not in public["usage"]

        empty = _serialize(db, cold_row)
        assert empty["usage"] == {
            "total": 0,
            "unique_users": 0,
            "by_event": {},
            "last_used_at": None,
        }

    # 列表 / 详情端点在补充 usage 字段后仍正常返回
    listed = client.get("/api/recipes", headers=headers)
    assert listed.status_code == 200, listed.text
    detail = client.get(f"/api/recipes/{hot['id']}", headers=headers)
    assert detail.status_code == 200, detail.text


def test_usage_stats_map_batches_queries(client, make_user, auth):
    make_user("13975200002")
    headers = auth("13975200002")
    ids = [
        _create_recipe(client, headers, title=f"usage-批量-{index}")["id"]
        for index in range(3)
    ]
    _record_usage(client, headers, ids[0], "apply", "usage-batch-evt-1")
    _record_usage(client, headers, ids[2], "generation_prepare", "usage-batch-evt-2")

    statements = []

    def _capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    with SessionLocal() as db:
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            stats = _usage_stats_map(db, ids)
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

    # 整页配方只发两条分组聚合查询，而不是每行一查
    assert len(statements) == 2, statements
    assert stats[ids[0]]["total"] == 1
    assert stats[ids[0]]["by_event"] == {"apply": 1}
    assert stats[ids[1]]["total"] == 0
    assert stats[ids[2]]["by_event"] == {"generation_prepare": 1}
    assert _usage_stats_map(SessionLocal(), []) == {}
