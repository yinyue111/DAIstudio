from app.db import SessionLocal
from app.models import CreditTransaction, ModelConfig, User
from app.services import gateway
from app.services.model_gateway_config import RuntimeGatewayConfig


def test_prompt_optimizer_uses_dedicated_model_and_product_context(client, make_user, auth, monkeypatch):
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
        charge = db.query(CreditTransaction).filter_by(
            user_id=user.id,
            type="consume",
            biz_type="prompt_optimize",
        ).one()
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
            "prompt": "10 秒产品广告，仅展示抽取和展开两个连续动作。",
            "usage": None,
            "latency_ms": 9,
            "compiler_metadata": {
                "version": "prompt-optimizer-v2",
                "output_format": "single_text",
            },
            "context_metadata": {
                "duration": 10,
                "subject_mode": "product",
                "reference_type": "product_image",
                "target_model_id": "doubao-seedance-2-0-mini-260615",
                "target_model_provider": "volcengine_ark",
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
    assert response.json() == {
        "prompt": "10 秒产品广告，仅展示抽取和展开两个连续动作。",
        "model_id": "gemini-3.5-flash-low",
        "optimizer_model_id": "gemini-3.5-flash-low",
        "compiler_metadata": {
            "version": "prompt-optimizer-v2",
            "output_format": "single_text",
        },
        "context_metadata": {
            "duration": 10,
            "subject_mode": "product",
            "reference_type": "product_image",
            "target_model_id": "doubao-seedance-2-0-mini-260615",
            "target_model_provider": "volcengine_ark",
        },
    }


def test_video_prompt_optimizer_infers_current_generation_model(client, make_user, auth, monkeypatch):
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
        assert db.query(CreditTransaction).filter_by(
            user_id=user.id,
            biz_type="prompt_optimize",
        ).count() == 0


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
        rows = db.query(CreditTransaction).filter_by(
            user_id=user.id,
            biz_type="prompt_optimize",
        ).order_by(CreditTransaction.id).all()
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
            "content": [{"type": "text", "text": "完整的产品商业提示词"}],
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
    assert "参考主体档案" in seen["payload"]["messages"][0]["content"]
    assert "3D 如意云纹" in seen["payload"]["messages"][0]["content"]
    assert result["prompt"] == "完整的产品商业提示词"
    assert result["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }
    assert result["optimizer_model_id"] == "gemini-3.5-flash-low"
    assert result["compiler_metadata"] == {
        "version": "prompt-optimizer-v2",
        "output_format": "single_text",
    }
    assert result["context_metadata"] == {
        "duration": 10,
        "subject_mode": "product",
        "reference_type": "product_image",
        "target_model_id": "doubao-seedance-2-0-mini-260615",
        "target_model_provider": "volcengine_ark",
    }


def test_openai_prompt_optimizer_uses_runtime_video_prompt_profile(monkeypatch):
    seen = {}

    def fake_post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, **kwargs)
        return {
            "choices": [{"message": {"content": "精简后的视频提示词"}}],
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
    assert result["prompt"] == "精简后的视频提示词"
