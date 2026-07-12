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
        gateway_config=cfg,
    )

    assert seen["path"] == "/messages"
    assert seen["payload"]["model"] == "gemini-3.5-flash-low"
    assert seen["payload"]["max_tokens"] == 4096
    assert "产品视频生成提示词" in seen["payload"]["system"]
    assert "不得凭空新增" in seen["payload"]["system"]
    assert "纸巾、花瓶、植物" in seen["payload"]["system"]
    assert "参考图已有道具仅可保留" in seen["payload"]["system"]
    assert result["prompt"] == "完整的产品商业提示词"
    assert result["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }
