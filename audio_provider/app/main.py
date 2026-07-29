from __future__ import annotations

import asyncio
import hmac
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .config import ProviderSettings
from .inference import (
    ANALYZER,
    CONTRACT_VERSION,
    DiarizationEngine,
    DisabledDiarizationEngine,
    FasterWhisperEngine,
    PyannoteDiarizationEngine,
    TranscriptionEngine,
    assign_speaker_labels,
)


def _authorization_dependency(settings: ProviderSettings):
    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not settings.api_key:
            return
        scheme, _, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token, settings.api_key
        ):
            raise HTTPException(401, "invalid audio provider credentials")

    return authorize


def _suffix(filename: str | None) -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    return (
        suffix
        if suffix
        in {
            ".aac",
            ".flac",
            ".m4a",
            ".mp3",
            ".mp4",
            ".mpeg",
            ".ogg",
            ".opus",
            ".wav",
            ".webm",
        }
        else ".audio"
    )


async def _persist_upload(file: UploadFile, settings: ProviderSettings) -> Path:
    payload = await file.read(settings.max_upload_bytes + 1)
    if not payload:
        raise HTTPException(422, "audio file is empty")
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(413, "audio file exceeds the configured byte limit")
    descriptor, name = tempfile.mkstemp(prefix="asr-", suffix=_suffix(file.filename))
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _speaker_health_status(
    diarizer: DiarizationEngine, *, asr_ready: bool
) -> str:
    status = str(diarizer.health().get("status") or "unsupported")
    if status not in {"available", "degraded", "unsupported"}:
        status = "unsupported"
    # The backend rejects speaker: available while ASR itself is not available,
    # and a broken ASR path means no diarized evidence can be produced anyway.
    if status == "available" and not asr_ready:
        return "degraded"
    return status


def _health_payload(
    engine: TranscriptionEngine,
    diarizer: DiarizationEngine,
    settings: ProviderSettings,
) -> tuple[dict[str, Any], int]:
    state = engine.health()
    ready = state.get("status") == "available"
    return (
        {
            "contract_version": CONTRACT_VERSION,
            "status": "ready" if ready else "degraded",
            "ok": ready,
            "analyzer": state.get("analyzer") or ANALYZER,
            "analyzer_version": state.get("analyzer_version") or "unavailable",
            "provider_model": settings.model,
            "capability_statuses": {
                "asr": "available" if ready else "degraded",
                "speaker": _speaker_health_status(diarizer, asr_ready=ready),
            },
            "degraded_reason": None if ready else state.get("degraded_reason"),
        },
        200 if ready else 503,
    )


def create_app(
    *,
    settings: ProviderSettings | None = None,
    engine: TranscriptionEngine | None = None,
    diarizer: DiarizationEngine | None = None,
) -> FastAPI:
    resolved_settings = settings or ProviderSettings.from_env()
    resolved_engine = engine or FasterWhisperEngine(resolved_settings)
    resolved_diarizer = diarizer or (
        PyannoteDiarizationEngine(resolved_settings)
        if resolved_settings.diarization_enabled
        else DisabledDiarizationEngine()
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if resolved_settings.eager_load:
            await asyncio.to_thread(resolved_engine.warmup)
            await asyncio.to_thread(resolved_diarizer.warmup)
        yield

    app = FastAPI(
        title="Self-hosted Audio Provider",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    authorize = _authorization_dependency(resolved_settings)

    @app.get("/health", include_in_schema=False)
    def health(_: None = Depends(authorize)) -> JSONResponse:
        payload, status = _health_payload(
            resolved_engine, resolved_diarizer, resolved_settings
        )
        return JSONResponse(status_code=status, content=payload)

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: Annotated[UploadFile, File()],
        model: Annotated[str, Form()],
        response_format: Annotated[str, Form()] = "json",
        timestamp_granularities: Annotated[
            list[str] | None, Form(alias="timestamp_granularities[]")
        ] = None,
        language: Annotated[str | None, Form()] = None,
        prompt: Annotated[str | None, Form()] = None,
        _: None = Depends(authorize),
    ) -> dict[str, Any]:
        del (
            timestamp_granularities
        )  # Segment timestamps are always returned for backend evidence validation.
        if model != resolved_settings.model:
            raise HTTPException(
                400, "model must match the configured local transcription model"
            )
        if response_format not in {"json", "verbose_json"}:
            raise HTTPException(400, "response_format must be json or verbose_json")
        if language is not None and (
            not language.isalpha() or len(language) not in {2, 3}
        ):
            raise HTTPException(
                422, "language must be a two or three letter alphabetic code"
            )
        if prompt is not None and len(prompt) > 4_000:
            raise HTTPException(422, "prompt exceeds the configured limit")

        path = await _persist_upload(file, resolved_settings)
        try:
            try:
                result = await asyncio.to_thread(
                    resolved_engine.transcribe,
                    path,
                    language=language.lower() if language else None,
                    prompt=prompt,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(503, "local transcription failed") from exc
            segments = result.get("segments")
            if not isinstance(segments, list):
                raise HTTPException(
                    503, "local transcription returned an invalid segment payload"
                )
            speaker_status = _speaker_health_status(
                resolved_diarizer, asr_ready=True
            )
            if speaker_status == "available":
                # ASR evidence must survive a diarization failure: keep the
                # segments and honestly downgrade speaker instead of failing.
                try:
                    turns = await asyncio.to_thread(resolved_diarizer.diarize, path)
                    labeled = assign_speaker_labels(
                        segments,
                        turns,
                        max_speakers=resolved_settings.diarization_max_speakers,
                    )
                    if not labeled:
                        speaker_status = "degraded"
                except Exception:  # noqa: BLE001
                    for row in segments:
                        if isinstance(row, dict):
                            row.pop("speaker_id", None)
                    speaker_status = "degraded"
        finally:
            path.unlink(missing_ok=True)
        return {
            "text": str(result.get("text") or ""),
            "language": result.get("language"),
            "segments": segments,
            "capability_statuses": {"asr": "available", "speaker": speaker_status},
        }

    return app


app = create_app()
