"""Unlock (pay to remove watermark / get HD) + download."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import GenAsset, User
from ..schemas import AssetOut
from ..services import audit, credits, gateway, storage
from ..services.config_store import get_model_config

router = APIRouter(prefix="/api/assets", tags=["assets"])


@router.post("/{asset_id}/unlock", response_model=AssetOut)
def unlock(asset_id: int, request: Request, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if asset.unlocked:
        return AssetOut.model_validate(asset)

    model = get_model_config(db, asset.type)  # image/video
    unlock_cost = int(model.unlock_cost) if model else 0

    # Atomic claim: only the request that flips unlocked False->True charges, so
    # a double-click / retry can never double-charge the unlock.
    claimed = db.execute(
        update(GenAsset)
        .where(GenAsset.id == asset_id, GenAsset.user_id == user.id,
               GenAsset.unlocked.is_(False))
        .values(unlocked=True, watermarked=False)
    ).rowcount
    if (claimed or 0) != 1:
        db.rollback()  # already unlocked by a concurrent request -> no charge
        db.refresh(asset)
        return AssetOut.model_validate(asset)

    if unlock_cost > 0:
        try:
            credits.unlock(db, user.id, unlock_cost, biz_ref=asset_id, commit=False)
        except credits.InsufficientCredits as e:
            db.rollback()  # reverts the claim -> stays locked, no charge
            raise HTTPException(400, str(e))
    db.commit()
    db.refresh(asset)
    audit.log(db, user_id=user.id, action="unlock", biz_type="unlock",
              biz_id=asset_id, ip=get_client_ip(request), detail={"cost": unlock_cost})
    return AssetOut.model_validate(asset)


@router.get("/{asset_id}/download")
def download(asset_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if not asset.unlocked:
        raise HTTPException(402, "请先解锁后再下载高清")

    url = asset.hd_url or asset.preview_url
    if not url:
        raise HTTPException(404, "高清资源不存在")

    key = storage.key_from_url(url)
    if key:  # served from local storage -> stream the original (no watermark)
        path = storage.local_path(key)
        if not path.exists():
            raise HTTPException(404, "文件丢失")
        return FileResponse(str(path), filename=path.name)
    # External (gateway/CDN) result -> proxy through the authenticated backend.
    # Browser fetch(blob) cannot reliably follow cross-origin redirects without CORS.
    filename = f"asset-{asset_id}.{'mp4' if asset.type == 'video' else 'bin'}"
    media_type = "video/mp4" if asset.type == "video" else "application/octet-stream"
    suffix = ".mp4" if asset.type == "video" else ".bin"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f"asset-{asset_id}-", suffix=suffix, delete=False) as f:
            tmp_path = Path(f.name)
        gateway.download_to_path(url, tmp_path)
    except gateway.GatewayError as e:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise HTTPException(502, f"高清资源下载失败:{e}")
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        str(tmp_path),
        media_type=media_type,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(lambda p: os.unlink(p) if os.path.exists(p) else None, str(tmp_path)),
    )


@router.post("/{asset_id}/favorite", response_model=AssetOut)
def toggle_favorite(asset_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    asset.favorite = not asset.favorite
    db.commit()
    db.refresh(asset)
    return AssetOut.model_validate(asset)


@router.delete("/{asset_id}")
def delete_asset(asset_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    for u in (asset.preview_url, asset.hd_url):
        key = storage.key_from_url(u or "")
        if key:
            try:
                storage.local_path(key).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    db.delete(asset)
    db.commit()
    audit.log(db, user_id=user.id, action="delete_asset", biz_type="asset",
              biz_id=asset_id, ip=get_client_ip(request))
    return {"ok": True}
