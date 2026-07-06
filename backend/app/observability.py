"""Request-id propagation, structured access logging, optional Sentry."""
from __future__ import annotations

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
log = logging.getLogger("access")

# --- Prometheus metrics (optional; only if prometheus_client is installed) ---
try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _REQ = Counter("http_requests_total", "HTTP requests",
                   ["method", "path", "status"])
    _LAT = Histogram("http_request_duration_seconds", "Request latency",
                     ["method", "path"])
    _PROM = True
except Exception:  # pragma: no cover
    _PROM = False

metrics_enabled = _PROM


def render_metrics() -> tuple[bytes, str]:
    """Render the current metrics exposition (served by a guarded route)."""
    if not _PROM:
        return b"", "text/plain"
    return generate_latest(), CONTENT_TYPE_LATEST

_ID_SEG = re.compile(r"/\d+")


def _norm_path(path: str) -> str:
    # collapse numeric ids to keep label cardinality bounded
    return _ID_SEG.sub("/:id", path)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def current_request_id() -> str:
    return request_id_ctx.get()


def set_request_id(value: str | None):
    return request_id_ctx.set((value or "-").strip() or "-")


def reset_request_id(token) -> None:
    request_id_ctx.reset(token)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
        )
    )
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if settings.debug else logging.INFO)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_ctx.set(rid)
        t0 = time.time()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error %s %s", request.method, request.url.path)
            raise
        try:
            dt = time.time() - t0
            path = request.url.path
            if not path.startswith(("/media", "/metrics")):
                log.info("%s %s -> %s %.0fms", request.method, path,
                         response.status_code, dt * 1000)
            if _PROM and not path.startswith(("/media", "/metrics")):
                np = _norm_path(path)
                _REQ.labels(request.method, np, response.status_code).inc()
                _LAT.labels(request.method, np).observe(dt)
            response.headers["x-request-id"] = rid
            return response
        finally:
            request_id_ctx.reset(token)


def init_sentry() -> bool:
    if not settings.sentry_dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.0)
        return True
    except Exception:  # noqa: BLE001
        log.warning("SENTRY_DSN set but sentry_sdk not installed; skipping")
        return False
