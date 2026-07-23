"""Generation entrypoints expose stable gateway configuration business errors."""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.models import CreditTransaction, GenerationQuote, GenTask
from app.services.model_gateway_config import ModelGatewayConfigError


def _payload() -> dict:
    return {
        "category": "image",
        "stage": "preview",
        "instruction": "gateway config error contract",
        "params": {"n": 1, "size": "1024x1024"},
    }


def _counts(user_id: int) -> tuple[int, int, int]:
    with SessionLocal() as db:
        return (
            db.query(GenerationQuote).filter_by(user_id=user_id).count(),
            db.query(GenTask).filter_by(user_id=user_id).count(),
            db.query(CreditTransaction).filter_by(user_id=user_id).count(),
        )


def test_invalid_runtime_config_has_zero_side_effects_at_quote(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13999071001")
    headers = auth("13999071001")
    before = _counts(user_id)
    monkeypatch.setattr(
        "app.routers.generate.model_snapshot",
        lambda _model: (_ for _ in ()).throw(ModelGatewayConfigError("secret detail")),
    )
    response = client.post("/api/quotes", json=_payload(), headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "MODEL_GATEWAY_CONFIG_INVALID",
        "message": "模型网关配置无效,请联系管理员检查配置",
    }
    assert "secret detail" not in response.text
    assert _counts(user_id) == before


def test_transient_decrypt_failure_is_503_before_quote_bound_mutation(
    client,
    make_user,
    auth,
    monkeypatch,
):
    user_id = make_user("13999071003")
    headers = auth("13999071003")
    quote = client.post("/api/quotes", json=_payload(), headers=headers)
    assert quote.status_code == 201, quote.text
    before = _counts(user_id)
    monkeypatch.setattr(
        "app.routers.generate.model_from_persisted_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ModelGatewayConfigError("模型 API Key 解密失败,内部密钥细节")
        ),
    )
    response = client.post(
        "/api/generate",
        json={**_payload(), "quote_id": quote.json()["quote_id"]},
        headers=headers,
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "MODEL_GATEWAY_RUNTIME_UNAVAILABLE"
    assert "内部密钥细节" not in response.text
    assert _counts(user_id) == before


def test_retry_runtime_config_failure_has_zero_side_effects(
    client,
    make_user,
    auth,
    monkeypatch,
    quote_and_generate,
):
    user_id = make_user("13999071004")
    headers = auth("13999071004")
    created = quote_and_generate(_payload(), headers=headers)
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])
    with SessionLocal() as db:
        task = db.get(GenTask, task_id)
        task.status = "failed"
        task.error = "known retryable failure"
        db.commit()
    before = _counts(user_id)
    monkeypatch.setattr(
        "app.routers.tasks.model_config_for_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ModelGatewayConfigError("模型提供商非法 secret-provider")
        ),
    )
    response = client.post(f"/api/tasks/{task_id}/retry", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_GATEWAY_CONFIG_INVALID"
    assert "secret-provider" not in response.text
    assert _counts(user_id) == before
