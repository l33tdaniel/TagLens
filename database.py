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
from typing import Any, AsyncIterator, Optional, Sequence

import aiosqlite

from crypto import decrypt_text, encrypt_text

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

    make: Optional[str] = None
    model: Optional[str] = None
    iso: Optional[int] = None
    f_stop: Optional[float] = None
    shutter: Optional[str] = None
    focal: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    loc_desc: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None


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


@dataclass
class FaceEmbeddingRecord:
    user_id: int
    tag: str
    embedding_json: str
    samples_count: int
    updated_at: str


@dataclass
class UserSettingsRecord:
    user_id: int
    ai_descriptions_enabled: int
    ocr_enabled: int
    face_recognition_enabled: int
    store_originals_enabled: int
    retention_days: Optional[int]
    created_at: str
    updated_at: str


@dataclass
class JobRecord:
    id: int
    user_id: int
    image_id: int
    kind: str
    payload_json: str
    status: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    error: Optional[str]


@dataclass
class PhotoShareRecord:
    id: int
    image_id: int
    token_hash: str
    token_prefix: str
    expires_at: Optional[str]
    revoked_at: Optional[str]
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
            # Enforce relational integrity at the SQLite layer.
            await conn.execute("PRAGMA foreign_keys = ON;")
            yield conn

    async def initialize(self) -> None:
        """Create directories and ensure the users table exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connection() as conn:
            # Improve read/write concurrency for concurrent requests.
            await conn.execute("PRAGMA journal_mode = WAL;")
            await conn.execute("PRAGMA synchronous = NORMAL;")
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
            # Images are stored in a single table; blob columns are optional.
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
                    make TEXT,
                    model TEXT,
                    iso INTEGER,
                    f_stop REAL,
                    shutter TEXT,
                    focal REAL,
                    lat REAL,
                    lon REAL,
                    loc_desc TEXT,
                    city TEXT,
                    state TEXT,
                    country TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """)
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
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS face_embeddings (
                    user_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    samples_count INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, tag),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    ai_descriptions_enabled INTEGER NOT NULL DEFAULT 1,
                    ocr_enabled INTEGER NOT NULL DEFAULT 1,
                    face_recognition_enabled INTEGER NOT NULL DEFAULT 1,
                    store_originals_enabled INTEGER NOT NULL DEFAULT 1,
                    retention_days INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS photo_shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS photo_acl (
                    image_id INTEGER NOT NULL,
                    grantee_user_id INTEGER NOT NULL,
                    permission TEXT NOT NULL DEFAULT 'read',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (image_id, grantee_user_id, permission),
                    FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE,
                    FOREIGN KEY(grantee_user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    image_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
                )
                """
            )
            try:
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
            try:
                await conn.execute(
                    "ALTER TABLE face_embeddings ADD COLUMN samples_count INTEGER NOT NULL DEFAULT 1"
                )
            except aiosqlite.OperationalError:
                pass
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_images_user_created_at ON images(user_id, created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_images_user_taken_at ON images(user_id, taken_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photo_shares_image_id ON photo_shares(image_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photo_acl_grantee_user ON photo_acl(grantee_user_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_image_id ON jobs(image_id)"
            )
            await conn.commit()

    async def healthcheck(self) -> bool:
        """
        Lightweight readiness probe for the sqlite database.

        We avoid touching application tables here; a trivial SELECT is enough
        to confirm the file is reachable and SQLite can execute a statement.
        """
        try:
            async with self._connection() as conn:
                await conn.execute("SELECT 1;")
            return True
        except Exception:
            return False

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

    async def fetch_image_by_id(self, image_id: int) -> Optional[ImageRecord]:
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
            WHERE id = ?
            """,
            (image_id,),
        )
        if not row:
            return None
        record = ImageRecord(**row)
        record.faces_json = decrypt_text(record.faces_json)
        record.ocr_text = decrypt_text(record.ocr_text)
        record.ai_description = decrypt_text(record.ai_description)
        return record

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
            user_id = cursor.lastrowid
            if user_id is None:
                raise RuntimeError("Failed to read the inserted user ID.")
            await conn.execute(
                """
                INSERT OR IGNORE INTO user_settings (
                    user_id,
                    ai_descriptions_enabled,
                    ocr_enabled,
                    face_recognition_enabled,
                    store_originals_enabled,
                    retention_days,
                    created_at,
                    updated_at
                )
                VALUES (?, 1, 1, 1, 1, NULL, ?, ?)
                """,
                (int(user_id), created_at, created_at),
            )
            await conn.commit()
        user_id_int = int(user_id)
        return UserRecord(
            id=user_id_int,
            username=username,
            email=email.lower(),
            password_hash=password_hash,
            created_at=created_at,
        )

    async def fetch_user_settings(self, user_id: int) -> Optional[UserSettingsRecord]:
        row = await self.fetch_one(
            """
            SELECT
                user_id,
                ai_descriptions_enabled,
                ocr_enabled,
                face_recognition_enabled,
                store_originals_enabled,
                retention_days,
                created_at,
                updated_at
            FROM user_settings
            WHERE user_id = ?
            """,
            (user_id,),
        )
        return UserSettingsRecord(**row) if row else None

    async def list_users_with_retention(self) -> list[tuple[int, int]]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT user_id, retention_days
                FROM user_settings
                WHERE retention_days IS NOT NULL AND retention_days >= 1
                ORDER BY user_id ASC
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [(int(r["user_id"]), int(r["retention_days"])) for r in rows]

    async def ensure_user_settings(self, user_id: int) -> UserSettingsRecord:
        settings = await self.fetch_user_settings(user_id)
        if settings:
            return settings
        now = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO user_settings (
                    user_id,
                    ai_descriptions_enabled,
                    ocr_enabled,
                    face_recognition_enabled,
                    store_originals_enabled,
                    retention_days,
                    created_at,
                    updated_at
                )
                VALUES (?, 1, 1, 1, 1, NULL, ?, ?)
                """,
                (user_id, now, now),
            )
            await conn.commit()
        settings = await self.fetch_user_settings(user_id)
        if not settings:
            raise RuntimeError("Failed to initialize user settings.")
        return settings

    async def update_user_settings(
        self,
        user_id: int,
        *,
        ai_descriptions_enabled: Optional[bool] = None,
        ocr_enabled: Optional[bool] = None,
        face_recognition_enabled: Optional[bool] = None,
        store_originals_enabled: Optional[bool] = None,
        retention_days: Optional[int] = None,
    ) -> UserSettingsRecord:
        current = await self.ensure_user_settings(user_id)
        updated_at = datetime.utcnow().isoformat()
        next_ai = int(ai_descriptions_enabled) if ai_descriptions_enabled is not None else current.ai_descriptions_enabled
        next_ocr = int(ocr_enabled) if ocr_enabled is not None else current.ocr_enabled
        next_faces = int(face_recognition_enabled) if face_recognition_enabled is not None else current.face_recognition_enabled
        next_originals = int(store_originals_enabled) if store_originals_enabled is not None else current.store_originals_enabled
        next_retention = retention_days if retention_days is not None else current.retention_days
        async with self._connection() as conn:
            await conn.execute(
                """
                UPDATE user_settings
                SET
                    ai_descriptions_enabled = ?,
                    ocr_enabled = ?,
                    face_recognition_enabled = ?,
                    store_originals_enabled = ?,
                    retention_days = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    next_ai,
                    next_ocr,
                    next_faces,
                    next_originals,
                    next_retention,
                    updated_at,
                    user_id,
                ),
            )
            await conn.commit()
        settings = await self.fetch_user_settings(user_id)
        if not settings:
            raise RuntimeError("Failed to read updated user settings.")
        return settings

    async def enqueue_job(
        self,
        *,
        user_id: int,
        image_id: int,
        kind: str,
        payload_json: str,
    ) -> int:
        created_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO jobs (user_id, image_id, kind, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                (user_id, image_id, kind, payload_json, created_at),
            )
            await conn.commit()
            job_id = cursor.lastrowid
        if job_id is None:
            raise RuntimeError("Failed to create job.")
        return int(job_id)

    async def claim_next_job(self, *, kind: str) -> Optional[JobRecord]:
        """Atomically claim the next queued job for processing."""
        now = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            await conn.execute("BEGIN IMMEDIATE;")
            cursor = await conn.execute(
                """
                SELECT
                    id, user_id, image_id, kind, payload_json, status,
                    created_at, started_at, completed_at, error
                FROM jobs
                WHERE status = 'queued' AND kind = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (kind,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if not row:
                await conn.commit()
                return None
            update_cursor = await conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, int(row["id"])),
            )
            claimed = (update_cursor.rowcount or 0) > 0
            await update_cursor.close()
            await conn.commit()
        if not claimed:
            return None
        data = dict(row)
        data["status"] = "running"
        data["started_at"] = now
        return JobRecord(**data)

    async def complete_job(self, job_id: int) -> None:
        now = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            await conn.execute(
                """
                UPDATE jobs
                SET status = 'completed', completed_at = ?, error = NULL
                WHERE id = ?
                """,
                (now, job_id),
            )
            await conn.commit()

    async def fail_job(self, job_id: int, error: str) -> None:
        now = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            await conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE id = ?
                """,
                (now, error[:1000], job_id),
            )
            await conn.commit()

    async def list_jobs_for_image(self, image_id: int, *, limit: int = 10) -> list[JobRecord]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, user_id, image_id, kind, payload_json, status,
                       created_at, started_at, completed_at, error
                FROM jobs
                WHERE image_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (image_id, int(limit)),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [JobRecord(**row) for row in rows]

    async def fetch_latest_job_for_image(self, image_id: int, *, kind: str) -> Optional[JobRecord]:
        row = await self.fetch_one(
            """
            SELECT id, user_id, image_id, kind, payload_json, status,
                   created_at, started_at, completed_at, error
            FROM jobs
            WHERE image_id = ? AND kind = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (image_id, kind),
        )
        return JobRecord(**row) if row else None

    async def update_image_processing_fields(
        self,
        *,
        image_id: int,
        user_id: int,
        faces_json: Optional[str] = None,
        ocr_text: Optional[str] = None,
        ai_description: Optional[str] = None,
        taken_at: Optional[str] = None,
    ) -> None:
        sets = []
        params: list[Any] = []
        if faces_json is not None:
            sets.append("faces_json = ?")
            params.append(encrypt_text(faces_json))
        if ocr_text is not None:
            sets.append("ocr_text = ?")
            params.append(encrypt_text(ocr_text))
        if ai_description is not None:
            sets.append("ai_description = ?")
            params.append(encrypt_text(ai_description))
        if taken_at is not None:
            sets.append("taken_at = ?")
            params.append(taken_at)
        if not sets:
            return
        params.extend([image_id, user_id])
        async with self._connection() as conn:
            await conn.execute(
                f"UPDATE images SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                params,
            )
            await conn.commit()

    async def create_photo_share(
        self,
        *,
        image_id: int,
        token_hash: str,
        token_prefix: str,
        expires_at: Optional[str],
    ) -> PhotoShareRecord:
        created_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO photo_shares (
                    image_id,
                    token_hash,
                    token_prefix,
                    expires_at,
                    revoked_at,
                    created_at
                )
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (image_id, token_hash, token_prefix, expires_at, created_at),
            )
            await conn.commit()
            share_id = cursor.lastrowid
        if share_id is None:
            raise RuntimeError("Failed to create photo share.")
        return PhotoShareRecord(
            id=int(share_id),
            image_id=image_id,
            token_hash=token_hash,
            token_prefix=token_prefix,
            expires_at=expires_at,
            revoked_at=None,
            created_at=created_at,
        )

    async def list_photo_shares_for_image(self, image_id: int) -> list[PhotoShareRecord]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, image_id, token_hash, token_prefix, expires_at, revoked_at, created_at
                FROM photo_shares
                WHERE image_id = ?
                ORDER BY id DESC
                """,
                (image_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [PhotoShareRecord(**row) for row in rows]

    async def fetch_photo_share_by_token_hash(self, token_hash: str) -> Optional[PhotoShareRecord]:
        row = await self.fetch_one(
            """
            SELECT id, image_id, token_hash, token_prefix, expires_at, revoked_at, created_at
            FROM photo_shares
            WHERE token_hash = ?
            """,
            (token_hash,),
        )
        return PhotoShareRecord(**row) if row else None

    async def revoke_photo_shares(
        self,
        *,
        image_id: int,
        token_prefix: Optional[str] = None,
    ) -> int:
        revoked_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            if token_prefix:
                cursor = await conn.execute(
                    """
                    UPDATE photo_shares
                    SET revoked_at = ?
                    WHERE image_id = ? AND token_prefix = ? AND revoked_at IS NULL
                    """,
                    (revoked_at, image_id, token_prefix),
                )
            else:
                cursor = await conn.execute(
                    """
                    UPDATE photo_shares
                    SET revoked_at = ?
                    WHERE image_id = ? AND revoked_at IS NULL
                    """,
                    (revoked_at, image_id),
                )
            await conn.commit()
            changed = cursor.rowcount or 0
            await cursor.close()
        return int(changed)

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
        make: Optional[str] = None,
        model: Optional[str] = None,
        iso: Optional[int] = None,
        f_stop: Optional[float] = None,
        shutter: Optional[str] = None,
        focal: Optional[float] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        loc_desc: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        country: Optional[str] = None,
    ) -> ImageRecord:
        """Insert processed image metadata and return the constructed dataclass."""
        created_at = datetime.utcnow().isoformat()
        faces_json_enc = encrypt_text(faces_json)
        ocr_text_enc = encrypt_text(ocr_text)
        ai_description_enc = encrypt_text(ai_description)
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
                    created_at,
                    make,
                    model,
                    iso,
                    f_stop,
                    shutter,
                    focal,
                    lat,
                    lon,
                    loc_desc,
                    city,
                    state,
                    country
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    filename,
                    faces_json_enc,
                    ocr_text_enc,
                    ai_description_enc,
                    content_type,
                    image_data,
                    thumbnail_data,
                    thumbnail_content_type,
                    taken_at,
                    created_at,
                    make,
                    model,
                    iso,
                    f_stop,
                    shutter,
                    focal,
                    lat,
                    lon,
                    loc_desc,
                    city,
                    state,
                    country,
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
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[ImageRecord]:
        """Return all images owned by the given user with configurable sorting."""
        sort_clause = "created_at"
        if sort_by == "taken":
            sort_clause = "COALESCE(taken_at, created_at)"
        order_clause = "DESC" if order.lower() == "desc" else "ASC"
        pagination_clause = ""
        params: list[Any] = [user_id]
        if limit is not None:
            pagination_clause = "LIMIT ? OFFSET ?"
            params.extend([limit, max(0, int(offset))])
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
                    image_data,
                    thumbnail_data,
                    thumbnail_content_type,
                    taken_at,
                    created_at,
                    make,
                    model,
                    iso,
                    f_stop,
                    shutter,
                    focal,
                    lat,
                    lon,
                    loc_desc,
                    city,
                    state,
                    country
                FROM images
                WHERE user_id = ?
                ORDER BY {sort_clause} {order_clause}, id DESC
                {pagination_clause}
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        records: list[ImageRecord] = []
        for row in rows:
            record = ImageRecord(**row)
            record.faces_json = decrypt_text(record.faces_json)
            record.ocr_text = decrypt_text(record.ocr_text)
            record.ai_description = decrypt_text(record.ai_description)
            records.append(record)
        return records

    async def list_image_file_refs_older_than(
        self,
        *,
        user_id: int,
        cutoff_iso: str,
        limit: int = 200,
    ) -> list[tuple[int, str]]:
        """Return (image_id, filename) for images older than cutoff by created_at."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, filename
                FROM images
                WHERE user_id = ? AND created_at < ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (user_id, cutoff_iso, int(limit)),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [(int(r["id"]), str(r["filename"])) for r in rows]

    async def delete_images_by_ids(self, *, user_id: int, image_ids: list[int]) -> int:
        if not image_ids:
            return 0
        placeholders = ",".join("?" for _ in image_ids)
        params: list[Any] = [user_id, *[int(i) for i in image_ids]]
        async with self._connection() as conn:
            cursor = await conn.execute(
                f"DELETE FROM images WHERE user_id = ? AND id IN ({placeholders})",
                tuple(params),
            )
            await conn.commit()
            changed = cursor.rowcount or 0
            await cursor.close()
        return int(changed)

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
                created_at,
                make,
                model,
                iso,
                f_stop,
                shutter,
                focal,
                lat,
                lon,
                loc_desc,
                city,
                state,
                country
            FROM images
            WHERE id = ? AND user_id = ?
            """,
            (image_id, user_id),
        )
        if not row:
            return None
        record = ImageRecord(**row)
        record.faces_json = decrypt_text(record.faces_json)
        record.ocr_text = decrypt_text(record.ocr_text)
        record.ai_description = decrypt_text(record.ai_description)
        return record

    async def fetch_image_for_access(
        self, image_id: int, requester_user_id: int
    ) -> Optional[ImageRecord]:
        """Return an image when requester is owner or has read ACL access."""
        row = await self.fetch_one(
            """
            SELECT
                i.id,
                i.user_id,
                i.filename,
                i.faces_json,
                i.ocr_text,
                i.ai_description,
                i.content_type,
                i.image_data,
                i.thumbnail_data,
                i.thumbnail_content_type,
                i.taken_at,
                i.created_at
            FROM images i
            WHERE i.id = ?
              AND (
                i.user_id = ?
                OR EXISTS (
                    SELECT 1
                    FROM photo_acl a
                    WHERE a.image_id = i.id
                      AND a.grantee_user_id = ?
                      AND a.permission = 'read'
                )
              )
            """,
            (image_id, requester_user_id, requester_user_id),
        )
        if not row:
            return None
        record = ImageRecord(**row)
        record.faces_json = decrypt_text(record.faces_json)
        record.ocr_text = decrypt_text(record.ocr_text)
        record.ai_description = decrypt_text(record.ai_description)
        return record

    async def list_shared_images_for_user(
        self,
        user_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ImageRecord]:
        """List images shared *to* a user via ACL (not share links)."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    i.id,
                    i.user_id,
                    i.filename,
                    i.faces_json,
                    i.ocr_text,
                    i.ai_description,
                    i.content_type,
                    i.image_data,
                    i.thumbnail_data,
                    i.thumbnail_content_type,
                    i.taken_at,
                    i.created_at
                FROM images i
                INNER JOIN photo_acl a
                  ON a.image_id = i.id
                WHERE a.grantee_user_id = ?
                  AND a.permission = 'read'
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, int(limit), max(0, int(offset))),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        records: list[ImageRecord] = []
        for row in rows:
            record = ImageRecord(**row)
            record.faces_json = decrypt_text(record.faces_json)
            record.ocr_text = decrypt_text(record.ocr_text)
            record.ai_description = decrypt_text(record.ai_description)
            records.append(record)
        return records

    async def grant_photo_acl(self, *, image_id: int, grantee_user_id: int) -> None:
        created_at = datetime.utcnow().isoformat()
        async with self._connection() as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO photo_acl (image_id, grantee_user_id, permission, created_at)
                VALUES (?, ?, 'read', ?)
                """,
                (image_id, grantee_user_id, created_at),
            )
            await conn.commit()

    async def revoke_photo_acl(self, *, image_id: int, grantee_user_id: int) -> int:
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM photo_acl
                WHERE image_id = ? AND grantee_user_id = ? AND permission = 'read'
                """,
                (image_id, grantee_user_id),
            )
            await conn.commit()
            changed = cursor.rowcount or 0
            await cursor.close()
        return int(changed)

    async def list_photo_acl(self, *, image_id: int) -> list[dict[str, Any]]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT grantee_user_id, permission, created_at
                FROM photo_acl
                WHERE image_id = ?
                ORDER BY grantee_user_id ASC
                """,
                (image_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [dict(row) for row in rows]

    async def list_photo_acl_with_users(self, *, image_id: int) -> list[dict[str, Any]]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    a.grantee_user_id,
                    u.username,
                    u.email,
                    a.permission,
                    a.created_at
                FROM photo_acl a
                INNER JOIN users u
                  ON u.id = a.grantee_user_id
                WHERE a.image_id = ?
                  AND a.permission = 'read'
                ORDER BY u.email ASC
                """,
                (image_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [dict(row) for row in rows]

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
                id, image_id, user_id, faces_json, ocr_text, caption,
                lat, lon, loc_description, loc_city, loc_state, loc_country,
                make, model, iso, f_stop, shutter_speed, focal_length,
                width, height, file_size_mb, taken_at, created_at, updated_at
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
                    image_id, user_id, faces_json, ocr_text, caption,
                    lat, lon, loc_description, loc_city, loc_state, loc_country,
                    make, model, iso, f_stop, shutter_speed, focal_length,
                    width, height, file_size_mb, taken_at, created_at, updated_at
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
                    image_id, user_id, faces_json, ocr_text, caption,
                    lat, lon, loc_description, loc_city, loc_state, loc_country,
                    make, model, iso, f_stop, shutter_speed, focal_length,
                    width, height, file_size_mb, taken_at, now, now,
                ),
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

    async def populate_fts_for_image(self, image_id: int, user_id: int) -> None:
        async with self._connection() as conn:
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

            await conn.execute("DELETE FROM images_fts WHERE image_id = ?", (str(image_id),))
            await conn.execute(
                "INSERT INTO images_fts (image_id, filename, ai_description, ocr_text, caption, location) VALUES (?, ?, ?, ?, ?, ?)",
                (str(image_id), img_row["filename"] or "", img_row["ai_description"] or "", ocr_text, caption, location),
            )
            await conn.commit()

    async def list_face_embeddings_for_user(self, user_id: int) -> list[FaceEmbeddingRecord]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                """
                SELECT user_id, tag, embedding_json, samples_count, updated_at
                FROM face_embeddings
                WHERE user_id = ?
                ORDER BY tag ASC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        records: list[FaceEmbeddingRecord] = []
        for row in rows:
            record = FaceEmbeddingRecord(**row)
            record.embedding_json = decrypt_text(record.embedding_json)
            records.append(record)
        return records

    async def upsert_face_embedding_for_user(
        self, user_id: int, tag: str, embedding_json: str
    ) -> None:
        now = datetime.utcnow().isoformat()
        embedding_json_enc = encrypt_text(embedding_json)
        async with self._connection() as conn:
            row_cursor = await conn.execute(
                """
                SELECT embedding_json, samples_count
                FROM face_embeddings
                WHERE user_id = ? AND tag = ?
                """,
                (user_id, tag),
            )
            existing = await row_cursor.fetchone()
            await row_cursor.close()
            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO face_embeddings (user_id, tag, embedding_json, samples_count, updated_at)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (user_id, tag, embedding_json_enc, now),
                )
            else:
                await conn.execute(
                    """
                    UPDATE face_embeddings
                    SET embedding_json = ?, samples_count = samples_count + 1, updated_at = ?
                    WHERE user_id = ? AND tag = ?
                    """,
                    (embedding_json_enc, now, user_id, tag),
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

    async def revoke_session_by_hash(self, token_hash: str, revoked_at: str) -> None:
        """Mark a session as revoked by its token hash."""
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?",
                (revoked_at, token_hash),
            )
            await conn.commit()
