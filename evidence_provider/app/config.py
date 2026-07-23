from __future__ import annotations

import os
from dataclasses import dataclass


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(
    name: str, default: float, *, minimum: float, maximum: float
) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    api_key: str = ""
    device: str = "cpu"
    eager_load: bool = True
    max_image_bytes: int = 12 * 1024 * 1024
    max_image_pixels: int = 24_000_000
    detector_score_threshold: float = 0.35
    segmenter_score_threshold: float = 0.55
    segmenter_min_pixels: int = 96
    max_evidence_rows: int = 64

    @classmethod
    def from_env(cls) -> ProviderSettings:
        device = os.getenv("EVIDENCE_PROVIDER_DEVICE", "cpu").strip().lower()
        if device not in {"cpu", "cuda", "mps"}:
            raise ValueError("EVIDENCE_PROVIDER_DEVICE must be cpu, cuda or mps")
        return cls(
            api_key=os.getenv("EVIDENCE_PROVIDER_API_KEY", "").strip(),
            device=device,
            eager_load=_boolean("EVIDENCE_PROVIDER_EAGER_LOAD", True),
            max_image_bytes=_bounded_int(
                "EVIDENCE_PROVIDER_MAX_IMAGE_BYTES",
                12 * 1024 * 1024,
                minimum=1024,
                maximum=32 * 1024 * 1024,
            ),
            max_image_pixels=_bounded_int(
                "EVIDENCE_PROVIDER_MAX_IMAGE_PIXELS",
                24_000_000,
                minimum=1_000_000,
                maximum=64_000_000,
            ),
            detector_score_threshold=_bounded_float(
                "EVIDENCE_PROVIDER_DETECTOR_THRESHOLD",
                0.35,
                minimum=0.05,
                maximum=0.99,
            ),
            segmenter_score_threshold=_bounded_float(
                "EVIDENCE_PROVIDER_SEGMENTER_THRESHOLD",
                0.55,
                minimum=0.05,
                maximum=0.99,
            ),
            segmenter_min_pixels=_bounded_int(
                "EVIDENCE_PROVIDER_SEGMENTER_MIN_PIXELS",
                96,
                minimum=4,
                maximum=100_000,
            ),
            max_evidence_rows=_bounded_int(
                "EVIDENCE_PROVIDER_MAX_EVIDENCE_ROWS",
                64,
                minimum=1,
                maximum=128,
            ),
        )
