"""
Optional at-rest encryption helpers for TagLens.

If `TAGLENS_ENCRYPTION_KEYS` (or `TAGLENS_ENCRYPTION_KEY`) is set, selected
DB fields are stored encrypted. Existing plaintext rows remain readable.

Encrypted values are prefixed with `enc:v1:`.
"""

from __future__ import annotations

import os
from typing import Optional

PREFIX = "enc:v1:"


def _key_materials() -> list[str]:
    raw = os.getenv("TAGLENS_ENCRYPTION_KEYS") or os.getenv("TAGLENS_ENCRYPTION_KEY") or ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts


def _fernet_instances():
    keys = _key_materials()
    if not keys:
        return []
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return []
    instances = []
    for k in keys:
        try:
            instances.append(Fernet(k.encode("utf-8")))
        except Exception:
            continue
    return instances


def encrypt_text(value: str) -> str:
    if not value:
        return value
    if value.startswith(PREFIX):
        return value
    fernets = _fernet_instances()
    if not fernets:
        return value
    token = fernets[0].encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{PREFIX}{token}"


def decrypt_text(value: Optional[str]) -> str:
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return value
    token = value[len(PREFIX) :]
    fernets = _fernet_instances()
    for f in fernets:
        try:
            return f.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception:
            continue
    # Key missing/invalid; avoid returning ciphertext to callers.
    return ""

