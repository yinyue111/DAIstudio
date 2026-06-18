"""JWT issue / verify + password hashing (PBKDF2-SHA256, stdlib only)."""
from __future__ import annotations

import hashlib
import hmac
import os
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

_PBKDF2_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    """Return 'pbkdf2_sha256$iterations$salt_b64$hash_b64'."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${b64encode(salt).decode()}${b64encode(dk).decode()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algo, iters, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), b64decode(salt_b64), int(iters)
        )
        return hmac.compare_digest(dk, b64decode(hash_b64))
    except Exception:
        return False


def create_access_token(user_id: int, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "tv": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> tuple[int, int] | None:
    """Returns (user_id, token_version) or None."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return int(payload["sub"]), int(payload.get("tv", 0))
    except Exception:
        return None
