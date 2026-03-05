"""
SQLite persistence layer for TagLens.

Purpose:
    Defines DB schema, record dataclasses, and async helpers for CRUD access.

Authorship (git history, mapped to real names):
    Daniel (l33tdaniel), Chloe (n518t893)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any, AsyncIterator, Optional, Sequence

import aiosqlite

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
    ai_description: str
    content_type: str
    image_data: Optional[bytes]
    thumbnail_data: Optional[bytes]
    thumbnail_content_type: str
    taken_at: Optional[str]
    created_at: str


@dataclass
class ImageMetadataRecord:
    id: int
    image_id: int
    user_id: int
    faces_json: str
    ocr_text: str
    caption: str
    lat: Optional[float]
    lon: Optional[float]
    loc_description: Optional[str]
    loc_city: Optional[str]
    loc_state: Optional[str]
    loc_country: Optional[str]
    make: Optional[str]
    model: Optional[str]
    iso: Optional[int]
    f_stop: Optional[float]
    shutter_speed: Optional[str]
    focal_length: Optional[float]
    width: Optional[int]
    height: Optional[int]
    file_size_mb: Optional[float]
    taken_at: Optional[str]
    created_at: str
    updated_at: str


class Database:
    """Lightweight wrapper around aiosqlite for user persistence."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def _get_conn(self) -> aiosqlite.Connection:
        """Return the persistent connection, creating it on first use."""
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            # WAL + foreign keys keep concurrency safe for multi-request access.
            await self._conn.execute("PRAGMA foreign_keys = ON;")
            await self._conn.execute("PRAGMA journal_mode = WAL;")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Return the persistent connection as a context manager."""
        yield await self._get_conn()

    async def initialize(self) -> None:
        """Create directories and ensure the users table exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connection() as conn:
            # Core identity tables.
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
                """)
            # Images are stored in a single table; image_data is optional when
            # using remote storage or lazy loading.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NULL,
                    filename TEXT NOT NULL,
                    faces_json TEXT NOT NULL,
                    ocr_text TEXT NOT NULL,
                    ai_description TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    image_data BLOB,
                    thumbnail_data BLOB,
                    thumbnail_content_type TEXT NOT NULL DEFAULT 'image/webp',
                    taken_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """)
            # Metadata is separate to allow incremental enrichment without
            # rewriting the image row.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS image_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    faces_json TEXT NOT NULL DEFAULT '[]',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    lat REAL,
                    lon REAL,
                    loc_description TEXT,
                    loc_city TEXT,
                    loc_state TEXT,
                    loc_country TEXT,
                    make TEXT,
                    model TEXT,
                    iso INTEGER,
                    f_stop REAL,
                    shutter_speed TEXT,
                    focal_length REAL,
                    width INTEGER,
                    height INTEGER,
                    file_size_mb REAL,
                    taken_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
                )
                """)
            try:
                # One-time migration for older DBs missing ai_description.
                await conn.execute(
                    "ALTER TABLE images ADD COLUMN ai_description TEXT NOT NULL DEFAULT ''"
                )
            except aiosqlite.OperationalError:
                # Existing databases will already have this column after first migration.
                pass
            try:
                await conn.execute(
                    "ALTER TABLE images ADD COLUMN content_type TEXT NOT NULL DEFAULT 'application/octet-stream'"
                )
            except aiosqlite.OperationalError:
                pass
            try:
                await conn.execute("ALTER TABLE images ADD COLUMN image_data BLOB")
            except aiosqlite.OperationalError:
                pass
            try:
                await conn.execute(
                    "ALTER TABLE images ADD COLUMN thumbnail_data BLOB"
                )
            except aiosqlite.OperationalError:
                pass
            try:
                await conn.execute(
                    "ALTER TABLE images ADD COLUMN thumbnail_content_type TEXT NOT NULL DEFAULT 'image/webp'"
                )
            except aiosqlite.OperationalError:
                pass
            try:
                await conn.execute(
                    "ALTER TABLE images ADD COLUMN taken_at TEXT"
                )
            except aiosqlite.OperationalError:
                pass

            # FTS5 full-text search index
            await conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(
                    image_id UNINDEXED,
                    filename,
                    ai_description,
                    ocr_text,
                    caption,
                    location,
                    content='',
                    tokenize='porter unicode61'
                )
            """)

            await conn.commit()

            # Populate FTS index from existing data if empty
            cursor = await conn.execute("SELECT COUNT(*) FROM images_fts")
            fts_count = (await cursor.fetchone())[0]
            await cursor.close()
            if fts_count == 0:
                await self._rebuild_fts_index(conn)

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
        self,
        filename: str,
        faces_json: str,
        ocr_text: str,
        user_id: Optional[int] = None,
        ai_description: str = "",
        content_type: str = "application/octet-stream",
        image_data: Optional[bytes] = None,
        thumbnail_data: Optional[bytes] = None,
        thumbnail_content_type: str = "image/webp",
        taken_at: Optional[str] = None,
    ) -> ImageRecord:
        """Insert processed image metadata and return the constructed dataclass."""
        created_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO images (
                    user_id,
                    filename,
                    faces_json,
                    ocr_text,
                    ai_description,
                    content_type,
                    image_data,
                    thumbnail_data,
                    thumbnail_content_type,
                    taken_at,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    filename,
                    faces_json,
                    ocr_text,
                    ai_description,
                    content_type,
                    image_data,
                    thumbnail_data,
                    thumbnail_content_type,
                    taken_at,
                    created_at,
                ),
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
            ai_description=ai_description,
            content_type=content_type,
            image_data=image_data,
            thumbnail_data=thumbnail_data,
            thumbnail_content_type=thumbnail_content_type,
            taken_at=taken_at,
            created_at=created_at,
        )

    async def list_images_for_user(
        self,
        user_id: int,
        *,
        sort_by: str = "uploaded",
        order: str = "desc",
    ) -> list[ImageRecord]:
        """Return all images owned by the given user with configurable sorting."""
        sort_clause = "created_at"
        if sort_by == "taken":
            sort_clause = "COALESCE(taken_at, created_at)"
        order_clause = "DESC" if order.lower() == "desc" else "ASC"
        async with self._connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT
                    id,
                    user_id,
                    filename,
                    faces_json,
                    ocr_text,
                    ai_description,
                    content_type,
                    NULL AS image_data,
                    NULL AS thumbnail_data,
                    thumbnail_content_type,
                    taken_at,
                    created_at
                FROM images
                WHERE user_id = ?
                ORDER BY {sort_clause} {order_clause}, id DESC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [ImageRecord(**row) for row in rows]

    async def fetch_image_for_user(
        self, image_id: int, user_id: int
    ) -> Optional[ImageRecord]:
        row = await self.fetch_one(
            """
            SELECT
                id,
                user_id,
                filename,
                faces_json,
                ocr_text,
                ai_description,
                content_type,
                image_data,
                thumbnail_data,
                thumbnail_content_type,
                taken_at,
                created_at
            FROM images
            WHERE id = ? AND user_id = ?
            """,
            (image_id, user_id),
        )
        return ImageRecord(**row) if row else None

    async def update_image_thumbnail(
        self,
        image_id: int,
        user_id: int,
        thumbnail_data: bytes,
        thumbnail_content_type: str = "image/webp",
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(
                """
                UPDATE images
                SET thumbnail_data = ?, thumbnail_content_type = ?
                WHERE id = ? AND user_id = ?
                """,
                (thumbnail_data, thumbnail_content_type, image_id, user_id),
            )
            await conn.commit()

    async def clear_image_thumbnail(self, image_id: int, user_id: int) -> None:
        async with self._connection() as conn:
            await conn.execute(
                """
                UPDATE images
                SET thumbnail_data = NULL, thumbnail_content_type = NULL
                WHERE id = ? AND user_id = ?
                """,
                (image_id, user_id),
            )
            await conn.commit()

    async def delete_image_for_user(self, image_id: int, user_id: int) -> bool:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM images WHERE id = ? AND user_id = ?",
                (image_id, user_id),
            )
            await conn.commit()
            deleted = cursor.rowcount or 0
            await cursor.close()
        return deleted > 0

    async def fetch_image_metadata_for_user(
        self, image_id: int, user_id: int
    ) -> Optional[ImageMetadataRecord]:
        row = await self.fetch_one(
            """
            SELECT
                id,
                image_id,
                user_id,
                faces_json,
                ocr_text,
                caption,
                lat,
                lon,
                loc_description,
                loc_city,
                loc_state,
                loc_country,
                make,
                model,
                iso,
                f_stop,
                shutter_speed,
                focal_length,
                width,
                height,
                file_size_mb,
                taken_at,
                created_at,
                updated_at
            FROM image_metadata
            WHERE image_id = ? AND user_id = ?
            """,
            (image_id, user_id),
        )
        return ImageMetadataRecord(**row) if row else None

    async def upsert_image_metadata(
        self,
        *,
        image_id: int,
        user_id: int,
        faces_json: str,
        ocr_text: str,
        caption: str,
        lat: Optional[float],
        lon: Optional[float],
        loc_description: Optional[str],
        loc_city: Optional[str],
        loc_state: Optional[str],
        loc_country: Optional[str],
        make: Optional[str],
        model: Optional[str],
        iso: Optional[int],
        f_stop: Optional[float],
        shutter_speed: Optional[str],
        focal_length: Optional[float],
        width: Optional[int],
        height: Optional[int],
        file_size_mb: Optional[float],
        taken_at: Optional[str],
    ) -> None:
        now = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            await conn.execute(
                """
                INSERT INTO image_metadata (
                    image_id,
                    user_id,
                    faces_json,
                    ocr_text,
                    caption,
                    lat,
                    lon,
                    loc_description,
                    loc_city,
                    loc_state,
                    loc_country,
                    make,
                    model,
                    iso,
                    f_stop,
                    shutter_speed,
                    focal_length,
                    width,
                    height,
                    file_size_mb,
                    taken_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                    faces_json=excluded.faces_json,
                    ocr_text=excluded.ocr_text,
                    caption=excluded.caption,
                    lat=excluded.lat,
                    lon=excluded.lon,
                    loc_description=excluded.loc_description,
                    loc_city=excluded.loc_city,
                    loc_state=excluded.loc_state,
                    loc_country=excluded.loc_country,
                    make=excluded.make,
                    model=excluded.model,
                    iso=excluded.iso,
                    f_stop=excluded.f_stop,
                    shutter_speed=excluded.shutter_speed,
                    focal_length=excluded.focal_length,
                    width=excluded.width,
                    height=excluded.height,
                    file_size_mb=excluded.file_size_mb,
                    taken_at=excluded.taken_at,
                    updated_at=excluded.updated_at
                """,
                (
                    image_id,
                    user_id,
                    faces_json,
                    ocr_text,
                    caption,
                    lat,
                    lon,
                    loc_description,
                    loc_city,
                    loc_state,
                    loc_country,
                    make,
                    model,
                    iso,
                    f_stop,
                    shutter_speed,
                    focal_length,
                    width,
                    height,
                    file_size_mb,
                    taken_at,
                    now,
                    now,
                ),
            )
            await conn.commit()

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

    async def update_image_description(
        self, image_id: int, user_id: int, description: str
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE images SET ai_description = ? WHERE id = ? AND user_id = ?",
                (description, image_id, user_id),
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

    # ── FTS5 Search ─────────────────────────────────────────

    async def populate_fts_for_image(self, image_id: int, user_id: int) -> None:
        async with self._connection() as conn:
            # Gather text from both tables
            cursor = await conn.execute(
                "SELECT filename, ai_description, ocr_text FROM images WHERE id = ? AND user_id = ?",
                (image_id, user_id),
            )
            img_row = await cursor.fetchone()
            await cursor.close()
            if not img_row:
                return

            cursor = await conn.execute(
                "SELECT caption, ocr_text, loc_description, loc_city, loc_state, loc_country FROM image_metadata WHERE image_id = ? AND user_id = ?",
                (image_id, user_id),
            )
            meta_row = await cursor.fetchone()
            await cursor.close()

            caption = ""
            location = ""
            meta_ocr = ""
            if meta_row:
                caption = meta_row["caption"] or ""
                meta_ocr = meta_row["ocr_text"] or ""
                location = " ".join(
                    filter(None, [meta_row["loc_description"], meta_row["loc_city"],
                                  meta_row["loc_state"], meta_row["loc_country"]])
                )

            ocr_text = img_row["ocr_text"] or meta_ocr

            # Delete old entry then insert (contentless FTS doesn't support UPDATE)
            await conn.execute("DELETE FROM images_fts WHERE image_id = ?", (str(image_id),))
            await conn.execute(
                "INSERT INTO images_fts (image_id, filename, ai_description, ocr_text, caption, location) VALUES (?, ?, ?, ?, ?, ?)",
                (str(image_id), img_row["filename"] or "", img_row["ai_description"] or "", ocr_text, caption, location),
            )
            await conn.commit()

    async def search_images_for_user(
        self,
        user_id: int,
        query: str,
        *,
        sort_by: str = "uploaded",
        order: str = "desc",
        limit: int = 50,
    ) -> list[ImageRecord]:
        # Escape FTS5 special characters and wrap each token with *
        clean = re.sub(r'[^\w\s]', ' ', query).strip()
        if not clean:
            return []
        tokens = clean.split()
        fts_query = " ".join(f'"{t}"*' for t in tokens)

        sort_clause = "i.created_at"
        if sort_by == "taken":
            sort_clause = "COALESCE(i.taken_at, i.created_at)"
        order_clause = "DESC" if order.lower() == "desc" else "ASC"

        async with self._connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT
                    i.id, i.user_id, i.filename, i.faces_json, i.ocr_text,
                    i.ai_description, i.content_type,
                    NULL AS image_data, NULL AS thumbnail_data,
                    i.thumbnail_content_type, i.taken_at, i.created_at
                FROM images_fts f
                JOIN images i ON CAST(f.image_id AS INTEGER) = i.id
                WHERE images_fts MATCH ? AND i.user_id = ?
                ORDER BY {sort_clause} {order_clause}, i.id DESC
                LIMIT ?
                """,
                (fts_query, user_id, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [ImageRecord(**row) for row in rows]

    async def _rebuild_fts_index(self, conn: aiosqlite.Connection) -> None:
        cursor = await conn.execute("""
            SELECT i.id, i.filename, i.ai_description, i.ocr_text,
                   m.caption, m.ocr_text AS meta_ocr,
                   m.loc_description, m.loc_city, m.loc_state, m.loc_country
            FROM images i
            LEFT JOIN image_metadata m ON m.image_id = i.id
        """)
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            location = " ".join(
                filter(None, [row["loc_description"], row["loc_city"],
                              row["loc_state"], row["loc_country"]])
            )
            ocr = row["ocr_text"] or row["meta_ocr"] or ""
            await conn.execute(
                "INSERT INTO images_fts (image_id, filename, ai_description, ocr_text, caption, location) VALUES (?, ?, ?, ?, ?, ?)",
                (str(row["id"]), row["filename"] or "", row["ai_description"] or "", ocr, row["caption"] or "", location),
            )
        await conn.commit()
