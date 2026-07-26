"""FastAPI entrypoint."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import observability
from .config import settings
from .db import SessionLocal, engine
from .observability import RequestContextMiddleware, init_sentry, setup_logging
from .redis_client import redis_client
from .routers import (
    admin,
    assets,
    auth,
    catalog,
    generate,
    me,
    parse,
    payments,
    profile,
    projects,
    prompt,
    prompt_optimizations,
    prompts,
    recipes,
    reproduction_assessments,
    subject_protection,
    task_center,
    tasks,
    uploads,
    v2,
    workflows,
    ws,
)
from .routers import (
    config as config_router,
)
from .runtime_config import validate_model_gateway_rows, validate_runtime_config
from .services.config_store import seed_from_yaml
from .services.gateway_config_errors import gateway_config_error_contract
from .services.model_gateway_config import ModelGatewayConfigError
from .services.payment_config import seed_defaults as seed_payment_defaults
from .services.product_edition import api_path_enabled
from .services.workflow_node_adapters import register_production_workflow_adapters

setup_logging()
log = logging.getLogger("main")
register_production_workflow_adapters()


class BodyTooLargeError(Exception):
    pass


class ProductEditionMiddleware:
    """Return 404 for public API prefixes whose owning feature switch is off.

    Resolution lives in services.product_edition: explicit FEATURE_*_ENABLED
    settings win, otherwise PRODUCT_EDITION supplies the default.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or api_path_enabled(str(scope.get("path") or "")):
            await self.app(scope, receive, send)
            return
        await BodySizeLimitMiddleware._send_plain(send, 404, "not found")


_SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _header_value(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin1")
    return ""


def _normalise_origin(value: str) -> str | None:
    if not value:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _csrf_allowed_origins() -> set[str]:
    origins = set()
    for raw in [
        *settings.cors_origin_list,
        *settings.csrf_allowed_origin_list,
        settings.public_base_url,
    ]:
        origin = _normalise_origin(raw)
        if origin:
            origins.add(origin)
    return origins


class CsrfOriginMiddleware:
    """Origin-check cookie-authenticated state-changing requests.

    The frontend now uses an HttpOnly cookie, so a cross-site form/fetch could
    otherwise ride the browser's cookie. Bearer API clients are unaffected.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "GET").upper()
        if method in _SAFE_HTTP_METHODS:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path in {"/api/payments/alipay/notify", "/api/payments/wechat/notify"}:
            await self.app(scope, receive, send)
            return
        origin = _normalise_origin(_header_value(scope, b"origin"))
        if not origin:
            origin = _normalise_origin(_header_value(scope, b"referer"))
        if path in {"/api/auth/login", "/api/auth/register"}:
            if origin and origin not in _csrf_allowed_origins():
                await BodySizeLimitMiddleware._send_plain(send, 403, "CSRF origin check failed")
                return
            await self.app(scope, receive, send)
            return
        cookie = _header_value(scope, b"cookie")
        if f"{settings.auth_cookie_name}=" not in cookie:
            await self.app(scope, receive, send)
            return
        authorization = _header_value(scope, b"authorization").strip().lower()
        if authorization.startswith("bearer "):
            await self.app(scope, receive, send)
            return
        if origin and origin not in _csrf_allowed_origins():
            await BodySizeLimitMiddleware._send_plain(send, 403, "CSRF origin check failed")
            return
        if not origin:
            await BodySizeLimitMiddleware._send_plain(send, 403, "CSRF origin required")
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Count bytes actually read from selected public endpoints.

    Content-Length can be absent or wrong (chunked uploads/callbacks), so the
    guard wraps the ASGI receive channel and aborts once the consumed body
    crosses the route-specific limit.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    def _limit_for_path(self, path: str) -> int | None:
        if path == "/api/uploads/image":
            return int(settings.max_upload_image_bytes) + 1024 * 1024
        if path == "/api/uploads/video":
            return int(settings.max_upload_video_bytes) + 16 * 1024 * 1024
        if path in {"/api/payments/alipay/notify", "/api/payments/wechat/notify"}:
            return int(settings.payment_notify_max_body_bytes)
        if path == "/api/payments/orders":
            return 32 * 1024
        if path.startswith("/api/admin/"):
            return 256 * 1024
        if path in {
            "/api/auth/register",
            "/api/auth/login",
            "/api/auth/sms/send",
            "/api/me/password",
        }:
            return 32 * 1024
        if path in {
            "/api/generate",
            "/api/prompt/reverse",
            "/api/prompt/optimize",
            "/api/parse",
            "/api/subject-protection/preview",
        } or path.startswith("/api/prompt/reverse-operations"):
            return 256 * 1024
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for_path(str(scope.get("path") or ""))
        if not limit:
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers") or []:
            if key.lower() == b"content-length":
                try:
                    declared = int(value.decode("latin1"))
                except ValueError:
                    await self._send_plain(send, 400, "Content-Length 非法")
                    return
                if declared > limit:
                    await self._send_plain(send, 413, "请求体过大")
                    return
                break

        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    raise BodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLargeError:
            await self._send_plain(send, 413, "请求体过大")

    @staticmethod
    async def _send_plain(send: Send, status: int, text: str) -> None:
        body = text.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_config()
    os.makedirs(settings.storage_dir, exist_ok=True)
    db = SessionLocal()
    try:
        seed_from_yaml(db)
        seed_payment_defaults(db)
        validate_model_gateway_rows(db)
    finally:
        db.close()
    init_sentry()
    log.info("startup complete. mock_mode=%s", settings.effective_mock_mode)
    yield


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)


@app.exception_handler(ModelGatewayConfigError)
async def model_gateway_config_error_handler(_request, exc: ModelGatewayConfigError):
    status_code, detail = gateway_config_error_contract(exc)
    return JSONResponse(status_code=status_code, content={"detail": detail})

app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(ProductEditionMiddleware)
app.add_middleware(CsrfOriginMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id", "Deprecation", "Sunset", "Link"],
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

for r in (auth.router, me.router, config_router.router, catalog.router, profile.router, projects.router, parse.router,
          prompt.router, prompt_optimizations.router, prompts.router, reproduction_assessments.router, recipes.router, subject_protection.router, generate.router,
          task_center.router, tasks.router, uploads.router, assets.router,
          payments.router, admin.router, workflows.router, ws.router, v2.router):
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


def _public_readiness(detail: dict) -> dict:
    return {
        "ok": bool(detail.get("ok")),
        "mock_mode": bool(detail.get("mock_mode")),
        "components": {
            name: "ok" if status == "ok" else "error"
            for name, status in (detail.get("components") or {}).items()
        },
    }


@app.get("/api/health")
def health():
    """Public liveness. Keep deployment internals off the internet."""
    return {"ok": True}


@app.get("/api/live")
def live():
    """Process liveness for container orchestration."""
    return {"ok": True}


@app.get("/api/ready")
def ready():
    """Readiness check: app process plus DB/Redis dependencies."""
    detail = _health_detail()
    return JSONResponse(
        content=_public_readiness(detail),
        status_code=200 if detail["ok"] else 503,
    )


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
