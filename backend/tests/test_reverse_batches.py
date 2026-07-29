from __future__ import annotations

from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import (
    CreditTransaction,
    ReverseOperation,
    ReverseOperationBatch,
    ReverseOperationBatchItem,
    User,
)
from app.routers import prompt
from app.services import reverse_operations
from tests.reverse_helpers import post_reverse_batch, post_reverse_retry


def _stub_batch_dependencies(monkeypatch):
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reverse_operations, "assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reverse_operations,
        "enqueue_operation",
        lambda operation_id: f"queued-{operation_id}",
    )


def _batch_body(client_request_id="reverse-batch-request-001", item_count=2):
    return {
        "client_request_id": client_request_id,
        "name": "商品图批量拆解",
        "target": "image",
        "analysis_focus": "product_ad",
        "analysis_precision": "standard",
        "output_purpose": "generation",
        "custom_instruction": "保留包装文字和品牌色",
        "include_audio": False,
        "items": [
            {
                "asset_url": f"https://cdn.example.com/product-{index}.jpg",
                "source_type": "image",
                "workspace_snapshot_v3": {
                    "version": 3,
                    "creation_mode": "image",
                    "selected": {
                        "type": "image",
                        "url": f"https://cdn.example.com/product-{index}.jpg",
                    },
                },
            }
            for index in range(item_count)
        ],
    }


def test_reverse_batch_create_replay_list_and_persisted_items(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13720000001", balance=100)
    headers = auth("13720000001")
    _stub_batch_dependencies(monkeypatch)
    body = _batch_body()

    created = post_reverse_batch(client, body, headers=headers)
    assert created.status_code == 202, created.text
    payload = created.json()
    assert payload["name"] == "商品图批量拆解"
    assert payload["target"] == "image"
    assert payload["status"] == "queued"
    assert payload["status_counts"] == {
        "queued": 2,
        "running": 0,
        "needs_confirmation": 0,
        "succeeded": 0,
        "failed": 0,
        "canceled": 0,
    }
    assert payload["shared_config_snapshot"]["analysis_focus"] == "product_ad"
    assert payload["capabilities"]["item_overrides"] is True
    assert payload["capabilities"]["audio_policies"] == ["inherit", "exclude", "analyze"]
    assert [item["index"] for item in payload["items"]] == [0, 1]
    assert all(item["operation"]["status"] == "queued" for item in payload["items"])
    assert all(item["operation"]["target"] == "image" for item in payload["items"])

    replay = post_reverse_batch(client, body, headers=headers)
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == payload["id"]
    assert [item["operation_id"] for item in replay.json()["items"]] == [
        item["operation_id"] for item in payload["items"]
    ]

    listed = client.get("/api/prompt/reverse-batches", headers=headers)
    assert listed.status_code == 200
    assert [batch["id"] for batch in listed.json()] == [payload["id"]]
    assert listed.json()[0]["items"] == []

    db = SessionLocal()
    try:
        assert db.query(ReverseOperationBatch).filter_by(user_id=user_id).count() == 1
        assert db.query(ReverseOperationBatchItem).filter_by(batch_id=payload["id"]).count() == 2
        assert db.query(ReverseOperation).filter_by(user_id=user_id).count() == 2
        assert db.query(CreditTransaction).filter_by(
            user_id=user_id,
            biz_type="reverse_operation",
            type="freeze",
        ).count() == 2
        user = db.get(User, user_id)
        assert user.balance_credits == 90
        assert user.frozen_credits == 10
    finally:
        db.close()


def test_serialize_batch_exposes_batch_level_costs_without_items(
    client, make_user, auth, monkeypatch
):
    """批次列表 (include_items=false) 无子项可求和，批级 cost_frozen /
    cost_settled 必须直接下发，否则前端总费用显示 0。"""
    make_user("13720000041", balance=100)
    headers = auth("13720000041")
    _stub_batch_dependencies(monkeypatch)

    created = post_reverse_batch(
        client, _batch_body("reverse-batch-costs-001"), headers=headers
    )
    assert created.status_code == 202, created.text
    batch_id = created.json()["id"]

    db = SessionLocal()
    try:
        operations = [
            operation
            for _, operation in reverse_operations._batch_item_rows(db, batch_id)
        ]
        assert len(operations) == 2
        # 模拟一项已结算、一项仍冻结
        operations[0].cost_frozen = 0
        operations[0].cost_settled = 7
        operations[1].cost_frozen = 5
        operations[1].cost_settled = 0
        db.commit()

        batch = db.get(ReverseOperationBatch, batch_id)
        summary = reverse_operations.serialize_batch(db, batch, include_items=False)
        assert summary["items"] == []
        assert summary["cost_frozen"] == 5
        assert summary["cost_settled"] == 7

        detail = reverse_operations.serialize_batch(db, batch, include_items=True)
        assert detail["cost_frozen"] == 5
        assert detail["cost_settled"] == 7
        assert [item["operation"]["cost_settled"] for item in detail["items"]] == [7, 0]
    finally:
        db.close()

    # 端到端：字段必须穿过 response_model=ReverseBatchOut 到达 HTTP 响应，
    # 而不是被 pydantic 静默剥离（ReverseBatchOut 缺字段时即会发生）。
    listed = client.get("/api/prompt/reverse-batches", headers=headers)
    assert listed.status_code == 200, listed.text
    summary_payload = next(
        batch for batch in listed.json() if batch["id"] == batch_id
    )
    assert summary_payload["cost_frozen"] == 5
    assert summary_payload["cost_settled"] == 7

    fetched = client.get(f"/api/prompt/reverse-batches/{batch_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    detail_payload = fetched.json()
    assert detail_payload["cost_frozen"] == 5
    assert detail_payload["cost_settled"] == 7


def test_reverse_batch_conflict_limit_and_atomic_credit_rollback(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13720000002", balance=5)
    headers = auth("13720000002")
    _stub_batch_dependencies(monkeypatch)
    body = _batch_body("reverse-batch-atomic-001")

    insufficient = post_reverse_batch(client, body, headers=headers)
    assert insufficient.status_code == 400, insufficient.text
    db = SessionLocal()
    try:
        assert db.query(ReverseOperationBatch).filter_by(user_id=user_id).count() == 0
        assert db.query(ReverseOperation).filter_by(user_id=user_id).count() == 0
        user = db.get(User, user_id)
        assert user.balance_credits == 5
        assert user.frozen_credits == 0
    finally:
        db.close()

    make_user("13720000002", balance=100)
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        user.balance_credits = 100
        db.commit()
    finally:
        db.close()
    created = post_reverse_batch(client, body, headers=headers)
    assert created.status_code == 202, created.text
    changed = _batch_body("reverse-batch-atomic-001")
    changed["items"][0]["asset_url"] = "https://cdn.example.com/changed.jpg"
    conflict = post_reverse_batch(client, changed, headers=headers)
    assert conflict.status_code == 409

    too_many = post_reverse_batch(
        client,
        _batch_body("reverse-batch-too-many-001", item_count=21),
        headers=headers,
    )
    assert too_many.status_code == 422


def test_reverse_batch_owner_isolation_and_cancel_only_active_items(
    client, make_user, auth, monkeypatch
):
    owner_id = make_user("13720000003", balance=100)
    make_user("13720000004", balance=100)
    owner_headers = auth("13720000003")
    other_headers = auth("13720000004")
    _stub_batch_dependencies(monkeypatch)
    created = post_reverse_batch(
        client,
        _batch_body("reverse-batch-cancel-001", item_count=3),
        headers=owner_headers,
    ).json()
    operation_ids = [item["operation_id"] for item in created["items"]]

    assert client.get("/api/prompt/reverse-batches", headers=other_headers).json() == []
    assert client.get(
        f"/api/prompt/reverse-batches/{created['id']}", headers=other_headers
    ).status_code == 404
    assert client.post(
        f"/api/prompt/reverse-batches/{created['id']}/cancel", headers=other_headers
    ).status_code == 404

    db = SessionLocal()
    try:
        succeeded = db.get(ReverseOperation, operation_ids[0])
        succeeded.status = "running"
        succeeded.phase = "calling_model"
        succeeded.started_at = datetime.now(timezone.utc)
        running = db.get(ReverseOperation, operation_ids[2])
        running.status = "running"
        running.phase = "calling_model"
        running.started_at = datetime.now(timezone.utc)
        db.commit()
        assert reverse_operations._finish_success(
            db,
            operation_ids[0],
            result={"structured": {"主体": "商品"}, "final_text": "商品棚拍"},
            reference_count=1,
            real_cost=5,
        ) is not None
    finally:
        db.close()

    canceled = client.post(
        f"/api/prompt/reverse-batches/{created['id']}/cancel",
        headers=owner_headers,
    )
    assert canceled.status_code == 200, canceled.text
    payload = canceled.json()
    assert payload["cancel_requested"] is True
    assert payload["status_counts"]["succeeded"] == 1
    assert payload["status_counts"]["canceled"] == 1
    assert payload["status_counts"]["running"] == 1
    by_id = {item["operation_id"]: item["operation"] for item in payload["items"]}
    assert by_id[operation_ids[0]]["status"] == "succeeded"
    assert by_id[operation_ids[1]]["status"] == "canceled"
    assert by_id[operation_ids[2]]["status"] == "running"
    assert by_id[operation_ids[2]]["cancel_requested"] is True

    assert reverse_operations.fail_operation(
        operation_ids[2], code="GATEWAY_ERROR", error="upstream failed"
    ) is True
    settled = client.get(
        f"/api/prompt/reverse-batches/{created['id']}", headers=owner_headers
    ).json()
    assert settled["status"] == "partial"
    assert settled["status_counts"]["succeeded"] == 1
    assert settled["status_counts"]["canceled"] == 2
    db = SessionLocal()
    try:
        assert db.get(User, owner_id).frozen_credits == 0
    finally:
        db.close()


def test_reverse_batch_item_retry_relinks_current_operation(
    client, make_user, auth, monkeypatch
):
    make_user("13720000005", balance=100)
    headers = auth("13720000005")
    _stub_batch_dependencies(monkeypatch)
    created = post_reverse_batch(
        client,
        _batch_body("reverse-batch-retry-001", item_count=1),
        headers=headers,
    ).json()
    original_operation_id = created["items"][0]["operation_id"]
    assert reverse_operations.fail_operation(
        original_operation_id,
        code="GATEWAY_ERROR",
        error="temporary failure",
    ) is True

    failed = client.get(
        f"/api/prompt/reverse-batches/{created['id']}", headers=headers
    ).json()
    assert failed["status"] == "failed"
    retried = post_reverse_retry(
        client,
        original_operation_id,
        {"client_request_id": "reverse-batch-item-retry-001"},
        headers=headers,
    )
    assert retried.status_code == 202, retried.text
    new_operation_id = retried.json()["id"]
    assert new_operation_id != original_operation_id

    batch = client.get(
        f"/api/prompt/reverse-batches/{created['id']}", headers=headers
    ).json()
    assert batch["status"] == "queued"
    assert batch["items"][0]["operation_id"] == new_operation_id
    assert batch["items"][0]["operation"]["retry_of_operation_id"] == original_operation_id
    db = SessionLocal()
    try:
        assert db.query(ReverseOperationBatchItem).filter_by(batch_id=created["id"]).count() == 1
        assert db.query(ReverseOperation).filter(
            ReverseOperation.id.in_([original_operation_id, new_operation_id])
        ).count() == 2
    finally:
        db.close()


def test_batch_item_overrides_are_isolated_from_immutable_shared_defaults():
    body = reverse_operations.ReverseBatchCreate.model_validate({
        **_batch_body("reverse-batch-overrides-001", item_count=2),
        "target": "video",
        "include_audio": True,
        "items": [
            {
                "asset_url": "https://cdn.example.com/a.mp4",
                "source_type": "video",
                "target": "video",
                "analysis_precision": "fine",
                "source_ranges": [{"start_seconds": 10, "end_seconds": 15}],
                "custom_keyframes": [11, 14],
                "audio_policy": "exclude",
            },
            {
                "asset_url": "https://cdn.example.com/b.mp4",
                "source_type": "video",
                "target": "video",
                "audio_policy": "inherit",
            },
        ],
    })
    operations = reverse_operations.batch_operation_bodies(body)

    assert operations[0].analysis_precision == "fine"
    assert operations[0].include_audio is False
    assert operations[0].source_ranges[0].start_seconds == 10
    assert operations[0].custom_keyframes == [11, 14]
    assert operations[1].analysis_precision == "standard"
    assert operations[1].include_audio is True
    assert body.analysis_precision == "standard"
    assert body.include_audio is True


def test_batch_item_sources_preserve_roles_asset_ids_and_fingerprint():
    request = {
        **_batch_body("reverse-batch-sources-001", item_count=1),
        "items": [{
            "asset_url": "https://cdn.example.com/primary.jpg",
            "source_type": "image",
            "sources": [
                {
                    "asset_url": "https://cdn.example.com/primary.jpg",
                    "source_type": "image",
                    "role": "primary",
                    "asset_id": 8101,
                },
                {
                    "asset_url": "https://cdn.example.com/product.jpg",
                    "source_type": "image",
                    "role": "product",
                    "asset_id": 8102,
                },
            ],
        }],
    }
    body = reverse_operations.ReverseBatchCreate.model_validate(request)
    operation = reverse_operations.batch_operation_bodies(body)[0]

    assert [source.role for source in operation.sources] == ["primary", "product"]
    assert [source.asset_id for source in operation.sources] == [8101, 8102]
    assert reverse_operations.batch_request_snapshot(body)["items"][0]["sources"][1] == {
        "asset_url": "https://cdn.example.com/product.jpg",
        "source_type": "image",
        "role": "product",
        "asset_id": 8102,
    }

    altered = reverse_operations.ReverseBatchCreate.model_validate({
        **request,
        "items": [{
            **request["items"][0],
            "sources": [
                {**request["items"][0]["sources"][0], "asset_id": 8199},
                request["items"][0]["sources"][1],
            ],
        }],
    })
    assert reverse_operations.batch_request_fingerprint(body) != (
        reverse_operations.batch_request_fingerprint(altered)
    )


def test_batch_item_rejects_mismatched_primary_and_video_auxiliary_sources():
    base = _batch_body("reverse-batch-invalid-sources-001", item_count=1)
    try:
        reverse_operations.ReverseBatchCreate.model_validate({
            **base,
            "items": [{
                "asset_url": "https://cdn.example.com/primary.jpg",
                "source_type": "image",
                "sources": [{
                    "asset_url": "https://cdn.example.com/other.jpg",
                    "source_type": "image",
                    "role": "primary",
                }],
            }],
        })
    except ValueError as exc:
        assert "主参考地址" in str(exc)
    else:
        raise AssertionError("主参考与 asset_url 不一致时应拒绝请求")

    try:
        reverse_operations.ReverseBatchCreate.model_validate({
            **base,
            "items": [{
                "asset_url": "https://cdn.example.com/primary.mp4",
                "source_type": "video",
                "target": "video",
                "sources": [
                    {
                        "asset_url": "https://cdn.example.com/primary.mp4",
                        "source_type": "video",
                        "role": "primary",
                    },
                    {
                        "asset_url": "https://cdn.example.com/aux.mp4",
                        "source_type": "video",
                        "role": "style",
                    },
                ],
            }],
        })
    except ValueError as exc:
        assert "辅助参考" in str(exc)
    else:
        raise AssertionError("辅助参考为视频时应拒绝请求")
