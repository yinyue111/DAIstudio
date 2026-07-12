"""Platform settings round-trip + video cost-estimate logic."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.celery_app as celery_module
import app.models  # noqa: F401  (register tables)
from app.celery_app import celery_app
from app.config import Settings, settings
from app.db import Base, SessionLocal
from app.redis_client import celery_redis_url, redis_connection_kwargs
from app.runtime_config import validate_model_gateway_rows, validate_runtime_config
from app.services import config_store, locks
from app.services.generation_model_runtime import model_snapshot
from app.services.generation_pricing import generation_cost_from_snapshot
from app.services.generation_prompts import (
    compact_generation_prompt_text,
    generation_prompt_for_model,
    is_portrait_generation_task,
    portrait_image_negative_prompt,
    product_image_negative_prompt,
)
from app.services.generation_request import estimate_generation_cost, validate_generation_params
from app.services.model_pricing import estimate_credits_from_usage


def make_session():
    _, path = tempfile.mkstemp(suffix=".db")
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_setup_script_creates_local_env_not_production_debug():
    root = Path(__file__).resolve().parents[2]
    setup = (root / "scripts" / "setup.sh").read_text(encoding="utf-8")

    assert '"DEPLOY_ENV=production": "DEPLOY_ENV=local"' in setup
    assert "DEPLOY_ENV=local, DEBUG=true, MOCK_MODE=true" in setup
    assert "chmod 600 .env" in setup


def test_compose_isolates_and_authenticates_redis_and_uses_readiness_probe():
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "/api/ready" in " ".join(services["api"]["healthcheck"]["test"])
    assert "--requirepass" in services["redis"]["command"]
    assert "REDISCLI_AUTH" in " ".join(services["redis"]["healthcheck"]["test"])
    assert services["redis"]["networks"] == ["backend_internal"]
    assert services["frontend"]["networks"] == ["frontend_edge"]
    assert set(services["api"]["networks"]) == {"backend_internal", "frontend_edge"}
    for name in ("worker", "worker_image", "worker_video", "worker_video_download", "worker_parse", "beat"):
        assert services[name]["depends_on"]["redis"]["condition"] == "service_healthy"
        assert set(services[name]["networks"]) == {"backend_internal", "external_access"}
        assert "${POSTGRES_PASSWORD" not in services[name]["environment"]["DATABASE_URL"]
        assert "${REDIS_PASSWORD" not in services[name]["environment"]["REDIS_URL"]
    assert compose["networks"]["backend_internal"]["internal"] is True
    assert compose["networks"]["external_access"] is None
    beat_health = " ".join(services["beat"]["healthcheck"]["test"])
    assert "/proc/1/cmdline" in beat_health
    assert "celery" in beat_health and "beat" in beat_health
    assert "pgrep" not in beat_health


def test_beat_runtime_requires_distributed_lease(monkeypatch):
    monkeypatch.setattr(celery_module, "_validate_worker_runtime_config", lambda: None)
    monkeypatch.setattr(locks, "acquire", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="scheduler.*active"):
        celery_module._validate_beat_runtime_config()


def test_settings_roundtrip():
    db = make_session()
    assert config_store.get_setting(db, "image_n") == 1          # default
    assert config_store.get_setting(db, "reverse_prompt_enabled") is True

    config_store.set_setting(db, "image_n", 6)
    config_store.set_setting(db, "reverse_prompt_enabled", False)
    assert config_store.get_setting(db, "image_n") == 6
    assert config_store.get_setting(db, "reverse_prompt_enabled") is False

    assert config_store.get_setting(db, "unknown_key", "fallback") == "fallback"


@pytest.mark.parametrize("value", [0, -1])
def test_upload_processing_parallelism_rejects_values_below_one(value):
    with pytest.raises(ValidationError, match="upload_processing_parallelism"):
        Settings(_env_file=None, upload_processing_parallelism=value)


@pytest.mark.parametrize("value", [0, -0.1, float("nan"), float("inf"), float("-inf")])
def test_upload_processing_acquire_timeout_requires_positive_finite_value(value):
    with pytest.raises(ValidationError, match="upload_processing_acquire_timeout_seconds"):
        Settings(_env_file=None, upload_processing_acquire_timeout_seconds=value)


def test_upload_processing_limits_accept_minimum_valid_values():
    configured = Settings(
        _env_file=None,
        upload_processing_parallelism=1,
        upload_processing_acquire_timeout_seconds=0.001,
    )
    assert configured.upload_processing_parallelism == 1
    assert configured.upload_processing_acquire_timeout_seconds == 0.001


def test_public_config_exposes_video_reverse_presets(client, make_user, auth):
    make_user("13900000181", balance=100)
    h = auth("13900000181")
    data = client.get("/api/config", headers=h).json()
    presets = data["reverse"]["video_presets"]
    assert data["reverse"]["video_default_preset"] == "standard"
    assert [p["key"] for p in presets] == ["fast", "standard", "fine"]
    assert presets[0]["short_range"] == "4帧"
    assert presets[0]["max_cost"] == 8
    assert presets[1]["long_range"] == "16-24帧"
    assert presets[1]["max_cost"] == 18
    assert presets[2]["max_frames"] == 36
    assert presets[2]["max_cost"] == 32


def test_redis_blocking_client_uses_bounded_pool_settings():
    kwargs = redis_connection_kwargs()

    assert kwargs["max_connections"] >= 1
    assert kwargs["socket_connect_timeout"] > 0
    assert kwargs["socket_timeout"] > 0


def test_redis_password_is_structured_for_client_and_encoded_for_celery(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://cache:6379/2")
    monkeypatch.setattr(settings, "redis_password", "p@ss:/?#[] word")

    assert redis_connection_kwargs()["password"] == "p@ss:/?#[] word"
    assert celery_redis_url() == "redis://:p%40ss%3A%2F%3F%23%5B%5D%20word@cache:6379/2"


def test_redis_acl_username_is_canonicalized_once_for_celery(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://service%40worker@cache:6379/2")
    monkeypatch.setattr(settings, "redis_password", "secret")

    assert redis_connection_kwargs()["password"] == "secret"
    assert celery_redis_url() == "redis://service%40worker:secret@cache:6379/2"


def test_redis_password_rejects_ambiguous_dual_sources():
    with pytest.raises(ValidationError, match="not both"):
        Settings(
            _env_file=None,
            redis_url="redis://:url-secret@localhost:6379/0",
            redis_password="separate-secret",
        )


@pytest.mark.parametrize(
    ("ttl", "refresh"),
    [(0, 1), (3, 0), (-1, 1), (2, 1), (5, 2)],
)
def test_beat_lease_settings_reject_invalid_durations(ttl, refresh):
    with pytest.raises(ValidationError, match="celery beat lease"):
        Settings(
            _env_file=None,
            celery_beat_lease_ttl_seconds=ttl,
            celery_beat_lease_refresh_seconds=refresh,
        )


def test_beat_lease_refreshes_and_releases_owner_token(monkeypatch):
    stop = celery_module.threading.Event()
    refreshed = celery_module.threading.Event()
    calls = []
    monkeypatch.setattr(settings, "celery_beat_lease_refresh_seconds", 1)
    monkeypatch.setattr(settings, "celery_beat_lease_ttl_seconds", 3)

    def refresh(key, token, ttl):
        calls.append((key, token, ttl))
        refreshed.set()
        return True

    monkeypatch.setattr(locks, "refresh", refresh)
    thread = celery_module.threading.Thread(
        target=celery_module._beat_lease_loop,
        kwargs={"stop_event": stop, "token": "owner", "exit_process": lambda _code: None},
    )
    thread.start()
    assert refreshed.wait(timeout=2)
    stop.set()
    thread.join(timeout=2)
    assert calls == [(settings.celery_beat_lease_key, "owner", 3)]


def test_beat_lease_loss_fails_closed(monkeypatch):
    exited = celery_module.threading.Event()
    monkeypatch.setattr(settings, "celery_beat_lease_refresh_seconds", 1)
    monkeypatch.setattr(settings, "celery_beat_lease_ttl_seconds", 3)
    monkeypatch.setattr(locks, "refresh", lambda *_args, **_kwargs: False)
    celery_module._beat_lease_loop(
        stop_event=_ImmediateWaitEvent(),
        token="lost-owner",
        exit_process=lambda code: exited.set() if code == 1 else None,
    )
    assert exited.is_set()


def test_beat_lease_refresh_exception_fails_closed(monkeypatch):
    exited = celery_module.threading.Event()
    monkeypatch.setattr(settings, "celery_beat_lease_refresh_seconds", 1)
    monkeypatch.setattr(settings, "celery_beat_lease_ttl_seconds", 3)
    monkeypatch.setattr(
        locks,
        "refresh",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("redis unavailable")),
    )

    celery_module._beat_lease_loop(
        stop_event=_ImmediateWaitEvent(),
        token="lost-owner",
        exit_process=lambda code: exited.set() if code == 1 else None,
    )

    assert exited.is_set()


def test_beat_lease_loss_exits_real_child_process_with_code_one():
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(backend)}
    code = """
import os
import sys
try:
    import app.celery_app as module
    module.locks.refresh = lambda *_args, **_kwargs: False
    class Immediate:
        def wait(self, _timeout): return False
        def is_set(self): return False
    print('READY', flush=True)
    module._beat_lease_loop(stop_event=Immediate(), token='lost')
except BaseException:
    os._exit(97)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend,
        env=env,
        check=False,
        timeout=10,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "READY"
    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr


def test_beat_lease_stop_wins_race_after_refresh_returns(monkeypatch):
    stop = celery_module.threading.Event()
    refresh_started = celery_module.threading.Event()
    allow_refresh_return = celery_module.threading.Event()
    exited = []

    def refresh(*_args, **_kwargs):
        refresh_started.set()
        assert allow_refresh_return.wait(timeout=2)
        return False

    monkeypatch.setattr(settings, "celery_beat_lease_refresh_seconds", 1)
    monkeypatch.setattr(settings, "celery_beat_lease_ttl_seconds", 3)
    monkeypatch.setattr(locks, "refresh", refresh)
    thread = celery_module.threading.Thread(
        target=celery_module._beat_lease_loop,
        kwargs={"stop_event": stop, "token": "owner", "exit_process": exited.append},
    )
    thread.start()
    assert refresh_started.wait(timeout=2)
    stop.set()
    allow_refresh_return.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert exited == []


class _ImmediateWaitEvent:
    def wait(self, _timeout):
        return False

    def is_set(self):
        return False


def test_beat_lease_start_is_idempotent_and_cleanup_is_token_scoped(monkeypatch):
    celery_module._beat_lease_token = None
    celery_module._beat_lease_thread = None
    acquired = []
    released = []
    monkeypatch.setattr(locks, "acquire", lambda key, ttl: acquired.append((key, ttl)) or "owner-token")
    monkeypatch.setattr(locks, "release", lambda key, token: released.append((key, token)))
    monkeypatch.setattr(celery_module.threading.Thread, "start", lambda _self: None)

    try:
        assert celery_module._start_beat_lease() == "owner-token"
        assert celery_module._start_beat_lease() == "owner-token"
        assert len(acquired) == 1
    finally:
        celery_module._stop_beat_lease()
    assert released == [(settings.celery_beat_lease_key, "owner-token")]


def test_beat_lease_thread_start_failure_releases_owner_token(monkeypatch):
    celery_module._beat_lease_token = None
    celery_module._beat_lease_thread = None
    released = []
    monkeypatch.setattr(locks, "acquire", lambda *_args, **_kwargs: "owner-token")
    monkeypatch.setattr(locks, "release", lambda key, token: released.append((key, token)))
    monkeypatch.setattr(
        celery_module.threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        celery_module._start_beat_lease()
    assert celery_module._beat_lease_token is None
    assert released == [(settings.celery_beat_lease_key, "owner-token")]


def test_beat_cleanup_does_not_release_while_renew_thread_is_still_running(monkeypatch):
    refresh_started = celery_module.threading.Event()
    allow_refresh_return = celery_module.threading.Event()
    released = []

    def refresh(*_args, **_kwargs):
        refresh_started.set()
        assert allow_refresh_return.wait(timeout=3)
        return True

    monkeypatch.setattr(settings, "celery_beat_lease_refresh_seconds", 1)
    monkeypatch.setattr(settings, "celery_beat_lease_ttl_seconds", 1)
    monkeypatch.setattr(locks, "refresh", refresh)
    monkeypatch.setattr(locks, "release", lambda key, token: released.append((key, token)))
    celery_module._beat_lease_stop.clear()
    thread = celery_module.threading.Thread(
        target=celery_module._beat_lease_loop,
        kwargs={
            "stop_event": celery_module._beat_lease_stop,
            "token": "owner-token",
            "exit_process": lambda _code: None,
        },
    )
    celery_module._beat_lease_token = "owner-token"
    celery_module._beat_lease_thread = thread
    thread.start()
    assert refresh_started.wait(timeout=2)

    celery_module._stop_beat_lease()
    assert thread.is_alive()
    assert released == []
    allow_refresh_return.set()
    thread.join(timeout=2)


def test_nested_settings_accessors_preserve_flat_env_fields(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///nested.db")
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6380/2")
    monkeypatch.setattr(settings, "storage_dir", "/tmp/nested-storage")
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "secret")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "payment_mock_enabled", True)
    monkeypatch.setattr(settings, "online_update_branch", "release")

    assert settings.database.url == settings.database_url
    assert settings.redis.url == settings.redis_url
    assert settings.storage.dir == settings.storage_dir
    assert settings.gateway.base_url == settings.gateway_base_url
    assert settings.gateway.video_base == settings.gateway_base_url
    assert settings.payment.mock_enabled is True
    assert settings.online_update.branch == "release"


def test_celery_shutdown_flag_helper_sets_local_and_redis_flags(monkeypatch):
    calls = {}
    monkeypatch.setattr(celery_module, "_worker_shutting_down", False)

    def fake_setex(key, ttl, value):
        calls["setex"] = (key, ttl, value)

    monkeypatch.setattr(celery_module.redis_client, "setex", fake_setex)

    celery_module.mark_worker_shutting_down(reason="test")

    assert celery_module.is_worker_shutting_down(check_redis=False) is True
    assert calls["setex"][0] == settings.celery.shutdown_flag_key
    assert calls["setex"][1] >= 60
    assert calls["setex"][2] == "test"


def test_production_requires_seeded_model_config_rows(monkeypatch):
    db = make_session()
    monkeypatch.setattr(settings, "debug", False)

    try:
        try:
            validate_model_gateway_rows(db)
        except RuntimeError as exc:
            assert "缺少必需的模型配置行" in str(exc)
            assert "image" in str(exc)
            assert "video" in str(exc)
            assert "vision" in str(exc)
        else:
            raise AssertionError("production runtime should require seeded model_configs rows")
    finally:
        db.close()


def test_production_rejects_default_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "jwt_secret", "change-me-in-production")
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "x" * 32)
    monkeypatch.setattr(settings, "model_config_secret", "")
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "payment_frontend_base_url", "http://localhost:3000")
    monkeypatch.setattr(settings, "gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "sms_provider", "mock")

    try:
        validate_runtime_config()
    except RuntimeError as exc:
        assert "JWT_SECRET 不安全" in str(exc)
    else:
        raise AssertionError("production runtime should reject the default JWT secret")


def _set_valid_production_deploy(monkeypatch):
    monkeypatch.setattr(settings, "deploy_env", "production")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "metrics_token", "strong-metrics-token-123")
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    monkeypatch.setattr(settings, "payment_config_secret", "")
    monkeypatch.setattr(settings, "model_config_secret", "x" * 48)
    monkeypatch.setattr(settings, "gateway_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "gateway_api_key", "sk-test")
    monkeypatch.setattr(settings, "video_gateway_format", "openai")
    monkeypatch.setattr(settings, "video_gateway_base_url", "")
    monkeypatch.setattr(settings, "video_gateway_api_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://studio.example.com")
    monkeypatch.setattr(settings, "payment_frontend_base_url", "https://studio.example.com")
    monkeypatch.setattr(settings, "cors_origins", "https://studio.example.com")
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_http_url", "https://sms.example.com/send")
    monkeypatch.setattr(settings, "trusted_egress_hosts", "")
    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1")
    monkeypatch.setattr(settings, "allow_broad_trusted_proxy_cidr", False)
    monkeypatch.setattr(settings, "online_update_enabled", False)
    monkeypatch.setattr(settings, "online_update_require_signed_commits", False)


def test_deploy_env_production_accepts_hardened_public_config(monkeypatch):
    _set_valid_production_deploy(monkeypatch)

    validate_runtime_config()


def test_deploy_env_production_rejects_debug_localhost_and_mock_sms(monkeypatch):
    _set_valid_production_deploy(monkeypatch)
    monkeypatch.setattr(settings, "debug", True)
    try:
        validate_runtime_config()
    except RuntimeError as exc:
        assert "DEBUG=false" in str(exc)
    else:
        raise AssertionError("production deploy must reject DEBUG=true")

    _set_valid_production_deploy(monkeypatch)
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    try:
        validate_runtime_config()
    except RuntimeError as exc:
        assert "PUBLIC_BASE_URL" in str(exc)
    else:
        raise AssertionError("production deploy must reject localhost public base")

    _set_valid_production_deploy(monkeypatch)
    monkeypatch.setattr(settings, "sms_provider", "mock")
    try:
        validate_runtime_config()
    except RuntimeError as exc:
        assert "SMS_PROVIDER=mock" in str(exc)
    else:
        raise AssertionError("production deploy must reject mock SMS")


def test_deploy_env_production_rejects_broad_trusted_proxy_cidr(monkeypatch):
    _set_valid_production_deploy(monkeypatch)
    monkeypatch.setattr(settings, "trusted_proxy_ips", "127.0.0.1,172.16.0.0/12")

    try:
        validate_runtime_config()
    except RuntimeError as exc:
        assert "TRUSTED_PROXY_IPS" in str(exc)
    else:
        raise AssertionError("production deploy must reject broad trusted proxy CIDRs by default")


def test_deploy_env_production_requires_signed_online_update(monkeypatch):
    _set_valid_production_deploy(monkeypatch)
    monkeypatch.setattr(settings, "online_update_enabled", True)
    monkeypatch.setattr(settings, "online_update_require_signed_commits", False)

    try:
        validate_runtime_config()
    except RuntimeError as exc:
        assert "ONLINE_UPDATE_REQUIRE_SIGNED_COMMITS=true" in str(exc)
    else:
        raise AssertionError("production online update must require signed commits")

    monkeypatch.setattr(settings, "online_update_require_signed_commits", True)
    validate_runtime_config()


def test_public_config_exposes_normalized_feature_flags(client, make_user, auth):
    make_user("13900000182", balance=100)
    db = SessionLocal()
    try:
        config_store.set_setting(db, "reverse_prompt_enabled", "false")
    finally:
        db.close()
    h = auth("13900000182")

    data = client.get("/api/config", headers=h).json()

    assert data["defaults"]["reverse_prompt_enabled"] == "false"
    assert data["features"]["reverse_prompt_enabled"] is False
    db = SessionLocal()
    try:
        config_store.set_setting(db, "reverse_prompt_enabled", True)
    finally:
        db.close()


class _Model:
    def __init__(self, cost, extra):
        self.model_id = "mock"
        self.cost_credits = cost
        self.unlock_cost = 0
        self.extra = extra
        self.enabled = True
        self.use = "image"
        self.provider = None
        self.base_url = None
        self.api_key_encrypted = None
        self.gateway_format = None


def test_video_cost_estimate():
    m = _Model(50, {"preview_cost": 5})
    assert estimate_generation_cost(m, "video", "preview", params={"duration": 5}) == 15
    assert estimate_generation_cost(m, "video", "final", params={"target_resolution": "720p", "target_duration": 10}) == 160
    assert estimate_generation_cost(m, "video", "final", params={"target_resolution": "1080p", "target_duration": 10}) == 280
    assert estimate_generation_cost(m, "image", "preview", 2, params={"size": "1024x1024"}) == 30
    assert estimate_generation_cost(
        m,
        "image",
        "preview",
        2,
        params={"size": "2048x2048", "subject_mode": "product"},
        source_type="image",
    ) == 110


def test_generation_model_snapshot_freezes_dynamic_credit_pricing():
    m = _Model(
        15,
        {
            "credit_pricing": {
                "image": {"1k": 11, "2k": 22, "4k": 44},
                "image_edit": {"1k": 13, "2k": 26, "4k": 52},
                "video_preview_cost": 9,
                "video_per_second": {"480p": 3, "720p": 6, "1080p": 12},
            }
        },
    )
    snapshot = model_snapshot(m)

    assert generation_cost_from_snapshot(
        snapshot,
        category="image",
        stage="preview",
        params={"size": "2048x2048"},
        n=2,
    ) == 44
    assert generation_cost_from_snapshot(
        snapshot,
        category="video",
        stage="final",
        params={"target_resolution": "1080p", "target_duration": 3},
    ) == 36


def test_generation_params_accepts_and_normalizes_variation_source_id():
    params = validate_generation_params(
        "image",
        {
            "n": 1,
            "size": "1024x1024",
            "variation_of_asset_id": "123",
        },
    )

    assert params["variation_of_asset_id"] == 123


def test_product_generation_prompt_uses_reference_style_not_reference_product():
    prompt = {
        "主体": "一瓶 Estee Lauder Advanced Night Repair 精华液",
        "商品服装": "棕色玻璃滴管瓶, Estee Lauder Logo 清晰",
        "材质纹理": "amber glass, dropper bottle, gold cap",
        "场景背景": "暖棕色渐变背景, 金色沙粒台面",
        "广告目标": "高端护肤品社媒广告",
        "风格": "luxury beauty ad, premium texture",
        "构图": "产品居中, 浅景深, 背景大面积留白",
        "光线": "柔和电影感主光, 边缘高光",
        "标签": "Estee Lauder, Advanced Night Repair, skincare bottle, amber glass",
        "final_text": "参考图复刻, 生成 Estee Lauder Advanced Night Repair 棕色滴管瓶广告",
    }

    out = compact_generation_prompt_text(
        prompt,
        {"subject_mode": "product"},
        prompt["final_text"],
        is_product=True,
    )

    assert "上传产品图作为唯一商品主体" in out
    assert "暖棕色渐变背景" in out
    assert "柔和电影感主光" in out
    assert "Estee Lauder" not in out
    assert "Advanced Night Repair" not in out
    assert "dropper bottle" not in out


def test_product_video_prompt_keeps_motion_request_not_reference_product():
    prompt = {
        "主体": "参考视频中的 Estee Lauder 棕色滴管瓶",
        "商品服装": "Advanced Night Repair bottle, gold cap",
        "材质纹理": "amber glass, dropper bottle",
        "场景背景": "暖棕色广告棚景和金色沙粒台面",
        "视角构图": "竖屏 9:16, 产品居中, 浅景深",
        "可迁移主体动作": "镜头从左侧入场，参考商品缓慢旋转，水花飞溅后切到 Logo 特写",
        "主体动作": "参考视频中的 Estee Lauder 产品旋转展示",
        "镜头运动": "缓慢推进并轻微环绕",
        "光线": "柔和电影感主光和边缘高光",
        "标签": "Estee Lauder, Advanced Night Repair, skincare bottle",
        "user_instruction": "产品旋转展示，水花飞溅，镜头推进",
        "final_text": "参考视频复刻, 生成 Estee Lauder Advanced Night Repair 棕色滴管瓶广告",
    }

    out = compact_generation_prompt_text(
        prompt,
        {
            "subject_mode": "product",
            "ratio": "9:16",
            "_category": "video",
            "product_lock_mode": "free",
        },
        prompt["final_text"],
        is_product=True,
    )

    assert "上传产品图作为唯一商品主体" in out
    assert "上传产品替换参考视频" in out
    assert "唯一视频主体" in out
    assert "可迁移动作" in out
    assert "暖棕色广告棚景" in out
    assert "缓慢推进并轻微环绕" in out
    assert "产品旋转展示" in out
    assert "镜头推进" in out
    assert "Estee Lauder" not in out
    assert "Advanced Night Repair" not in out
    assert "dropper bottle" not in out


def test_product_image_prompt_rewrites_reference_subject_and_keeps_complete_product():
    prompt = {
        "产品身份档案": (
            "上传产品是 DAMAH 黑魔法棉柔巾，白色长方体软包，Logo、抽口、顶部标签和包装文字必须完整保留。"
            "产品占画幅55%-75%，替换参考素材原主体。"
        ),
        "场景背景": "户外水边草地；前景为深绿色长草叶，覆盖画面下半部并向右上倾斜；远景为虚化水面。",
        "广告目标": "展示一款名为 KaHi 的 Eau de Toilette 香水，传达清新自然气味联想。",
        "风格": "日系胶片感产品大片、香水广告海报、社媒竖版封面。",
        "构图": "竖版 2:3；瓶身占画面宽度约22%、高度约29%；草叶形成斜向引导线。",
        "景别": "产品近景；香水瓶占画幅面积约7%，草地与水面环境占93%。",
        "视角镜头": "低机位平视略俯，镜头高度接近草尖与瓶身中线。",
        "final_text": "参考图复刻，生成 KaHi 香水水边草地广告。",
    }

    out = compact_generation_prompt_text(
        prompt,
        {"subject_mode": "product"},
        prompt["final_text"],
        is_product=True,
    )

    assert "DAMAH 黑魔法棉柔巾" in out
    assert "完整产品主体入镜" in out
    assert "五成五到七成五" in out
    assert "前景元素只围绕产品底部和边缘" in out
    assert "不得遮挡上传产品包装" in out
    assert "KaHi" not in out
    assert "Eau de Toilette" not in out
    assert "香水" not in out
    assert "瓶身" not in out
    assert "占画幅-" not in out
    assert "适中占比-适中占比" not in out


def test_product_image_negative_prompt_sanitizes_reference_product_terms():
    out = product_image_negative_prompt("KaHi 变形，瓶身比例畸变，文字乱码")

    assert "KaHi" not in out
    assert "瓶身" not in out
    assert "上传产品 变形" in out
    assert "上传产品比例畸变" in out
    assert "产品残缺" in out
    assert "半截产品" in out
    assert "产品被裁切" in out


def test_portrait_image_negative_prompt_merges_fidelity_guards_without_duplicates():
    prompt = (
        "成年女性斜向后仰坐姿，低机位仰拍与近距离透视，前景自然放大；"
        "服装为非婚纱的冰晶有机雕塑结构；左上大面积柔光，暗部保留冷蓝层次；"
        "低对比柔雾与宽泛光晕，抬升黑位，低锐化、非 HDR。"
    )

    out = portrait_image_negative_prompt("多余肢体，主动瘦身", prompt)

    assert "多余肢体" in out
    assert out.count("主动瘦身") == 1
    assert "躯干拉长" in out
    assert "颈部拉长" in out
    assert "腿部拉长" in out
    assert "胸廓-腰线-胯部比例改变" in out
    assert "直立居中姿态" in out
    assert "前景透视丢失" in out
    assert "通用婚纱蕾丝" in out
    assert "硬质影棚光" in out
    assert "平坦阴影" in out
    assert "暗部死黑" in out
    assert "柔雾丢失" in out
    assert "光晕丢失" in out
    assert "HDR" in out
    assert "硬锐化" in out


def test_portrait_image_negative_prompt_does_not_force_reference_specific_style():
    out = portrait_image_negative_prompt(
        "多余肢体",
        "成年职业人像，直立居中，白色婚纱，硬质影棚光，高锐度 HDR。",
    )

    assert "主动瘦身" in out
    assert "胸廓-腰线-胯部比例改变" in out
    for term in (
        "直立居中姿态",
        "前景透视丢失",
        "通用婚纱蕾丝",
        "硬质影棚光",
        "平坦阴影",
        "暗部死黑",
        "柔雾丢失",
        "光晕丢失",
        "HDR",
        "硬锐化",
    ):
        assert term not in out


def test_structured_portrait_task_is_detected_when_reference_is_present():
    task = SimpleNamespace(
        params={
            "n": 1,
            "size": "576x1024",
            "reference_image_url": "http://localhost:8000/media/preview/portrait.png",
        },
        prompt={"图像类型": "人物图", "final_text": "参考图复刻"},
        source_type="image",
        source_asset_url="http://localhost:8000/media/preview/portrait.png",
    )

    assert is_portrait_generation_task(task) is True


def test_structured_portrait_without_reference_is_not_reference_fidelity_task():
    task = SimpleNamespace(
        params={"n": 1, "size": "576x1024"},
        prompt={"图像类型": "人物图", "final_text": "成年职业人像，直立居中，硬质影棚光"},
        source_type="image",
        source_asset_url=None,
    )

    assert is_portrait_generation_task(task) is False


def test_product_video_locked_prompt_rewrites_orbit_and_keeps_product_in_frame():
    prompt = {
        "产品身份档案": "上传产品是 DAMAH 黑魔法棉柔巾, Logo 和包装文字必须完整保留。",
        "场景背景": "冷调棚拍背景",
        "主体动作": "参考商品旋转展示并切到 Logo 特写",
        "镜头运动": "缓慢推进并轻微环绕",
        "user_instruction": "产品旋转展示，水花飞溅，镜头推进",
        "final_text": "参考视频复刻",
    }

    out = compact_generation_prompt_text(
        prompt,
        {"subject_mode": "product", "ratio": "1:1", "_category": "video"},
        prompt["final_text"],
        is_product=True,
    )

    assert "DAMAH 黑魔法棉柔巾" in out
    assert "固定正面" in out
    assert "完整包装、Logo" in out
    assert "避免裁切主体" in out
    assert "缓慢推进并轻微环绕" not in out
    assert "产品旋转展示" not in out
    assert "背景水花或光影点缀且不遮挡包装" in out


def test_video_generation_params_accept_product_video_template():
    params = validate_generation_params(
        "video",
        {
            "duration": 5,
            "resolution": "1080p",
            "ratio": "9:16",
            "subject_mode": "product",
            "product_lock_mode": "locked",
            "product_video_template": "soft_splash",
        },
    )

    assert params["product_video_template"] == "soft_splash"

    try:
        validate_generation_params("video", {"product_video_template": "wild_spin"})
    except HTTPException as e:
        assert e.status_code == 400
        assert "product_video_template" in str(e.detail)
    else:
        raise AssertionError("invalid product video template should be rejected")


def test_product_video_prompt_preserves_uploaded_product_profile():
    prompt = {
        "产品身份档案": (
            "上传产品是唯一商品主角: DAMAH 黑魔法全棉棉柔巾洗脸巾, 正面包装文字包含"
            "黑魔法、全棉棉柔巾、200抽; 黑白包装结构、抽取式开口、Logo和所有文字必须完整保留。"
        ),
        "主体": "参考视频中的 Estee Lauder 棕色滴管瓶",
        "商品服装": "Advanced Night Repair bottle, gold cap",
        "场景背景": "暖棕色广告棚景和金色沙粒台面",
        "可迁移主体动作": "参考商品旋转展示, 切到 Logo 特写",
        "镜头运动": "缓慢推进并轻微环绕",
        "光线": "柔和电影感主光和边缘高光",
        "user_instruction": "产品旋转展示，水花飞溅，镜头推进",
        "final_text": "参考视频复刻, 生成 Estee Lauder Advanced Night Repair 棕色滴管瓶广告",
    }

    out = compact_generation_prompt_text(
        prompt,
        {"subject_mode": "product", "ratio": "9:16", "_category": "video"},
        prompt["final_text"],
        is_product=True,
    )

    assert "DAMAH 黑魔法全棉棉柔巾" in out
    assert "200抽" in out
    assert "所有文字必须完整保留" in out
    assert "上传产品替换参考视频" in out
    assert "Estee Lauder" not in out
    assert "Advanced Night Repair" not in out


def test_product_video_prompt_applies_template_constraint():
    prompt = {
        "产品身份档案": "上传产品是 DAMAH 黑魔法棉柔巾, Logo 和包装文字必须完整保留。",
        "可迁移主体动作": "参考商品入场，水花飞溅",
        "镜头运动": "快速环绕",
        "final_text": "参考视频复刻",
    }

    out = compact_generation_prompt_text(
        prompt,
        {
            "subject_mode": "product",
            "ratio": "9:16",
            "_category": "video",
            "product_video_template": "soft_splash",
        },
        prompt["final_text"],
        is_product=True,
    )

    assert "产品视频模板：轻水花" in out
    assert "不得覆盖Logo、包装文字" in out
    assert "快速环绕" not in out


def test_portrait_generation_prompt_reframes_body_language_safely():
    prompt = {
        "图像类型": "人物图",
        "主体": "成年女性模特, 低机位仰拍, 性感姿态",
        "人像意图": "高级时尚杂志 Lookbook",
        "人物比例": "头身比约 7.5, 肩颈舒展, 腿部被镜头拉长",
        "身材体态": "挑逗姿势, 胸大臀翘, 身体前倾展示身材",
        "体态线条": "S 型姿态, 腰线自然, 腿部线条流畅",
        "服装结构": "剪裁合身的品牌连衣裙, 面料垂坠",
        "服装覆盖": "低胸, 大面积露肤, 湿身诱惑",
        "妆发五官": "自然微笑, 精致妆容, 发型蓬松",
        "场景背景": "明亮摄影棚, 奶白背景",
        "光线": "柔和主光, 高级商业人像光影",
        "user_instruction": "性感姿态, 低机位展示, 低胸, 湿身诱惑",
        "final_text": "性感美女, 胸部特写, 低机位展示, 成人写真",
    }

    out = compact_generation_prompt_text(prompt, {"subject_mode": "portrait"}, prompt["final_text"])

    assert "商业人像" in out
    assert "品牌 Lookbook" in out
    assert "体态线条" in out
    assert "服装结构" in out
    assert "平视或自然时尚摄影视角" in out
    assert "服装覆盖自然得体" in out
    assert "性感" not in out
    assert "挑逗" not in out
    assert "胸大臀翘" not in out
    assert "胸部特写" not in out
    assert "低机位" not in out
    assert "湿身诱惑" not in out
    assert "成人写真" not in out
    assert "三围" not in out


def test_portrait_generation_prompt_preserves_clothed_body_silhouette_without_measurements():
    prompt = {
        "图像类型": "人物图",
        "主体": "成年女性模特，坐姿，身体轻微后仰，面向镜头",
        "人像意图": "幻想时尚 editorial 商业人像",
        "人物比例": "躯干长度、肩宽和头身比例贴近参考图",
        "身材体态": "坐姿重心稳定，肩颈舒展，躯干轻微后仰",
        "身材曲线": "服装覆盖下的胸廓饱满度、自然腰线、胯宽和胸腰胯过渡贴近参考图",
        "体态线条": "整体呈自然 S 形轮廓，不主动瘦身或拉长躯干",
        "服装结构": "合身硬质胸衣完整覆盖上身，骨线和腰封结构清晰",
        "final_text": "成年女性幻想礼服商业人像",
    }

    out = compact_generation_prompt_text(prompt, {}, prompt["final_text"])

    assert "胸廓饱满度" in out
    assert "自然腰线" in out
    assert "胯宽" in out
    assert "不主动瘦身或拉长躯干" in out
    assert "三围" not in out
    assert "罩杯" not in out


def test_portrait_generation_prompt_prefers_meaningful_canonical_final_text():
    canonical = (
        "参考图复刻，成年女性斜向后仰坐姿，躯干因透视自然缩短，保持服装覆盖下饱满胸廓、自然腰线、"
        "较宽胯部与近镜大腿的原始比例；镜头位于腰至大腿高度，约十五度低机位仰拍，35mm 近距离透视；"
        "左前上方大面积柔光，下方珍珠反光，冷紫蓝轮廓光，抬起黑位、低微对比、宽泛光晕与柔雾，低锐化、非 HDR。"
    )
    prompt = {
        "图像类型": "人物图",
        "主体": "成年女性直立居中，身体纤细修长",
        "人物比例": "7.5 头身，长颈，长腿，躯干拉长",
        "光线": "正面硬质影棚光，高反差 HDR，阴影死黑",
        "final_text": canonical,
    }

    out = compact_generation_prompt_text(prompt, {}, canonical)

    assert "斜向后仰坐姿" in out
    assert "宽泛光晕与柔雾" in out
    assert "直立居中" not in out
    assert "7.5 头身" not in out
    assert "正面硬质影棚光" not in out


def test_portrait_generation_prompt_expands_placeholder_final_text_from_structured_fields():
    prompt = {
        "图像类型": "人物图",
        "主体": "成年女性斜向后仰坐姿",
        "身材体态": "胸廓、自然腰线、胯宽和坐姿重心按参考图保持",
        "视角镜头": "腰至大腿高度机位，35mm 近距离透视",
        "光线": "左前上方大面积柔光，下方珍珠反光，冷紫蓝轮廓光",
        "final_text": "参考图复刻",
    }

    out = compact_generation_prompt_text(prompt, {}, prompt["final_text"])

    assert "斜向后仰坐姿" in out
    assert "35mm 近距离透视" in out
    assert "下方珍珠反光" in out
    assert not out.endswith("参考图复刻")


def test_portrait_prompt_sanitization_preserves_visual_parameters_but_removes_body_measurements():
    canonical = (
        "参考图复刻，人物中心位于画面横向 82%、纵向 25%，背景主色 #F2EEF2；"
        "使用低机位仰拍与 35mm 近距离透视，保持前景大腿自然放大、躯干透视缩短；"
        "分析记录中的胸围/腰围/臀围 88cm/60cm/92cm 不得进入生成提示，只保留服装覆盖下的整体轮廓。"
    )
    prompt = {"图像类型": "人物图", "final_text": canonical}

    out = compact_generation_prompt_text(prompt, {}, canonical)

    assert "82%" in out
    assert "25%" in out
    assert "#F2EEF2" in out
    assert "低机位仰拍" in out
    assert "35mm" in out
    assert "胸围" not in out
    assert "腰围" not in out
    assert "臀围" not in out
    assert "88cm" not in out
    assert "60cm" not in out
    assert "92cm" not in out


@pytest.mark.parametrize(
    "measurement",
    [
        "胸围88cm/60cm/92cm",
        "胸围 88cm/腰围 60cm/臀围 92cm",
        "胸围88厘米，腰围60厘米，臀围92厘米",
    ],
)
def test_portrait_prompt_removes_labeled_compact_body_measurements(measurement):
    prompt = {
        "图像类型": "人物图",
        "身材体态": f"服装覆盖下的整体轮廓；分析记录 {measurement} 不得进入生成提示",
        "构图": "人物中心位于画面横向 82%，背景主色 #F2EEF2",
        "视角镜头": "35mm 近距离透视",
        "final_text": "参考图复刻",
    }

    out = compact_generation_prompt_text(prompt, {}, prompt["final_text"])

    assert "胸围" not in out
    assert "腰围" not in out
    assert "臀围" not in out
    assert "88cm" not in out and "88厘米" not in out
    assert "60cm" not in out and "60厘米" not in out
    assert "92cm" not in out and "92厘米" not in out
    assert "35mm" in out
    assert "82%" in out
    assert "#F2EEF2" in out


def test_portrait_generation_prompt_prioritizes_reference_lighting_and_finish_when_long():
    prompt = {
        "图像类型": "人物图",
        "反推重点": "人物比例、礼服结构和原图光影必须优先复刻。" * 18,
        "主体": "成年女性模特坐姿，头部后仰，右手靠近下颌。" * 12,
        "人像意图": "幻想时尚 editorial 商业人像",
        "人物比例": "中近景，躯干和裙摆透视贴近原图。" * 10,
        "身材体态": "肩颈、胸廓、腰线、胯线和坐姿重心贴近原图。" * 10,
        "体态线条": "保持原图自然 S 形体态，不瘦身、不增大、不缩小。" * 10,
        "服装结构": "白色硬质胸衣、蕾丝骨线、透明纱袖和珠光装饰。" * 10,
        "场景背景": "冷蓝黑水晶珍珠背景。" * 15,
        "构图": "9:16 中近景，人物中心偏上。" * 15,
        "光线": "左上四十五度大面积柔光；脸部与胸衣有柔和珠光高光；右侧银白补光；暗部保留冷蓝黑层次。",
        "色调配色": "珍珠白、冷蓝灰和暗蓝黑低饱和配色。",
        "材质纹理": "水晶高折射、胸衣柔润光泽、薄纱半透明散射。",
        "后期质感": "低对比柔雾，高光扩散但不吞没五官和胸衣结构，暗部不死黑。",
        "标签": "portrait, editorial, crystal, fantasy, fashion" * 20,
        "final_text": "参考图复刻",
    }

    out = compact_generation_prompt_text(prompt, {}, prompt["final_text"])

    assert len(out) <= 1500
    assert "左上四十五度大面积柔光" in out
    assert "脸部与胸衣有柔和珠光高光" in out
    assert "暗部保留冷蓝黑层次" in out
    assert "薄纱半透明散射" in out
    assert "低对比柔雾" in out
    assert "高光扩散" in out


def test_model_prompt_infers_portrait_fidelity_from_reverse_image_type():
    task = SimpleNamespace(
        category="image",
        params={"n": 1, "size": "576x1024"},
        prompt={
            "图像类型": "人物图",
            "主体": "成年女性模特坐姿",
            "身材曲线": "服装覆盖下的胸廓饱满度、自然腰线和胯宽贴近原图",
            "光线": "左上大面积柔光，脸部与胸衣高光柔和，暗部保留冷蓝层次",
            "final_text": "参考图复刻",
        },
    )

    out = generation_prompt_for_model(task.prompt["final_text"], task)

    assert "不得主动瘦身" in out
    assert "胸廓饱满度" in out
    assert "脸部与胸衣高光柔和" in out


def test_generation_params_rejects_invalid_variation_source_id():
    try:
        validate_generation_params("image", {"variation_of_asset_id": "bad"})
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "variation_of_asset_id 非法" in str(exc.detail)
    else:
        raise AssertionError("invalid variation source id should be rejected")


def test_legacy_generation_snapshot_preserves_static_price():
    snapshot = {"cost_credits": 5, "extra": {"preview_cost": 2}}

    assert generation_cost_from_snapshot(
        snapshot,
        category="image",
        stage="preview",
        params={"size": "256x256"},
        n=4,
    ) == 20
    assert generation_cost_from_snapshot(
        snapshot,
        category="video",
        stage="preview",
        params={"duration": 5},
    ) == 2


def test_gpt55_official_pricing_usage_estimate():
    extra = {
        "official_pricing": {
            "source": "https://openai.com/api/pricing/",
            "currency": "USD",
            "unit": "per_1m_tokens",
            "input_per_1m": 5.0,
            "cached_input_per_1m": 0.5,
            "output_per_1m": 30.0,
            "usd_to_cny": 7.25,
            "credit_value_cny": 0.10,
        }
    }
    usage = {
        "prompt_tokens": 1_000_000,
        "cached_tokens": 200_000,
        "completion_tokens": 100_000,
    }
    # (800k*5 + 200k*0.5 + 100k*30) / 1M USD = $7.10
    # $7.10 * 7.25 CNY/USD / 0.10 CNY per credit = 514.75 -> ceil 515 credits.
    assert estimate_credits_from_usage(extra, usage) == 515


def test_gpt55_official_pricing_reads_nested_cached_tokens():
    extra = {
        "official_pricing": {
            "source": "https://openai.com/api/pricing/",
            "currency": "USD",
            "unit": "per_1m_tokens",
            "input_per_1m": 5.0,
            "cached_input_per_1m": 0.5,
            "output_per_1m": 30.0,
            "usd_to_cny": 7.25,
            "credit_value_cny": 0.10,
        }
    }
    usage = {
        "prompt_tokens": 1_000_000,
        "prompt_tokens_details": {"cached_tokens": 200_000},
        "completion_tokens": 100_000,
    }
    assert estimate_credits_from_usage(extra, usage) == 515


def test_seedance2_official_pricing_usage_estimate():
    extra = {
        "official_pricing": {
            "source": "https://www.volcengine.com/docs/82379/1544106",
            "provider": "volcengine_ark",
            "model_family": "Seedance 2.0",
            "currency": "CNY",
            "unit": "per_1m_tokens",
            "total_per_1m": 31.0,
            "credit_value_cny": 0.10,
        }
    }
    usage = {"total_tokens": 1_000_000}
    assert estimate_credits_from_usage(extra, usage) == 310


def test_seedance2_official_pricing_rounds_up_fractional_credits():
    extra = {
        "official_pricing": {
            "source": "https://www.volcengine.com/docs/82379/1544106",
            "provider": "volcengine_ark",
            "model_family": "Seedance 2.0",
            "currency": "CNY",
            "unit": "per_1m_tokens",
            "total_per_1m": 31.0,
            "credit_value_cny": 0.10,
        }
    }
    usage = {"total_tokens": 10_000}
    # 10k * 31 / 1M / 0.10 = 3.1 credits -> ceil 4.
    assert estimate_credits_from_usage(extra, usage) == 4


def test_public_config_exposes_video_duration_limit(client, make_user, auth):
    make_user("13900000180", balance=1000)
    h = auth("13900000180")
    r = client.get("/api/config", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["video_duration_max_seconds"] == 15


def test_celery_task_routes_are_split_by_workload():
    router = celery_app.amqp.router
    assert router.route({}, "generate.image", args=(), kwargs={})["queue"].name == "image"
    assert router.route({}, "generate.video", args=(), kwargs={})["queue"].name == "video_submit"
    assert router.route({}, "poll.video", args=(), kwargs={})["queue"].name == "video_poll"
    assert router.route({}, "download.video", args=(), kwargs={})["queue"].name == "video_download"
    assert router.route({}, "parse.url", args=(), kwargs={})["queue"].name == "parse"
    assert router.route({}, "cleanup.reap_stuck", args=(), kwargs={})["queue"].name == "cleanup"
    assert router.route({}, "cleanup.reap_reverse", args=(), kwargs={})["queue"].name == "cleanup"
    assert router.route({}, "payments.reconcile", args=(), kwargs={})["queue"].name == "payment"


def test_reverse_reaper_is_registered_and_scheduled():
    assert "cleanup.reap_reverse" in celery_app.tasks
    assert any(
        entry.get("task") == "cleanup.reap_reverse"
        for entry in celery_app.conf.beat_schedule.values()
    )


def test_redis_and_celery_connection_limits_are_configured():
    redis_kwargs = redis_connection_kwargs()

    assert redis_kwargs["max_connections"] > 0
    assert redis_kwargs["socket_connect_timeout"] > 0
    assert redis_kwargs["socket_timeout"] > 0
    assert redis_kwargs["retry_on_timeout"] is True
    assert celery_app.conf.broker_pool_limit == redis_kwargs["max_connections"]
    assert celery_app.conf.redis_max_connections == redis_kwargs["max_connections"]
    assert celery_app.conf.broker_transport_options["socket_timeout"] == redis_kwargs["socket_timeout"]
    assert celery_app.conf.result_backend_transport_options["retry_on_timeout"] is True
    assert celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit
