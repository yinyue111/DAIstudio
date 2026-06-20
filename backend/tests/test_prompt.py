import pytest
from fastapi import HTTPException

from app.models import User
from app.routers import prompt
from app.schemas import ReverseIn
from app.services import gateway


class _Model:
    enabled = True
    model_id = "gpt-5.5"
    cost_credits = 0


def test_reverse_rejects_video_url_before_gateway(monkeypatch):
    monkeypatch.setattr(prompt, "get_model_config", lambda db, use: _Model())
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
