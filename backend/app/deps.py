"""Common FastAPI dependencies (current user, admin guard, client ip)."""
from __future__ import annotations

import ipaddress

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User
from .security import decode_access_token


def get_client_ip(request: Request) -> str:
    direct_ip = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    trusted = settings.trusted_proxy_ip_list
    if fwd and trusted and _trusted_proxy_ip(direct_ip, trusted):
        # The leftmost XFF entry is client-controlled (spoofable). Walk
        # right-to-left, skipping our own trusted proxies; the first non-trusted
        # hop is the real client.
        for hop in reversed([h.strip() for h in fwd.split(",") if h.strip()]):
            if not _trusted_proxy_ip(hop, trusted):
                return hop
    return direct_ip


def _trusted_proxy_ip(ip: str, trusted: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in trusted:
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def resolve_token_user(db: Session, user_id: int, tv: int) -> User | None:
    """Return the user iff the token is still valid — not revoked (token_version)
    and active. None otherwise. Shared by the REST guard and the WS handler
    so the two auth paths can't drift."""
    user = db.get(User, user_id)
    if not user or tv != user.token_version or user.status != "active":
        return None
    return user


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    user_id, tv = decoded
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    if tv != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked, please re-login")
    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account not active")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user
