from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict

from passlib.context import CryptContext

# Author: Daniel Neugent

SESSION_COOKIE_NAME = "taglens_session"
CSRF_COOKIE_NAME = "taglens_csrf"
DEFAULT_MAX_AGE = 60 * 60 * 24  # 1 day

# Prefer Argon2 for new hashes; allow legacy PBKDF2/bcrypt verification.
pwd_context = CryptContext(
    schemes=["argon2", "pbkdf2_sha256", "bcrypt"],
    default="argon2",
    deprecated="auto",
)


@dataclass
class SessionToken:
    token: str
    token_hash: str


def _token_hash(raw: str) -> str:
    """Return a stable hash of the session token for database storage."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_session_token() -> SessionToken:
    """Create a new opaque session token and its hash."""
    token = secrets.token_urlsafe(32)
    return SessionToken(token=token, token_hash=_token_hash(token))


def verify_session_token(raw: str, expected_hash: str) -> bool:
    """Constant-time check that a raw token matches a stored hash."""
    if not raw or not expected_hash:
        return False
    return secrets.compare_digest(_token_hash(raw), expected_hash)


def hash_session_token(raw: str) -> str:
    """Generate the hash for a raw session token."""
    return _token_hash(raw)


def generate_csrf_token() -> str:
    """Return a random token suitable for double-submit CSRF protection."""
    return secrets.token_urlsafe(32)


def verify_csrf_token(cookie_token: str | None, form_token: str | None) -> bool:
    """Compare CSRF tokens using constant time comparison."""
    if not cookie_token or not form_token:
        return False
    return secrets.compare_digest(cookie_token, form_token)


def session_expiration(max_age: timedelta | int = DEFAULT_MAX_AGE) -> int:
    """Normalize expiration into integer seconds."""
    return int(max_age.total_seconds() if isinstance(max_age, timedelta) else max_age)


def hash_password(plain: str) -> str:
    """Wrap passlib's bcrypt hash generator."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Compare a plaintext password against the stored hash."""
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def _resolve_secure_flag(secure: bool | None = None) -> bool:
    """Determine if cookies should be marked secure."""
    env_secure = os.getenv("ROBYN_SECURE_COOKIES")
    forced_secure = None
    if env_secure is not None:
        forced_secure = env_secure.lower() in {"1", "true", "yes"}
    if secure is not None:
        return secure
    if forced_secure is not None:
        return forced_secure
    env_mode = (os.getenv("ROBYN_ENV") or os.getenv("ENV") or "").lower()
    return env_mode in {"prod", "production"}


def cookie_settings(*, secure: bool | None = None) -> Dict[str, Any]:
    """Standard cookie arguments that make session cookies httponly and samesite=lax."""
    secure_flag = _resolve_secure_flag(secure)
    return {
        "http_only": True,
        "same_site": "lax",
        "secure": secure_flag,
        "max_age": DEFAULT_MAX_AGE,
        "path": "/",
    }


def csrf_cookie_settings(*, secure: bool | None = None) -> Dict[str, Any]:
    """Cookie settings for CSRF tokens (not httponly)."""
    secure_flag = _resolve_secure_flag(secure)
    return {
        "http_only": False,
        "same_site": "lax",
        "secure": secure_flag,
        "max_age": DEFAULT_MAX_AGE,
        "path": "/",
    }


def cookie_clear_settings(*, secure: bool | None = None) -> Dict[str, Any]:
    """Special cookie instructions required to immediately forget a session."""
    secure_flag = _resolve_secure_flag(secure)
    return {
        "max_age": 0,
        "expires": "Thu, 01 Jan 1970 00:00:00 GMT",
        "path": "/",
        "secure": secure_flag,
        "http_only": True,
        "same_site": "lax",
    }
