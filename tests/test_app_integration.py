from __future__ import annotations

from datetime import datetime, timedelta
import base64
import json
import re
import sqlite3
from uuid import uuid4

from auth import hash_session_token
from tests.conftest import ServerInfo, TestClient


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token input not found in HTML."
    return match.group(1)


def _register_user(
    client: TestClient, username: str, email: str, password: str
) -> None:
    register_page = client.request("GET", "/register")
    csrf_token = _extract_csrf_token(register_page.body)
    response = client.request(
        "POST",
        "/register",
        data={
            "username": username,
            "email": email,
            "password": password,
            "confirm_password": password,
            "csrf_token": csrf_token,
        },
    )
    assert response.status in {302, 303}


def _login_user(client: TestClient, email: str, password: str) -> None:
    login_page = client.request("GET", "/login")
    csrf_token = _extract_csrf_token(login_page.body)
    response = client.request(
        "POST",
        "/login",
        data={
            "email": email,
            "password": password,
            "next": "/dashboard",
            "csrf_token": csrf_token,
        },
    )
    assert response.status in {302, 303}


def test_public_sets_csrf_cookie(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/")
    assert response.status == 200
    assert client.get_cookie("taglens_csrf")


def test_dashboard_redirects_when_unauthenticated(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/dashboard")
    assert response.status in {302, 303}
    location = response.headers.get("Location")
    assert location is not None
    assert location.startswith("/login")


def test_public_route_is_removed(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/public")
    assert response.status == 404


def test_register_flow_sets_csrf_cookie(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/register")
    assert response.status == 200
    csrf_cookie = client.get_cookie("taglens_csrf")
    assert csrf_cookie is not None
    csrf_form = _extract_csrf_token(response.body)
    assert csrf_cookie == csrf_form


def test_login_rejects_invalid_csrf(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request(
        "POST",
        "/login",
        data={
            "email": "nobody@example.com",
            "password": "not_a_real_password",
            "next": "/dashboard",
            "csrf_token": "invalid",
        },
    )
    assert response.status == 200
    assert "CSRF validation failed" in response.body
    assert client.get_cookie("taglens_session") is None


def test_login_creates_session_and_allows_dashboard(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)
    assert client.get_cookie("taglens_session")
    dashboard = client.request("GET", "/dashboard")
    assert dashboard.status == 200
    assert username in dashboard.body


def test_logout_clears_session_and_blocks_dashboard(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)
    csrf_token = client.get_cookie("taglens_csrf")
    assert csrf_token is not None
    response = client.request("POST", "/logout", data={"csrf_token": csrf_token})
    assert response.status in {302, 303}
    set_cookies = response.headers.get_all("Set-Cookie") or []
    assert any(
        header.lower().startswith("taglens_session=") and "max-age=0" in header.lower()
        for header in set_cookies
    )
    redirect = client.request("GET", "/dashboard")
    assert redirect.status in {302, 303}


def test_expired_session_is_revoked_and_cleared(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)
    session_token = client.get_cookie("taglens_session")
    assert session_token is not None
    token_hash = hash_session_token(session_token)
    expires_at = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(server.db_path) as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (expires_at, token_hash),
        )
        conn.commit()
    response = client.request("GET", "/dashboard")
    assert response.status in {302, 303}
    set_cookies = response.headers.get_all("Set-Cookie") or []
    assert any(
        header.lower().startswith("taglens_session=") and "max-age=0" in header.lower()
        for header in set_cookies
    )


def test_login_revokes_existing_session(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)
    old_token = client.get_cookie("taglens_session")
    assert old_token is not None
    old_hash = hash_session_token(old_token)
    csrf_token = client.get_cookie("taglens_csrf")
    assert csrf_token is not None
    response = client.request(
        "POST",
        "/login",
        data={
            "email": email,
            "password": password,
            "next": "/dashboard",
            "csrf_token": csrf_token,
        },
    )
    assert response.status in {302, 303}
    with sqlite3.connect(server.db_path) as conn:
        row = conn.execute(
            "SELECT revoked_at FROM sessions WHERE token_hash = ?",
            (old_hash,),
        ).fetchone()
    assert row is not None
    assert row[0] is not None


def test_profile_api_requires_authentication(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/api/profile")
    assert response.status == 401


def test_profile_page_requires_authentication(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/profile")
    assert response.status in {302, 303}


def test_profile_page_renders_when_authenticated(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)
    response = client.request("GET", "/profile")
    assert response.status == 200
    assert username in response.body


def test_userprofile_js_route_returns_script(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/UserProfile.js")
    assert response.status == 200
    assert "placeholder loaded" in response.body


def test_photo_upload_persists_generated_description_field(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)

    image_base64 = base64.b64encode(b"fake image bytes").decode("utf-8")
    upload = client.request(
        "POST",
        "/api/photos",
        json_data={
            "filename": "test.png",
            "image_base64": image_base64,
        },
    )
    assert upload.status == 201

    payload = client.request("GET", "/api/profile")
    assert payload.status == 200
    assert "test.png" in payload.body


def test_profile_photo_sort_by_taken_date(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)

    first = base64.b64encode(b"first-image").decode("utf-8")
    second = base64.b64encode(b"second-image").decode("utf-8")
    created = client.request(
        "POST",
        "/api/photos",
        json_data={
            "filename": "older-upload-newer-taken.jpg",
            "image_base64": first,
            "taken_at": "2025-01-10T10:00:00Z",
            "content_type": "image/jpeg",
        },
    )
    assert created.status == 201
    created = client.request(
        "POST",
        "/api/photos",
        json_data={
            "filename": "newer-upload-older-taken.jpg",
            "image_base64": second,
            "taken_at": "2024-01-10T10:00:00Z",
            "content_type": "image/jpeg",
        },
    )
    assert created.status == 201

    response = client.request("GET", "/api/profile?sort_by=taken&order=asc")
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["photos"][0]["filename"] == "newer-upload-older-taken.jpg"
    assert payload["photos"][1]["filename"] == "older-upload-newer-taken.jpg"


def test_photo_delete_requires_confirmation_and_deletes(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)

    image_base64 = base64.b64encode(b"to-delete").decode("utf-8")
    upload = client.request(
        "POST",
        "/api/photos",
        json_data={
            "filename": "delete-me.png",
            "image_base64": image_base64,
            "content_type": "image/png",
        },
    )
    assert upload.status == 201
    uploaded = json.loads(upload.body)
    photo_id = uploaded["id"]

    rejected = client.request(
        "DELETE",
        "/api/photos",
        json_data={"photo_id": photo_id, "confirm_delete": False},
    )
    assert rejected.status == 400

    deleted = client.request(
        "DELETE",
        "/api/photos",
        json_data={"photo_id": photo_id, "confirm_delete": True},
    )
    assert deleted.status == 200

    profile = client.request("GET", "/api/profile")
    assert profile.status == 200
    payload = json.loads(profile.body)
    assert payload["photos"] == []


def test_photo_download_returns_uploaded_binary(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)

    raw = b"download-me-image"
    upload = client.request(
        "POST",
        "/api/photos",
        json_data={
            "filename": "download-me.webp",
            "image_base64": base64.b64encode(raw).decode("utf-8"),
            "content_type": "image/webp",
        },
    )
    assert upload.status == 201
    saved = json.loads(upload.body)

    download = client.request(
        "GET",
        f"/api/photos/download?photo_id={saved['id']}",
    )
    assert download.status == 200
    payload = json.loads(download.body)
    assert payload["filename"] == "download-me.webp"
    assert payload["content_type"] == "image/webp"
    assert base64.b64decode(payload["image_base64"]) == raw


def test_photo_view_requires_authentication(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/api/photos/view?photo_id=1")
    assert response.status in {302, 303}


def test_photo_thumb_requires_authentication(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/api/photos/thumb?photo_id=1")
    assert response.status in {302, 303}


def test_photo_thumb_rejects_non_integer_photo_id(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)

    response = client.request("GET", "/api/photos/thumb?photo_id=abc")
    assert response.status == 400


def test_photo_view_returns_image_when_authenticated(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)

    raw = b"view-me-image"
    upload = client.request(
        "POST",
        "/api/photos",
        json_data={
            "filename": "view-me.webp",
            "image_base64": base64.b64encode(raw).decode("utf-8"),
            "content_type": "image/webp",
        },
    )
    assert upload.status == 201
    saved = json.loads(upload.body)

    viewed = client.request("GET", f"/api/photos/view?photo_id={saved['id']}")
    assert viewed.status == 200


def test_photo_thumb_returns_image_when_authenticated(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    unique = uuid4().hex[:8]
    username = f"user{unique}"
    email = f"{username}@example.com"
    password = "password123"
    _register_user(client, username, email, password)
    _login_user(client, email, password)

    raw = b"thumb-me-image"
    upload = client.request(
        "POST",
        "/api/photos",
        json_data={
            "filename": "thumb-me.webp",
            "image_base64": base64.b64encode(raw).decode("utf-8"),
            "content_type": "image/webp",
        },
    )
    assert upload.status == 201
    saved = json.loads(upload.body)

    thumb = client.request("GET", f"/api/photos/thumb?photo_id={saved['id']}")
    assert thumb.status == 200


def test_debug_b2_route_available(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    response = client.request("GET", "/debug/b2")
    assert response.status == 200
    payload = json.loads(response.body)
    assert "files" in payload


def test_docs_and_openapi_routes_available(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    docs = client.request("GET", "/docs")
    assert docs.status == 200
    openapi = client.request("GET", "/openapi.json")
    assert openapi.status == 200


def test_static_assets_and_favicon_available(server: ServerInfo) -> None:
    client = TestClient(server.base_url)
    favicon = client.request("GET", "/favicon.ico")
    assert favicon.status == 200
    tailwind = client.request("GET", "/static/tailwindcss.js")
    assert tailwind.status == 200
    assert "tailwind" in tailwind.body.lower()
    dropzone_js = client.request("GET", "/static/dropzone.min.js")
    assert dropzone_js.status == 200
    dropzone_css = client.request("GET", "/static/dropzone.min.css")
    assert dropzone_css.status == 200
