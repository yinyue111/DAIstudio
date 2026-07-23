"""Helpers for deleting derived media files that share a storage stem."""
from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import GenAsset
from . import storage

log = logging.getLogger("media_sidecars")


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


def storage_bytes_for_keys(keys) -> int | None:
    """Return bytes occupied by existing local or object-storage keys."""
    total = 0
    found = False
    seen: set[str] = set()
    for key in keys or []:
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            size = storage.object_size(key)
            if size is not None:
                total += int(size)
                found = True
        except Exception:  # noqa: BLE001
            log.debug("asset size inspection skipped for %s", key, exc_info=True)
    return total if found else None


def storage_bytes_for_asset_urls(*urls: str | None) -> int | None:
    return storage_bytes_for_keys(keys_for_asset_urls(*urls))


def direct_keys_for_asset_urls(*urls: str | None) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for url in urls:
        key = storage.key_from_url(url or "")
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def _asset_key_is_referenced(db: Session, key: str, *, excluding_asset_id: int) -> bool:
    url = storage.public_url(key)
    if not url:
        return False
    return bool(
        db.execute(
            select(GenAsset.id)
            .where(
                GenAsset.id != excluding_asset_id,
                or_(GenAsset.preview_url == url, GenAsset.hd_url == url),
            )
            .limit(1)
        ).first()
    )


def collect_unreferenced_asset_keys(db: Session, asset: GenAsset) -> list[str]:
    """Collect files for an asset without breaking other rows sharing the same URL."""
    keys: list[str] = []
    seen: set[str] = set()
    for key in direct_keys_for_asset_urls(asset.preview_url, asset.hd_url):
        if _asset_key_is_referenced(db, key, excluding_asset_id=asset.id):
            continue
        for candidate in [key, *related_model_ref_keys(key)]:
            if candidate in seen:
                continue
            seen.add(candidate)
            keys.append(candidate)
    return keys


def safe_unlink_asset_files(db: Session, asset: GenAsset) -> None:
    unlink_keys(collect_unreferenced_asset_keys(db, asset))


def unlink_keys(keys) -> None:
    for key in keys or []:
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001
            log.debug("media sidecar unlink skipped for %s", key, exc_info=True)
