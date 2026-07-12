from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.db import SessionLocal
from app.models import CreditTransaction, ModelConfig, ReverseOperation, User, UserPrompt
from app.routers import prompt
from app.schemas import ReverseIn
from app.services import config_store, gateway, gateway_prompting, retention


class _Model:
    enabled = True
    model_id = "gpt-5.5"
    cost_credits = 0


def test_reverse_image_template_captures_commercial_material_dimensions():
    template = gateway_prompting.reverse_template("image")

    assert "社媒商业素材复刻" in template
    assert '"图像类型"' in template
    assert '"反推重点"' in template
    assert "产品图" in template
    assert "人物图" in template
    assert "人物+产品混合图" in template
    assert "按图像类型自动取舍" in template
    assert '"商品服装"' in template
    assert '"人像意图"' in template
    assert '"人物比例"' in template
    assert '"身材体态"' in template
    assert '"体态线条"' in template
    assert '"服装结构"' in template
    assert '"服装覆盖"' in template
    assert '"妆发五官"' in template
    assert '"广告目标"' in template
    assert '"文字版式"' in template
    assert '"一致性约束"' in template
    assert "头身比" in template
    assert "服装覆盖范围" in template
    assert "商业人像" in template
    assert "品牌 Lookbook" in template
    assert "不得写三围尺寸" in template
    assert "解剖比例与镜头透视畸变分开记录" in template
    assert "不得默认写成纤细修长、7.5 头身或长颈" in template
    assert "机位高度、主体距离、仰角" in template
    assert "近镜前景放大" in template
    assert "人物自身左/右" in template
    assert "画面左/右" in template
    assert "不得臆造婚纱蕾丝" in template
    assert "不得臆造浅色隐形眼镜" in template
    assert (
        "人物图 final_text 的优先顺序必须是姿态重心/整体轮廓/视角镜头 "
        "→ 妆发五官/服装结构 → 光线/材质纹理/后期质感"
    ) in template
    assert "不要写成普通美图描述" in template
    assert '"身材曲线"' not in template
    assert '"尺码三围"' not in template
    assert '"露肤度"' not in template


def test_reverse_product_profile_template_extracts_subject_identity():
    template = gateway_prompting.reverse_template("product_profile")

    assert "产品身份档案" in template
    assert '"品牌Logo"' in template
    assert '"包装文字"' in template
    assert '"不可改项"' in template
    assert "上传产品是唯一商品主角" in template
    assert "替换参考素材原主体" in template


def test_reverse_rejects_video_url_before_gateway(monkeypatch):
    monkeypatch.setattr(prompt, "get_model_config", lambda db, use: _Model())
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(gateway.GatewayError("called gateway")),
    )

    with pytest.raises(HTTPException) as exc:
        prompt.reverse(
            ReverseIn(asset_url="https://sns-video-zl.xhscdn.com/stream/test.mp4?sign=abc"),
            db=object(),
            user=User(id=1, phone="13800000000", status="active"),
        )

    assert exc.value.status_code == 400
    assert "反推只支持图片素材" in exc.value.detail


def test_reverse_source_type_video_uses_video_template_without_suffix(client, make_user, monkeypatch):
    seen = {}
    uid = make_user("13800000011", balance=100)

    monkeypatch.setattr(prompt, "get_model_config", lambda db, use: _Model())
    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_gateway_ref", lambda db, user, url: "data:image/png;base64,abc")
    monkeypatch.setattr(prompt.credits, "consume", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)

    def fake_reverse(refs, model_id, target="image"):
        seen.update({"refs": refs, "model_id": model_id, "target": target})
        return {"structured": {"主体": "x"}, "final_text": "x"}

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(
                asset_url="https://cdn.example.com/video-stream?id=1",
                target="video",
                source_type="video",
                fallback_image="https://cdn.example.com/cover.png",
            ),
            db=db,
            user=db.get(User, uid),
        )
    finally:
        db.close()

    assert out.final_text == "x"
    assert seen["target"] == "video"
    assert seen["refs"] == ["data:image/png;base64,abc"]


def test_reverse_product_profile_uses_single_image_ref(client, make_user, monkeypatch):
    seen = {}
    uid = make_user("13800000012", balance=100)

    monkeypatch.setattr(prompt, "get_model_config", lambda db, use: _Model())
    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_gateway_ref", lambda db, user, url: "data:image/png;base64,product")
    monkeypatch.setattr(prompt.credits, "consume", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "remember_prompt", lambda *_args, **_kwargs: None)

    def fake_reverse(refs, model_id, target="image"):
        seen.update({"refs": refs, "model_id": model_id, "target": target})
        return {
            "structured": {"产品品类": "棉柔巾", "包装文字": "黑魔法"},
            "final_text": "上传产品是唯一商品主角: 黑魔法棉柔巾包装文字完整保留",
        }

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(
                asset_url="https://cdn.example.com/product.jpg",
                target="product_profile",
                source_type="image",
            ),
            db=db,
            user=db.get(User, uid),
        )
    finally:
        db.close()

    assert out.charged_credits == 2
    assert out.reference_count == 1
    assert out.final_text.startswith("上传产品是唯一商品主角")
    assert seen["target"] == "product_profile"
    assert seen["refs"] == ["data:image/png;base64,product"]


def test_reverse_video_usage_cost_is_not_multiplied_by_frame_count(client, make_user, monkeypatch):
    uid = make_user("13800000002", balance=100)
    db = SessionLocal()
    try:
        model = db.query(ModelConfig).filter(ModelConfig.use == "vision").one()
        model.model_id = "gpt-5.5"
        model.cost_credits = 1
        model.enabled = True
        model.extra = {
            "official_pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "total_per_1m": 100_000,
                "credit_value_cny": 1,
            }
        }
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_collect_refs", lambda *_args, **_kwargs: [
        "data:image/jpeg;base64,a",
        "data:image/jpeg;base64,b",
        "data:image/jpeg;base64,c",
        "data:image/jpeg;base64,d",
    ])
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(
                asset_url="https://cdn.example.com/video.mp4",
                target="video",
                source_type="video",
            ),
            db=db,
            user=db.get(User, uid),
        )
        user = db.get(User, uid)
        assert out.reference_count == 4
        assert out.charged_credits == 18
        assert user.balance_credits == 82
    finally:
        db.close()


def test_reverse_blocks_unsafe_gateway_output_before_history(client, make_user, monkeypatch):
    uid = make_user("13800000004", balance=100)
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_collect_refs", lambda *_args, **_kwargs: ["data:image/jpeg;base64,a"])
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "forbidden-output product"},
            "final_text": "forbidden-output product poster",
            "usage": {"total_tokens": 10},
        },
    )

    db = SessionLocal()
    try:
        config_store.set_settings(
            db,
            {
                "reverse_prompt_enabled": True,
                "content_safety_enabled": True,
                "content_safety_banned_terms": "forbidden-output",
            },
        )

        with pytest.raises(HTTPException) as exc:
            prompt.reverse(
                ReverseIn(asset_url="https://cdn.example.com/a.jpg", target="image"),
                db=db,
                user=db.get(User, uid),
            )

        assert exc.value.status_code == 400
        user = db.get(User, uid)
        assert user.balance_credits == 100
        assert (
            db.query(UserPrompt)
            .filter(UserPrompt.user_id == uid, UserPrompt.source == "reverse")
            .count()
            == 0
        )
    finally:
        config_store.set_settings(
            db,
            {"content_safety_enabled": False, "content_safety_banned_terms": ""},
        )
        db.close()


def test_reverse_usage_metadata_does_not_override_fixed_image_price(client, make_user, monkeypatch):
    uid = make_user("13800000003", balance=2)
    db = SessionLocal()
    try:
        model = db.query(ModelConfig).filter(ModelConfig.use == "vision").one()
        model.model_id = "gpt-5.5"
        model.cost_credits = 1
        model.enabled = True
        model.extra = {
            "official_pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "total_per_1m": 1_000_000,
                "credit_value_cny": 1,
            }
        }
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_collect_refs", lambda *_args, **_kwargs: ["data:image/jpeg;base64,a"])
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )

    db = SessionLocal()
    try:
        out = prompt.reverse(
            ReverseIn(asset_url="https://cdn.example.com/a.jpg", target="image"),
            db=db,
            user=db.get(User, uid),
        )
        user = db.get(User, uid)
        assert out.charged_credits == 2
        assert user.balance_credits == 0
    finally:
        db.close()


def test_reverse_client_request_id_replays_without_double_charge(client, make_user, auth, monkeypatch):
    uid = make_user("13800000005", balance=100)
    headers = auth("13800000005")
    calls = {"gateway": 0, "rate": 0}

    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_collect_refs", lambda *_args, **_kwargs: ["data:image/jpeg;base64,a"])

    def fake_rate(*_args, **_kwargs):
        calls["rate"] += 1
        return calls["rate"]

    def fake_reverse(*_args, **_kwargs):
        calls["gateway"] += 1
        return {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(prompt, "incr_window", fake_rate)
    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)
    body = {
        "client_request_id": "reverse-retry-001",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }

    first = client.post("/api/prompt/reverse", json=body, headers=headers)
    assert first.status_code == 200, first.text
    second = client.post("/api/prompt/reverse", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert calls == {"gateway": 1, "rate": 1}

    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 98
        assert db.query(ReverseOperation).filter_by(client_request_id="reverse-retry-001").count() == 1
    finally:
        db.close()


def test_reverse_without_client_request_id_persists_operation_and_uses_it_for_billing(
    client,
    make_user,
    auth,
    monkeypatch,
):
    uid = make_user("13800000008", balance=100)
    headers = auth("13800000008")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        prompt,
        "_collect_refs",
        lambda *_args, **_kwargs: ["data:image/jpeg;base64,a"],
    )
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)

    response = client.post(
        "/api/prompt/reverse",
        json={"asset_url": "https://cdn.example.com/no-id.jpg", "target": "image"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    db = SessionLocal()
    try:
        operation = db.query(ReverseOperation).filter_by(user_id=uid).one()
        assert operation.client_request_id is None
        assert operation.status == "succeeded"
        assert operation.charged_credits == 2
        charge = db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse",
            type="consume",
        ).one()
        assert charge.biz_ref == operation.id
        assert db.get(User, uid).balance_credits == 98
    finally:
        db.close()


def test_reverse_charge_rejects_terminal_operation(client, make_user):
    uid = make_user("13800000013", balance=100)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id=None,
            request_fingerprint="e" * 64,
            target="image",
            asset_url="https://cdn.example.com/terminal.jpg",
            status="failed",
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id

        with pytest.raises(prompt.ReverseOperationClosed):
            prompt._charge_reverse_operation(
                db,
                operation_id,
                2,
                note="must not charge terminal operation",
            )

        db.expire_all()
        assert db.get(ReverseOperation, operation_id).charged_credits == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse",
            biz_ref=operation_id,
        ).count() == 0
    finally:
        db.close()


def test_reverse_client_request_id_rejects_different_payload(client, make_user, auth, monkeypatch):
    make_user("13800000006", balance=100)
    headers = auth("13800000006")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_collect_refs", lambda *_args, **_kwargs: ["data:image/jpeg;base64,a"])
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        },
    )
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)

    body = {
        "client_request_id": "reverse-retry-002",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }
    assert client.post("/api/prompt/reverse", json=body, headers=headers).status_code == 200
    conflict = client.post(
        "/api/prompt/reverse",
        json={**body, "asset_url": "https://cdn.example.com/b.jpg"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert "client_request_id 已用于不同反推请求" in conflict.text


def test_reverse_http_exception_refunds_and_marks_operation_failed(client, make_user, auth, monkeypatch):
    uid = make_user("13800000007", balance=100)
    headers = auth("13800000007")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_collect_refs", lambda *_args, **_kwargs: ["data:image/jpeg;base64,a"])
    monkeypatch.setattr(
        gateway,
        "reverse_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(502, "provider rejected")),
    )
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)

    body = {
        "client_request_id": "reverse-retry-003",
        "asset_url": "https://cdn.example.com/a.jpg",
        "target": "image",
    }
    failed = client.post("/api/prompt/reverse", json=body, headers=headers)
    assert failed.status_code == 502

    db = SessionLocal()
    try:
        assert db.get(User, uid).balance_credits == 100
        op = db.query(ReverseOperation).filter_by(client_request_id="reverse-retry-003").one()
        assert op.status == "failed"
        assert op.charged_credits == 0
        assert "provider rejected" in (op.error or "")
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse",
            biz_ref=op.id,
            type="refund",
        ).count() == 1
    finally:
        db.close()


def test_reverse_failure_refund_error_rolls_back_operation_state(client, make_user, monkeypatch):
    uid = make_user("13800000014", balance=100)
    db = SessionLocal()
    try:
        operation = ReverseOperation(
            user_id=uid,
            client_request_id=None,
            request_fingerprint="f" * 64,
            target="image",
            asset_url="https://cdn.example.com/refund-error.jpg",
            status="running",
            charged_credits=2,
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id
        prompt.credits.consume(
            db,
            uid,
            2,
            biz_type="reverse",
            biz_ref=operation_id,
        )

        def fail_refund(*_args, **_kwargs):
            raise RuntimeError("refund unavailable")

        monkeypatch.setattr(prompt.credits, "refund_consumed", fail_refund)

        assert prompt._fail_reverse_operation(
            db,
            operation_id,
            "gateway failed",
            refund_note="failed reverse refund",
        ) is False

        db.expire_all()
        unchanged = db.get(ReverseOperation, operation_id)
        assert unchanged.status == "running"
        assert unchanged.charged_credits == 2
        assert db.get(User, uid).balance_credits == 98
        assert db.query(CreditTransaction).filter_by(
            user_id=uid,
            biz_type="reverse",
            biz_ref=operation_id,
            type="refund",
        ).count() == 0
    finally:
        db.close()


def test_reverse_completion_cannot_overwrite_stale_refund(client, make_user, auth, monkeypatch):
    uid = make_user("13800000009", balance=100)
    headers = auth("13800000009")
    monkeypatch.setattr(prompt, "assert_safe_user_asset_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_collect_refs", lambda *_args, **_kwargs: ["data:image/jpeg;base64,a"])
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)

    def reverse_after_reaper(*_args, **_kwargs):
        reaper_db = SessionLocal()
        try:
            operation = reaper_db.query(ReverseOperation).filter_by(
                client_request_id="reverse-race-refund"
            ).one()
            operation.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
            reaper_db.commit()
            assert retention.reap_stuck_reverse_operations(reaper_db, max_minutes=15) == 1
        finally:
            reaper_db.close()
        return {
            "structured": {"主体": "x"},
            "final_text": "x",
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(gateway, "reverse_prompt", reverse_after_reaper)

    response = client.post(
        "/api/prompt/reverse",
        json={
            "client_request_id": "reverse-race-refund",
            "asset_url": "https://cdn.example.com/race-refund.jpg",
            "target": "image",
        },
        headers=headers,
    )

    assert response.status_code == 409, response.text
    db = SessionLocal()
    try:
        operation = db.query(ReverseOperation).filter_by(
            client_request_id="reverse-race-refund"
        ).one()
        assert operation.status == "failed"
        assert operation.charged_credits == 0
        assert db.get(User, uid).balance_credits == 100
        assert db.query(UserPrompt).filter_by(user_id=uid, source="reverse").count() == 0
    finally:
        db.close()
