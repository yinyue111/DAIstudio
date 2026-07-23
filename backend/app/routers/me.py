from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..billing_schemas import BillingEntryPageOut, CreditTransactionPageOut
from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import User, UserDraft
from ..password_policy import MIN_PASSWORD_LEN
from ..redis_client import redis_client
from ..schemas import (
    AssetSimilarityOut,
    ChangePasswordIn,
    UserAssetBatchDeleteIn,
    UserAssetListOut,
    UserAssetMetadataIn,
    UserAssetMetadataOut,
    UserAssetMutationOut,
    UserAssetTagsIn,
    UserDraftIn,
    UserDraftOut,
    UserOut,
)
from ..security import hash_password, verify_password
from ..services import audit, billing, storage, user_assets
from ..services.rate_limit import incr_window

router = APIRouter(prefix="/api", tags=["me"])

PASSWORD_FAIL_LIMIT = 8
PASSWORD_FAIL_WINDOW = 15 * 60
STUDIO_DRAFT_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_DRAFT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.get("/me/credit-transactions", response_model=CreditTransactionPageOut)
def list_me_credit_transactions(
    transaction_type: str = Query(
        default="all",
        alias="type",
        pattern="^(all|grant|freeze|settle|refund|unlock|consume)$",
    ),
    biz_type: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return billing.list_credit_transactions(
            db,
            user_id=user.id,
            transaction_type=transaction_type,
            biz_type=biz_type,
            limit=limit,
            cursor=cursor,
        )
    except billing.InvalidBillingCursor as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/me/billing/entries", response_model=BillingEntryPageOut)
def list_me_billing_entries(
    kind: str = Query(
        default="all",
        pattern="^(all|generation|reverse|workflow|prompt|unlock|recharge|grant|other)$",
    ),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return billing.list_billing_entries(
            db,
            user_id=user.id,
            kind=kind,
            limit=limit,
            cursor=cursor,
        )
    except billing.InvalidBillingCursor as exc:
        raise HTTPException(400, str(exc)) from exc


def _optional_bool(value: str | None) -> bool | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise HTTPException(400, "favorite 应为 true 或 false")


def _asset_error(error: Exception) -> HTTPException:
    if isinstance(error, (user_assets.InvalidAssetRef, user_assets.InvalidCursor)):
        return HTTPException(400, str(error))
    if isinstance(error, user_assets.AssetNotFound):
        return HTTPException(404, str(error))
    if isinstance(error, user_assets.AssetInUse):
        return HTTPException(409, str(error))
    raise error


@router.get("/me/assets", response_model=UserAssetListOut)
def list_me_assets(
    origin: str = Query(default="all", pattern="^(all|generated|uploaded)$"),
    type: str = Query(default="all", pattern="^(all|image|video)$"),
    favorite: str | None = None,
    retention: str = Query(default="all", pattern="^(all|retained|expiring)$"),
    q: str | None = Query(default=None, max_length=100),
    tag: str | None = Query(default=None, max_length=32),
    folder: str = Query(default="all", pattern=r"^(all|unfiled|[1-9][0-9]*)$"),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return user_assets.list_user_assets(
            db,
            user.id,
            origin=origin,
            asset_type=type,
            favorite=_optional_bool(favorite),
            retention_filter=retention,
            search_query=q,
            tag_filter=tag,
            folder_filter=folder,
            limit=limit,
            offset=offset,
            cursor=cursor,
        )
    except (
        user_assets.InvalidCursor,
        user_assets.InvalidAssetRef,
        user_assets.AssetNotFound,
    ) as error:
        raise _asset_error(error) from None


@router.post("/me/assets/metadata", response_model=UserAssetMutationOut)
def update_me_asset_metadata(
    body: UserAssetMetadataIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        asset_refs = user_assets.update_asset_metadata(
            db,
            user.id,
            body.asset_refs,
            favorite=body.favorite,
            retained=body.retained,
        )
    except (user_assets.InvalidAssetRef, user_assets.AssetNotFound) as error:
        db.rollback()
        raise _asset_error(error) from None
    db.commit()
    audit.log(
        db,
        user_id=user.id,
        action="update_user_asset_metadata",
        biz_type="user_asset",
        ip=get_client_ip(request),
        detail={
            "asset_refs": asset_refs,
            "favorite": body.favorite,
            "retained": body.retained,
        },
    )
    return UserAssetMutationOut(asset_refs=asset_refs)


@router.put("/me/assets/{asset_ref}/tags", response_model=UserAssetMetadataOut)
def update_me_asset_tags(
    asset_ref: str,
    body: UserAssetTagsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        row = user_assets.update_asset_tags(db, user.id, asset_ref, body.tags)
    except (user_assets.InvalidAssetRef, user_assets.AssetNotFound) as error:
        db.rollback()
        raise _asset_error(error) from None
    db.commit()
    audit.log(
        db,
        user_id=user.id,
        action="update_user_asset_tags",
        biz_type="user_asset",
        ip=get_client_ip(request),
        detail={"asset_ref": asset_ref, "tags": body.tags},
    )
    db.refresh(row)
    return row


@router.post("/me/assets/{asset_ref}/similar", response_model=AssetSimilarityOut)
def find_me_similar_assets(
    asset_ref: str,
    request: Request,
    max_distance: int = Query(default=8, ge=0, le=64),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        library = user_assets.list_user_assets(
            db,
            user.id,
            origin="all",
            asset_type="all",
            favorite=None,
            retention_filter="all",
            search_query=None,
            tag_filter=None,
            folder_filter="all",
            limit=1_000_000,
            offset=0,
            cursor=None,
        )
        result = user_assets.find_similar_assets(
            db,
            user.id,
            asset_ref,
            [item["asset_ref"] for item in library["items"]],
            max_distance=max_distance,
        )
    except (user_assets.InvalidAssetRef, user_assets.AssetNotFound) as error:
        db.rollback()
        raise _asset_error(error) from None
    db.commit()
    audit.log(
        db,
        user_id=user.id,
        action="analyze_user_asset_similarity",
        biz_type="user_asset",
        ip=get_client_ip(request),
        detail={
            "asset_ref": asset_ref,
            "status": result["status"],
            "match_count": len(result["matches"]),
        },
    )
    return result


@router.post("/me/assets/batch-delete", response_model=UserAssetMutationOut)
def batch_delete_me_assets(
    body: UserAssetBatchDeleteIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        asset_refs = user_assets.delete_assets(db, user.id, body.asset_refs)
    except (
        user_assets.InvalidAssetRef,
        user_assets.AssetNotFound,
        user_assets.AssetInUse,
    ) as error:
        db.rollback()
        raise _asset_error(error) from None
    audit.log(
        db,
        user_id=user.id,
        action="batch_delete_user_assets",
        biz_type="user_asset",
        ip=get_client_ip(request),
        detail={"asset_refs": asset_refs},
    )
    return UserAssetMutationOut(asset_refs=asset_refs)


@router.get("/me/assets/download")
def download_me_asset(
    request: Request,
    asset_ref: str = Query(min_length=3, max_length=512),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        resolved = user_assets.resolve_asset_ref(db, user.id, asset_ref)
    except (user_assets.InvalidAssetRef, user_assets.AssetNotFound) as error:
        raise _asset_error(error) from None
    if resolved.origin == "generated":
        from .assets import download as download_generated_asset

        return download_generated_asset(resolved.row.id, request, db, user)

    row = resolved.row
    try:
        path = storage.local_path(row.key)
    except Exception:
        raise HTTPException(404, "原文件不存在") from None
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "原文件不存在")
    audit.log(
        db,
        user_id=user.id,
        action="download_user_asset",
        biz_type="upload",
        ip=get_client_ip(request),
        detail={"asset_ref": resolved.asset_ref, "type": row.mime},
    )
    return FileResponse(
        str(path),
        filename=row.original_filename or path.name,
        media_type=row.mime or None,
    )


def _validate_draft_key(key: str) -> str:
    value = (key or "").strip().lower()
    if not _DRAFT_KEY_RE.match(value):
        raise HTTPException(400, "草稿 key 只能包含小写字母、数字、下划线和短横线")
    return value


def _draft_out(key: str, row: UserDraft | None) -> UserDraftOut:
    if row is None:
        return UserDraftOut(key=key, payload={}, updated_at=None)
    return UserDraftOut(key=row.key, payload=row.payload or {}, updated_at=row.updated_at)


def _numeric_saved_at(payload: dict, now_ms: int) -> int | None:
    value = payload.get("savedAt")
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    if (
        value <= 0
        or value > MAX_SAFE_INTEGER
        or value > now_ms + STUDIO_DRAFT_MAX_FUTURE_SKEW_MS
    ):
        return None
    return value


def _normalized_studio_payload(payload: dict, now_ms: int) -> tuple[dict, int | None]:
    saved_at = _numeric_saved_at(payload, now_ms)
    if "savedAt" not in payload:
        return payload, None
    normalized = dict(payload)
    if saved_at is None:
        normalized.pop("savedAt", None)
    else:
        normalized["savedAt"] = saved_at
    return normalized, saved_at


@router.get("/me/drafts/{key}", response_model=UserDraftOut)
def get_draft(
    key: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    key = _validate_draft_key(key)
    row = db.query(UserDraft).filter(UserDraft.user_id == user.id, UserDraft.key == key).first()
    return _draft_out(key, row)


@router.put("/me/drafts/{key}", response_model=UserDraftOut)
def save_draft(
    key: str,
    body: UserDraftIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    key = _validate_draft_key(key)
    if key == "studio":
        if db.get_bind().dialect.name == "sqlite":
            # SQLite ignores SELECT FOR UPDATE. A no-value-change write starts
            # a real write transaction so concurrent sessions serialize here.
            db.execute(update(User).where(User.id == user.id).values(id=User.id))
        else:
            # The user row always exists and serializes both first insert and
            # later updates for this user's singleton Studio draft.
            db.query(User.id).filter(User.id == user.id).with_for_update().one()
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    incoming_payload = body.payload
    incoming_saved_at = None
    if key == "studio":
        incoming_payload, incoming_saved_at = _normalized_studio_payload(body.payload, now_ms)
    row = db.query(UserDraft).filter(UserDraft.user_id == user.id, UserDraft.key == key).first()
    if row is None:
        row = UserDraft(user_id=user.id, key=key, payload=incoming_payload, updated_at=now)
        db.add(row)
    else:
        stored_saved_at = (
            _numeric_saved_at(row.payload or {}, now_ms) if key == "studio" else None
        )
        if (
            stored_saved_at is not None
            and (
                incoming_saved_at is None
                or (
                    stored_saved_at <= now_ms
                    and incoming_saved_at <= stored_saved_at
                )
            )
        ):
            db.commit()
            db.refresh(row)
            return _draft_out(key, row)
        row.payload = incoming_payload
        row.updated_at = now
    db.commit()
    db.refresh(row)
    return _draft_out(key, row)


@router.delete("/me/drafts/{key}")
def delete_draft(
    key: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    key = _validate_draft_key(key)
    row = db.query(UserDraft).filter(UserDraft.user_id == user.id, UserDraft.key == key).first()
    if row is not None:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.post("/me/password")
def change_password(body: ChangePasswordIn, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    fail_key = f"password:fail:{user.id}"
    ip_key = f"password:failip:{get_client_ip(request)}"
    if int(redis_client.get(fail_key) or 0) >= PASSWORD_FAIL_LIMIT:
        raise HTTPException(429, "密码错误次数过多,请稍后再试")
    if int(redis_client.get(ip_key) or 0) >= PASSWORD_FAIL_LIMIT * 3:
        raise HTTPException(429, "密码错误次数过多,请稍后再试")
    if not verify_password(body.old_password, user.password_hash):
        for key in (fail_key, ip_key):
            incr_window(key, PASSWORD_FAIL_WINDOW)
        audit.log(db, user_id=user.id, action="change_password_failed",
                  ip=get_client_ip(request))
        raise HTTPException(400, "原密码不正确")
    if len(body.new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"新密码至少 {MIN_PASSWORD_LEN} 位")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # invalidate all existing tokens -> must re-login
    db.commit()
    redis_client.delete(fail_key)
    audit.log(db, user_id=user.id, action="change_password", ip=get_client_ip(request))
    return {"ok": True, "relogin": True}


@router.post("/me/logout")
def logout(response: Response, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Server-side logout: invalidates every issued token for this user."""
    user.token_version += 1
    db.commit()
    response.delete_cookie(settings.auth_cookie_name, path="/", samesite="lax")
    return {"ok": True}
