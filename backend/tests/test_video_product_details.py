import base64
import io
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from PIL import Image

from app.db import SessionLocal
from app.models import GenAsset, GenTask, ModelConfig, UploadedAsset
from app.services import storage
from app.services.generation_request import validate_generation_params


def _png_bytes(color=(80, 120, 180)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (96, 128), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _upload_image(client, headers, name, color=(80, 120, 180)) -> str:
    response = client.post(
        "/api/uploads/image",
        files={"file": (name, _png_bytes(color), "image/png")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["url"]


def _request(theme_url: str, details, *, stage="preview", parent_task_id=None) -> dict:
    return {
        "source_asset_url": theme_url,
        "source_type": "image",
        "category": "video",
        "stage": stage,
        "parent_task_id": parent_task_id,
        "prompt": {"final_text": "保持产品身份并展示包装细节"},
        "params": {
            "duration": 5,
            "resolution": "720p",
            "ratio": "9:16",
            "subject_mode": "product",
            "product_reference_image": theme_url,
            "product_detail_images": details,
        },
    }


@pytest.fixture()
def product_detail_model(client):
    with SessionLocal() as db:
        model = db.query(ModelConfig).filter(ModelConfig.use == "video").one()
        model_id = model.id
        original_model_id = model.model_id
        original_extra = deepcopy(model.extra)
        model.model_id = "doubao-seedance-2-0-260128"
        model.extra = {
            **dict(model.extra or {}),
            "capabilities": {
                "text_to_video": True,
                "image_to_video": True,
                "reference_image": True,
                "multi_reference": True,
                "max_reference_images": 9,
                "video_to_video": True,
            },
        }
        db.commit()
    try:
        yield model_id
    finally:
        with SessionLocal() as db:
            model = db.get(ModelConfig, model_id)
            if model is not None:
                model.model_id = original_model_id
                model.extra = original_extra
                db.commit()


@pytest.mark.parametrize(
    ("details", "message"),
    [
        ("https://example.com/detail.png", "必须是图片链接数组"),
        (["https://example.com/a.png", 2], "product_detail_images\\[1\\] 非法"),
        (["https://example.com/a.png"] * 11, "最多 10 张"),
        (["https://example.com/a.png", "https://example.com/a.png"], "不能重复"),
    ],
)
def test_product_detail_contract_rejects_invalid_arrays(details, message):
    with pytest.raises(HTTPException, match=message):
        validate_generation_params(
            "video",
            {
                "product_reference_image": "https://example.com/product.png",
                "product_detail_images": details,
            },
        )


def test_product_detail_contract_requires_distinct_theme_and_preserves_order():
    with pytest.raises(HTTPException, match="必须先提供产品主题图"):
        validate_generation_params(
            "video",
            {"product_detail_images": ["https://example.com/detail.png"]},
        )
    with pytest.raises(HTTPException, match="主题图和细节图不能重复"):
        validate_generation_params(
            "video",
            {
                "product_reference_image": "https://example.com/product.png",
                "product_detail_images": ["https://example.com/product.png"],
            },
        )

    params = validate_generation_params(
        "video",
        {
            "product_reference_image": " https://example.com/product.png ",
            "product_detail_images": [
                f" https://example.com/detail-{index}.png " for index in range(10)
            ],
        },
    )
    assert params["product_detail_images"] == [
        f"https://example.com/detail-{index}.png" for index in range(10)
    ]


def test_every_product_detail_url_gets_ssrf_validation(
    client,
    make_user,
    auth,
    product_detail_model,
):
    make_user("13900003201", balance=1000)
    headers = auth("13900003201")
    theme = _upload_image(client, headers, "theme.png")

    response = client.post(
        "/api/quotes",
        json=_request(
            theme,
            [theme.replace("/api/uploads/", "/api/uploads/") + "-safe", "http://169.254.169.254/meta"],
        ),
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert "安全策略" in response.text


def test_product_detail_owner_isolation(
    client,
    make_user,
    auth,
    product_detail_model,
):
    make_user("13900003202", balance=1000)
    make_user("13900003203", balance=1000)
    owner_headers = auth("13900003202")
    other_headers = auth("13900003203")
    theme = _upload_image(client, other_headers, "theme.png")
    foreign_detail = _upload_image(client, owner_headers, "foreign-detail.png")

    response = client.post(
        "/api/quotes",
        json=_request(theme, [foreign_detail]),
        headers=other_headers,
    )

    assert response.status_code == 404, response.text
    assert "上传素材不存在" in response.text


@pytest.mark.parametrize("target", ["theme", "detail"])
def test_product_references_reject_video_mime(
    client,
    make_user,
    auth,
    product_detail_model,
    target,
):
    user_id = make_user(f"1390000321{0 if target == 'theme' else 1}", balance=1000)
    headers = auth(f"1390000321{0 if target == 'theme' else 1}")
    valid_theme = _upload_image(client, headers, "valid-theme.png")
    video_key = f"upload_video/{target}-not-image.mp4"
    with SessionLocal() as db:
        db.add(
            UploadedAsset(
                key=video_key,
                user_id=user_id,
                mime="video/mp4",
                bytes=10,
                original_filename="not-image.mp4",
            )
        )
        db.commit()
    video_url = storage.upload_api_url(video_key)
    theme = video_url if target == "theme" else valid_theme
    details = [] if target == "theme" else [video_url]

    response = client.post(
        "/api/quotes",
        json=_request(theme, details),
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert "仅支持图片素材" in response.text


@pytest.mark.parametrize("target", ["theme", "detail"])
def test_product_references_reject_expired_images(
    client,
    make_user,
    auth,
    product_detail_model,
    target,
):
    phone = f"1390000322{0 if target == 'theme' else 1}"
    user_id = make_user(phone, balance=1000)
    headers = auth(phone)
    valid_theme = _upload_image(client, headers, "valid-theme.png")
    expired_key = storage.save_bytes_named(
        _png_bytes(),
        "upload",
        f"expired-{target}.png",
    )
    with SessionLocal() as db:
        db.add(
            UploadedAsset(
                key=expired_key,
                user_id=user_id,
                mime="image/png",
                bytes=len(_png_bytes()),
                original_filename=f"expired-{target}.png",
                created_at=datetime.now(timezone.utc) - timedelta(days=365),
            )
        )
        db.commit()
    expired_url = storage.upload_api_url(expired_key)
    theme = expired_url if target == "theme" else valid_theme
    details = [] if target == "theme" else [expired_url]

    response = client.post(
        "/api/quotes",
        json=_request(theme, details),
        headers=headers,
    )

    assert response.status_code == 410, response.text
    assert "已失效" in response.text


def test_product_theme_rejects_missing_local_file(
    client,
    make_user,
    auth,
    product_detail_model,
):
    user_id = make_user("13900003230", balance=1000)
    headers = auth("13900003230")
    missing_key = "upload/missing-theme.png"
    with SessionLocal() as db:
        db.add(
            UploadedAsset(
                key=missing_key,
                user_id=user_id,
                mime="image/png",
                bytes=10,
                original_filename="missing-theme.png",
            )
        )
        db.commit()

    response = client.post(
        "/api/quotes",
        json=_request(storage.upload_api_url(missing_key), []),
        headers=headers,
    )

    assert response.status_code == 410, response.text
    assert "文件已失效" in response.text


def test_final_render_revalidates_inherited_product_details(
    client,
    make_user,
    auth,
    product_detail_model,
):
    user_id = make_user("13900003231", balance=1000)
    headers = auth("13900003231")
    with SessionLocal() as db:
        parent = GenTask(
            user_id=user_id,
            model_config_id=product_detail_model,
            source_type="image",
            category="video",
            stage="preview",
            prompt={"final_text": "preview"},
            params={
                "subject_mode": "product",
                "product_reference_image": "http://localhost:8000/api/uploads/theme.png",
                "product_detail_images": "not-an-array",
            },
            status="succeeded",
            cost_frozen=0,
            cost_settled=0,
        )
        db.add(parent)
        db.flush()
        db.add(
            GenAsset(
                task_id=parent.id,
                user_id=user_id,
                type="video",
                preview_url="http://localhost:8000/media/video_preview/parent.mp4",
                unlocked=True,
            )
        )
        db.commit()
        parent_id = parent.id

    response = client.post(
        "/api/quotes",
        json={
            "category": "video",
            "stage": "final",
            "parent_task_id": parent_id,
            "prompt": {"final_text": "client value must be ignored"},
            "params": {},
        },
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert "product_detail_images 必须是图片链接数组" in response.text


def test_worker_localizes_ordered_details_but_persists_original_urls(
    client,
    make_user,
    auth,
    monkeypatch,
    product_detail_model,
    quote_and_generate,
):
    make_user("13900003232", balance=1000)
    headers = auth("13900003232")
    theme = _upload_image(client, headers, "theme.png", (30, 60, 90))
    details = [
        _upload_image(client, headers, "detail-a.png", (200, 20, 20)),
        _upload_image(client, headers, "detail-b.png", (20, 200, 20)),
        _upload_image(client, headers, "detail-c.png", (20, 20, 200)),
        _upload_image(client, headers, "detail-d.png", (200, 200, 20)),
        _upload_image(client, headers, "detail-e.png", (200, 20, 200)),
    ]
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(params)
        return "video-product-details"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = quote_and_generate(
        _request(theme, details),
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert len(submitted["product_detail_images"]) == 5
    assert all(
        value.startswith("data:image/jpeg;base64,")
        for value in submitted["product_detail_images"]
    )
    decoded = [
        Image.open(io.BytesIO(base64.b64decode(value.split(",", 1)[1]))).getpixel((0, 0))
        for value in submitted["product_detail_images"]
    ]
    assert decoded[0][0] > decoded[0][1]
    assert decoded[1][1] > decoded[1][0]
    assert decoded[2][2] > decoded[2][0]
    assert decoded[3][0] > decoded[3][2] and decoded[3][1] > decoded[3][2]
    assert decoded[4][0] > decoded[4][1] and decoded[4][2] > decoded[4][1]

    with SessionLocal() as db:
        task = db.get(GenTask, response.json()["id"])
        assert task.params["product_reference_image"] == theme
        assert task.params["product_detail_images"] == details
        assert [item["role"] for item in task.params["_video_reference_roles"]] == [
            "product",
            "product_detail_1",
            "product_detail_2",
            "product_detail_3",
            "product_detail_4",
            "product_detail_5",
        ]
