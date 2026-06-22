import pytest
from fastapi import HTTPException

from app.db import SessionLocal
from app.models import ModelConfig, User
from app.routers import prompt
from app.schemas import ReverseIn
from app.services import gateway


class _Model:
    enabled = True
    model_id = "gpt-5.5"
    cost_credits = 0


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


def test_reverse_source_type_video_uses_video_template_without_suffix(monkeypatch):
    seen = {}

    monkeypatch.setattr(prompt, "get_model_config", lambda db, use: _Model())
    monkeypatch.setattr(prompt, "get_setting", lambda db, key, default=None: True)
    monkeypatch.setattr(prompt, "_assert_text_allowed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt, "_gateway_ref", lambda db, user, url: "data:image/png;base64,abc")
    monkeypatch.setattr(prompt.usage, "record_call", lambda *_args, **_kwargs: None)

    def fake_reverse(refs, model_id, target="image"):
        seen.update({"refs": refs, "model_id": model_id, "target": target})
        return {"structured": {"主体": "x"}, "final_text": "x"}

    monkeypatch.setattr(gateway, "reverse_prompt", fake_reverse)

    out = prompt.reverse(
        ReverseIn(
            asset_url="https://cdn.example.com/video-stream?id=1",
            target="video",
            source_type="video",
            fallback_image="https://cdn.example.com/cover.png",
        ),
        db=object(),
        user=User(id=2, phone="13800000001", status="active", balance_credits=100),
    )

    assert out.final_text == "x"
    assert seen["target"] == "video"
    assert seen["refs"] == ["data:image/png;base64,abc"]


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
        assert out.charged_credits == 1
        assert user.balance_credits == 99
    finally:
        db.close()


def test_reverse_success_keeps_precharge_when_usage_adjust_insufficient(client, make_user, monkeypatch):
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
        assert out.final_text == "x"
        assert out.charged_credits == 1
        assert user.balance_credits == 1
    finally:
        db.close()
