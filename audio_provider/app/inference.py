from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import ProviderSettings

CONTRACT_VERSION = "audio-evidence.v1"
ANALYZER = "faster-whisper"
DIARIZATION_ANALYZER = "pyannote-speaker-diarization"


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


class DiarizationEngine(Protocol):
    """Speaker diarization adapter.

    ``health()`` reports one of ``available``/``degraded``/``unsupported``.
    ``diarize()`` returns real speaker turns or raises; it must never invent
    labels — the backend cross-checks claims against returned evidence.
    """

    def warmup(self) -> None: ...

    def health(self) -> dict[str, Any]: ...

    def diarize(self, audio_path: Path) -> list[dict[str, Any]]: ...


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


class DisabledDiarizationEngine:
    """Honest default: diarization is off, so speaker stays unsupported."""

    def warmup(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {
            "status": "unsupported",
            "analyzer": DIARIZATION_ANALYZER,
            "analyzer_version": "unavailable",
            "degraded_reason": "speaker diarization is disabled",
        }

    def diarize(self, audio_path: Path) -> list[dict[str, Any]]:
        raise RuntimeError("speaker diarization is disabled")


class PyannoteDiarizationEngine:
    """Local speaker diarization backed by a pyannote.audio pipeline.

    The pipeline weights are gated on Hugging Face and are not bundled with
    this repository; until they are installed and licensed the engine reports
    ``degraded`` and never fabricates speaker labels.
    """

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
                import pyannote.audio
                from pyannote.audio import Pipeline

                pipeline = Pipeline.from_pretrained(
                    self.settings.diarization_model,
                    use_auth_token=self.settings.diarization_auth_token or None,
                )
                if pipeline is None:
                    raise RuntimeError("pipeline weights are not available")
                if self.settings.diarization_device == "cuda":
                    import torch

                    pipeline.to(torch.device("cuda"))
                self._state.model = pipeline
                self._state.version = str(
                    getattr(pyannote.audio, "__version__", "unknown")
                )
            except Exception as exc:  # noqa: BLE001
                # Keep the reason type-only: tokens/paths must not leak.
                self._state.error = (
                    f"{type(exc).__name__}: diarization pipeline initialization failed"
                )
        return self._state

    def health(self) -> dict[str, Any]:
        state = self._ensure_loaded() if self.settings.eager_load else self._state
        available = state.model is not None
        return {
            "status": "available" if available else "degraded",
            "analyzer": DIARIZATION_ANALYZER,
            "analyzer_version": state.version or "unavailable",
            "degraded_reason": None
            if available
            else state.error or "diarization pipeline has not been loaded",
        }

    def diarize(self, audio_path: Path) -> list[dict[str, Any]]:
        state = self._ensure_loaded()
        if state.model is None:
            raise RuntimeError(state.error or "diarization pipeline is unavailable")
        with self._inference_lock:
            annotation = state.model(str(audio_path))
        turns: list[dict[str, Any]] = []
        for turn, _track, label in annotation.itertracks(yield_label=True):
            start = float(turn.start)
            end = float(turn.end)
            if end <= start:
                continue
            turns.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": str(label),
                }
            )
        turns.sort(key=lambda row: (row["start"], row["end"]))
        return turns


def assign_speaker_labels(
    segments: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    *,
    max_speakers: int = 32,
) -> int:
    """Attach ``speaker_id`` to ASR segments by maximum temporal overlap.

    Labels are renumbered ``S1..Sn`` in order of first appearance so the ids
    are stable, safe ASCII, and carry no provider-internal naming. Segments
    without any overlapping speaker turn stay unlabeled — a missing label is
    honest evidence, a guessed one is fabrication. Returns the labeled count.
    """
    if not segments or not turns:
        return 0
    aliases: dict[str, str] = {}
    labeled = 0
    for segment in segments:
        seg_start = float(segment.get("start") or 0.0)
        seg_end = float(segment.get("end") or 0.0)
        best_label: str | None = None
        best_overlap = 0.0
        for turn in turns:
            overlap = min(seg_end, float(turn["end"])) - max(
                seg_start, float(turn["start"])
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = str(turn["speaker"])
        if best_label is None or best_overlap <= 0:
            continue
        if best_label not in aliases:
            if len(aliases) >= max_speakers:
                continue
            aliases[best_label] = f"S{len(aliases) + 1}"
        segment["speaker_id"] = aliases[best_label]
        labeled += 1
    return labeled
