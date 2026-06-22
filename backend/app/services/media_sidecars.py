"""Helpers for deleting derived media files that share a storage stem."""
from __future__ import annotations

from . import storage


def related_model_ref_keys(key: str | None) -> list[str]:
    if not key or not key.startswith("preview/"):
        return []
    png_key = key.replace("preview/", "model_ref/", 1)
    jpg_key = png_key.rsplit(".", 1)[0] + ".jpg"
    return [jpg_key, png_key]


def related_model_ref_key(key: str | None) -> str | None:
    keys = related_model_ref_keys(key)
    return keys[0] if keys else None


def keys_for_asset_urls(*urls: str | None) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for url in urls:
        key = storage.key_from_url(url or "")
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)
        for model_ref_key in related_model_ref_keys(key):
            if model_ref_key in seen:
                continue
            keys.append(model_ref_key)
            seen.add(model_ref_key)
    return keys


def unlink_keys(keys) -> None:
    for key in keys or []:
        try:
            storage.local_path(key).unlink(missing_ok=True)
        except Exception:
            pass
