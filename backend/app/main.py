"""FastAPI entrypoint."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import observability
from .config import settings
from .db import Base, SessionLocal, engine
from .observability import RequestContextMiddleware, init_sentry, setup_logging
from .redis_client import redis_client
from .routers import (
    admin,
    assets,
    auth,
    generate,
    me,
    parse,
    payments,
    profile,
    prompt,
    tasks,
    uploads,
    ws,
)
from .routers import (
    config as config_router,
)
from .services.config_store import seed_from_yaml
from .services.payment_config import seed_defaults as seed_payment_defaults
from .services.payments import mock_payments_allowed, public_base_is_local
from .services.sms import implemented_providers as implemented_sms_providers

setup_logging()
log = logging.getLogger("main")


# Known placeholder / weak secrets that must never reach production. Includes the
# .env.example sample so "copy example + set DEBUG=false" still refuses to boot.
_INSECURE_JWT_SECRETS = {
    "",
    "change-me-in-production",
    "please-change-me-to-a-long-random-string",
}
_MIN_JWT_SECRET_LEN = 32


def validate_runtime_config() -> None:
    if not public_base_is_local() and settings.debug:
        raise RuntimeError("PUBLIC_BASE_URL 非本地域名时禁止 DEBUG=true")
    if not settings.debug and settings.sms_provider == "mock":
        raise RuntimeError("生产环境(DEBUG=false)禁止 SMS_PROVIDER=mock")
    if not settings.debug and settings.sms_provider not in implemented_sms_providers():
        raise RuntimeError(
            "生产环境(DEBUG=false)仅支持已实现的 SMS_PROVIDER="
            f"{','.join(sorted(implemented_sms_providers() - {'mock'}))}"
        )
    # Production must never silently fall back to local placeholder generations.
    if not settings.debug and (settings.mock_mode or not settings.gateway_base_url or not settings.gateway_api_key):
        raise RuntimeError("生产环境(DEBUG=false)必须配置真实 GATEWAY_BASE_URL/GATEWAY_API_KEY 且 MOCK_MODE=false")
    # Fail fast: a production deploy must not run with a default/weak JWT secret,
    # otherwise anyone can forge tokens for any user (incl. admin).
    if not settings.debug and (
        settings.jwt_secret in _INSECURE_JWT_SECRETS
        or len(settings.jwt_secret) < _MIN_JWT_SECRET_LEN
    ):
        raise RuntimeError(
            "JWT_SECRET 不安全(为占位值或过短)。生产环境(DEBUG=false)拒绝启动,"
            f"请设置长度 >= {_MIN_JWT_SECRET_LEN} 的强随机密钥"
            "(如 python -c \"import secrets;print(secrets.token_urlsafe(48))\")。"
        )
    if not settings.debug and observability.metrics_enabled and not settings.metrics_token:
        raise RuntimeError("生产环境(DEBUG=false)启用 /metrics 时必须设置 METRICS_TOKEN")
    if not settings.debug and settings.payment_mock_enabled:
        raise RuntimeError("生产环境(DEBUG=false)必须设置 PAYMENT_MOCK_ENABLED=false")
    if settings.payment_mock_enabled and not mock_payments_allowed():
        raise RuntimeError(
            "PAYMENT_MOCK_ENABLED=true 仅允许 PUBLIC_BASE_URL 和 "
            "PAYMENT_FRONTEND_BASE_URL 同时为 localhost/127.0.0.1"
        )
    if settings.payment_config_secret and len(settings.payment_config_secret) < 32:
        raise RuntimeError("PAYMENT_CONFIG_SECRET 过短,请使用长度 >= 32 的强随机密钥")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_config()
    os.makedirs(settings.storage_dir, exist_ok=True)
    # Dev convenience only: auto-create tables. Production (DEBUG=false) must use
    # Alembic (alembic upgrade head) — never let the app silently build schema.
    if settings.debug:
        Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_from_yaml(db)
        seed_payment_defaults(db)
    finally:
        db.close()
    init_sentry()
    log.info("startup complete. mock_mode=%s", settings.effective_mock_mode)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id"],
)

# Only low-risk previews are public. HD images and final videos stay behind
# /api/assets/{id}/download, which checks ownership + unlock state.
os.makedirs(settings.storage_dir, exist_ok=True)
for public_subdir, mount_path in (
    ("preview", "/media/preview"),
    ("video_preview", "/media/video_preview"),
):
    os.makedirs(os.path.join(settings.storage_dir, public_subdir), exist_ok=True)
    app.mount(
        mount_path,
        StaticFiles(directory=os.path.join(settings.storage_dir, public_subdir)),
        name=f"media_{public_subdir}",
    )

for r in (auth.router, me.router, config_router.router, profile.router, parse.router,
          prompt.router, generate.router, tasks.router, uploads.router, assets.router,
          payments.router, admin.router, ws.router):
    app.include_router(r)


def _health_detail() -> dict:
    components: dict[str, str] = {}
    ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        components["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        ok = False
        components["db"] = f"error: {str(e)[:80]}"
    try:
        redis_client.ping()
        components["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        ok = False
        components["redis"] = f"error: {str(e)[:80]}"
    return {"ok": ok, "mock_mode": settings.effective_mock_mode, "components": components}


@app.get("/api/health")
def health():
    """Public liveness. Keep deployment internals off the internet."""
    return {"ok": _health_detail()["ok"]}


@app.get("/api/health/detail")
def health_detail(authorization: str | None = Header(default=None)):
    token = settings.metrics_token
    if token:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if supplied != token:
            raise HTTPException(401, "health unauthorized")
    elif not settings.debug:
        raise HTTPException(404, "health detail not enabled")
    return _health_detail()


@app.get("/metrics")
def metrics(authorization: str | None = Header(default=None)):
    """Prometheus metrics. Guarded by METRICS_TOKEN (Bearer) when configured, so
    it isn't world-readable on a published API port."""
    if not observability.metrics_enabled:
        raise HTTPException(404, "metrics not enabled")
    token = settings.metrics_token
    if not token:
        if not settings.debug:
            raise HTTPException(404, "metrics not enabled")
    else:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if supplied != token:
            raise HTTPException(401, "metrics unauthorized")
    body, content_type = observability.render_metrics()
    return Response(content=body, media_type=content_type)
