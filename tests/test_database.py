"""
Async tests for Database CRUD paths.

Purpose:
    Confirms user/session lifecycle and image metadata behaviors.

Authorship (git history, mapped to real names):
    Daniel (l33tdaniel)
"""

from datetime import datetime, timedelta
import hashlib

import pytest

from database import Database


@pytest.mark.asyncio
async def test_user_and_session_lifecycle(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()

    user = await db.create_user("alice", "alice@example.com", "hashed")
    fetched_email = await db.fetch_user_by_email("alice@example.com")
    fetched_id = await db.fetch_user_by_id(user.id)

    assert fetched_email is not None
    assert fetched_email.id == user.id
    assert fetched_id is not None
    assert fetched_id.email == "alice@example.com"

    expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    session = await db.create_session(
        user_id=user.id,
        token_hash="tokenhash",
        expires_at=expires_at,
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    fetched_session = await db.fetch_session_by_token_hash("tokenhash")
    assert fetched_session is not None
    assert fetched_session.user_id == user.id

    touched_at = datetime.utcnow().isoformat()
    await db.touch_session(session.id, touched_at)
    touched = await db.fetch_session_by_token_hash("tokenhash")
    assert touched is not None
    assert touched.last_seen_at == touched_at

    revoked_at = datetime.utcnow().isoformat()
    await db.revoke_session(session.id, revoked_at)
    revoked = await db.fetch_session_by_token_hash("tokenhash")
    assert revoked is not None
    assert revoked.revoked_at == revoked_at


@pytest.mark.asyncio
async def test_user_settings_created_and_update(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    user = await db.create_user("erin", "erin@example.com", "hashed")

    settings = await db.fetch_user_settings(user.id)
    assert settings is not None
    assert settings.ai_descriptions_enabled == 1
    assert settings.ocr_enabled == 1
    assert settings.face_recognition_enabled == 1

    updated = await db.update_user_settings(user.id, ocr_enabled=False, ai_descriptions_enabled=False)
    assert updated.ocr_enabled == 0
    assert updated.ai_descriptions_enabled == 0


@pytest.mark.asyncio
async def test_jobs_enqueue_and_claim(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    user = await db.create_user("frank", "frank@example.com", "hashed")
    image = await db.create_image_metadata(
        filename="photo.jpg",
        faces_json="[]",
        ocr_text="",
        user_id=user.id,
    )
    job_id = await db.enqueue_job(
        user_id=user.id,
        image_id=image.id,
        kind="process_image",
        payload_json='{"do_ocr":true}',
    )
    claimed = await db.claim_next_job(kind="process_image")
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status == "running"
    await db.complete_job(job_id)

@pytest.mark.asyncio
async def test_photo_shares_create_list_fetch_and_revoke(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    user = await db.create_user("gina", "gina@example.com", "hashed")
    image = await db.create_image_metadata(
        filename="photo.jpg",
        faces_json="[]",
        ocr_text="",
        user_id=user.id,
    )
    token = "token-value"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    share = await db.create_photo_share(
        image_id=image.id,
        token_hash=token_hash,
        token_prefix=token[:8],
        expires_at=None,
    )
    shares = await db.list_photo_shares_for_image(image.id)
    assert len(shares) == 1
    fetched = await db.fetch_photo_share_by_token_hash(token_hash)
    assert fetched is not None
    assert fetched.id == share.id
    changed = await db.revoke_photo_shares(image_id=image.id, token_prefix=token[:8])
    assert changed == 1
    fetched2 = await db.fetch_photo_share_by_token_hash(token_hash)
    assert fetched2 is not None
    assert fetched2.revoked_at is not None


@pytest.mark.asyncio
async def test_image_metadata_includes_ai_description(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    user = await db.create_user("bob", "bob@example.com", "hashed")

    saved = await db.create_image_metadata(
        filename="photo.jpg",
        faces_json="[]",
        ocr_text="",
        user_id=user.id,
        ai_description="A person standing near a tree.",
        content_type="image/jpeg",
        image_data=b"123",
        taken_at="2025-01-01T00:00:00+00:00",
    )
    assert saved.ai_description == "A person standing near a tree."
    assert saved.content_type == "image/jpeg"
    assert saved.image_data == b"123"
    assert saved.taken_at == "2025-01-01T00:00:00+00:00"

    rows = await db.list_images_for_user(user.id)
    assert len(rows) == 1
    assert rows[0].filename == "photo.jpg"
    assert rows[0].ai_description == "A person standing near a tree."


@pytest.mark.asyncio
async def test_list_images_can_sort_by_taken_date(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    user = await db.create_user("carol", "carol@example.com", "hashed")

    await db.create_image_metadata(
        filename="a.jpg",
        faces_json="[]",
        ocr_text="",
        user_id=user.id,
        taken_at="2025-03-01T00:00:00+00:00",
    )
    await db.create_image_metadata(
        filename="b.jpg",
        faces_json="[]",
        ocr_text="",
        user_id=user.id,
        taken_at="2024-03-01T00:00:00+00:00",
    )
    rows = await db.list_images_for_user(user.id, sort_by="taken", order="asc")
    assert [row.filename for row in rows] == ["b.jpg", "a.jpg"]


@pytest.mark.asyncio
async def test_face_embedding_index_upsert_and_list(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    await db.initialize()
    user = await db.create_user("dave", "dave@example.com", "hashed")

    await db.upsert_face_embedding_for_user(
        user.id, "person_1", "[0.1,0.2,0.3]"
    )
    await db.upsert_face_embedding_for_user(
        user.id, "person_1", "[0.2,0.3,0.4]"
    )
    await db.upsert_face_embedding_for_user(
        user.id, "person_2", "[1.0,0.0,0.0]"
    )

    rows = await db.list_face_embeddings_for_user(user.id)
    assert len(rows) == 2
    person_1 = next(row for row in rows if row.tag == "person_1")
    assert person_1.samples_count == 2
    assert person_1.embedding_json == "[0.2,0.3,0.4]"
