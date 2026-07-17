"""Admin-configured model providers and model probing."""

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import GenTask, ModelConfig
from app.runtime_config import validate_model_gateway_rows
from app.services import gateway
from app.services.model_gateway_config import (
    _env_runtime_config,
    decrypt_row_api_key,
    normalise_gateway_format,
)


def test_admin_model_config_encrypts_and_masks_api_key(client, make_user, auth):
    make_user("13900001001", balance=1000, admin=True)
    h = auth("13900001001")

    r = client.put(
        "/api/admin/models",
        json={
            "use": "image",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "gateway_format": "openai",
            "api_key": "sk-model-secret",
            "model_id": "gpt-image-2",
            "cost_credits": 5,
            "unlock_cost": 5,
            "enabled": True,
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text

    got = client.get("/api/admin/models", headers=h)
    assert got.status_code == 200, got.text
    image = next(m for m in got.json()["models"] if m["use"] == "image")
    assert image["api_key_configured"] is True
    assert image["api_key_masked"] == "已配置"
    assert "sk-model-secret" not in got.text

    db = SessionLocal()
    try:
        row = db.query(ModelConfig).filter(ModelConfig.use == "image").one()
        assert row.api_key_encrypted != "sk-model-secret"
        assert decrypt_row_api_key(row) == "sk-model-secret"
    finally:
        db.close()


def test_prompt_env_gateway_is_independent_from_vision_credentials(monkeypatch):
    monkeypatch.setattr(settings, "gateway_base_url", "https://vision.example.com/v1")
    monkeypatch.setattr(settings, "gateway_api_key", "vision-key")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://prompt.example.com/antigravity")
    monkeypatch.setattr(settings, "anthropic_auth_token", "prompt-key")

    config = _env_runtime_config("prompt")

    assert config.provider == "anthropic"
    assert config.base_url == "https://prompt.example.com/antigravity"
    assert config.api_key == "prompt-key"
    assert config.gateway_format == "anthropic"


def test_prompt_gateway_defaults_to_anthropic_messages_format():
    assert normalise_gateway_format(None, None, "prompt") == "anthropic"


def test_models_yaml_seeds_prompt_optimizer_model(client):
    with SessionLocal() as db:
        row = db.query(ModelConfig).filter(ModelConfig.use == "prompt").one()

        assert row.model_id == "gemini-3.5-flash-low"
        assert row.cost_credits == 3
        assert row.enabled is True


def test_production_accepts_complete_anthropic_env_gateway(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "gateway_base_url", "https://vision.example.com/v1")
    monkeypatch.setattr(settings, "gateway_api_key", "vision-key")
    monkeypatch.setattr(settings, "video_gateway_base_url", "https://video.example.com/v1")
    monkeypatch.setattr(settings, "video_gateway_api_key", "video-key")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://prompt.example.com/antigravity")
    monkeypatch.setattr(settings, "anthropic_auth_token", "prompt-key")

    with SessionLocal() as db:
        for row in db.query(ModelConfig).all():
            row.base_url = None
            row.api_key_encrypted = None
        try:
            validate_model_gateway_rows(db)
        finally:
            db.rollback()


@pytest.mark.parametrize(
    ("base_url", "auth_token"),
    [
        ("https://prompt.example.com/antigravity", ""),
        ("", "prompt-key"),
    ],
)
def test_production_rejects_partial_anthropic_env_gateway(
    client,
    monkeypatch,
    base_url,
    auth_token,
):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "anthropic_base_url", base_url)
    monkeypatch.setattr(settings, "anthropic_auth_token", auth_token)

    with SessionLocal() as db:
        prompt = db.query(ModelConfig).filter(ModelConfig.use == "prompt").one()
        prompt.base_url = None
        prompt.api_key_encrypted = None
        try:
            with pytest.raises(
                RuntimeError,
                match="ANTHROPIC_BASE_URL 和 ANTHROPIC_AUTH_TOKEN 必须同时配置",
            ):
                validate_model_gateway_rows(db)
        finally:
            db.rollback()


def test_debug_runtime_rejects_anthropic_vision_row(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", True)

    with SessionLocal() as db:
        vision = db.query(ModelConfig).filter(ModelConfig.use == "vision").one()
        vision.provider = "anthropic"
        vision.gateway_format = "anthropic"
        try:
            with pytest.raises(RuntimeError, match="视觉反推不支持 Anthropic 原生协议"):
                validate_model_gateway_rows(db)
        finally:
            db.rollback()


def test_admin_model_config_empty_key_keeps_existing_secret(client, make_user, auth):
    make_user("13900001002", balance=1000, admin=True)
    h = auth("13900001002")
    body = {
        "use": "vision",
        "provider": "custom_openai",
        "base_url": "https://vision.example.com/v1",
        "gateway_format": "openai",
        "model_id": "vision-a",
        "cost_credits": 1,
        "unlock_cost": 0,
        "enabled": True,
        "admin_password": "pass123456",
    }
    r = client.put("/api/admin/models", json={**body, "api_key": "first-key"}, headers=h)
    assert r.status_code == 200, r.text
    r = client.put("/api/admin/models", json={**body, "model_id": "vision-b"}, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        row = db.query(ModelConfig).filter(ModelConfig.use == "vision").one()
        assert row.model_id == "vision-b"
        assert decrypt_row_api_key(row) == "first-key"
    finally:
        db.close()


def test_admin_model_config_audit_records_before_after_without_secret(client, make_user, auth):
    make_user("13900001008", balance=1000, admin=True)
    h = auth("13900001008")
    body = {
        "use": "image",
        "provider": "openai",
        "base_url": "https://audit-old.example.com/v1",
        "gateway_format": "openai",
        "api_key": "audit-old-secret",
        "model_id": "model-old",
        "cost_credits": 5,
        "unlock_cost": 2,
        "enabled": True,
        "admin_password": "pass123456",
    }
    first = client.put("/api/admin/models", json=body, headers=h)
    assert first.status_code == 200, first.text

    changed = client.put(
        "/api/admin/models",
        json={
            **body,
            "base_url": "https://audit-new.example.com/v1",
                "api_key": "audit-new-secret",
                "model_id": "model-new",
                "cost_credits": 8,
                "enabled": True,
            "extra": {"internal_note": "sensitive-extra-value"},
        },
        headers=h,
    )
    assert changed.status_code == 200, changed.text

    audit_log = client.get("/api/admin/audit?action=update_model", headers=h)
    assert audit_log.status_code == 200, audit_log.text
    assert "audit-old-secret" not in audit_log.text
    assert "audit-new-secret" not in audit_log.text
    assert "sensitive-extra-value" not in audit_log.text
    detail = audit_log.json()[0]["detail"]
    assert detail["api_key_changed"] is True
    assert detail["before"]["model_id"] == "model-old"
    assert detail["before"]["base_url"] == "https://audit-old.example.com/v1"
    assert detail["before"]["api_key_configured"] is True
    assert detail["after"]["model_id"] == "model-new"
    assert detail["after"]["base_url"] == "https://audit-new.example.com/v1"
    assert detail["after"]["cost_credits"] == 8
    assert detail["after"]["enabled"] is True
    assert detail["after"]["extra_keys"] == ["internal_note"]


def test_admin_model_config_rejects_gateway_identity_change_without_new_key(client, make_user, auth):
    make_user("13900001005", balance=1000, admin=True)
    h = auth("13900001005")
    body = {
        "use": "image",
        "provider": "openai",
        "base_url": "https://saved.example.com/v1",
        "gateway_format": "openai",
        "model_id": "gpt-image-2",
        "cost_credits": 5,
        "unlock_cost": 5,
        "enabled": True,
        "admin_password": "pass123456",
    }
    first = client.put("/api/admin/models", json={**body, "api_key": "saved-key"}, headers=h)
    assert first.status_code == 200, first.text

    changed = client.put(
        "/api/admin/models",
        json={**body, "base_url": "https://other.example.com/v1"},
        headers=h,
    )
    assert changed.status_code == 400
    assert "重新输入 API Key" in changed.text

    changed_with_key = client.put(
        "/api/admin/models",
        json={**body, "base_url": "https://other.example.com/v1", "api_key": "new-key"},
        headers=h,
    )
    assert changed_with_key.status_code == 200, changed_with_key.text


def test_admin_model_config_rejects_partial_gateway_config(client, make_user, auth):
    make_user("13900001004", balance=1000, admin=True)
    h = auth("13900001004")
    db = SessionLocal()
    try:
        row = db.query(ModelConfig).filter(ModelConfig.use == "image").one()
        row.provider = None
        row.base_url = None
        row.api_key_encrypted = None
        row.gateway_format = None
        db.commit()
    finally:
        db.close()
    body = {
        "use": "image",
        "provider": "openai",
        "gateway_format": "openai",
        "model_id": "gpt-image-2",
        "cost_credits": 5,
        "unlock_cost": 5,
        "enabled": True,
        "admin_password": "pass123456",
    }

    only_base = client.put(
        "/api/admin/models",
        json={**body, "base_url": "https://api.openai.com/v1"},
        headers=h,
    )
    assert only_base.status_code == 400
    assert "同时配置" in only_base.text

    only_key = client.put(
        "/api/admin/models",
        json={**body, "api_key": "sk-model-secret"},
        headers=h,
    )
    assert only_key.status_code == 400
    assert "同时配置" in only_key.text


def test_admin_model_config_blocks_gateway_change_with_active_tasks(client, make_user, auth):
    uid = make_user("13900001007", balance=1000, admin=True)
    h = auth("13900001007")
    body = {
        "use": "image",
        "provider": "openai",
        "base_url": "https://saved.example.com/v1",
        "gateway_format": "openai",
        "api_key": "saved-key",
        "model_id": "gpt-image-2",
        "cost_credits": 5,
        "unlock_cost": 5,
        "enabled": True,
        "admin_password": "pass123456",
    }
    first = client.put("/api/admin/models", json=body, headers=h)
    assert first.status_code == 200, first.text

    db = SessionLocal()
    try:
        t = GenTask(
            user_id=uid,
            category="image",
            stage="preview",
            model_use="image",
            prompt={"final_text": "queued"},
            params={},
            status="queued",
            cost_frozen=5,
        )
        db.add(t)
        db.commit()
        tid = t.id
    finally:
        db.close()

    changed = client.put(
        "/api/admin/models",
        json={**body, "api_key": "new-key"},
        headers=h,
    )
    assert changed.status_code == 409
    assert "仍有排队" in changed.text

    price_only = client.put(
        "/api/admin/models",
        json={**body, "api_key": "", "cost_credits": 8},
        headers=h,
    )
    assert price_only.status_code == 200, price_only.text

    db = SessionLocal()
    try:
        task = db.get(GenTask, tid)
        task.status = "failed"
        db.commit()
    finally:
        db.close()

    changed_after_finish = client.put(
        "/api/admin/models",
        json={**body, "api_key": "new-key"},
        headers=h,
    )
    assert changed_after_finish.status_code == 200, changed_after_finish.text


def test_admin_probe_models_uses_unsaved_or_saved_key(client, make_user, auth, monkeypatch):
    make_user("13900001003", balance=1000, admin=True)
    h = auth("13900001003")
    seen = []

    def fake_list_models(cfg):
        seen.append((cfg.base_url, cfg.api_key, cfg.gateway_format))
        return [{"id": "model-a"}, {"id": "model-b", "owned_by": "provider"}]

    monkeypatch.setattr(gateway, "list_models", fake_list_models)
    r = client.post(
        "/api/admin/models/probe",
        json={
            "use": "image",
            "provider": "openai",
            "base_url": "https://probe.example.com/v1",
            "api_key": "probe-key",
            "gateway_format": "openai",
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["models"]] == ["model-a", "model-b"]
    assert seen[-1] == ("https://probe.example.com/v1", "probe-key", "openai")

    save = client.put(
        "/api/admin/models",
        json={
            "use": "image",
            "provider": "openai",
            "base_url": "https://saved.example.com/v1",
            "api_key": "saved-key",
            "gateway_format": "openai",
            "model_id": "model-a",
            "cost_credits": 5,
            "unlock_cost": 5,
            "enabled": True,
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert save.status_code == 200, save.text
    r = client.post(
        "/api/admin/models/probe",
        json={"use": "image", "admin_password": "pass123456"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert seen[-1] == ("https://saved.example.com/v1", "saved-key", "openai")


def test_admin_probe_models_allows_logged_in_admin_with_new_api_key(client, make_user, auth, monkeypatch):
    make_user("13900001008", balance=1000, admin=True)
    h = auth("13900001008")
    called = False

    def fake_list_models(cfg):
        nonlocal called
        called = True
        return [{"id": "model-a"}]

    monkeypatch.setattr(gateway, "list_models", fake_list_models)
    r = client.post(
        "/api/admin/models/probe",
        json={
            "use": "image",
            "provider": "openai",
            "base_url": "https://probe.example.com/v1",
            "api_key": "probe-key",
            "gateway_format": "openai",
        },
        headers=h,
    )

    assert r.status_code == 200, r.text
    assert called is True
    assert [m["id"] for m in r.json()["models"]] == ["model-a"]


def test_admin_probe_rejects_saved_key_with_temporary_base_url(client, make_user, auth, monkeypatch):
    make_user("13900001006", balance=1000, admin=True)
    h = auth("13900001006")
    called = False

    def fake_list_models(cfg):
        nonlocal called
        called = True
        return [{"id": "model-a"}]

    monkeypatch.setattr(gateway, "list_models", fake_list_models)
    save = client.put(
        "/api/admin/models",
        json={
            "use": "image",
            "provider": "openai",
            "base_url": "https://saved.example.com/v1",
            "api_key": "saved-key",
            "gateway_format": "openai",
            "model_id": "model-a",
            "cost_credits": 5,
            "unlock_cost": 5,
            "enabled": True,
            "admin_password": "pass123456",
        },
        headers=h,
    )
    assert save.status_code == 200, save.text

    saved = client.post("/api/admin/models/probe", json={"use": "image"}, headers=h)
    assert saved.status_code == 200, saved.text
    assert called is True
    called = False

    leaked = client.post(
        "/api/admin/models/probe",
        json={
            "use": "image",
            "base_url": "https://attacker.example.com/v1",
        },
        headers=h,
    )
    assert leaked.status_code == 400
    assert "不能临时覆盖 Base URL" in leaked.text
    assert called is False
