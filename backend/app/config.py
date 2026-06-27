"""Application configuration.

Secrets (DB, Redis, JWT, gateway api_key, SMS keys) come from environment /
.env.  Model ids and credit costs are stored in the DB (model_configs) so an
admin can change them at runtime without editing code; models.yaml is only used
to seed those rows on first boot.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    app_name: str = "AI Material Studio"
    debug: bool = False
    # Comma separated list of allowed CORS origins for the internal frontend.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # Only these reverse-proxy source IPs are allowed to supply X-Forwarded-For.
    # Leave blank for direct local/dev deployments.
    trusted_proxy_ips: str = ""

    # --- Datastores ---
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/ai_studio"
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth ---
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days
    auth_cookie_name: str = "ai_studio_token"
    # Browser auth uses the HttpOnly cookie. Keep bearer tokens out of login
    # responses in production unless an API-client deployment explicitly opts in.
    auth_bearer_response_enabled: bool = False
    sms_code_ttl_seconds: int = 300  # 5 minutes
    sms_send_cooldown_seconds: int = 60  # min gap between two codes
    sms_send_hourly_limit: int = 5  # max codes / phone / hour
    sms_verify_max_attempts: int = 5  # wrong tries before code is burned

    # --- Model gateway (your existing gateway) ---
    gateway_base_url: str = ""
    gateway_api_key: str = ""
    gateway_timeout_seconds: int = 120
    gateway_max_retries: int = 2
    # Vision reverse-prompt calls can include multiple keyframes and may take
    # longer than normal chat calls, but should remain shorter than rendering.
    reverse_gateway_timeout_seconds: int = 150
    # Image generation can legitimately take several minutes for large outputs.
    # Keep it separate from normal gateway calls so prompt/reverse endpoints do
    # not wait as long as render endpoints.
    image_gateway_timeout_seconds: int = 300
    image_download_timeout_seconds: int = 600
    generated_image_max_bytes: int = 80 * 1024 * 1024
    generated_image_batch_max_bytes: int = 160 * 1024 * 1024
    # Batch image generation is implemented as repeated single-image requests
    # because the current gateway rejects n/tool-count parameters. Run those
    # repeated requests concurrently so n=4/8 does not become a serial queue.
    image_gateway_parallelism: int = 8
    # Extra replacement slots for batch image generation when a sub-request
    # failed before the provider accepted work. Unknown-submit failures are not
    # refilled because that can duplicate upstream renders.
    image_gateway_refill_attempts: int = 4
    # Image render POSTs are non-idempotent for most providers. Do not retry by
    # default after a timeout/5xx: the upstream may have already accepted work.
    image_gateway_max_retries: int = 0
    # When true (or when gateway_api_key is empty) the gateway client returns
    # locally generated placeholder media so the whole flow runs offline.
    mock_mode: bool = False

    # --- Video gateway (may differ from the image/vision gateway,
    #     e.g. Volcengine Ark / Doubao Seedance). Falls back to the main
    #     gateway when left blank. ---
    video_gateway_base_url: str = ""
    video_gateway_api_key: str = ""
    video_gateway_format: str = "ark"  # ark | openai
    video_submit_timeout_seconds: int = 300
    # Async video render polling (a worker stays busy for up to this long per
    # render — tune down, or run a dedicated video worker, under high load).
    video_poll_max_seconds: int = 7200
    video_poll_interval_seconds: int = 5
    video_download_max_attempts: int = 5
    # Provider-rendered videos can be large and remote object storage may be
    # slow after generation completes. Keep the media download timeout wider
    # than image downloads while preserving content-type and size checks.
    video_download_timeout_seconds: int = 3600
    video_download_max_bytes: int = 2 * 1024 * 1024 * 1024

    # Hard ceiling for a single Celery task. Must exceed image render waits and
    # video lifecycle backstops so the worker does not kill valid long renders.
    celery_task_time_limit_seconds: int = 8100

    # --- Storage (local filesystem for the bare-metal MVP) ---
    storage_dir: str = str(BASE_DIR / "storage")
    # Public base url the frontend uses to load media served by this backend.
    public_base_url: str = "http://localhost:8000"

    # --- SMS provider (mock by default; returns code in API response in dev) ---
    sms_provider: str = "mock"  # mock | http
    sms_sign_name: str = ""
    sms_template_code: str = ""
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    # Generic HTTP SMS gateway. Configure this when you have a real SMS service
    # behind an internal/API-gateway endpoint. The endpoint receives JSON:
    # {phone, code, sign_name, template_code}; success is HTTP 2xx.
    sms_http_url: str = ""
    sms_http_api_key: str = ""
    sms_http_timeout_seconds: int = 10

    # --- Misc business rules ---
    parse_cache_minutes: int = 30
    user_gen_rate_per_hour: int = 60  # crude per-user generation rate limit
    user_parse_rate_per_hour: int = 60  # crude per-user parse rate limit
    # Link-scrape resource guards. Playwright spawns a real Chromium per render,
    # so cap how many can run at once process-wide (the rest fail fast rather
    # than pile up browsers / starve the thread pool). The raw-body cap bounds
    # the *compressed* bytes we read so a gzip bomb can't inflate unbounded, and
    # the render-html cap bounds the serialized DOM Playwright hands back.
    parse_playwright_parallelism: int = 2
    parse_playwright_acquire_timeout_seconds: int = 8
    parse_pending_limit: int = 3
    parse_pending_max_age_minutes: int = 30
    parse_max_raw_body_bytes: int = 3_000_000
    parse_max_render_html_bytes: int = 8_000_000
    parse_max_assets: int = 80
    parse_localize_image_max_bytes: int = 20 * 1024 * 1024
    parse_localize_image_max_pixels: int = 50_000_000
    # Upper bound for gateway-produced images before PIL converts/decompresses.
    # Keep it aligned with upload/parse caps so a bad gateway response cannot OOM a worker.
    generated_image_max_pixels: int = 50_000_000
    # Guard rails on user-supplied generation params (reject before hitting the
    # gateway / allocating media, so n=99999 or size=99999x99999 can't OOM us).
    max_image_n: int = 8
    max_image_dim: int = 3840  # OpenAI-compatible image max edge for gpt-image-2 4K
    max_upload_image_bytes: int = 20 * 1024 * 1024
    max_upload_image_pixels: int = 24_000_000
    max_upload_video_bytes: int = 512 * 1024 * 1024
    user_upload_storage_quota_bytes: int = 2 * 1024 * 1024 * 1024
    payment_notify_max_body_bytes: int = 64 * 1024
    payment_order_rate_limit_per_hour: int = 20
    payment_order_pending_limit: int = 5
    # Production SSRF guard for operator-configured egress endpoints. Leave
    # blank unless a gateway really must point at an internal host.
    trusted_egress_hosts: str = ""
    max_video_seconds: int = 15 * 60
    max_prompt_chars: int = 8_000
    max_generate_params_bytes: int = 16_384
    # How many keyframes to sample from a reference video for understanding.
    reverse_video_frames: int = 4
    # Cap concurrent video downloads/ffmpeg jobs used by reverse-video analysis.
    reverse_video_parallelism: int = 2
    reverse_video_acquire_timeout_seconds: int = 2
    # Default image-to-image edit endpoint used in reverse-off (图+指令→图) mode
    # when the image model has no explicit extra.edit_path. Set "" to force plain
    # text->image even with a reference image present.
    image_edit_path: str = "/v1/images/edits"
    asset_retention_days: int = 30  # generated assets kept this long by default
    parse_retention_days: int = 7  # link-parse records kept this long

    # --- Payment / credit recharge ---
    # Local/dev can validate the whole order -> paid -> credit flow without real
    # merchant accounts. Set PAYMENT_MOCK_ENABLED=false in production.
    payment_mock_enabled: bool = False
    payment_order_expire_minutes: int = 30
    payment_reconcile_enabled: bool = True
    payment_reconcile_interval_minutes: int = 10
    payment_reconcile_lookback_hours: int = 24
    payment_reconcile_max_orders: int = 50
    payment_frontend_base_url: str = "http://localhost:3000"
    payment_subject_prefix: str = "造梦 Studio 积分充值"
    # Used to encrypt payment merchant secrets stored from the admin UI. In
    # production set this to a strong random value and keep it outside the DB.
    payment_config_secret: str = ""
    # Used to encrypt model-provider API keys configured from the admin UI.
    # Falls back to PAYMENT_CONFIG_SECRET when left blank for small deployments.
    model_config_secret: str = ""
    # Optional merchant PID/seller id expected in Alipay notify payloads.
    alipay_seller_id: str = ""

    # Alipay face-to-face precreate (QR code). Private/public keys may contain
    # escaped newlines in .env.
    alipay_gateway_url: str = "https://openapi.alipay.com/gateway.do"
    alipay_app_id: str = ""
    alipay_private_key: str = ""
    alipay_public_key: str = ""
    alipay_notify_url: str = ""

    # WeChat Pay API v3 Native payment.
    wechat_pay_gateway_url: str = "https://api.mch.weixin.qq.com"
    wechat_pay_appid: str = ""
    wechat_pay_mchid: str = ""
    wechat_pay_serial_no: str = ""
    wechat_pay_platform_serial_no: str = ""
    wechat_pay_private_key: str = ""
    wechat_pay_api_v3_key: str = ""
    wechat_pay_platform_cert_pem: str = ""
    wechat_pay_notify_url: str = ""

    # --- Observability ---
    sentry_dsn: str = ""  # optional; enables Sentry when set + sdk installed
    # When set, /metrics requires `Authorization: Bearer <token>` (so the metrics
    # endpoint isn't world-readable on a published API port). Empty = open (dev).
    metrics_token: str = ""

    # --- Online update ---
    # Disabled by default because Docker/images usually do not run from a real Git
    # checkout. Enable explicitly on a server with a fixed remote/branch/apply
    # command.
    online_update_enabled: bool = False
    online_update_repo_dir: str = str(BASE_DIR.parent)
    online_update_remote: str = "https://github.com/yinyue111/DAIstudio.git"
    online_update_branch: str = "main"
    # For private GitHub repositories, set a fine-grained PAT with read-only
    # Contents access. It is passed to git through transient env config, never
    # embedded in the remote URL.
    online_update_github_token: str = ""
    online_update_apply_command: str = ""
    online_update_timeout_seconds: int = 600
    online_update_allow_dirty: bool = False
    # Development/test escape hatch for local bare repositories. Production
    # should keep this false so a named remote cannot hide a local file path.
    online_update_allow_local_remote: bool = False

    @field_validator("cors_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def trusted_proxy_ip_list(self) -> list[str]:
        return [o.strip() for o in self.trusted_proxy_ips.split(",") if o.strip()]

    @property
    def trusted_egress_host_list(self) -> list[str]:
        return [o.strip().lower() for o in self.trusted_egress_hosts.split(",") if o.strip()]

    @property
    def effective_mock_mode(self) -> bool:
        return self.mock_mode or not self.gateway_api_key or not self.gateway_base_url

    # Video gateway resolves to its own creds when set, else the main gateway.
    @property
    def video_base(self) -> str:
        return self.video_gateway_base_url or (self.gateway_base_url if self.video_gateway_format == "openai" else "")

    @property
    def video_key(self) -> str:
        return self.video_gateway_api_key or (self.gateway_api_key if self.video_gateway_format == "openai" else "")

    @property
    def effective_video_mock(self) -> bool:
        return self.mock_mode or not self.video_base or not self.video_key


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_models_yaml() -> dict:
    """Seed data for model_configs + defaults. Safe if the file is missing."""
    path = BASE_DIR / "models.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


settings = get_settings()
