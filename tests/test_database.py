from datetime import datetime, timedelta

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
    )
    assert saved.ai_description == "A person standing near a tree."

    rows = await db.list_images_for_user(user.id)
    assert len(rows) == 1
    assert rows[0].filename == "photo.jpg"
    assert rows[0].ai_description == "A person standing near a tree."
