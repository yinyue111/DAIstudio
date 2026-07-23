from __future__ import annotations

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
    ANALYZER,
    CAPABILITIES,
    CONTRACT_VERSION,
    OpenCvSemanticEngine,
    SemanticEngine,
)

log = logging.getLogger("video_semantic_provider")


class FrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_index: int = Field(ge=1)
    absolute_timestamp_seconds: float = Field(ge=0)
    source_segment_index: int = Field(ge=1)
    source_content_hash: str = Field(min_length=1, max_length=128)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    image_media_type: Literal["image/png"]
    image_base64: str = Field(min_length=4, max_length=45_000_000)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["video-semantic-evidence.v1"]
    capabilities: list[Literal["subject_tracking", "pose", "action", "transition"]]
    frames: list[FrameRequest] = Field(min_length=1, max_length=64)


def _decode_frame(frame: FrameRequest, settings: ProviderSettings) -> dict[str, Any]:
    try:
        raw = base64.b64decode(frame.image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "frame image_base64 is not valid base64") from exc
    if not raw or len(raw) > settings.max_frame_bytes:
        raise HTTPException(413, "frame exceeds the configured byte limit")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(422, "frame image_base64 does not contain a PNG") from exc
    if image.format != "PNG":
        raise HTTPException(422, "frame image_media_type must match PNG bytes")
    if image.width != frame.width or image.height != frame.height:
        raise HTTPException(422, "frame width and height must match decoded image")
    if image.width * image.height > settings.max_frame_pixels:
        raise HTTPException(413, "frame exceeds the configured pixel limit")
    import numpy as np

    return {
        "frame_index": frame.frame_index,
        "absolute_timestamp_seconds": frame.absolute_timestamp_seconds,
        "source_segment_index": frame.source_segment_index,
        "source_content_hash": frame.source_content_hash,
        "image": np.asarray(image.convert("RGB")),
    }


def _validate_request(
    body: AnalyzeRequest, settings: ProviderSettings
) -> list[dict[str, Any]]:
    if tuple(body.capabilities) != CAPABILITIES or len(set(body.capabilities)) != len(
        CAPABILITIES
    ):
        raise HTTPException(
            422,
            "capabilities must be the complete ordered video-semantic-evidence.v1 set",
        )
    if len(body.frames) > settings.max_frames:
        raise HTTPException(413, "request exceeds the configured frame limit")
    identities: set[tuple[int, int]] = set()
    decoded = []
    for frame in body.frames:
        identity = (frame.source_segment_index, frame.frame_index)
        if identity in identities:
            raise HTTPException(
                422, "frame identities must be unique within each source segment"
            )
        identities.add(identity)
        decoded.append(_decode_frame(frame, settings))
    return decoded


def _authorization_dependency(settings: ProviderSettings):
    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not settings.api_key:
            return
        scheme, _, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token, settings.api_key
        ):
            raise HTTPException(401, "invalid video semantic provider credentials")

    return authorize


def create_app(
    *, settings: ProviderSettings | None = None, engine: SemanticEngine | None = None
) -> FastAPI:
    resolved_settings = settings or ProviderSettings.from_env()
    resolved_engine = engine or OpenCvSemanticEngine(resolved_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield

    app = FastAPI(
        title="Video Semantic Evidence Provider",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    authorize = _authorization_dependency(resolved_settings)

    @app.get("/health", include_in_schema=False)
    def health(_: None = Depends(authorize)) -> JSONResponse:
        state = resolved_engine.health()
        ready = state.get("status") == "available"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "contract_version": CONTRACT_VERSION,
                "status": "ready" if ready else "degraded",
                "ok": ready,
                "analyzer": state.get("analyzer") or ANALYZER,
                "analyzer_version": state.get("analyzer_version") or "unavailable",
                "capability_statuses": state.get("capability_statuses"),
            },
        )

    @app.post("/v1/video-semantic/analyze")
    def analyze(body: AnalyzeRequest, _: None = Depends(authorize)) -> dict[str, Any]:
        frames = _validate_request(body, resolved_settings)
        try:
            return resolved_engine.analyze(frames)
        except Exception:  # noqa: BLE001 - provider failures must not leak internals
            log.exception("video semantic analysis failed")
            return {
                "contract_version": CONTRACT_VERSION,
                "analyzer": ANALYZER,
                "analyzer_version": "unavailable",
                "capabilities": {
                    capability: {
                        "status": "degraded",
                        "evidence": [],
                        "degraded_reason": "provider inference failed",
                    }
                    for capability in ("subject_tracking", "transition")
                }
                | {
                    "pose": {
                        "status": "unsupported",
                        "evidence": [],
                        "degraded_reason": "This provider does not include a pose estimation model",
                    },
                    "action": {
                        "status": "unsupported",
                        "evidence": [],
                        "degraded_reason": "This provider does not include an action recognition model",
                    },
                },
            }

    return app


app = create_app()
