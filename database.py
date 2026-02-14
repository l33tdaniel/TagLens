from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Sequence

import aiosqlite

# Author: Daniel Neugent

DB_PATH = Path(__file__).resolve().parent / "data" / "users.db"


@dataclass
class UserRecord:
    id: int
    username: str
    email: str
    password_hash: str
    created_at: str


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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NULL,
                    filename TEXT NOT NULL,
                    faces_json TEXT NOT NULL,
                    ocr_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """)
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
