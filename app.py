from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Tuple
from urllib import response
from urllib.parse import parse_qs, urlencode
from urllib import error as urllib_error, request as urllib_request
import base64
import json
import os
import pathlib
from scripts.metadata import get_complete_metadata, handle_video
from scripts.database_helper import init_db
from b2sdk.v2 import B2Api, InMemoryAccountInfo


from markupsafe import escape
from robyn import Request, Response, Robyn
from robyn.templating import JinjaTemplate
import jinja2
import mimetypes
import logging
import asyncio

from auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    cookie_clear_settings,
    cookie_settings,
    csrf_cookie_settings,
    generate_csrf_token,
    generate_session_token,
    hash_session_token,
    hash_password,
    session_expiration,
    verify_csrf_token,
    verify_password,
)
from database import Database, SessionRecord, UserRecord
import aiosqlite

# Author: Daniel Neugent

app = Robyn(__file__)
logger = logging.getLogger(__name__)

current_file_path = pathlib.Path(__file__).parent.resolve()

jinja_template = JinjaTemplate(
    os.path.join(current_file_path, "frontend/pages")
)

# Singletons used by every request
db = Database()
conn = init_db()

KEY_ID = os.getenv("KEY_ID")
APP_KEY = os.getenv("APP_KEY")
BUCKET_NAME = os.getenv("BUCKET_NAME")

info = InMemoryAccountInfo()
b2_api = B2Api(info)

b2_api.authorize_account("production", KEY_ID, APP_KEY)

bucket = b2_api.get_bucket_by_name(BUCKET_NAME)


async def _ensure_database() -> None:
    """Prepare the sqlite file before handling the first request."""
    await db.initialize()


app.startup_handler(_ensure_database)


@dataclass
class AuthContext:
    user: Optional[UserRecord]
    session: Optional[SessionRecord]
    clear_cookie: bool


def _get_cookie_value(request: Request, name: str) -> Optional[str]:
    """Extract a single cookie value from the request headers."""
    cookie_header = request.headers.get("cookie")
    if not cookie_header:
        return None
    for chunk in cookie_header.split(";"):
        key, sep, value = chunk.strip().partition("=")
        if not sep:
            continue
        if key.strip() == name:
            return value
    return None


def _form_data(request: Request) -> dict[str, str]:
    """Return form fields, including urlencoded fallback parsing for Robyn 0.77."""
    native = request.form_data or {}
    if native:
        return {str(k): str(v) for k, v in native.items()}
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" not in content_type:
        return {}
    raw_body = request.body
    if isinstance(raw_body, (bytes, bytearray)):
        body_bytes = bytes(raw_body)
    elif isinstance(raw_body, list):
        body_bytes = bytes(raw_body)
    elif isinstance(raw_body, str):
        body_bytes = raw_body.encode("utf-8")
    else:
        return {}
    parsed = parse_qs(
        body_bytes.decode("utf-8", errors="replace"), keep_blank_values=True
    )
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _raw_body_bytes(request: Request) -> bytes:
    raw_body = request.body
    if isinstance(raw_body, (bytes, bytearray)):
        return bytes(raw_body)
    if isinstance(raw_body, list):
        return bytes(raw_body)
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    return b""


def _json_data(request: Request) -> dict:
    body = _raw_body_bytes(request)
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _is_expired(expires_at: str) -> bool:
    try:
        return datetime.fromisoformat(expires_at) <= datetime.utcnow()
    except ValueError:
        return True


def _get_or_create_csrf_token(request: Request) -> Tuple[str, bool]:
    token = _get_cookie_value(request, CSRF_COOKIE_NAME)
    if token:
        return token, False
    return generate_csrf_token(), True


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(CSRF_COOKIE_NAME, token, **csrf_cookie_settings())


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE_NAME, token, **cookie_settings())


def _clear_session_cookie(response: Response) -> None:
    response.set_cookie(SESSION_COOKIE_NAME, "", **cookie_clear_settings())


def _apply_common_cookies(
    response: Response,
    *,
    clear_session: bool = False,
    csrf_token: Optional[str] = None,
    set_csrf: bool = False,
) -> None:
    if clear_session:
        _clear_session_cookie(response)
    if set_csrf and csrf_token:
        _set_csrf_cookie(response, csrf_token)


def _html_response(body: str, *, status: int = 200) -> Response:
    """Wrap an HTML body inside a minimal Robyn response."""
    return Response(
        status_code=status,
        headers={"content-type": "text/html; charset=utf-8"},
        description=body,
    )


def _build_nav(user: Optional[UserRecord], csrf_token: Optional[str]) -> str:
    """Render the shared navigation bar shown at the top of every page."""
    links = ['<a href="/">Home</a>', '<a href="/public">Public</a>']
    if user:
        logout_html = ""
        if csrf_token:
            logout_html = f"""
                <form method="post" action="/logout" class="logout-form">
                  <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
                  <button type="submit">Log out</button>
                </form>
            """
        links.extend(
            [
                '<a href="/dashboard">Dashboard</a>',
                '<a href="/profile">Profile</a>',
                f'<span class="status">Signed in as {escape(user.username)}</span>',
            ]
        )
        if logout_html:
            links.append(logout_html)
    else:
        links.extend(
            [
                '<a href="/register">Register</a>',
                '<a href="/login">Log in</a>',
            ]
        )
    return """<nav>""" + " | ".join(links) + """</nav>"""


def _message_block(messages: Iterable[str], kind: str = "info") -> str:
    """Return a styled message list, used for errors and confirmations."""
    if not messages:
        return ""
    items = "".join(f"<li>{escape(text)}</li>" for text in messages)
    return f"""
        <div class=\"message {kind}\">
            <ul>{items}</ul>
        </div>
    """


def _page_template(
    *,
    title: str,
    body: str,
    user: Optional[UserRecord] = None,
    csrf_token: Optional[str] = None,
    messages: Iterable[str] | None = None,
    message_kind: str = "info",
) -> str:
    """Wrap any page body inside a styled template with optional flash messages."""
    nav = _build_nav(user, csrf_token)
    message_html = _message_block(messages or [], message_kind)
    return f"""
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 1.5rem; }}
    nav {{ margin-bottom: 1rem; display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }}
    nav a {{ text-decoration: none; color: #0f62fe; font-weight: 600; }}
    nav .logout-form {{ display: inline; margin: 0; }}
    nav .logout-form button {{ width: auto; padding: 0.35rem 0.75rem; font-size: 0.9rem; }}
    .message {{ border-radius: 6px; padding: 0.75rem 1rem; margin-bottom: 1.25rem; }}
    .message.info {{ background: #e8f0fe; border: 1px solid #bad1fe; }}
    .message.error {{ background: #ffe3e3; border: 1px solid #f5b7b7; }}
    .status {{ font-weight: 600; }}
    footer {{ margin-top: 3rem; font-size: 0.9rem; color: #555; }}
    input, button {{ font: inherit; margin-top: 0.25rem; width: 100%; padding: 0.6rem; border-radius: 6px; border: 1px solid #c4c4c4; }}
    button {{ cursor: pointer; background: #0f62fe; border: none; color: white; font-weight: 600; }}
    form {{ max-width: 380px; }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)}</h1>
    {nav}
  </header>
  <main>
    {message_html}
    {body}
  </main>
  <footer>This service is run with Robyn and keeps credentials hashed.</footer>
</body>
</html>
"""


async def _get_auth_context(request: Request) -> AuthContext:
    """Resolve the current user and session from the cookie, if present."""
    token = _get_cookie_value(request, SESSION_COOKIE_NAME)
    if not token:
        return AuthContext(user=None, session=None, clear_cookie=False)
    token_hash = hash_session_token(token)
    session = await db.fetch_session_by_token_hash(token_hash)
    if not session:
        return AuthContext(user=None, session=None, clear_cookie=True)
    if session.revoked_at or _is_expired(session.expires_at):
        if not session.revoked_at:
            await db.revoke_session(session.id, _now_iso())
        return AuthContext(user=None, session=None, clear_cookie=True)
    user = await db.fetch_user_by_id(session.user_id)
    if not user:
        return AuthContext(user=None, session=None, clear_cookie=True)
    await db.touch_session(session.id, _now_iso())
    return AuthContext(user=user, session=session, clear_cookie=False)


async def _current_user(request: Request) -> Optional[UserRecord]:
    """Resolve the current user from the session cookie (if present)."""
    return (await _get_auth_context(request)).user


def _redirect(location: str) -> Response:
    """Send a 303 redirect to the user agent."""
    return Response(
        status_code=303,
        headers={"location": location},
        description="",
    )


def _json_response(payload: dict, *, status: int = 200) -> Response:
    return Response(
        status_code=status,
        headers={"content-type": "application/json; charset=utf-8"},
        description=json.dumps(payload),
    )


def _ollama_endpoint() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "llava")


def _normalize_content_type(content_type: str, filename: str) -> str:
    candidate = (content_type or "").strip().lower()
    if candidate.startswith("image/"):
        return candidate
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "application/octet-stream"


def _parse_taken_at(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.isoformat()


def _generate_image_description_with_ollama(
    image_bytes: bytes,
    *,
    filename: str,
) -> str:
    endpoint = f"{_ollama_endpoint()}/api/generate"
    payload = {
        "model": _ollama_model(),
        "prompt": (
            "You are describing a user photo for search and organization. "
            "Write 1-2 concise sentences with the key visible subjects, setting, and notable details."
        ),
        "stream": False,
        "images": [base64.b64encode(image_bytes).decode("utf-8")],
    }
    req = urllib_request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    description = (parsed.get("response") or "").strip()
    if not description:
        return ""
    return f"{description}"


def _redirect_with_next(path: str, *, query: Optional[str] = None) -> Response:
    """Append a query string (usually the original destination) before redirecting."""
    dest = path
    if query:
        dest = f"{path}?{query}"
    return _redirect(dest)


def _normalize_redirect_path(raw: Optional[str], default: str = "/dashboard") -> str:
    """Ensure redirect paths are never empty and always rooted (start with '/')."""
    candidate = (raw or "").strip()
    if not candidate or not candidate.startswith("/"):
        return default
    return candidate


async def _ensure_authenticated(request: Request) -> Response | AuthContext:
    """Return the authenticated context or issue a login redirect if missing."""
    context = await _get_auth_context(request)
    if context.user:
        return context
    target = _normalize_redirect_path(request.url.path)
    response = _redirect_with_next("/login", query=urlencode({"next": target}))
    if context.clear_cookie:
        _clear_session_cookie(response)
    return response


def _login_form(
    next_path: str,
    csrf_token: str,
    *,
    messages: Iterable[str] | None = None,
) -> str:
    """Render the login form, optionally showing validation errors."""
    return f"""
    <section>
      <h2>Log in</h2>
      {_message_block(messages or [], 'error')}
      <form method=\"post\">
        <label>
          Email
          <input type=\"email\" name=\"email\" required autofocus>
        </label>
        <label>
          Password
        <input type=\"password\" name=\"password\" required minlength=8>
      </label>
      <input type=\"hidden\" name=\"next\" value=\"{escape(next_path)}\">
      <input type=\"hidden\" name=\"csrf_token\" value=\"{escape(csrf_token)}\">
      <button type=\"submit\">Sign in</button>
      </form>
    </section>
    """


def _register_form(
    csrf_token: str,
    values: dict[str, str] | None = None,
    *,
    messages: Iterable[str] | None = None,
) -> str:
    """Return the registration form while preserving prior input and errors."""
    vals = values or {}
    username = escape(vals.get("username", ""))
    email = escape(vals.get("email", ""))
    return f"""
    <section>
      <h2>Create an account</h2>
      {_message_block(messages or [], 'error')}
      <form method=\"post\">
        <label>
          Username
          <input type=\"text\" name=\"username\" value=\"{username}\" required minlength=3 maxlength=50>
        </label>
        <label>
          Email
          <input type=\"email\" name=\"email\" value=\"{email}\" required>
        </label>
        <label>
          Password
          <input type=\"password\" name=\"password\" required minlength=8>
        </label>
        <label>
          Confirm password
          <input type=\"password\" name=\"confirm_password\" required minlength=8>
        </label>
        <input type=\"hidden\" name=\"csrf_token\" value=\"{escape(csrf_token)}\">
        <button type=\"submit\">Create account</button>
      </form>
    </section>
    """


@app.get("/")
async def home(request: Request) -> Response:
    """Landing page that always renders regardless of authentication state."""
    context = await _get_auth_context(request)
    csrf_token, set_csrf = _get_or_create_csrf_token(request)
    body = """
    <section>
      <p>TagLens keeps every sensitive credential hashed and salted before hitting the database.</p>
      <p>Navigate using the links above; authenticated areas require a session cookie.</p>
    </section>
    """
    response = jinja_template.render_template(
        "base/Base.html",
        request=request,
        title="Welcome to TagLens",
        body=body,
    )
    _apply_common_cookies(
        response,
        clear_session=context.clear_cookie,
        csrf_token=csrf_token,
        set_csrf=set_csrf,
    )
    return response


@app.get("/public")
async def public_page(request: Request) -> Response:
    """Informational route accessible to unauthenticated visitors."""
    context = await _get_auth_context(request)
    csrf_token, set_csrf = _get_or_create_csrf_token(request)
    body = """
    <section>
      <h2>Public area</h2>
      <p>Anyone can reach this without signing in.</p>
    </section>
    """
    response = _html_response(
        _page_template(
            title="Public",
            body=body,
            user=context.user,
            csrf_token=csrf_token,
        )
    )
    _apply_common_cookies(
        response,
        clear_session=context.clear_cookie,
        csrf_token=csrf_token,
        set_csrf=set_csrf,
    )
    return response


@app.get("/dashboard")
async def dashboard(request: Request) -> Response:
    """Private dashboard that requires a valid session cookie."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth
    context = auth
    user = context.user
    csrf_token, set_csrf = _get_or_create_csrf_token(request)
    body = f"""
    <section>
      <h2>Dashboard</h2>
      <p>Welcome back, {escape(user.username)}.</p>
      <p>Your account was created on {escape(user.created_at)} UTC.</p>
    </section>
    """
    response = jinja_template.render_template(
        "dashboard/Dashboard.html",
        request=request,
        title="Dashboard",
        user=user,
    )
    _apply_common_cookies(
        response,
        clear_session=context.clear_cookie,
        csrf_token=csrf_token,
        set_csrf=set_csrf,
    )
    return response



@app.get("/profile")
async def profile(request: Request) -> Response:
    """Show account metadata for the signed-in user."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth
    context = auth
    user = context.user
    csrf_token, set_csrf = _get_or_create_csrf_token(request)
    

    created_at = datetime.fromisoformat(user.created_at)

    user_dict = {
    "username": user.username,
    "email": user.email,
    "created_at": created_at
    }

    
    response = jinja_template.render_template(
        "user_profile/UserProfile.html",
        request=request,
        user=user_dict
    )
    
    _apply_common_cookies(
        response,
        clear_session=context.clear_cookie,
        csrf_token=csrf_token,
        set_csrf=set_csrf,
    )
    return response


@app.get("/UserProfile.js")
async def profile_js(_: Request) -> Response:
    with open("frontend/pages/user_profile/Userprofile.js", "r", encoding="utf-8") as f:
        content = f.read()
    return Response(
        status_code=200,
        headers={"content-type": "application/javascript; charset=utf-8"},
        description=content,
    )


@app.get("/api/profile")
async def profile_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    user = auth.user
    sort_by = str(request.query_params.get("sort_by", "uploaded")).strip().lower()
    order = str(request.query_params.get("order", "desc")).strip().lower()
    if sort_by not in {"uploaded", "taken"}:
        return _json_response({"error": "sort_by must be uploaded or taken"}, status=400)
    if order not in {"asc", "desc"}:
        return _json_response({"error": "order must be asc or desc"}, status=400)
    images = await db.list_images_for_user(user.id, sort_by=sort_by, order=order)
    logger.info(
        "profile photos listed user_id=%s count=%s sort_by=%s order=%s",
        user.id,
        len(images),
        sort_by,
        order,
    )
    return _json_response(
        {
            "username": user.username,
            "email": user.email,
            "created_at": user.created_at,
            "photos": [
                {
                    "id": record.id,
                    "filename": record.filename,
                    "created_at": record.created_at,
                    "taken_at": record.taken_at,
                    "description": record.ai_description,
                    "ocr_text": record.ocr_text,
                }
                for record in images
            ],
        }
    )

@app.post("/api/photos")
async def upload_photo_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    payload = _json_data(request)
    filename = str(payload.get("filename", "")).strip()
    image_base64 = str(payload.get("image_base64", "")).strip()
    taken_at = _parse_taken_at(payload.get("taken_at"))
    content_type = _normalize_content_type(
        str(payload.get("content_type", "")),
        filename,
    )
    if not filename or not image_base64:
        logger.warning(
            "upload rejected user_id=%s reason=missing_fields filename_present=%s image_present=%s",
            auth.user.id,
            bool(filename),
            bool(image_base64),
        )
        return _json_response(
            {"error": "filename and image_base64 are required"},
            status=400,
        )
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (ValueError, TypeError):
        logger.warning(
            "upload rejected user_id=%s filename=%s reason=invalid_base64",
            auth.user.id,
            filename,
        )
        return _json_response({"error": "invalid image_base64 payload"}, status=400)
    if not image_bytes:
        logger.warning(
            "upload rejected user_id=%s filename=%s reason=empty_payload",
            auth.user.id,
            filename,
        )
        return _json_response({"error": "empty image payload"}, status=400)
    logger.info(
        "upload started user_id=%s filename=%s bytes=%s content_type=%s",
        auth.user.id,
        filename,
        len(image_bytes),
        content_type,
    )
    try:
        # 1. Generate caption
        description = await asyncio.to_thread(
            _generate_image_description_with_ollama,
            image_bytes,
            filename=filename,
        )

        # 2. Create temporary file (needed for metadata if required)
        os.makedirs("temp", exist_ok=True)
        temp_path = f"temp/{filename}"

        with open(temp_path, "wb") as f:
            f.write(image_bytes)

        # 3. Insert metadata FIRST to get photo_id
        saved = await db.create_image_metadata(
            filename=filename,
            faces_json="[]",
            ocr_text="",
            user_id=auth.user.id,
            ai_description=description,
            content_type=content_type,
            image_data=None,  # 🚨 REMOVE blob storage
            taken_at=taken_at,
        )

        photo_id = saved.id
        ext = pathlib.Path(filename).suffix.lower()
        b2_key = f"{auth.user.id}/{photo_id}{ext}"

        # 4. Upload to B2
        await asyncio.to_thread(
            bucket.upload_bytes,
            image_bytes,
            b2_key,
            content_type=content_type,
        )

        try:
            bucket.get_file_info_by_name(b2_key)
            print("B2 CONFIRMED EXISTS:", b2_key)
        except Exception as e:
            print("B2 NOT FOUND:", e)
        # 5. Delete temp file
        os.remove(temp_path)

    except Exception:
        logger.exception("upload failed user_id=%s filename=%s", auth.user.id, filename)
        return _json_response({"error": "upload failed unexpectedly"}, status=500)
    logger.info(
        "upload completed user_id=%s photo_id=%s filename=%s",
        auth.user.id,
        saved.id,
        filename,
    )
    return _json_response(
        {
            "id": saved.id,
            "filename": saved.filename,
            "created_at": saved.created_at,
            "taken_at": saved.taken_at,
            "description": saved.ai_description,
        },
        status=201,
    )


@app.get("/api/photos/download")
async def download_photo_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)

    raw_photo_id = str(request.query_params.get("photo_id", "")).strip()
    if not raw_photo_id.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)

    photo_id = int(raw_photo_id)

    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)

    # 🔥 Reconstruct B2 key
    ext = pathlib.Path(record.filename).suffix.lower()
    file_key = f"{auth.user.id}/{photo_id}{ext}"

    # 🔥 Generate signed URL (5 minute access)
    auth_token = bucket.get_download_authorization(
        file_key,
        valid_duration_in_seconds=300
    )

    download_base = bucket.get_download_url("")
    signed_url = f"{download_base}{file_key}?Authorization={auth_token}"

    return _json_response({
        "url": signed_url,
        "filename": record.filename
    })

@app.get("/api/photos/view")
async def view_photo(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth

    raw_photo_id = request.query_params.get("photo_id", None)
    if not raw_photo_id or not raw_photo_id.isdigit():
        return Response(status_code=400, headers={}, description="Invalid photo_id")

    photo_id = int(raw_photo_id)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return Response(status_code=404, headers={}, description="Not found")

    ext = pathlib.Path(record.filename).suffix.lower()
    b2_key = f"{auth.user.id}/{photo_id}{ext}"

    auth_token = bucket.get_download_authorization(b2_key, valid_duration_in_seconds=300)
    download_base = bucket.get_download_url("")
    signed_url = f"{download_base}{b2_key}?Authorization={auth_token}"

    return Response(
        status_code=302,
        headers={"location": signed_url},
        description="",
    )


@app.delete("/api/photos")
async def delete_photo_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    payload = _json_data(request)
    photo_id_raw = str(payload.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    if payload.get("confirm_delete") is not True:
        return _json_response(
            {"error": "confirm_delete=true is required to delete a photo"},
            status=400,
        )
    photo_id = int(photo_id_raw)
    logger.info("delete requested user_id=%s photo_id=%s", auth.user.id, photo_id)
    try:
        deleted = await db.delete_image_for_user(photo_id, auth.user.id)
    except Exception:
        logger.exception(
            "delete failed user_id=%s photo_id=%s reason=db_error",
            auth.user.id,
            photo_id,
        )
        return _json_response({"error": "delete failed unexpectedly"}, status=500)
    if not deleted:
        logger.warning(
            "delete rejected user_id=%s photo_id=%s reason=not_found",
            auth.user.id,
            photo_id,
        )
        return _json_response({"error": "photo not found"}, status=404)
    logger.info("delete completed user_id=%s photo_id=%s", auth.user.id, photo_id)
    return _json_response({"status": "deleted", "photo_id": photo_id})


@app.get("/login")
async def login_get(request: Request) -> Response:
    """Render the login form and show flash messages if redirected from registration."""
    context = await _get_auth_context(request)
    if context.user:
        return _redirect("/dashboard")
    next_path = _normalize_redirect_path(request.query_params.get("next", None))
    csrf_token, set_csrf = _get_or_create_csrf_token(request)
    messages: list[str] = []
    if request.query_params.get("registered", None) == "1":
        messages.append("Account created. Please sign in.")
    response = jinja_template.render_template(
        "login/Login.html",
        request=request,
        next_path=next_path,
        csrf_token=csrf_token,
    )
    _apply_common_cookies(
        response,
        clear_session=context.clear_cookie,
        csrf_token=csrf_token,
        set_csrf=set_csrf,
    )
    return response


@app.post("/login")
async def login_post(request: Request) -> Response:
    """Validate credentials and issue a session token when authentication succeeds."""
    form = _form_data(request)
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    next_path = _normalize_redirect_path(form.get("next"))
    csrf_cookie = _get_cookie_value(request, CSRF_COOKIE_NAME)
    csrf_form = form.get("csrf_token")
    errors = []
    csrf_valid = verify_csrf_token(csrf_cookie, csrf_form)
    if not csrf_valid:
        errors.append("CSRF validation failed. Please try again.")
    if not email:
        errors.append("Email is required.")
    if not password:
        errors.append("Password is required.")
    if errors:
        csrf_token = generate_csrf_token()
        template_response = jinja_template.render_template(
            "login/Login.html",
            request=request,
            next_path=next_path,
            csrf_token=csrf_token,
            errors=errors,
        )

        status = 400 if not csrf_valid else 200

        response = Response(
            description=template_response.description,
            status_code=status,
            headers=template_response.headers
        )
        _set_csrf_cookie(response, csrf_token)
        return response
    user = await db.fetch_user_by_email(email)
    if not user or not verify_password(password, user.password_hash):
        csrf_token = generate_csrf_token()
        template_response = jinja_template.render_template(
            "login/Login.html",
            request=request,
            title="Sign in",
            next_path=next_path,
            csrf_token=csrf_token,
            messages=["Invalid credentials."]
        )

        response = Response(
            description=template_response.description,
            status_code=401,
            headers=template_response.headers
        )
        _set_csrf_cookie(response, csrf_token)
        return response
    existing_token = _get_cookie_value(request, SESSION_COOKIE_NAME)
    if existing_token:
        await db.revoke_session_by_hash(hash_session_token(existing_token), _now_iso())
    session_token = generate_session_token()
    user_agent = request.headers.get("user-agent")
    ip_addr = getattr(request, "ip_addr", None) or request.headers.get(
        "x-forwarded-for"
    )
    expires_at = (
        datetime.utcnow() + timedelta(seconds=session_expiration())
    ).isoformat()
    await db.create_session(
        user_id=user.id,
        token_hash=session_token.token_hash,
        expires_at=expires_at,
        user_agent=user_agent,
        ip_address=ip_addr,
    )
    redirect_destination = next_path if next_path.strip() else "/dashboard"
    response = _redirect(redirect_destination)
    # Store the session in a httponly cookie so browsers send it automatically.
    _set_session_cookie(response, session_token.token)
    csrf_token = generate_csrf_token()
    _set_csrf_cookie(response, csrf_token)
    return response


@app.get("/register")
async def register_get(request: Request) -> Response:
    """Show the account creation form unless the user is already signed in."""
    context = await _get_auth_context(request)
    if context.user:
        return _redirect("/dashboard")
    csrf_token, set_csrf = _get_or_create_csrf_token(request)
    response = jinja_template.render_template(
        "register/Register.html",
        request=request,
        csrf_token=csrf_token,)
    _apply_common_cookies(
        response,
        clear_session=context.clear_cookie,
        csrf_token=csrf_token,
        set_csrf=set_csrf,
    )
    return response


@app.post("/register")
async def register_post(request: Request) -> Response:
    """Process registration data, enforce validation, and persist new users."""
    form = _form_data(request)
    username = (form.get("username") or "").strip()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    confirm = form.get("confirm_password") or ""
    csrf_cookie = _get_cookie_value(request, CSRF_COOKIE_NAME)
    csrf_form = form.get("csrf_token")
    # Keep track of every validation failure so we can redisplay them at once.
    errors = []
    csrf_valid = verify_csrf_token(csrf_cookie, csrf_form)
    if not csrf_valid:
        errors.append("CSRF validation failed. Please try again.")
    if len(username) < 3:
        errors.append("Pick a username that is at least 3 characters.")
    if not email:
        errors.append("Email is required.")
    if len(password) < 8:
        errors.append("Passwords must be at least 8 characters.")
    if password != confirm:
        errors.append("Password confirmation does not match.")
    if errors:
        csrf_token = generate_csrf_token()
        template_response = jinja_template.render_template(
            "register/Register.html",
            request=request,
            csrf_token=csrf_token,
        )

        status = 400 if not csrf_valid else 200

        response = Response(
            description=template_response.description,
            status_code=status,
            headers=template_response.headers
        )
        _set_csrf_cookie(response, csrf_token)
        return response
    try:
        password_hash = hash_password(password)
        await db.create_user(username, email, password_hash)
    except aiosqlite.IntegrityError:
        # The DB enforces uniqueness so a duplicate inserts will raise here.
        errors.append("That username or email is already registered.")
        csrf_token = generate_csrf_token()
        template_response = jinja_template.render_template(
            "register/Register.html",
            request=request,
            csrf_token=csrf_token,
            errors=errors,
        )

        status = 400 if not csrf_valid else 200

        response = Response(
            description=template_response.description,
            status_code=status,
            headers=template_response.headers
        )
        _set_csrf_cookie(response, csrf_token)
        return response
    return _redirect_with_next("/login", query=urlencode({"registered": "1"}))


@app.post("/logout")
async def logout(request: Request) -> Response:
    """Revoke the session cookie and return to the public landing page."""
    form = _form_data(request)
    csrf_cookie = _get_cookie_value(request, CSRF_COOKIE_NAME)
    csrf_form = form.get("csrf_token")
    if not verify_csrf_token(csrf_cookie, csrf_form):
        response = _redirect("/")
        _clear_session_cookie(response)
        return response
    token = _get_cookie_value(request, SESSION_COOKIE_NAME)
    if token:
        await db.revoke_session_by_hash(hash_session_token(token), _now_iso())
    response = _redirect("/")
    _clear_session_cookie(response)
    return response

@app.get("/debug/b2")
def debug_b2():
    files = []
    for file_version, _ in bucket.ls(recursive=True):
        files.append({
            "file_name": file_version.file_name,
            "size": file_version.size,
            "upload_timestamp": file_version.upload_timestamp,
        })
    return {"files": files}


if __name__ == "__main__":
    app.start(_check_port=False)