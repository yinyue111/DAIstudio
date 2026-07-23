from __future__ import annotations

import os
from dataclasses import dataclass


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    api_key: str = ""
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    eager_load: bool = True
    max_upload_bytes: int = 48 * 1024 * 1024
    max_segments: int = 1000
    beam_size: int = 5

    @classmethod
    def from_env(cls) -> "ProviderSettings":
        device = os.getenv("ASR_PROVIDER_DEVICE", "cpu").strip().lower()
        if device not in {"cpu", "cuda", "auto"}:
            raise ValueError("ASR_PROVIDER_DEVICE must be cpu, cuda or auto")
        model = os.getenv("ASR_PROVIDER_MODEL", "small").strip()
        if not model or len(model) > 128:
            raise ValueError("ASR_PROVIDER_MODEL must be between 1 and 128 characters")
        compute_type = os.getenv("ASR_PROVIDER_COMPUTE_TYPE", "int8").strip().lower()
        if compute_type not in {
            "int8",
            "int8_float16",
            "int16",
            "float16",
            "float32",
            "default",
        }:
            raise ValueError("ASR_PROVIDER_COMPUTE_TYPE is unsupported")
        return cls(
            api_key=os.getenv("ASR_PROVIDER_API_KEY", "").strip(),
            model=model,
            device=device,
            compute_type=compute_type,
            eager_load=_boolean("ASR_PROVIDER_EAGER_LOAD", True),
            max_upload_bytes=_bounded_int(
                "ASR_PROVIDER_MAX_UPLOAD_BYTES",
                48 * 1024 * 1024,
                minimum=1024,
                maximum=512 * 1024 * 1024,
            ),
            max_segments=_bounded_int(
                "ASR_PROVIDER_MAX_SEGMENTS", 1000, minimum=1, maximum=10_000
            ),
            beam_size=_bounded_int("ASR_PROVIDER_BEAM_SIZE", 5, minimum=1, maximum=10),
        )
