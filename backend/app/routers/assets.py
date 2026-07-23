"""Unlock (pay to remove watermark / get HD) + download."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user, resolve_token_user
from ..models import AssetReport, GenAsset, GenTask, User
from ..redis_client import redis_client
from ..schemas import (
    AssetBatchDeleteOut,
    AssetBatchIn,
    AssetBatchItemOut,
    AssetOut,
    AssetReportIn,
    AssetReportOut,
    AssetUnlockIn,
)
from ..security import decode_access_token
from ..services import audit, credits, gateway, locks, storage
from ..services.asset_output import (
    is_generated_asset_task,
    is_settled_generated_asset_task,
    to_asset_out,
    unlock_cost_for_asset,
)
from ..services.media_sidecars import collect_unreferenced_asset_keys, unlink_keys
from ..services.rate_limit import incr_window
from ..services.retention import detach_asset_reports

router = APIRouter(prefix="/api/assets", tags=["assets"])
_PLAYBACK_TICKET_TTL_SECONDS = 600
_BATCH_DOWNLOAD_MAX_ITEMS = 30
_BATCH_DOWNLOAD_MAX_VIDEO_ITEMS = 5
_BATCH_DOWNLOAD_MAX_BYTES = 512 * 1024 * 1024
_BATCH_DOWNLOAD_RATE_LIMIT = 12
_BATCH_DOWNLOAD_RATE_WINDOW_SECONDS = 3600
_BATCH_DOWNLOAD_PARALLELISM = 2
_BATCH_DOWNLOAD_SLOT_TTL_SECONDS = 900
_BATCH_DOWNLOAD_SEMAPHORE_KEY = "semaphore:asset-batch-download"
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"RIFF", "image/webp", "webp"),
)


def _download_media_info(asset: GenAsset, path: Path) -> tuple[str, str]:
    if asset.type == "video":
        return "video/mp4", "mp4"
    try:
        with path.open("rb") as stream:
            head = stream.read(16)
    except OSError:
        head = b""
    for signature, media_type, ext in _IMAGE_SIGNATURES:
        if head.startswith(signature) and (ext != "webp" or head[8:12] == b"WEBP"):
            return media_type, ext
    return "application/octet-stream", "bin"


def _external_download_policy(asset: GenAsset) -> tuple[int, tuple[str, ...]]:
    if asset.type == "video":
        return int(settings.video_download_max_bytes), ("video/", "application/octet-stream")
    return int(settings.generated_image_max_bytes), ("image/",)


def _consume_redis_key(key: str) -> str | None:
    getdel = getattr(redis_client, "getdel", None)
    if callable(getdel):
        return getdel(key)
    value = redis_client.get(key)
    if value is not None:
        redis_client.delete(key)
    return value


def _request_user_from_cookie_or_bearer(request: Request, db: Session) -> User | None:
    token = None
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        return None
    decoded = decode_access_token(token)
    if not decoded:
        return None
    user_id, token_version = decoded
    return resolve_token_user(db, user_id, token_version)


def _user_from_playback_ticket(asset_id: int, ticket: str | None, db: Session) -> User | None:
    if not ticket:
        return None
    key = f"asset:playback:{ticket}"
    raw = _consume_redis_key(key)
    if not raw:
        raise HTTPException(401, "播放凭证已过期,请重新打开预览")
    try:
        user_id_s, asset_id_s, token_version_s = str(raw).split(":", 2)
        user_id = int(user_id_s)
        ticket_asset_id = int(asset_id_s)
        token_version = int(token_version_s)
    except (TypeError, ValueError):
        raise HTTPException(401, "播放凭证无效")
    if ticket_asset_id != asset_id:
        raise HTTPException(404, "素材不存在")
    user = db.get(User, user_id)
    if not user or user.token_version != token_version or user.status != "active":
        raise HTTPException(401, "播放凭证无效")
    return user


def _unlocked_owner_asset(db: Session, asset_id: int, user: User) -> GenAsset:
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if asset.moderation_status != "active":
        raise HTTPException(410, "素材已下架")
    task = db.get(GenTask, asset.task_id) if asset.task_id else None
    if is_generated_asset_task(task) and not is_settled_generated_asset_task(task):
        raise HTTPException(409, "生成任务尚未完成结算,暂不能下载高清")
    if not asset.unlocked and not is_generated_asset_task(task):
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


@router.post("/batch/delete", response_model=AssetBatchDeleteOut)
def batch_delete_assets(
    body: AssetBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    deleted: list[int] = []
    failed: list[AssetBatchItemOut] = []
    unlink_after_commit: list[str] = []
    for asset_id in body.asset_ids:
        asset = db.get(GenAsset, asset_id)
        if not asset or asset.user_id != user.id:
            failed.append(AssetBatchItemOut(id=asset_id, ok=False, error="素材不存在"))
            continue
        try:
            unlink_after_commit.extend(collect_unreferenced_asset_keys(db, asset))
            detach_asset_reports(db, asset.id)
            db.delete(asset)
            deleted.append(asset_id)
        except Exception as e:  # noqa: BLE001
            failed.append(AssetBatchItemOut(id=asset_id, ok=False, error=str(e)[:120] or "删除失败"))
    if deleted:
        db.commit()
        unlink_keys(unlink_after_commit)
        audit.log(
            db,
            user_id=user.id,
            action="batch_delete_assets",
            biz_type="asset",
            biz_id=None,
            ip=get_client_ip(request),
            detail={"asset_ids": deleted, "failed": [f.model_dump() for f in failed]},
        )
    else:
        db.rollback()
    return AssetBatchDeleteOut(deleted=deleted, failed=failed)


def _build_batch_download_response(
    body: AssetBatchIn,
    request: Request,
    db: Session,
    user: User,
):
    if len(body.asset_ids) > _BATCH_DOWNLOAD_MAX_ITEMS:
        raise HTTPException(413, f"单次最多打包 {_BATCH_DOWNLOAD_MAX_ITEMS} 个素材")
    video_count = 0
    total_bytes = 0
    tmp = tempfile.NamedTemporaryFile(prefix="assets-", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    added = 0
    skipped: list[dict] = []
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for asset_id in body.asset_ids:
                external_tmp: Path | None = None
                try:
                    asset = _unlocked_owner_asset(db, asset_id, user)
                    if asset.type == "video":
                        video_count += 1
                        if video_count > _BATCH_DOWNLOAD_MAX_VIDEO_ITEMS:
                            raise HTTPException(413, f"单次最多打包 {_BATCH_DOWNLOAD_MAX_VIDEO_ITEMS} 个视频")
                    url = asset.hd_url or asset.preview_url
                    key = storage.key_from_url(url)
                    if key:
                        path = _local_download_path(asset)
                    elif url:
                        suffix = ".mp4" if asset.type == "video" else ".bin"
                        with tempfile.NamedTemporaryFile(
                            prefix=f"asset-{asset.id}-",
                            suffix=suffix,
                            delete=False,
                        ) as f:
                            external_tmp = Path(f.name)
                        max_bytes, allowed_content_types = _external_download_policy(asset)
                        remaining_bytes = _BATCH_DOWNLOAD_MAX_BYTES - total_bytes
                        if remaining_bytes <= 0:
                            raise HTTPException(413, "打包文件过大,请减少素材数量后重试")
                        gateway.download_to_path(
                            url,
                            external_tmp,
                            max_bytes=min(max_bytes, remaining_bytes),
                            allowed_content_types=allowed_content_types,
                            timeout_seconds=(
                                int(settings.video_download_timeout_seconds)
                                if asset.type == "video"
                                else int(settings.image_download_timeout_seconds)
                            ),
                        )
                        path = external_tmp
                    else:
                        raise HTTPException(404, "资源不存在")
                    try:
                        item_size = path.stat().st_size
                    except OSError:
                        item_size = 0
                    total_bytes += max(0, int(item_size))
                    if total_bytes > _BATCH_DOWNLOAD_MAX_BYTES:
                        raise HTTPException(413, "打包文件过大,请减少素材数量后重试")
                    media_type, ext = _download_media_info(asset, path)
                    name = f"asset-{asset.id}.{ext}"
                    if name in zf.namelist():
                        name = f"asset-{asset.id}-{added + 1}.{ext}"
                    zf.write(path, arcname=name)
                    added += 1
                except HTTPException as e:
                    if e.status_code == 413:
                        raise
                    skipped.append({"id": asset_id, "error": str(e.detail)})
                except Exception as e:  # noqa: BLE001
                    skipped.append({"id": asset_id, "error": str(e)[:120] or "打包失败"})
                finally:
                    if external_tmp is not None:
                        external_tmp.unlink(missing_ok=True)
            if skipped:
                zf.writestr(
                    "skipped.json",
                    json.dumps(skipped, ensure_ascii=False, indent=2),
                )
        if added <= 0:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(400, "没有可下载的素材")
        audit.log(
            db,
            user_id=user.id,
            action="batch_download_assets",
            biz_type="asset",
            biz_id=None,
            ip=get_client_ip(request),
            detail={"asset_ids": body.asset_ids, "added": added, "skipped": skipped},
        )
        filename = f"assets-{user.id}-{secrets.token_hex(4)}.zip"
        return FileResponse(
            str(tmp_path),
            media_type="application/zip",
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            background=BackgroundTask(lambda p: os.unlink(p) if os.path.exists(p) else None, str(tmp_path)),
        )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


@router.post("/batch/download")
def batch_download_assets(
    body: AssetBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rate = incr_window(
        f"asset:batch-download-rate:{user.id}",
        _BATCH_DOWNLOAD_RATE_WINDOW_SECONDS,
    )
    if rate > _BATCH_DOWNLOAD_RATE_LIMIT:
        raise HTTPException(429, "打包下载过于频繁,请稍后再试")

    slot = locks.RedisSemaphore(
        _BATCH_DOWNLOAD_SEMAPHORE_KEY,
        limit=_BATCH_DOWNLOAD_PARALLELISM,
        ttl=_BATCH_DOWNLOAD_SLOT_TTL_SECONDS,
        wait_timeout=0,
    )
    try:
        slot.__enter__()
    except TimeoutError:
        raise HTTPException(429, "打包下载任务繁忙,请稍后再试") from None
    try:
        return _build_batch_download_response(body, request, db, user)
    finally:
        slot.__exit__(None, None, None)


@router.post("/{asset_id}/unlock", response_model=AssetOut)
def unlock(
    asset_id: int,
    request: Request,
    body: AssetUnlockIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if asset.moderation_status != "active":
        raise HTTPException(410, "素材已下架")
    if asset.unlocked:
        return to_asset_out(db, asset)

    unlock_cost = unlock_cost_for_asset(db, asset)
    quote = None
    if unlock_cost > 0:
        if body is None:
            raise HTTPException(
                422,
                detail={
                    "code": "QUOTE_REQUIRED",
                    "message": "付费高清解锁必须先获取并确认服务端报价",
                },
            )
        from ..services.generation_quotes import (
            consume_execution_quote,
            lock_execution_quote,
            validate_asset_unlock_quote,
        )

        quote = lock_execution_quote(
            db,
            quote_id=body.quote_id,
            user_id=user.id,
            kind="asset_unlock",
            allow_consumed=True,
        )
        if quote.status == "consumed":
            if (
                quote.consumed_ref_type == "asset_unlock"
                and int(quote.consumed_ref_id or 0) == int(asset_id)
            ):
                db.rollback()
                db.refresh(asset)
                if asset.unlocked:
                    return to_asset_out(db, asset)
                raise HTTPException(
                    409,
                    detail={
                        "code": "QUOTE_STATE_INVALID",
                        "message": "解锁报价已消费，但素材状态不一致，请联系管理员处理",
                    },
                )
            raise HTTPException(
                409,
                detail={"code": "QUOTE_CONSUMED", "message": "报价已被其他操作使用"},
            )
        unlock_cost = validate_asset_unlock_quote(db, quote, asset_id=asset_id)

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
            credits.freeze(
                db,
                user.id,
                unlock_cost,
                biz_ref=asset_id,
                biz_type="unlock",
                commit=False,
            )
            consume_execution_quote(
                quote,
                ref_type="asset_unlock",
                ref_id=asset_id,
            )
            credits.settle(
                db,
                user.id,
                unlock_cost,
                unlock_cost,
                biz_ref=asset_id,
                biz_type="unlock",
                commit=False,
            )
        except credits.InsufficientCredits as e:
            db.rollback()  # reverts the claim -> stays locked, no charge
            raise HTTPException(400, str(e))
        except Exception:
            db.rollback()
            raise
    db.commit()
    db.refresh(asset)
    audit.log(db, user_id=user.id, action="unlock", biz_type="unlock",
              biz_id=asset_id, ip=get_client_ip(request), detail={"cost": unlock_cost})
    return to_asset_out(db, asset)


@router.get("/{asset_id}/download")
def download(asset_id: int, request: Request, db: Session = Depends(get_db),
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
        audit.log(
            db,
            user_id=user.id,
            action="download_asset",
            biz_type="asset",
            biz_id=asset_id,
            ip=get_client_ip(request),
            detail={"type": asset.type, "task_id": asset.task_id, "source": "local", "key": key},
        )
        return FileResponse(str(path), filename=path.name)
    # External (gateway/CDN) result -> proxy through the authenticated backend.
    # Browser fetch(blob) cannot reliably follow cross-origin redirects without CORS.
    suffix = ".mp4" if asset.type == "video" else ".bin"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f"asset-{asset_id}-", suffix=suffix, delete=False) as f:
            tmp_path = Path(f.name)
        max_bytes, allowed_content_types = _external_download_policy(asset)
        gateway.download_to_path(
            url,
            tmp_path,
            max_bytes=max_bytes,
            allowed_content_types=allowed_content_types,
            timeout_seconds=(
                int(settings.video_download_timeout_seconds)
                if asset.type == "video"
                else int(settings.image_download_timeout_seconds)
            ),
        )
    except gateway.GatewayError as e:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise HTTPException(502, f"高清资源下载失败:{str(e)[:120]}")
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    media_type, ext = _download_media_info(asset, tmp_path)
    filename = f"asset-{asset_id}.{ext}"
    audit.log(
        db,
        user_id=user.id,
        action="download_asset",
        biz_type="asset",
        biz_id=asset_id,
        ip=get_client_ip(request),
        detail={"type": asset.type, "task_id": asset.task_id, "source": "external"},
    )
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
    request: Request,
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
    audit.log(
        db,
        user_id=user.id,
        action="create_playback_ticket",
        biz_type="asset",
        biz_id=asset_id,
        ip=get_client_ip(request),
        detail={"type": asset.type, "task_id": asset.task_id, "ttl_seconds": _PLAYBACK_TICKET_TTL_SECONDS},
    )
    return {"ticket": ticket, "expires_in": _PLAYBACK_TICKET_TTL_SECONDS}


@router.get("/{asset_id}/stream")
def stream(
    asset_id: int,
    request: Request,
    ticket: str | None = None,
    db: Session = Depends(get_db),
):
    user = (
        _user_from_playback_ticket(asset_id, ticket, db)
        if ticket
        else _request_user_from_cookie_or_bearer(request, db)
    )
    if user is None:
        raise HTTPException(401, "请登录后预览视频")
    asset = _unlocked_owner_asset(db, asset_id, user)
    if asset.type != "video":
        raise HTTPException(400, "仅视频素材支持在线播放")
    path = _local_download_path(asset)
    audit.log(
        db,
        user_id=user.id,
        action="stream_asset",
        biz_type="asset",
        biz_id=asset_id,
        ip=get_client_ip(request),
        detail={"type": asset.type, "task_id": asset.task_id},
    )
    return FileResponse(str(path), media_type="video/mp4", headers={"Cache-Control": "no-store"})


@router.post("/{asset_id}/favorite", response_model=AssetOut)
def toggle_favorite(asset_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if asset.moderation_status != "active":
        raise HTTPException(410, "素材已下架")
    asset.favorite = not asset.favorite
    db.commit()
    db.refresh(asset)
    return to_asset_out(db, asset)


@router.post("/{asset_id}/report", response_model=AssetReportOut)
def report_asset(
    asset_id: int,
    body: AssetReportIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    if asset.moderation_status != "active":
        raise HTTPException(410, "素材已下架")
    note = (body.note or "").strip()
    existing = db.execute(
        select(AssetReport).where(
            AssetReport.asset_id == asset.id,
            AssetReport.reporter_user_id == user.id,
            AssetReport.status == "open",
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    report = AssetReport(
        asset_id=asset.id,
        reporter_user_id=user.id,
        owner_user_id=asset.user_id,
        reason=body.reason,
        note=note or None,
        status="open",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    audit.log(
        db,
        user_id=user.id,
        action="report_asset",
        biz_type="asset_report",
        biz_id=report.id,
        ip=get_client_ip(request),
        detail={
            "asset_id": asset.id,
            "owner_user_id": asset.user_id,
            "reason": body.reason,
            "note": note,
        },
    )
    return report


@router.delete("/{asset_id}")
def delete_asset(asset_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    asset = db.get(GenAsset, asset_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(404, "素材不存在")
    unlink_after_commit = collect_unreferenced_asset_keys(db, asset)
    detach_asset_reports(db, asset.id)
    db.delete(asset)
    db.commit()
    unlink_keys(unlink_after_commit)
    audit.log(db, user_id=user.id, action="delete_asset", biz_type="asset",
              biz_id=asset_id, ip=get_client_ip(request))
    return {"ok": True}
