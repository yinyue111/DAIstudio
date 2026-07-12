from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import User, UserDraft
from ..password_policy import MIN_PASSWORD_LEN
from ..redis_client import redis_client
from ..schemas import ChangePasswordIn, UserDraftIn, UserDraftOut, UserOut
from ..security import hash_password, verify_password
from ..services import audit
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
