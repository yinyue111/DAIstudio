import io

from PIL import Image

from app.db import SessionLocal
from app.models import GenTask, ModelConfig


def _png_bytes(size=(96, 128), color=(232, 244, 238)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _configure_video_model(
    model_id: str = "grok-imagine-video-1.5",
    *,
    prompt_profile: dict | None = None,
) -> None:
    with SessionLocal() as db:
        model = db.query(ModelConfig).filter(ModelConfig.use == "video").one()
        model.model_id = model_id
        model.provider = "yinyue"
        model.enabled = True
        model.cost_credits = 1
        model.extra = {
            **dict(model.extra or {}),
            "prompt_profile": prompt_profile or {
                "max_prompt_chars": 900,
                "max_shots_by_duration": {"5": 2, "10": 3, "15": 4},
            },
        }
        db.commit()


def test_direct_video_prompt_compiles_without_inventing_a_reference(
    client, make_user, auth, monkeypatch
):
    make_user("13900003101", balance=1000)
    headers = auth("13900003101")
    _configure_video_model()
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-prompt-direct"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = client.post(
        "/api/generate",
        json={
            "category": "video",
            "stage": "final",
            "prompt": {
                "raw_text": "真实家庭浴室，成年女性从墙面包装底部抽出一张洗脸巾；切到微距展示3D如意云纹。",
                "final_text": "真实家庭浴室，成年女性从墙面包装底部抽出一张洗脸巾；切到微距展示3D如意云纹。",
            },
            "params": {"duration": 10, "resolution": "720p", "ratio": "9:16"},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert "参考图复刻" not in submitted["prompt"]
    assert "参考片复刻" not in submitted["prompt"]
    assert "从墙面包装底部抽出一张洗脸巾" in submitted["prompt"]
    assert "3D如意云纹" in submitted["prompt"]

    task = client.get(f"/api/tasks/{response.json()['id']}", headers=headers).json()
    assert task["raw_prompt_text"].startswith("真实家庭浴室")
    assert task["assembled_prompt_text"].startswith("真实家庭浴室")
    assert task["generation_prompt_text"] == submitted["prompt"]
    assert task["prompt_compiler_version"]
    assert task["sequence_required"] is False


def test_layered_video_prompt_submits_assembled_user_draft_and_keeps_history(
    client, make_user, auth, monkeypatch
):
    make_user("13900003104", balance=1000)
    headers = auth("13900003104")
    _configure_video_model()
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-prompt-layered"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = client.post(
        "/api/generate",
        json={
            "category": "video",
            "stage": "final",
            "prompt": {
                "shots": ["旧分镜：产品静置"],
                "raw_text": "原始稿：产品静置",
                "optimized_text": "优化稿：产品缓慢入镜",
                "optimizer_model_id": "gemini-3.5-flash-low",
                "assembled_text": (
                    "Shot 1：手持产品稳定入镜；"
                    "Shot 2：慢速推近 Logo 特写。"
                ),
                "final_text": (
                    "Shot 1：手持产品稳定入镜；"
                    "Shot 2：慢速推近 Logo 特写。"
                ),
            },
            "params": {"duration": 10, "resolution": "720p", "ratio": "9:16"},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert "旧分镜" not in submitted["prompt"]
    assert "手持产品稳定入镜" in submitted["prompt"]
    assert "慢速推近 Logo 特写" in submitted["prompt"]

    task = client.get(f"/api/tasks/{response.json()['id']}", headers=headers).json()
    assert task["raw_prompt_text"] == "原始稿：产品静置"
    assert task["optimized_prompt_text"] == "优化稿：产品缓慢入镜"
    assert task["assembled_prompt_text"].startswith("Shot 1：手持产品")
    assert task["generation_prompt_text"] == submitted["prompt"]
    assert task["prompt_optimizer_model_id"] == "gemini-3.5-flash-low"


def test_overloaded_video_prompt_returns_sequence_required_before_charging_or_submit(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13900003102", balance=1000)
    headers = auth("13900003102")
    _configure_video_model("doubao-seedance-2-0-mini-260615")
    called = False

    def fake_submit(*_args, **_kwargs):
        nonlocal called
        called = True
        return "should-not-submit"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = client.post(
        "/api/generate",
        json={
            "category": "video",
            "stage": "final",
            "prompt": {
                "raw_text": (
                    "女主伸懒腰；走到墙面包装；从底部抽出洗脸巾；展开洗脸巾；"
                    "对折展示厚度；浸水吸水；双手拧干；擦拭脸颊；擦拭手臂；"
                    "撕开展示纤维；最后展示产品英雄镜头。"
                ),
                "final_text": (
                    "女主伸懒腰；走到墙面包装；从底部抽出洗脸巾；展开洗脸巾；"
                    "对折展示厚度；浸水吸水；双手拧干；擦拭脸颊；擦拭手臂；"
                    "撕开展示纤维；最后展示产品英雄镜头。"
                ),
            },
            "params": {"duration": 10, "resolution": "720p", "ratio": "9:16"},
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "video_sequence_required"
    assert detail["sequence_required"] is True
    assert detail["recommended_clip_count"] >= 2
    assert called is False
    with SessionLocal() as db:
        assert db.query(GenTask).filter(
            GenTask.user_id == user_id,
            GenTask.category == "video",
        ).count() == 0
        assert db.get(ModelConfig, 1) is not None
        from app.models import User

        assert db.get(User, user_id).balance_credits == 1000


def test_preview_capacity_uses_actual_preview_duration_before_task_creation(
    client, make_user, auth, monkeypatch
):
    user_id = make_user("13900003107", balance=1000)
    headers = auth("13900003107")
    _configure_video_model(
        "custom-video",
        prompt_profile={
            "max_prompt_chars": 900,
            "max_shots_by_duration": {"5": 1, "10": 2},
        },
    )
    called = False

    def fake_submit(*_args, **_kwargs):
        nonlocal called
        called = True
        return "should-not-submit-preview"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = client.post(
        "/api/generate",
        json={
            "category": "video",
            "stage": "preview",
            "prompt": {"final_text": "Shot 1：产品入镜；Shot 2：推近 Logo。"},
            "params": {"duration": 10, "resolution": "720p", "ratio": "9:16"},
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "video_sequence_required"
    assert called is False
    with SessionLocal() as db:
        assert db.query(GenTask).filter(GenTask.user_id == user_id).count() == 0


def test_product_video_compiles_one_fidelity_guard_and_separates_post_production(
    client, make_user, auth, monkeypatch
):
    make_user("13900003103", balance=1000)
    headers = auth("13900003103")
    _configure_video_model()
    upload = client.post(
        "/api/uploads/image",
        files={"file": ("tissue.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-prompt-product"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    raw_prompt = (
        "真实家庭浴室，米白和浅粉绿色调。Shot 1：手从墙面包装底部抽出一张洗脸巾；"
        "Shot 2：微距展开并展示3D如意云纹。字幕“干湿两用”；"
        "旁白：“让洗脸这件事，成为一天温柔的开始。”"
    )
    response = client.post(
        "/api/generate",
        json={
            "source_asset_url": upload.json()["url"],
            "source_type": "image",
            "category": "video",
            "stage": "final",
            "prompt": {
                "raw_text": raw_prompt,
                "assembled_text": raw_prompt,
                "产品身份档案": "同一SKU悬挂式洗脸巾包装，保持Logo、底部出纸口和3D如意云纹。",
                "final_text": raw_prompt,
                "instruction": raw_prompt,
            },
            "params": {
                "duration": 10,
                "resolution": "720p",
                "ratio": "9:16",
                "subject_mode": "product",
                "product_lock_mode": "free",
                "product_video_template": "reference_sequence",
            },
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert submitted["prompt"].count("产品身份约束") == 1
    assert "干湿两用" not in submitted["prompt"]
    assert "让洗脸这件事，成为一天温柔的开始" not in submitted["prompt"]
    assert "底部抽出一张洗脸巾" in submitted["prompt"]
    assert "3D如意云纹" in submitted["prompt"]
    assert "产品视频策略：参考分镜" in submitted["prompt"]
    assert "不强制每个镜头静态正面" in submitted["prompt"]

    task = client.get(f"/api/tasks/{response.json()['id']}", headers=headers).json()
    assert task["post_overlays"] == ["干湿两用"]
    assert task["voiceover"] == "让洗脸这件事，成为一天温柔的开始。"
    assert task["prompt_compiler_version"]
    assert task["model_id"] == "grok-imagine-video-1.5"
