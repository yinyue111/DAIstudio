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
    client, make_user, auth, monkeypatch, quote_and_generate
):
    make_user("13900003101", balance=1000)
    headers = auth("13900003101")
    _configure_video_model()
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-prompt-direct"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = quote_and_generate(
        {
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
    with SessionLocal() as db:
        persisted = db.get(GenTask, response.json()["id"])
        assert persisted.params["_video_submit_contract_version"] == "video-submit-v3"


def test_layered_video_prompt_submits_assembled_user_draft_and_keeps_history(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    make_user("13900003104", balance=1000)
    headers = auth("13900003104")
    _configure_video_model()
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-prompt-layered"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = quote_and_generate(
        {
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


def test_dense_video_prompt_is_preserved_instead_of_requesting_a_sequence(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    user_id = make_user("13900003102", balance=1000)
    headers = auth("13900003102")
    _configure_video_model("doubao-seedance-2-0-mini-260615")
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-condensed-overload"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = quote_and_generate(
        {
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

    assert response.status_code == 200, response.text
    assert submitted["prompt"].count("Shot ") == 11
    for action in ("伸懒腰", "底部抽出", "浸水吸水", "双手拧干", "撕开展示纤维", "产品英雄镜头"):
        assert action in submitted["prompt"]
    task = client.get(f"/api/tasks/{response.json()['id']}", headers=headers).json()
    assert task["generation_prompt_text"] == submitted["prompt"]
    assert any("动作密度较高" in warning for warning in task["prompt_warnings"])
    with SessionLocal() as db:
        assert db.query(GenTask).filter(
            GenTask.user_id == user_id,
            GenTask.category == "video",
        ).count() == 1
        assert db.get(ModelConfig, 1) is not None


def test_default_video_generation_preserves_dense_prompt_and_submits(
    client, make_user, auth, monkeypatch, quote_and_generate
):
    user_id = make_user("13900003108", balance=1000)
    headers = auth("13900003108")
    _configure_video_model("doubao-seedance-2-0-mini-260615")
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-condensed-1"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    raw_prompt = (
        "高端日系美妆个护广告；女主走向墙面悬挂包装；"
        "手从包装底部抽出洗脸巾；微距展开并展示3D如意云纹和厚度；"
        "俯拍浸水；双手拧干；擦拭脸颊；擦拭手臂；"
        "撕开展示纤维；最后展示产品英雄镜头。"
    )
    response = quote_and_generate(
        {
            "category": "video",
            "stage": "final",
            "prompt": {"raw_text": raw_prompt, "final_text": raw_prompt},
            "params": {"duration": 10, "resolution": "720p", "ratio": "9:16"},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert submitted["prompt"].count("Shot ") == 9
    assert "底部抽出" in submitted["prompt"]
    assert "3D如意云纹" in submitted["prompt"]
    assert "浸水" in submitted["prompt"]
    assert "双手拧干" in submitted["prompt"]
    assert "撕开展示纤维" in submitted["prompt"]
    task = client.get(f"/api/tasks/{response.json()['id']}", headers=headers).json()
    assert task["raw_prompt_text"] == raw_prompt
    assert task["generation_prompt_text"] == submitted["prompt"]
    assert task["sequence_required"] is False
    with SessionLocal() as db:
        persisted = db.query(GenTask).filter(GenTask.user_id == user_id).one()
        metadata = persisted.params["_video_prompt_metadata"]
        assert metadata["condensed_for_single_clip"] is False
        assert metadata["source_shot_count"] == metadata["selected_shot_count"]
        assert metadata["omitted_shot_count"] == 0


def test_preview_capacity_keeps_all_actions_for_actual_preview_duration(
    client, make_user, auth, monkeypatch, quote_and_generate
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
    submitted = {}

    def fake_submit(prompt, video_model_id, params, extra=None):
        submitted.update(prompt=prompt, model_id=video_model_id, params=params)
        return "video-condensed-preview"

    monkeypatch.setattr("app.services.gateway.submit_video", fake_submit)
    response = quote_and_generate(
        {
            "category": "video",
            "stage": "preview",
            "prompt": {"final_text": "Shot 1：产品入镜；Shot 2：推近 Logo。"},
            "params": {"duration": 10, "resolution": "720p", "ratio": "9:16"},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert submitted["prompt"].count("Shot ") == 2
    assert "产品入镜" in submitted["prompt"]
    assert "推近 Logo" in submitted["prompt"]
    with SessionLocal() as db:
        assert db.query(GenTask).filter(GenTask.user_id == user_id).count() == 1


def test_direct_product_video_submits_visual_copy_and_keeps_post_metadata(
    client, make_user, auth, monkeypatch, quote_and_generate
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
    response = quote_and_generate(
        {
            "source_asset_url": upload.json()["url"],
            "source_type": "image",
            "category": "video",
            "stage": "final",
            "prompt": {
                "input_mode": "direct_input",
                "user_instruction": raw_prompt,
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
                "product_reference_image": upload.json()["url"],
                "product_lock_mode": "locked",
                "product_video_template": "prompt_driven",
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
    assert "Shot 1" in submitted["prompt"]
    assert submitted["prompt"].count("Shot ") == 2
    assert "产品视频策略" not in submitted["prompt"]
    assert "画面无字" not in submitted["prompt"]
    assert submitted["params"]["product_reference_image"].startswith("data:image/jpeg;base64,")
    assert "first_frame_image" not in submitted["params"]
    assert "last_frame_image" not in submitted["params"]
    assert "style_reference_image" not in submitted["params"]

    task = client.get(f"/api/tasks/{response.json()['id']}", headers=headers).json()
    assert task["post_overlays"] == ["干湿两用"]
    assert task["voiceover"] == "让洗脸这件事，成为一天温柔的开始。"
    assert task["prompt_compiler_version"]
    assert task["model_id"] == "grok-imagine-video-1.5"
    with SessionLocal() as db:
        persisted = db.get(GenTask, response.json()["id"])
        assert persisted.params["_video_prompt_metadata"]["prompt_mode"] == "direct_passthrough"
