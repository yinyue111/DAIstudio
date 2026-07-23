from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import ProviderSettings

CONTRACT_VERSION = "audio-evidence.v1"
ANALYZER = "faster-whisper"


class TranscriptionEngine(Protocol):
    def warmup(self) -> None: ...

    def health(self) -> dict[str, Any]: ...

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        prompt: str | None,
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class _EngineState:
    model: Any = None
    version: str | None = None
    error: str | None = None


class FasterWhisperEngine:
    """A single-model local ASR engine. Diarization is intentionally not implemented."""

    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self._state = _EngineState()
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def warmup(self) -> None:
        self._ensure_loaded()

    def _ensure_loaded(self) -> _EngineState:
        if self._state.model is not None or self._state.error:
            return self._state
        with self._load_lock:
            if self._state.model is not None or self._state.error:
                return self._state
            try:
                import faster_whisper
                from faster_whisper import WhisperModel

                self._state.model = WhisperModel(
                    self.settings.model,
                    device=self.settings.device,
                    compute_type=self.settings.compute_type,
                )
                self._state.version = str(
                    getattr(faster_whisper, "__version__", "unknown")
                )
            except Exception as exc:  # noqa: BLE001
                self._state.error = f"{type(exc).__name__}: model initialization failed"
        return self._state

    def health(self) -> dict[str, Any]:
        state = self._ensure_loaded() if self.settings.eager_load else self._state
        available = state.model is not None
        return {
            "status": "available" if available else "degraded",
            "analyzer": ANALYZER,
            "analyzer_version": state.version or "unavailable",
            "degraded_reason": None
            if available
            else state.error or "model has not been loaded",
        }

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        prompt: str | None,
    ) -> dict[str, Any]:
        state = self._ensure_loaded()
        if state.model is None:
            raise RuntimeError(state.error or "transcription model is unavailable")
        with self._inference_lock:
            segments, info = state.model.transcribe(
                str(audio_path),
                beam_size=self.settings.beam_size,
                language=language,
                initial_prompt=prompt,
                vad_filter=True,
                word_timestamps=False,
            )
            rows = []
            for index, segment in enumerate(segments):
                text = str(segment.text or "").strip()
                start = float(segment.start)
                end = float(segment.end)
                if not text or start < 0 or end <= start:
                    continue
                rows.append(
                    {
                        "id": index,
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "text": text,
                    }
                )
                if len(rows) >= self.settings.max_segments:
                    break
        return {
            "text": " ".join(row["text"] for row in rows),
            "language": str(getattr(info, "language", "") or "") or None,
            "segments": rows,
        }
