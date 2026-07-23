from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import io
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from .config import ProviderSettings
from .inference import (
    CAPABILITIES,
    CONTRACT_VERSION,
    EvidenceEngine,
    TorchvisionEvidenceEngine,
)

log = logging.getLogger("evidence_provider")


class RegionAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["region-analyzer.v1"]
    capability: Literal["detector", "segmenter"]
    image_base64: str = Field(min_length=4, max_length=45_000_000)


def _decode_image(value: str, settings: ProviderSettings) -> Image.Image:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "image_base64 is not valid base64") from exc
    if not raw or len(raw) > settings.max_image_bytes:
        raise HTTPException(413, "image exceeds the configured byte limit")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(
            422, "image_base64 does not contain a supported image"
        ) from exc
    if (
        image.width <= 0
        or image.height <= 0
        or image.width * image.height > settings.max_image_pixels
    ):
        raise HTTPException(413, "image exceeds the configured pixel limit")
    return image.convert("RGB")


def _authorization_dependency(settings: ProviderSettings):
    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not settings.api_key:
            return
        scheme, _, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token, settings.api_key
        ):
            raise HTTPException(401, "invalid evidence provider credentials")

    return authorize


def _health_payload(
    engine: EvidenceEngine, capability: str
) -> tuple[dict[str, Any], int]:
    state = engine.health(capability)
    available = state.get("status") == "available"
    payload = {
        "contract_version": CONTRACT_VERSION,
        "capability": capability,
        "status": "ready" if available else "degraded",
        "ok": available,
        "analyzer": state.get("analyzer") or f"torchvision_{capability}",
        "analyzer_version": state.get("analyzer_version") or "unavailable",
        "degraded_reason": None if available else state.get("degraded_reason"),
    }
    return payload, 200 if available else 503


def create_app(
    *,
    settings: ProviderSettings | None = None,
    engine: EvidenceEngine | None = None,
) -> FastAPI:
    resolved_settings = settings or ProviderSettings.from_env()
    resolved_engine = engine or TorchvisionEvidenceEngine(resolved_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if resolved_settings.eager_load:
            await asyncio.to_thread(resolved_engine.warmup)
        yield

    app = FastAPI(
        title="AI Media Evidence Provider",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    authorize = _authorization_dependency(resolved_settings)

    @app.get("/health", include_in_schema=False)
    def health(_: None = Depends(authorize)) -> JSONResponse:
        states = {
            capability: resolved_engine.health(capability)
            for capability in CAPABILITIES
        }
        ready = all(state.get("status") == "available" for state in states.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "contract_version": CONTRACT_VERSION,
                "status": "ready" if ready else "degraded",
                "ok": ready,
                "capability_statuses": {
                    capability: state.get("status")
                    for capability, state in states.items()
                },
            },
        )

    @app.get("/health/{capability}", include_in_schema=False)
    def capability_health(
        capability: Literal["detector", "segmenter"],
        _: None = Depends(authorize),
    ) -> JSONResponse:
        payload, status_code = _health_payload(resolved_engine, capability)
        return JSONResponse(status_code=status_code, content=payload)

    @app.post("/v1/region/analyze")
    def analyze(
        body: RegionAnalyzeRequest, _: None = Depends(authorize)
    ) -> dict[str, Any]:
        image = _decode_image(body.image_base64, resolved_settings)
        try:
            evidence = resolved_engine.analyze(image, body.capability)
            state = resolved_engine.health(body.capability)
            return {
                "contract_version": CONTRACT_VERSION,
                "capability": body.capability,
                "status": "analyzed",
                "analyzer": state.get("analyzer") or f"torchvision_{body.capability}",
                "analyzer_version": state.get("analyzer_version") or "unknown",
                "degraded_reason": None,
                "evidence": evidence,
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("%s inference failed", body.capability)
            return {
                "contract_version": CONTRACT_VERSION,
                "capability": body.capability,
                "status": "degraded",
                "analyzer": f"torchvision_{body.capability}",
                "analyzer_version": "unavailable",
                "degraded_reason": f"{type(exc).__name__}: inference failed",
                "evidence": [],
            }

    return app


app = create_app()
