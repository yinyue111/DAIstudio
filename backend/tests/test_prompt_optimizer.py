import pytest

from app.db import SessionLocal
from app.models import CreditTransaction, ModelConfig, User
from app.services import gateway
from app.services.model_gateway_config import RuntimeGatewayConfig


def test_prompt_optimizer_uses_dedicated_model_and_product_context(
    client, make_user, auth, monkeypatch
):
    make_user("13900002991", balance=100)
    headers = auth("13900002991")
    db = SessionLocal()
    try:
        row = db.query(ModelConfig).filter(ModelConfig.use == "prompt").one()
        row.model_id = "gemini-3.5-flash-low"
        row.cost_credits = 1
        row.enabled = True
        db.commit()
    finally:
        db.close()

    seen = {}

    def fake_optimize(prompt, model_id, **kwargs):
        seen.update(prompt=prompt, model_id=model_id, **kwargs)
        return {
            "prompt": "完整展示同一SKU产品，保留包装、Logo和文字，使用柔和棚拍光与稳定商业构图。",
            "usage": {"prompt_tokens": 20, "completion_tokens": 18, "total_tokens": 38},
            "latency_ms": 12,
        }

    monkeypatch.setattr("app.services.gateway.optimize_prompt", fake_optimize)
    response = client.post(
        "/api/prompt/optimize",
        json={"prompt": "产品放浴室里", "category": "image", "product_mode": True},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["model_id"] == "gemini-3.5-flash-low"
    assert "Logo和文字" in response.json()["prompt"]
    assert seen["prompt"] == "产品放浴室里"
    assert seen["model_id"] == "gemini-3.5-flash-low"
    assert seen["category"] == "image"
    assert seen["product_mode"] is True
    assert seen["gateway_config"].use == "prompt"
    with SessionLocal() as db:
        user = db.query(User).filter(User.phone == "13900002991").one()
        assert user.balance_credits == 99
        charge = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=user.id,
                type="consume",
                biz_type="prompt_optimize",
            )
            .one()
        )
        assert charge.change == -1


def test_prompt_optimizer_passes_video_generation_context_and_returns_metadata(
    client, make_user, auth, monkeypatch
):
    make_user("13900002997", balance=100)
    headers = auth("13900002997")
    db = SessionLocal()
    try:
        row = db.query(ModelConfig).filter(ModelConfig.use == "prompt").one()
        row.model_id = "gemini-3.5-flash-low"
        row.cost_credits = 1
        row.enabled = True
        db.commit()
    finally:
        db.close()

    seen = {}

    def fake_optimize(prompt, model_id, **kwargs):
        seen.update(prompt=prompt, model_id=model_id, **kwargs)
        return {
            "prompt": (
                "风格设定：高端日系个护广告，粉绿与米白色调，柔和自然光。\n"
                "场景脚本：\n"
                "Shot 1：手从悬挂包装底部抽出一张洗脸巾。\n"
                "Shot 2：微距展开洗脸巾，展示 3D 如意云纹。\n"
                "技术约束：总时长 10 秒；画幅 9:16；分辨率 1080p。"
            ),
            "usage": None,
            "latency_ms": 9,
            "compiler_metadata": {
                "version": "prompt-optimizer-v3",
                "output_format": "structured_video_text",
                "sections": ["风格设定", "场景脚本", "技术约束"],
            },
            "context_metadata": {
                "duration": 10,
                "subject_mode": "product",
                "reference_type": "product_image",
                "target_model_id": "doubao-seedance-2-0-mini-260615",
                "target_model_provider": "volcengine_ark",
                "aspect_ratio": "9:16",
                "resolution": "1080p",
                "product_lock_mode": "locked",
                "product_video_template": "slow_push",
            },
        }

    monkeypatch.setattr("app.services.gateway.optimize_prompt", fake_optimize)
    response = client.post(
        "/api/prompt/optimize",
        json={
            "prompt": "女主抽出洗脸巾，展开后浸水。",
            "category": "video",
            "product_mode": True,
            "duration": 10,
            "subject_mode": "product",
            "reference_type": "product_image",
            "subject_profile": {
                "identity": "同一 SKU 粉绿色悬挂包装",
                "texture": "3D 如意云纹",
            },
            "target_model_id": "doubao-seedance-2-0-mini-260615",
            "target_model_provider": "volcengine_ark",
            "aspect_ratio": "9:16",
            "resolution": "1080p",
            "product_lock_mode": "locked",
            "product_video_template": "slow_push",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert seen["duration"] == 10
    assert seen["subject_mode"] == "product"
    assert seen["reference_type"] == "product_image"
    assert seen["subject_profile"] == {
        "identity": "同一 SKU 粉绿色悬挂包装",
        "texture": "3D 如意云纹",
    }
    assert seen["target_model_id"] == "doubao-seedance-2-0-mini-260615"
    assert seen["target_model_provider"] == "volcengine_ark"
    assert seen["aspect_ratio"] == "9:16"
    assert seen["resolution"] == "1080p"
    assert seen["product_lock_mode"] == "locked"
    assert seen["product_video_template"] == "slow_push"
    assert response.json() == {
        "prompt": (
            "风格设定：高端日系个护广告，粉绿与米白色调，柔和自然光。\n"
            "场景脚本：\n"
            "Shot 1：手从悬挂包装底部抽出一张洗脸巾。\n"
            "Shot 2：微距展开洗脸巾，展示 3D 如意云纹。\n"
            "技术约束：总时长 10 秒；画幅 9:16；分辨率 1080p。"
        ),
        "model_id": "gemini-3.5-flash-low",
        "optimizer_model_id": "gemini-3.5-flash-low",
        "compiler_metadata": {
            "version": "prompt-optimizer-v3",
            "output_format": "structured_video_text",
            "sections": ["风格设定", "场景脚本", "技术约束"],
        },
        "context_metadata": {
            "duration": 10,
            "subject_mode": "product",
            "reference_type": "product_image",
            "target_model_id": "doubao-seedance-2-0-mini-260615",
            "target_model_provider": "volcengine_ark",
            "aspect_ratio": "9:16",
            "resolution": "1080p",
            "product_lock_mode": "locked",
            "product_video_template": "slow_push",
        },
    }


def test_video_prompt_optimizer_infers_current_generation_model(
    client, make_user, auth, monkeypatch
):
    make_user("13900002998", balance=100)
    headers = auth("13900002998")
    with SessionLocal() as db:
        prompt_model = db.query(ModelConfig).filter(ModelConfig.use == "prompt").one()
        prompt_model.enabled = True
        prompt_model.cost_credits = 1
        video_model = db.query(ModelConfig).filter(ModelConfig.use == "video").one()
        video_model.model_id = "doubao-seedance-2-0-mini-260615"
        video_model.provider = "volcengine_ark"
        video_model.enabled = True
        video_model.extra = {
            "prompt_profile": {
                "max_prompt_chars": 900,
                "max_shots_by_duration": {"5": 1, "10": 4},
            }
        }
        db.commit()

    seen = {}

    def fake_optimize(prompt, model_id, **kwargs):
        seen.update(kwargs)
        return {"prompt": prompt, "usage": None, "latency_ms": 1}

    monkeypatch.setattr("app.services.gateway.optimize_prompt", fake_optimize)
    response = client.post(
        "/api/prompt/optimize",
        json={"prompt": "两个连续产品镜头", "category": "video", "duration": 10},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert seen["target_model_id"] == "doubao-seedance-2-0-mini-260615"
    assert seen["target_model_provider"] == "volcengine_ark"
    assert seen["target_model_extra"]["prompt_profile"]["max_shots_by_duration"] == {
        "5": 1,
        "10": 4,
    }


def test_prompt_optimizer_rejects_empty_prompt(client, make_user, auth):
    make_user("13900002992", balance=100)
    response = client.post(
        "/api/prompt/optimize",
        json={"prompt": "", "category": "video", "product_mode": False},
        headers=auth("13900002992"),
    )
    assert response.status_code == 422


def test_prompt_optimizer_rejects_whitespace_only_prompt_without_charging(
    client, make_user, auth, monkeypatch
):
    make_user("13900002996", balance=100)
    called = False

    def fake_optimize(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"prompt": "should not be called"}

    monkeypatch.setattr("app.services.gateway.optimize_prompt", fake_optimize)
    headers = auth("13900002996")
    for prompt in (" ", "\n\t"):
        response = client.post(
            "/api/prompt/optimize",
            json={"prompt": prompt, "category": "image", "product_mode": False},
            headers=headers,
        )
        assert response.status_code == 422

    assert called is False
    with SessionLocal() as db:
        user = db.query(User).filter(User.phone == "13900002996").one()
        assert user.balance_credits == 100
        assert (
            db.query(CreditTransaction)
            .filter_by(
                user_id=user.id,
                biz_type="prompt_optimize",
            )
            .count()
            == 0
        )


def test_prompt_optimizer_refunds_charge_when_gateway_fails(client, make_user, auth, monkeypatch):
    make_user("13900002993", balance=10)

    def fail_optimize(*_args, **_kwargs):
        raise gateway.GatewayError("upstream unavailable")

    monkeypatch.setattr("app.services.gateway.optimize_prompt", fail_optimize)
    response = client.post(
        "/api/prompt/optimize",
        json={"prompt": "产品放到浴室台面", "category": "image", "product_mode": True},
        headers=auth("13900002993"),
    )

    assert response.status_code == 502
    with SessionLocal() as db:
        user = db.query(User).filter(User.phone == "13900002993").one()
        assert user.balance_credits == 10
        rows = (
            db.query(CreditTransaction)
            .filter_by(
                user_id=user.id,
                biz_type="prompt_optimize",
            )
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [row.type for row in rows] == ["consume", "refund"]


def test_prompt_optimizer_body_size_is_limited(client):
    response = client.post(
        "/api/prompt/optimize",
        content=b"x" * (256 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_anthropic_prompt_optimizer_uses_messages_protocol(monkeypatch):
    seen = {}

    def fake_post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, **kwargs)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"风格设定":"高端日系个护广告，粉绿与米白色调，柔和自然光",'
                        '"场景脚本":["手从悬挂包装底部抽出洗脸巾","微距展开并展示3D如意云纹"],'
                        '"技术约束":"稳定运镜，产品包装和Logo保持一致"}'
                    ),
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="anthropic",
        base_url="https://example.com/antigravity",
        api_key="secret",
        gateway_format="anthropic",
    )
    result = gateway.optimize_prompt(
        "产品放到浴室台面",
        "gemini-3.5-flash-low",
        category="video",
        product_mode=True,
        duration=10,
        subject_mode="product",
        reference_type="product_image",
        subject_profile={
            "identity": "同一 SKU 粉绿色悬挂包装",
            "texture": "3D 如意云纹",
        },
        target_model_id="doubao-seedance-2-0-mini-260615",
        target_model_provider="volcengine_ark",
        aspect_ratio="9:16",
        resolution="1080p",
        product_lock_mode="locked",
        product_video_template="slow_push",
        target_model_extra={
            "prompt_profile": {
                "max_prompt_chars": 900,
                "max_shots_by_duration": {"5": 1, "10": 4},
            }
        },
        gateway_config=cfg,
    )

    assert seen["path"] == "/messages"
    assert seen["payload"]["model"] == "gemini-3.5-flash-low"
    assert seen["payload"]["max_tokens"] == 4096
    assert "产品视频生成提示词" in seen["payload"]["system"]
    assert "不得凭空新增" in seen["payload"]["system"]
    assert "纸巾、花瓶、植物" in seen["payload"]["system"]
    assert "参考图已有道具仅可保留" in seen["payload"]["system"]
    assert "10 秒" in seen["payload"]["system"]
    assert "doubao-seedance-2-0-mini-260615" in seen["payload"]["system"]
    assert "最多 4 个主要镜头" in seen["payload"]["system"]
    assert "产品参考图只锁定产品身份" in seen["payload"]["system"]
    assert "不得用产品参考图锁定人物身份、场景、构图或光线" in seen["payload"]["system"]
    assert "后期叠字" in seen["payload"]["system"]
    assert "后期配音" in seen["payload"]["system"]
    assert "不要求视频模型渲染精确字幕或旁白" in seen["payload"]["system"]
    assert "风格设定" in seen["payload"]["system"]
    assert "场景脚本" in seen["payload"]["system"]
    assert "技术约束" in seen["payload"]["system"]
    assert "只返回一个 JSON 对象" in seen["payload"]["system"]
    assert "画幅 9:16" in seen["payload"]["system"]
    assert "分辨率 1080p" in seen["payload"]["system"]
    assert "slow_push" in seen["payload"]["system"]
    assert "参考主体档案" in seen["payload"]["messages"][0]["content"]
    assert "3D 如意云纹" in seen["payload"]["messages"][0]["content"]
    assert result["prompt"].startswith("风格设定：高端日系个护广告")
    assert "\n场景脚本：\nShot 1：手从悬挂包装底部抽出洗脸巾" in result["prompt"]
    assert "\nShot 2：微距展开并展示3D如意云纹" in result["prompt"]
    assert "\n技术约束：" in result["prompt"]
    assert "画幅 9:16" in result["prompt"]
    assert "分辨率 1080p" in result["prompt"]
    assert "同一 SKU" in result["prompt"]
    assert result["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }
    assert result["optimizer_model_id"] == "gemini-3.5-flash-low"
    metadata = result["compiler_metadata"]
    assert metadata["version"] == "prompt-optimizer-v3"
    assert metadata["output_format"] == "structured_video_text"
    assert metadata["sections"] == ["风格设定", "场景脚本", "技术约束"]
    assert metadata["shot_count"] == 2
    assert metadata["recommended_max_shots"] == 4
    assert metadata["prompt_budget_chars"] == 900
    assert metadata["prompt_char_count"] < 900
    assert metadata["recommended_clip_count"] == 1
    assert metadata["sequence_required"] is False
    assert result["context_metadata"] == {
        "duration": 10,
        "subject_mode": "product",
        "reference_type": "product_image",
        "target_model_id": "doubao-seedance-2-0-mini-260615",
        "target_model_provider": "volcengine_ark",
        "aspect_ratio": "9:16",
        "resolution": "1080p",
        "product_lock_mode": "locked",
        "product_video_template": "slow_push",
        "effective_product_mode": True,
        "product_mode_source": "explicit",
    }


def test_openai_prompt_optimizer_uses_runtime_video_prompt_profile(monkeypatch):
    seen = {}

    def fake_post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, **kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"风格设定":"极简商业广告，柔和侧光",'
                            '"场景脚本":["产品稳定入镜","镜头慢速推近Logo"],'
                            '"技术约束":"保持时空连续"}'
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        "产品先入镜，再推近 Logo",
        "gemini-3.5-flash-low",
        category="video",
        duration=10,
        target_model_id="custom-video-v7",
        target_model_provider="private",
        target_model_extra={
            "video_prompt_profile": {
                "family": "private_v7",
                "recommended_max_shots": 5,
            }
        },
        gateway_config=cfg,
    )

    assert seen["path"] == "/chat/completions"
    assert "最多 5 个主要镜头" in seen["payload"]["messages"][0]["content"]
    assert result["prompt"].startswith("风格设定：极简商业广告，柔和侧光")
    assert "场景脚本：\nShot 1：产品稳定入镜\nShot 2：镜头慢速推近Logo" in result["prompt"]
    assert "画面无字，精确字幕、旁白和音效仅后期添加" in result["prompt"]
    assert "必须完整执行且不得替换的原始动作要求" in result["prompt"]


def test_video_prompt_optimizer_infers_product_constraints_from_direct_text(monkeypatch):
    seen = {}

    def fake_post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, **kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            "## 风格设定\n高端个护广告，真实浴室柔光\n"
                            "## 场景脚本\n镜头1：手从粉绿色包装底部抽出洗脸巾\n"
                            "## 技术约束\n包装保持一致"
                        )
                    }
                }
            ],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        "粉绿色洗脸巾包装悬挂在墙面，手从底部抽出一张洗脸巾。",
        "gemini-3.5-flash-low",
        category="video",
        product_mode=False,
        duration=5,
        target_model_id="grok-imagine-video",
        target_model_provider="yinyue",
        gateway_config=cfg,
    )

    assert "这是产品视频生成提示词" in seen["payload"]["messages"][0]["content"]
    assert result["context_metadata"]["effective_product_mode"] is True
    assert result["context_metadata"]["product_mode_source"] == "inferred_from_prompt"
    assert result["prompt"].startswith("风格设定：高端个护广告，真实浴室柔光")
    assert "Shot 1：手从粉绿色包装底部抽出洗脸巾" in result["prompt"]
    assert "同一 SKU" in result["prompt"]

    general_result = gateway.optimize_prompt(
        "Brand film production，女主沿城市街道自然行走，镜头稳定跟拍。",
        "gemini-3.5-flash-low",
        category="video",
        product_mode=False,
        duration=5,
        target_model_id="grok-imagine-video",
        target_model_provider="yinyue",
        gateway_config=cfg,
    )
    assert general_result["context_metadata"]["effective_product_mode"] is False
    assert "这是产品视频生成提示词" not in seen["payload"]["messages"][0]["content"]

    product_manager_result = gateway.optimize_prompt(
        "产品经理走进办公室，向团队介绍软件产品功能。",
        "gemini-3.5-flash-low",
        category="video",
        product_mode=False,
        duration=5,
        target_model_id="grok-imagine-video",
        target_model_provider="yinyue",
        gateway_config=cfg,
    )
    assert product_manager_result["context_metadata"]["effective_product_mode"] is False
    assert "这是产品视频生成提示词" not in seen["payload"]["messages"][0]["content"]

    for digital_prompt in (
        "SaaS 产品功能展示，镜头推近数据看板。",
        "App 产品在手机界面中展示核心功能。",
    ):
        digital_result = gateway.optimize_prompt(
            digital_prompt,
            "gemini-3.5-flash-low",
            category="video",
            product_mode=False,
            duration=5,
            target_model_id="grok-imagine-video",
            target_model_provider="yinyue",
            gateway_config=cfg,
        )
        assert digital_result["context_metadata"]["effective_product_mode"] is False
        assert "这是产品视频生成提示词" not in seen["payload"]["messages"][0]["content"]


def test_image_prompt_optimizer_does_not_infer_product_mode_from_subject_mode(monkeypatch):
    seen = {}

    def fake_post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, **kwargs)
        return {
            "choices": [{"message": {"content": "极简软件界面宣传图，清晰展示功能层级"}}],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        "软件产品功能宣传图",
        "gemini-3.5-flash-low",
        category="image",
        product_mode=False,
        subject_mode="product",
        gateway_config=cfg,
    )

    assert "产品图片生成提示词" not in seen["payload"]["messages"][0]["content"]
    assert result["prompt"] == "极简软件界面宣传图，清晰展示功能层级"


def test_video_prompt_optimizer_preserves_overloaded_scene_script_for_single_clip(monkeypatch):
    def fake_post(_path, _payload, **_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"风格设定":"真实生活方式短片",'
                            '"场景脚本":["人物走进浴室","人物拿起毛巾","人物走出浴室"],'
                            '"技术约束":"稳定跟拍"}'
                        )
                    }
                }
            ],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        "人物走进浴室，拿起毛巾，再走出浴室。",
        "gemini-3.5-flash-low",
        category="video",
        duration=5,
        target_model_id="doubao-seedance-2-0-mini-260615",
        target_model_provider="volcengine_ark",
        gateway_config=cfg,
    )

    assert result["compiler_metadata"]["shot_count"] == 3
    assert result["compiler_metadata"]["source_shot_count"] == 3
    assert result["compiler_metadata"]["recommended_max_shots"] == 1
    assert result["compiler_metadata"]["sequence_required"] is False
    assert result["compiler_metadata"]["condensed_for_single_clip"] is False
    assert result["compiler_metadata"]["omitted_shot_count"] == 0
    assert "Shot 1：人物走进浴室" in result["prompt"]
    assert "Shot 2：人物拿起毛巾" in result["prompt"]
    assert "Shot 3：人物走出浴室" in result["prompt"]
    assert "需拆分为多段生成" not in result["prompt"]


def test_video_prompt_optimizer_restores_all_source_actions_omitted_by_model(
    monkeypatch,
):
    def fake_post(_path, _payload, **_kwargs):
        return {
            "choices": [{"message": {"content": (
                '{"风格设定":"真实生活方式短片",'
                '"场景脚本":["人物在浴室活动"],'
                '"技术约束":"稳定跟拍"}'
            )}}],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        "人物走进浴室，拿起毛巾，再走出浴室。",
        "gemini-3.5-flash-low",
        category="video",
        duration=5,
        target_model_id="doubao-seedance-2-0-mini-260615",
        target_model_provider="volcengine_ark",
        gateway_config=cfg,
    )

    assert "Shot 1：人物走进浴室" in result["prompt"]
    assert "Shot 2：拿起毛巾" in result["prompt"]
    assert "Shot 3：再走出浴室" in result["prompt"]
    assert result["compiler_metadata"]["shot_count"] == 3
    assert result["compiler_metadata"]["source_shot_count"] == 3
    assert result["compiler_metadata"]["sequence_required"] is False
    assert result["compiler_metadata"]["omitted_shot_count"] == 0


def test_video_prompt_optimizer_restores_replaced_action_when_shot_count_matches(monkeypatch):
    def fake_post(_path, _payload, **_kwargs):
        return {
            "choices": [{"message": {"content": (
                '{"风格设定":"真实生活方式短片",'
                '"场景脚本":["人物走进浴室","人物看向镜子"],'
                '"技术约束":"稳定跟拍"}'
            )}}],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        "人物走进浴室，拿起毛巾。",
        "gemini-3.5-flash-low",
        category="video",
        duration=10,
        target_model_id="doubao-seedance-2-0-mini-260615",
        target_model_provider="volcengine_ark",
        gateway_config=cfg,
    )

    assert "Shot 1：人物走进浴室" in result["prompt"]
    assert "Shot 2：人物看向镜子" in result["prompt"]
    assert "必须完整执行且不得替换的原始动作要求（按顺序）：1. 拿起毛巾" in result["prompt"]
    assert result["compiler_metadata"]["shot_count"] == 2


def test_video_prompt_optimizer_preserves_directional_and_common_source_actions(monkeypatch):
    def fake_post(_path, payload, **_kwargs):
        prompt_text = str(payload)
        scene = "人物看向镜头" if "打开瓶盖" in prompt_text else "人物走进浴室"
        return {
            "choices": [{"message": {"content": (
                '{"风格设定":"真实生活方式短片",'
                f'"场景脚本":["{scene}"],'
                '"技术约束":"稳定跟拍"}'
            )}}],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    for source, expected in (
        ("人物走出浴室。", "1. 人物走出浴室"),
        ("人物打开瓶盖。", "1. 人物打开瓶盖"),
    ):
        result = gateway.optimize_prompt(
            source,
            "gemini-3.5-flash-low",
            category="video",
            duration=5,
            target_model_id="doubao-seedance-2-0-mini-260615",
            target_model_provider="volcengine_ark",
            gateway_config=cfg,
        )
        assert expected in result["prompt"]
        assert "必须完整执行且不得替换的原始动作要求" in result["prompt"]


def test_video_prompt_optimizer_keeps_subject_actions_that_mention_the_camera(monkeypatch):
    def fake_post(_path, _payload, **_kwargs):
        return {
            "choices": [{"message": {"content": (
                '{"风格设定":"真实生活方式短片",'
                '"场景脚本":["人物走进浴室"],'
                '"技术约束":"稳定跟拍"}'
            )}}],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        "人物凝视镜头，随后轻抚脸颊。",
        "gemini-3.5-flash-low",
        category="video",
        duration=5,
        gateway_config=cfg,
    )

    assert "原始动作要求（按顺序）：1. 人物凝视镜头，随后轻抚脸颊" in result["prompt"]


def test_video_prompt_optimizer_excludes_style_prefix_from_action_inventory(monkeypatch):
    def fake_post(_path, _payload, **_kwargs):
        return {
            "choices": [{"message": {"content": (
                '{"风格设定":"高端日系广告",'
                '"场景脚本":["人物看向镜头"],'
                '"技术约束":"稳定跟拍"}'
            )}}],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    for source, expected_action, excluded_prefix in (
        ("高端日系广告，人物走进浴室。", "人物走进浴室", "高端日系广告"),
        ("柔和自然光，人物走进浴室。", "人物走进浴室", "柔和自然光"),
        ("微距浅景深，手打开瓶盖。", "手打开瓶盖", "微距浅景深"),
        ("人物看向镜头，微笑。", "人物看向镜头，微笑", ""),
    ):
        result = gateway.optimize_prompt(
            source,
            "gemini-3.5-flash-low",
            category="video",
            duration=5,
            gateway_config=cfg,
        )

        assert f"原始动作要求（按顺序）：1. {expected_action}" in result["prompt"]
        if excluded_prefix:
            assert f"原始动作要求（按顺序）：1. {excluded_prefix}" not in result["prompt"]


def test_video_prompt_optimizer_reports_over_budget_without_truncating_source_action(monkeypatch):
    long_action = "人物保持稳定步伐向前行走并持续观察周围环境" * 12

    def fake_post(_path, _payload, **_kwargs):
        return {
            "choices": [{"message": {"content": (
                '{"风格设定":"真实生活方式短片",'
                f'"场景脚本":["{long_action}"],'
                '"技术约束":"稳定跟拍"}'
            )}}],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    result = gateway.optimize_prompt(
        long_action,
        "gemini-3.5-flash-low",
        category="video",
        duration=5,
        target_model_id="doubao-seedance-2-0-mini-260615",
        target_model_provider="volcengine_ark",
        target_model_extra={"prompt_profile": {"max_prompt_chars": 180}},
        gateway_config=cfg,
    )

    assert result["compiler_metadata"]["shot_count"] == 1
    assert result["compiler_metadata"]["prompt_budget_chars"] == 180
    assert result["compiler_metadata"]["prompt_char_count"] > 180
    assert result["compiler_metadata"]["sequence_required"] is False
    assert result["compiler_metadata"]["prompt_over_budget"] is True
    assert result["compiler_metadata"]["compacted_for_budget"] is True
    assert long_action in result["prompt"]


def test_video_prompt_optimizer_rejects_extreme_output_instead_of_truncating_action(monkeypatch):
    def fake_post(_path, _payload, **_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"风格设定":"真实商业短片",'
                            f'"场景脚本":["{"连续动作" * 2100}"],'
                            '"技术约束":"保持连续"}'
                        )
                    }
                }
            ],
            "usage": None,
        }

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_gateway_mock", lambda _config=None: False)
    cfg = RuntimeGatewayConfig(
        use="prompt",
        provider="custom_openai",
        base_url="https://example.com/v1",
        api_key="secret",
        gateway_format="openai",
    )

    with pytest.raises(gateway.GatewayError, match="结构化结果过长"):
        gateway.optimize_prompt(
            "生成一段连续动作视频。",
            "gemini-3.5-flash-low",
            category="video",
            duration=5,
            gateway_config=cfg,
        )
