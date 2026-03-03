from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

import app as app_module
from database import ImageRecord, UserRecord
from scripts.upload_metadata import UploadMetadata


class _FakeRequest:
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
        self.url = SimpleNamespace(path="/api/photos")
        self.ip_addr = "127.0.0.1"


def _auth_context() -> app_module.AuthContext:
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
    async def _fake_auth(_request):
        return _auth_context()

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
        async def fetch_image_for_user(self, image_id: int, user_id: int):
            assert image_id == 10
            assert user_id == 42
            return record

    class _FakeBucket:
        def get_download_authorization(self, file_key: str, valid_duration_in_seconds: int):
            assert valid_duration_in_seconds == 300
            assert file_key.endswith("/10.jpg")
            return "signed-token"

        def get_download_url(self, _prefix: str) -> str:
            return "https://files.example/"

    monkeypatch.setattr(app_module, "_ensure_authenticated", _fake_auth)
    monkeypatch.setattr(app_module, "db", _FakeDb())
    monkeypatch.setattr(app_module, "bucket", _FakeBucket())

    response = await app_module.download_photo_api(
        _FakeRequest(query_params={"photo_id": "10"})
    )
    assert response.status_code == 200
    payload = json.loads(response.description)
    assert payload["url"].startswith("https://files.example/")
    assert "Authorization=signed-token" in payload["url"]


@pytest.mark.asyncio
async def test_upload_rolls_back_record_when_b2_upload_fails(monkeypatch) -> None:
    async def _fake_auth(_request):
        return _auth_context()

    async def _fake_detect_and_tag(*_args, **_kwargs):
        return []

    class _FakeDb:
        def __init__(self) -> None:
            self.deleted = None

        async def create_image_metadata(self, **_kwargs):
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
        def upload_bytes(self, *_args, **_kwargs):
            raise RuntimeError("upload failed")

    fake_db = _FakeDb()
    body = json.dumps(
        {
            "filename": "photo.png",
            "image_base64": base64.b64encode(b"img").decode("utf-8"),
            "content_type": "image/png",
        }
    ).encode("utf-8")

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
    assert response.status_code == 500
    assert fake_db.deleted == (99, 42)


@pytest.mark.asyncio
async def test_upload_rate_limit_returns_429_when_limit_exceeded(monkeypatch) -> None:
    async def _fake_auth(_request):
        return _auth_context()

    async def _fake_detect_and_tag(*_args, **_kwargs):
        return []

    class _FakeDb:
        def __init__(self) -> None:
            self.next_id = 1

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
    monkeypatch.setattr(app_module, "bucket", None)
    monkeypatch.setattr(app_module, "_upload_limiter", app_module.RateLimiter(2, 60))

    first = await app_module.upload_photo_api(request)
    second = await app_module.upload_photo_api(request)
    third = await app_module.upload_photo_api(request)
    assert first.status_code == 201
    assert second.status_code == 201
    assert third.status_code == 429
