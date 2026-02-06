from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from itsdangerous import BadData, URLSafeTimedSerializer
from passlib.context import CryptContext

# Author: Daniel Neugent

SESSION_COOKIE_NAME = "taglens_session"
DEFAULT_MAX_AGE = 60 * 60 * 24  # 1 day

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@dataclass
class Session:
    token: str
    user_id: int


def _load_secret_key() -> str:
    """Try official and fallback env variables before generating a temporary secret."""
    secret = os.getenv("ROBYN_SECRET_KEY")
    if secret:
        return secret
    fallback = os.getenv("TAGLENS_FALLBACK_SECRET")
    if fallback:
        return fallback
    random = secrets.token_urlsafe(32)
    print(
        "[WARN] Using a randomly generated signing secret. Sessions will break when the process restarts. Set ROBYN_SECRET_KEY to a fixed value."
    )
    return random


class SessionManager:
    """Generates and validates signed session tokens for website visitors."""

    def __init__(self, *, expiration: timedelta | int = DEFAULT_MAX_AGE) -> None:
        raw_secret = _load_secret_key()
        self.serializer = URLSafeTimedSerializer(raw_secret, salt="taglens.session")
        self.expiration_seconds = int(
            expiration.total_seconds()
            if isinstance(expiration, timedelta)
            else expiration
        )

    def create_token(self, user_id: int) -> Session:
        """Issue a signed token that embeds the user id."""
        self._assert_positive(user_id)
        token = self.serializer.dumps({"uid": user_id})
        return Session(token=token, user_id=user_id)

    def decode(self, token: str) -> Optional[int]:
        """Return the user id encoded in the token, if it is valid and not expired."""
        if not token:
            return None
        try:
            payload = self.serializer.loads(token, max_age=self.expiration_seconds)
            return int(payload.get("uid"))
        except (BadData, ValueError):
            return None

    @staticmethod
    def _assert_positive(value: Any) -> None:
        if not isinstance(value, int) or value <= 0:
            raise ValueError("user_id must be a positive integer")


def hash_password(plain: str) -> str:
    """Wrap passlib's bcrypt hash generator."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Compare a plaintext password against the stored hash."""
    return pwd_context.verify(plain, hashed)


def cookie_settings(*, secure: bool | None = None) -> Dict[str, Any]:
    """Standard cookie arguments that make session cookies httponly and samesite=lax."""
    env_secure = os.getenv("ROBYN_SECURE_COOKIES")
    forced_secure = None
    if env_secure is not None:
        forced_secure = env_secure.lower() in {"1", "true", "yes"}
    secure_flag = (
        secure
        if secure is not None
        else forced_secure if forced_secure is not None else False
    )
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": secure_flag,
        "max_age": DEFAULT_MAX_AGE,
        "path": "/",
    }


def cookie_clear_settings() -> Dict[str, Any]:
    """Special cookie instructions required to immediately forget a session."""
    return {
        "max_age": 0,
        "expires": "Thu, 01 Jan 1970 00:00:00 GMT",
        "path": "/",
        "secure": False,
        "httponly": True,
        "samesite": "lax",
    }
