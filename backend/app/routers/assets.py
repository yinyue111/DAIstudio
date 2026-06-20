"""Unlock (pay to remove watermark / get HD) + download."""
from __future__ import annotations

import os
import secrets
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
from ..redis_client import redis_client
from ..schemas import AssetOut
from ..services import audit, credits, gateway, storage
from ..services.asset_output import to_asset_out, unlock_cost_for_asset

router = APIRouter(prefix="/api/assets", tags=["assets"])
_PLAYBACK_TICKET_TTL_SECONDS = 600


def _unlocked_owner_asset(db: Session, asset_id: int, user: User) -> GenAsset:
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if not asset.unlocked:
        raise HTTPException(402, "请先解锁后再下载高清")
    return asset


def _local_download_path(asset: GenAsset) -> Path:
    url = asset.hd_url or asset.preview_url
    if not url:
        raise HTTPException(404, "高清资源不存在")
    key = storage.key_from_url(url)
    if not key:
        raise HTTPException(400, "该资源暂不支持在线播放")
    path = storage.local_path(key)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "文件丢失")
    return path


@router.post("/{asset_id}/unlock", response_model=AssetOut)
def unlock(asset_id: int, request: Request, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if asset.unlocked:
        return to_asset_out(db, asset)

    unlock_cost = unlock_cost_for_asset(db, asset)

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
        return to_asset_out(db, asset)

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
    return to_asset_out(db, asset)


@router.get("/{asset_id}/download")
def download(asset_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    asset = _unlocked_owner_asset(db, asset_id, user)

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
        raise HTTPException(502, f"高清资源下载失败:{str(e)[:120]}")
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


@router.post("/{asset_id}/playback-ticket")
def create_playback_ticket(
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = _unlocked_owner_asset(db, asset_id, user)
    if asset.type != "video":
        raise HTTPException(400, "仅视频素材支持在线播放")
    _local_download_path(asset)
    ticket = secrets.token_urlsafe(32)
    redis_client.setex(
        f"asset:playback:{ticket}",
        _PLAYBACK_TICKET_TTL_SECONDS,
        f"{user.id}:{asset_id}:{user.token_version}",
    )
    return {"ticket": ticket, "expires_in": _PLAYBACK_TICKET_TTL_SECONDS}


@router.get("/{asset_id}/stream")
def stream(asset_id: int, ticket: str, db: Session = Depends(get_db)):
    raw = redis_client.get(f"asset:playback:{ticket}")
    if not raw:
        raise HTTPException(401, "播放凭证已过期,请重新打开预览")
    try:
        user_id_s, asset_id_s, token_version_s = str(raw).split(":", 2)
        user_id = int(user_id_s)
        ticket_asset_id = int(asset_id_s)
        token_version = int(token_version_s)
    except (TypeError, ValueError):
        redis_client.delete(f"asset:playback:{ticket}")
        raise HTTPException(401, "播放凭证无效")
    if ticket_asset_id != asset_id:
        raise HTTPException(404, "素材不存在")
    user = db.get(User, user_id)
    if not user or user.token_version != token_version or user.status != "active":
        redis_client.delete(f"asset:playback:{ticket}")
        raise HTTPException(401, "播放凭证无效")
    asset = _unlocked_owner_asset(db, asset_id, user)
    if asset.type != "video":
        raise HTTPException(400, "仅视频素材支持在线播放")
    path = _local_download_path(asset)
    return FileResponse(str(path), media_type="video/mp4")


@router.post("/{asset_id}/favorite", response_model=AssetOut)
def toggle_favorite(asset_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    asset.favorite = not asset.favorite
    db.commit()
    db.refresh(asset)
    return to_asset_out(db, asset)


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
