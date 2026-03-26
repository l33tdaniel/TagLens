"""
Unit tests for storage-path behaviour (B2 cloud uploads, downloads, rate-limits).

These tests monkeypatch the real database and B2 bucket objects with lightweight
fakes so that the API handlers can be exercised without external services.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

import app as app_module
from database import ImageRecord, UserRecord, UserSettingsRecord
from scripts.upload_metadata import UploadMetadata


def _default_user_settings(user_id: int = 42) -> UserSettingsRecord:
    """Return a UserSettingsRecord with all features enabled for testing.

    Every _FakeDb mock must expose ensure_user_settings() because the upload
    and download handlers now read user privacy preferences before proceeding.
    This helper keeps those stubs DRY.
    """
    return UserSettingsRecord(
        user_id=user_id,
        ai_descriptions_enabled=1,
        ocr_enabled=1,
        face_recognition_enabled=1,
        store_originals_enabled=1,
        retention_days=None,
        created_at="2025-01-01T00:00:00",
        updated_at="2025-01-01T00:00:00",
    )


class _FakeRequest:
    """Minimal stand-in for a Robyn Request object.

    Only the attributes actually read by the handlers under test are populated;
    everything else defaults to an empty dict or bytes.
    """

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        query_params: dict[str, str] | None = None,
        form_data: dict[str, str] | None = None,
    ) -> None:
        self.headers = headers or {}
        self.body = body if body is not None else b""
        self.query_params = query_params or {}
        self.form_data = form_data or {}
        # SimpleNamespace mimics the real Robyn URL object with a .path attribute.
        self.url = SimpleNamespace(path="/api/photos")
        self.ip_addr = "127.0.0.1"


def _auth_context() -> app_module.AuthContext:
    """Build a fake authenticated context for user 'alice' (id=42).

    Used by every test to bypass the real authentication middleware.
    """
    user = UserRecord(
        id=42,
        username="alice",
        email="alice@example.com",
        password_hash="hashed",
        created_at="2025-01-01T00:00:00",
    )
    return app_module.AuthContext(user=user, session=None, clear_cookie=False)


@pytest.mark.asyncio
async def test_download_returns_signed_url_when_b2_backing_is_used(monkeypatch) -> None:
    """Verify that the download endpoint returns a signed B2 URL.

    When the app is backed by Backblaze B2 storage, downloading a photo should
    generate a time-limited signed URL rather than serving raw bytes directly.
    The response JSON must include an 'Authorization=signed-token' query param.
    """

    async def _fake_auth(_request):
        return _auth_context()

    # Construct a minimal image record with no inline image_data — forces the
    # handler down the B2 signed-URL path instead of the local-bytes path.
    record = ImageRecord(
        id=10,
        user_id=42,
        filename="photo.jpg",
        faces_json="[]",
        ocr_text="",
        ai_description="",
        content_type="image/jpeg",
        image_data=None,
        thumbnail_data=None,
        thumbnail_content_type="image/webp",
        taken_at=None,
        created_at="2025-01-01T00:00:00",
    )

    class _FakeDb:
        """Stub database that returns canned user settings and the test record."""

        async def ensure_user_settings(self, user_id: int):
            return _default_user_settings(user_id)

        async def fetch_image_for_access(self, image_id: int, requester_user_id: int):
            # Uses fetch_image_for_access (not fetch_image_for_user) because
            # the download handler checks ACL-based access for shared photos.
            assert image_id == 10
            assert requester_user_id == 42
            return record

    class _FakeBucket:
        """Stub B2 bucket that returns deterministic signed URLs."""

        def get_download_authorization(self, file_key: str, valid_duration_in_seconds: int):
            # The real B2 SDK generates a time-limited auth token for the key.
            assert valid_duration_in_seconds == 300
            assert file_key.endswith("/10.jpg")
            return "signed-token"

        def get_download_url(self, _prefix: str) -> str:
            return "https://files.example/"

    # Patch auth, database, and bucket to isolate the handler logic.
    monkeypatch.setattr(app_module, "_ensure_authenticated", _fake_auth)
    monkeypatch.setattr(app_module, "db", _FakeDb())
    monkeypatch.setattr(app_module, "bucket", _FakeBucket())

    response = await app_module.download_photo_api(
        _FakeRequest(query_params={"photo_id": "10"})
    )

    # The handler should return 200 with a JSON body containing the signed URL.
    assert response.status_code == 200
    payload = json.loads(response.description)
    assert payload["url"].startswith("https://files.example/")
    assert "Authorization=signed-token" in payload["url"]


@pytest.mark.asyncio
async def test_upload_rolls_back_record_when_b2_upload_fails(monkeypatch) -> None:
    """Ensure the DB record is deleted when the B2 upload step fails.

    The upload flow is: create DB record -> upload bytes to B2.  If the B2 step
    throws, the handler must roll back by deleting the newly-created DB record
    so we don't end up with orphaned metadata pointing to a missing file.
    """

    async def _fake_auth(_request):
        return _auth_context()

    async def _fake_detect_and_tag(*_args, **_kwargs):
        return []

    class _FakeDb:
        """Tracks whether delete_image_for_user was called after a failed upload."""

        def __init__(self) -> None:
            self.deleted = None  # Will be set to (image_id, user_id) on rollback.

        async def ensure_user_settings(self, user_id: int):
            return _default_user_settings(user_id)

        async def create_image_metadata(self, **_kwargs):
            # Return a record with id=99 so we can verify the rollback targets it.
            return ImageRecord(
                id=99,
                user_id=42,
                filename="photo.png",
                faces_json="[]",
                ocr_text="",
                ai_description="desc",
                content_type="image/png",
                image_data=b"payload",
                thumbnail_data=b"thumb",
                thumbnail_content_type="image/webp",
                taken_at=None,
                created_at="2025-01-01T00:00:00",
            )

        async def delete_image_for_user(self, image_id: int, user_id: int) -> bool:
            self.deleted = (image_id, user_id)
            return True

    class _FailingBucket:
        """Simulates a B2 bucket that always fails on upload."""

        def upload_bytes(self, *_args, **_kwargs):
            raise RuntimeError("upload failed")

    fake_db = _FakeDb()

    # Construct the JSON upload payload with a base64-encoded image.
    body = json.dumps(
        {
            "filename": "photo.png",
            "image_base64": base64.b64encode(b"img").decode("utf-8"),
            "content_type": "image/png",
        }
    ).encode("utf-8")

    # Patch all dependencies so only the B2 upload path is exercised.
    monkeypatch.setattr(app_module, "_ensure_authenticated", _fake_auth)
    monkeypatch.setattr(app_module, "_verify_api_csrf", lambda _request: True)
    monkeypatch.setattr(
        app_module,
        "_allow_rate_limited_request",
        lambda _limiter, _request: True,
    )
    monkeypatch.setattr(
        app_module,
        "_generate_image_description_with_ollama",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        app_module,
        "extract_upload_metadata",
        lambda _payload: UploadMetadata(ocr_text="", taken_at=None),
    )
    monkeypatch.setattr(
        app_module,
        "_generate_thumbnail_webp",
        lambda _payload: b"thumb",
    )
    monkeypatch.setattr(
        app_module,
        "detect_and_tag_faces_for_user",
        _fake_detect_and_tag,
    )
    monkeypatch.setattr(app_module, "db", fake_db)
    monkeypatch.setattr(app_module, "bucket", _FailingBucket())

    response = await app_module.upload_photo_api(
        _FakeRequest(
            headers={"content-type": "application/json", "x-csrf-token": "ok"},
            body=body,
        )
    )

    # The handler should return 500 AND clean up the orphaned DB record.
    assert response.status_code == 500
    assert fake_db.deleted == (99, 42)


@pytest.mark.asyncio
async def test_upload_rate_limit_returns_429_when_limit_exceeded(monkeypatch) -> None:
    """Confirm the upload endpoint enforces per-user rate limiting.

    With a RateLimiter configured to allow 2 requests per 60-second window,
    the first two uploads should succeed (201) while the third must be rejected
    with HTTP 429 (Too Many Requests).
    """

    async def _fake_auth(_request):
        return _auth_context()

    async def _fake_detect_and_tag(*_args, **_kwargs):
        return []

    class _FakeDb:
        """Auto-incrementing stub that hands out unique image IDs."""

        def __init__(self) -> None:
            self.next_id = 1

        async def ensure_user_settings(self, user_id: int):
            return _default_user_settings(user_id)

        async def create_image_metadata(self, **_kwargs):
            image_id = self.next_id
            self.next_id += 1
            return ImageRecord(
                id=image_id,
                user_id=42,
                filename="photo.png",
                faces_json="[]",
                ocr_text="",
                ai_description="",
                content_type="image/png",
                image_data=b"img",
                thumbnail_data=b"thumb",
                thumbnail_content_type="image/webp",
                taken_at=None,
                created_at="2025-01-01T00:00:00",
            )

    # Build a reusable JSON upload payload.
    body = json.dumps(
        {
            "filename": "photo.png",
            "image_base64": base64.b64encode(b"img").decode("utf-8"),
            "content_type": "image/png",
        }
    ).encode("utf-8")
    request = _FakeRequest(
        headers={"content-type": "application/json", "x-csrf-token": "ok"},
        body=body,
    )

    # Force production mode so the rate limiter is active (dev mode skips it).
    monkeypatch.setenv("ROBYN_ENV", "production")
    monkeypatch.setattr(app_module, "_ensure_authenticated", _fake_auth)
    monkeypatch.setattr(app_module, "_verify_api_csrf", lambda _request: True)
    monkeypatch.setattr(
        app_module,
        "_generate_image_description_with_ollama",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        app_module,
        "extract_upload_metadata",
        lambda _payload: UploadMetadata(ocr_text="", taken_at=None),
    )
    monkeypatch.setattr(
        app_module,
        "_generate_thumbnail_webp",
        lambda _payload: b"thumb",
    )
    monkeypatch.setattr(
        app_module,
        "detect_and_tag_faces_for_user",
        _fake_detect_and_tag,
    )
    monkeypatch.setattr(app_module, "db", _FakeDb())
    # bucket=None means no B2 backing — uploads go straight to local storage.
    monkeypatch.setattr(app_module, "bucket", None)
    # Install a strict rate limiter: 2 uploads allowed per 60-second window.
    monkeypatch.setattr(app_module, "_upload_limiter", app_module.RateLimiter(2, 60))

    # Fire three uploads in quick succession.
    first = await app_module.upload_photo_api(request)
    second = await app_module.upload_photo_api(request)
    third = await app_module.upload_photo_api(request)

    # First two should succeed; third should be rate-limited.
    assert first.status_code == 201
    assert second.status_code == 201
    assert third.status_code == 429
