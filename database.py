from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Sequence

import aiosqlite

# Author: Daniel Neugent

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "users.db"


def _resolve_db_path() -> Path:
    env_path = os.getenv("TAGLENS_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


DB_PATH = _resolve_db_path()


@dataclass
class UserRecord:
    id: int
    username: str
    email: str
    password_hash: str
    created_at: str


@dataclass
class SessionRecord:
    id: int
    user_id: int
    token_hash: str
    created_at: str
    expires_at: str
    last_seen_at: str
    user_agent: Optional[str]
    ip_address: Optional[str]
    revoked_at: Optional[str]


@dataclass
class ImageRecord:
    id: int
    user_id: Optional[int]
    filename: str
    faces_json: str
    ocr_text: str
    created_at: str


class Database:
    """Lightweight wrapper around aiosqlite for user persistence."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open a connection with foreign-key support enabled."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON;")
            yield conn

    async def initialize(self) -> None:
        """Create directories and ensure the users table exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connection() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    user_agent TEXT,
                    ip_address TEXT,
                    revoked_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NULL,
                    filename TEXT NOT NULL,
                    faces_json TEXT NOT NULL,
                    ocr_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """
            )
            await conn.commit()

    async def fetch_one(
        self, query: str, params: Sequence[Any]
    ) -> Optional[aiosqlite.Row]:
        """Execute a single-row SELECT statement with given parameters."""
        async with self._connection() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def fetch_user_by_email(self, email: str) -> Optional[UserRecord]:
        """Find a user row by their normalized (lowercased) email address."""
        row = await self.fetch_one(
            "SELECT id, username, email, password_hash, created_at FROM users WHERE email = ?",
            (email.lower(),),
        )
        return UserRecord(**row) if row else None

    async def fetch_user_by_id(self, user_id: int) -> Optional[UserRecord]:
        """Retrieve a user record directly from its primary key."""
        row = await self.fetch_one(
            "SELECT id, username, email, password_hash, created_at FROM users WHERE id = ?",
            (user_id,),
        )
        return UserRecord(**row) if row else None

    async def create_user(
        self, username: str, email: str, password_hash: str
    ) -> UserRecord:
        """Insert a new user and return the constructed dataclass."""
        created_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO users (username, email, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (username, email.lower(), password_hash, created_at),
            )
            await conn.commit()
            lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise RuntimeError("Failed to read the inserted user ID.")
        user_id = int(lastrowid)
        return UserRecord(
            id=user_id,
            username=username,
            email=email.lower(),
            password_hash=password_hash,
            created_at=created_at,
        )

    async def create_session(
        self,
        *,
        user_id: int,
        token_hash: str,
        expires_at: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> SessionRecord:
        """Insert a new session row for a user."""
        created_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO sessions (
                    user_id,
                    token_hash,
                    created_at,
                    expires_at,
                    last_seen_at,
                    user_agent,
                    ip_address,
                    revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    user_id,
                    token_hash,
                    created_at,
                    expires_at,
                    created_at,
                    user_agent,
                    ip_address,
                ),
            )
            await conn.commit()
            lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise RuntimeError("Failed to read the inserted session ID.")
        return SessionRecord(
            id=int(lastrowid),
            user_id=user_id,
            token_hash=token_hash,
            created_at=created_at,
            expires_at=expires_at,
            last_seen_at=created_at,
            user_agent=user_agent,
            ip_address=ip_address,
            revoked_at=None,
        )

    async def create_image_metadata(
        self, filename: str, faces_json: str, ocr_text: str, user_id: Optional[int] = None
    ) -> ImageRecord:
        """Insert processed image metadata and return the constructed dataclass."""
        created_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO images (user_id, filename, faces_json, ocr_text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, filename, faces_json, ocr_text, created_at),
            )
            await conn.commit()
            lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise RuntimeError("Failed to read the inserted image ID.")
        image_id = int(lastrowid)
        return ImageRecord(
            id=image_id,
            user_id=user_id,
            filename=filename,
            faces_json=faces_json,
            ocr_text=ocr_text,
            created_at=created_at,
        )

    async def fetch_session_by_token_hash(
        self, token_hash: str
    ) -> Optional[SessionRecord]:
        """Retrieve a session by its hashed token value."""
        row = await self.fetch_one(
            """
            SELECT id, user_id, token_hash, created_at, expires_at,
                   last_seen_at, user_agent, ip_address, revoked_at
            FROM sessions
            WHERE token_hash = ?
            """,
            (token_hash,),
        )
        return SessionRecord(**row) if row else None

    async def touch_session(self, session_id: int, last_seen_at: str) -> None:
        """Update the session activity timestamp."""
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (last_seen_at, session_id),
            )
            await conn.commit()

    async def revoke_session(self, session_id: int, revoked_at: str) -> None:
        """Mark a session as revoked."""
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ?",
                (revoked_at, session_id),
            )
            await conn.commit()

    async def revoke_session_by_hash(self, token_hash: str, revoked_at: str) -> None:
        """Mark a session as revoked by its token hash."""
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?",
                (revoked_at, token_hash),
            )
            await conn.commit()
