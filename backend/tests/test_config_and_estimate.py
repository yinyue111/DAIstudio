"""Platform settings round-trip + video cost-estimate logic."""
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (register tables)
from app.celery_app import celery_app
from app.db import Base, SessionLocal
from app.routers.generate import _estimate_cost
from app.services import config_store
from app.services.generation_model_runtime import model_snapshot
from app.services.generation_pricing import generation_cost_from_snapshot
from app.services.model_pricing import estimate_credits_from_usage


def make_session():
    _, path = tempfile.mkstemp(suffix=".db")
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_settings_roundtrip():
    db = make_session()
    assert config_store.get_setting(db, "image_n") == 4          # default
    assert config_store.get_setting(db, "reverse_prompt_enabled") is True

    config_store.set_setting(db, "image_n", 6)
    config_store.set_setting(db, "reverse_prompt_enabled", False)
    assert config_store.get_setting(db, "image_n") == 6
    assert config_store.get_setting(db, "reverse_prompt_enabled") is False

    assert config_store.get_setting(db, "unknown_key", "fallback") == "fallback"


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
    assert _estimate_cost(m, "video", "preview", params={"duration": 5}) == 15
    assert _estimate_cost(m, "video", "final", params={"target_resolution": "720p", "target_duration": 10}) == 160
    assert _estimate_cost(m, "video", "final", params={"target_resolution": "1080p", "target_duration": 10}) == 280
    assert _estimate_cost(m, "image", "preview", 2, params={"size": "1024x1024"}) == 30
    assert _estimate_cost(
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
    assert r.json()["video_duration_max_seconds"] == 900


def test_celery_task_routes_are_split_by_workload():
    router = celery_app.amqp.router
    assert router.route({}, "generate.image", args=(), kwargs={})["queue"].name == "image"
    assert router.route({}, "generate.video", args=(), kwargs={})["queue"].name == "video_submit"
    assert router.route({}, "poll.video", args=(), kwargs={})["queue"].name == "video_poll"
    assert router.route({}, "download.video", args=(), kwargs={})["queue"].name == "video_download"
    assert router.route({}, "parse.url", args=(), kwargs={})["queue"].name == "parse"
    assert router.route({}, "cleanup.reap_stuck", args=(), kwargs={})["queue"].name == "cleanup"
    assert router.route({}, "payments.reconcile", args=(), kwargs={})["queue"].name == "payment"
