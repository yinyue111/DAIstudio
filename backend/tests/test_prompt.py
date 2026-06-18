import pytest
from fastapi import HTTPException

from app.models import User
from app.routers import prompt
from app.schemas import ReverseIn
from app.services import gateway


class _Model:
    enabled = True
    model_id = "gpt-5.5"


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
