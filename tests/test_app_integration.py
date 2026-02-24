from __future__ import annotations

from datetime import datetime, timedelta
import base64
import re
import sqlite3
from uuid import uuid4

from auth import hash_session_token
from tests.conftest import ServerInfo, TestClient


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token input not found in HTML."
    return match.group(1)


def _register_user(client: TestClient, username: str, email: str, password: str) -> None:
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
    assert response.status == 400
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
    dashboard = client.request("GET", "/dashboard")
    csrf_token = _extract_csrf_token(dashboard.body)
    response = client.request("POST", "/logout", data={"csrf_token": csrf_token})
    assert response.status in {302, 303}
    set_cookies = response.headers.get_all("Set-Cookie") or []
    assert any(
        header.lower().startswith("taglens_session=")
        and "max-age=0" in header.lower()
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
        header.lower().startswith("taglens_session=")
        and "max-age=0" in header.lower()
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
    dashboard = client.request("GET", "/dashboard")
    csrf_token = _extract_csrf_token(dashboard.body)
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
