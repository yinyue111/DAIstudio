"""Platform settings round-trip + video cost-estimate logic."""
import tempfile
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.celery_app as celery_module
import app.models  # noqa: F401  (register tables)
from app.celery_app import celery_app
from app.config import settings
from app.db import Base, SessionLocal
from app.redis_client import redis_connection_kwargs
from app.runtime_config import validate_model_gateway_rows, validate_runtime_config
from app.services import config_store
from app.services.generation_model_runtime import model_snapshot
from app.services.generation_pricing import generation_cost_from_snapshot
from app.services.generation_prompts import (
    compact_generation_prompt_text,
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


def test_settings_roundtrip():
    db = make_session()
    assert config_store.get_setting(db, "image_n") == 1          # default
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


def test_redis_blocking_client_uses_bounded_pool_settings():
    kwargs = redis_connection_kwargs()

    assert kwargs["max_connections"] >= 1
    assert kwargs["socket_connect_timeout"] > 0
    assert kwargs["socket_timeout"] > 0


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
    assert router.route({}, "payments.reconcile", args=(), kwargs={})["queue"].name == "payment"


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
