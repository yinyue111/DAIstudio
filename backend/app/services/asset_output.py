"""Shared response helpers for generated assets."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import GenAsset, GenTask
from ..schemas import AssetOut
from .config_store import get_model_config


def unlock_cost_for_asset(db: Session, asset: GenAsset, task: GenTask | None = None) -> int:
    task = task or (db.get(GenTask, asset.task_id) if asset.task_id else None)
    snapshot = ((task.params or {}).get("_model_snapshot") or {}) if task else {}
    if "unlock_cost" in snapshot:
        try:
            return max(0, int(snapshot.get("unlock_cost") or 0))
        except (TypeError, ValueError):
            pass
    model = get_model_config(db, asset.type)
    return max(0, int(model.unlock_cost or 0)) if model else 0


def to_asset_out(db: Session, asset: GenAsset, task: GenTask | None = None) -> AssetOut:
    out = AssetOut.model_validate(asset)
    if asset.moderation_status != "active":
        out.preview_url = None
        out.hd_url = None
        out.unlock_cost = 0
        return out
    if not asset.unlocked:
        out.hd_url = None
        out.unlock_cost = unlock_cost_for_asset(db, asset, task=task)
    else:
        out.unlock_cost = 0
    return out
