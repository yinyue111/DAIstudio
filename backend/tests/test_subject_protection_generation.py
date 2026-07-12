import io

from PIL import Image, ImageDraw

from app.db import SessionLocal
from app.models import GenTask, User
from app.services import gateway
from app.services.generation_media import EditMaskResult, SubjectProtectionBusy


def _product_png_bytes() -> bytes:
    image = Image.new("RGB", (320, 240), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((80, 45, 250, 205), radius=12, fill=(235, 235, 232))
    draw.rectangle((105, 120, 225, 170), fill=(30, 30, 30))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _submit_product_edit(client, auth_headers, asset_url):
    return client.post(
        "/api/generate",
        json={
            "source_asset_url": asset_url,
            "source_type": "image",
            "source_asset_meta": {
                "selected_type": "image",
                "mode": "image_edit",
                "product_generation_mode": True,
            },
            "category": "image",
            "stage": "preview",
            "prompt": {
                "final_text": "replace the background and keep the product",
                "instruction": "replace the background and keep the product",
            },
            "params": {
                "n": 1,
                "size": "256x256",
                "edit_mask_mode": "protect_subject",
            },
        },
        headers=auth_headers,
    )


def test_generation_retries_a_busy_product_mask_before_sending(client, make_user, auth, monkeypatch):
    uid = make_user("13900002994", balance=1000)
    headers = auth("13900002994")
    upload = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _product_png_bytes(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text

    calls = 0
    seen = {}

    def flaky_mask(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SubjectProtectionBusy("主体保护处理繁忙,请稍后再试")
        return EditMaskResult(
            data_uri="data:image/png;base64,bWFzaw==",
            mode="auto_subject",
            confidence=0.88,
            bbox=(80, 45, 250, 205),
            width=320,
            height=240,
            reason="test_retry",
        )

    def fake_gen_image(*_args, **kwargs):
        seen["extra_payload"] = kwargs.get("extra_payload") or {}
        return [gateway._mock_image("subject protection retry", "256x256", 0)]

    monkeypatch.setattr("app.services.generation_image_flow.gateway_image_edit_mask", flaky_mask)
    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    response = _submit_product_edit(client, headers, upload.json()["url"])
    assert response.status_code == 200, response.text
    assert calls == 2
    assert seen["extra_payload"]["mask"].startswith("data:image/png;base64,")

    with SessionLocal() as db:
        task = (
            db.query(GenTask)
            .filter(GenTask.user_id == uid)
            .order_by(GenTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.params["_edit_mask_sent"] is True
        assert task.params["_edit_mask_source"] == "test_retry"


def test_generation_stops_instead_of_silently_dropping_product_mask(client, make_user, auth, monkeypatch):
    uid = make_user("13900002995", balance=1000)
    headers = auth("13900002995")
    upload = client.post(
        "/api/uploads/image",
        files={"file": ("product.png", _product_png_bytes(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    generated = False
    mask_calls = 0

    def failed_mask(*_args, **_kwargs):
        nonlocal mask_calls
        mask_calls += 1
        raise RuntimeError("mask decoder failed")

    def fake_gen_image(*_args, **kwargs):
        nonlocal generated
        generated = True
        return [gateway._mock_image("subject protection failure", "256x256", 0)]

    monkeypatch.setattr("app.services.generation_image_flow.gateway_image_edit_mask", failed_mask)
    monkeypatch.setattr("app.services.gateway.gen_image", fake_gen_image)

    response = _submit_product_edit(client, headers, upload.json()["url"])
    assert response.status_code == 200, response.text
    assert mask_calls == 2
    assert generated is False

    with SessionLocal() as db:
        task = (
            db.query(GenTask)
            .filter(GenTask.user_id == uid)
            .order_by(GenTask.id.desc())
            .first()
        )
        assert task is not None
        assert task.params["_edit_mask_mode"] == "none"
        assert task.params["_edit_mask_sent"] is False
        assert task.params["_edit_mask_source"] == "generation_mask_error"
        assert task.status == "failed"
        assert "产品主体保护未能可靠识别" in task.error
        user = db.get(User, uid)
        assert user.balance_credits == 1000
        assert user.frozen_credits == 0
