from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlencode
from urllib import error as urllib_error, request as urllib_request
import base64
import collections
import io
import json
import os
import pathlib
import re
import threading
import time
from b2sdk.v2 import B2Api, InMemoryAccountInfo


from markupsafe import escape
from robyn import Request, Response, Robyn
from robyn.templating import JinjaTemplate
import mimetypes
import logging
import asyncio
try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # pragma: no cover - optional dependency in some environments
    Image = None

    class UnidentifiedImageError(Exception):
        pass

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
try:  # Optional metadata pipeline (best-effort).
    from scripts.metadata import (
        extract_metadata_from_bytes,
        metadata_to_dict,
        dependency_report,
        missing_dependencies,
    )
except Exception:  # pragma: no cover - optional dependency
    extract_metadata_from_bytes = None
    metadata_to_dict = None
    dependency_report = None
    missing_dependencies = None

# Author: Daniel Neugent

logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
for _noisy in ("httpx", "easyocr", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

app = Robyn(__file__)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

current_file_path = pathlib.Path(__file__).parent.resolve()
static_dir = current_file_path / "frontend" / "static"

jinja_template = JinjaTemplate(os.path.join(current_file_path, "frontend/pages"))

class RateLimiter:
    """Simple in-memory sliding-window rate limiter keyed by IP."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, collections.deque())
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max_requests:
                return False
            dq.append(now)
            return True


# Per-route limiters
_login_limiter = RateLimiter(max_requests=10, window_seconds=60)
_register_limiter = RateLimiter(max_requests=5, window_seconds=60)
_upload_limiter = RateLimiter(max_requests=30, window_seconds=60)

# Singletons used by every request
db = Database()

KEY_ID = os.getenv("KEY_ID")
APP_KEY = os.getenv("APP_KEY") or os.getenv("API_KEY")
BUCKET_NAME = os.getenv("BUCKET_NAME")

bucket = None


def _initialize_b2_bucket() -> None:
    """Initialize Backblaze bucket access when credentials are configured."""
    global bucket
    if not KEY_ID or not APP_KEY or not BUCKET_NAME:
        logger.warning(
            "Backblaze disabled: missing KEY_ID/API_KEY(APP_KEY)/BUCKET_NAME."
        )
        return
    try:
        info = InMemoryAccountInfo()
        b2_api = B2Api(info)
        b2_api.authorize_account("production", KEY_ID, APP_KEY)
        bucket = b2_api.get_bucket_by_name(BUCKET_NAME)
    except Exception as exc:  # pragma: no cover - external dependency failure
        bucket = None
        logger.warning("Backblaze disabled: authorization failed (%s)", exc)


async def _ensure_database() -> None:
    """Prepare the sqlite file before handling the first request."""
    await db.initialize()
    _initialize_b2_bucket()
    if _metadata_enabled():
        if missing_dependencies is None:
            logger.warning("metadata deps check unavailable: metadata module import failed")
        else:
            missing = missing_dependencies()
            if missing:
                logger.warning("metadata deps missing: %s", ", ".join(missing))
            else:
                logger.info("metadata deps satisfied")


app.startup_handler(_ensure_database)
app.serve_directory(
    route="/static",
    directory_path=str(static_dir),
    show_files_listing=False,
    index_file=None,
)


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
    return _utc_now().isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_expired(expires_at: str) -> bool:
    try:
        return _as_utc(datetime.fromisoformat(expires_at)) <= _utc_now()
    except ValueError:
        return True


def _get_or_create_csrf_token(request: Request) -> Tuple[str, bool]:
    token = _get_cookie_value(request, CSRF_COOKIE_NAME)
    if token:
        return token, False
    return generate_csrf_token(), True


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting."""
    return (
        getattr(request, "ip_addr", None)
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or "unknown"
    )


def _verify_api_csrf(request: Request) -> bool:
    """Validate CSRF for JSON API requests using X-CSRF-Token header."""
    csrf_cookie = _get_cookie_value(request, CSRF_COOKIE_NAME)
    csrf_header = request.headers.get("x-csrf-token")
    return verify_csrf_token(csrf_cookie, csrf_header)


def _cookie_header(name: str, value: str, settings: dict) -> str:
    parts = [f"{name}={value}"]
    max_age = settings.get("max_age")
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    expires = settings.get("expires")
    if expires:
        parts.append(f"Expires={expires}")
    path = settings.get("path")
    if path:
        parts.append(f"Path={path}")
    same_site = settings.get("same_site")
    if same_site:
        normalized = str(same_site).strip().capitalize()
        parts.append(f"SameSite={normalized}")
    if settings.get("secure"):
        parts.append("Secure")
    if settings.get("http_only"):
        parts.append("HttpOnly")
    return "; ".join(parts)


def _append_set_cookie(response: Response, name: str, value: str, settings: dict) -> None:
    response.headers.append("Set-Cookie", _cookie_header(name, value, settings))


def _set_csrf_cookie(response: Response, token: str) -> None:
    _append_set_cookie(response, CSRF_COOKIE_NAME, token, csrf_cookie_settings())


def _set_session_cookie(response: Response, token: str) -> None:
    _append_set_cookie(response, SESSION_COOKIE_NAME, token, cookie_settings())


def _clear_session_cookie(response: Response) -> None:
    _append_set_cookie(response, SESSION_COOKIE_NAME, "", cookie_clear_settings())


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
    # Throttle session touch to once per 5 minutes to reduce write I/O
    try:
        last_seen = _as_utc(datetime.fromisoformat(session.last_seen_at))
        if (_utc_now() - last_seen).total_seconds() > 300:
            await db.touch_session(session.id, _now_iso())
    except (ValueError, TypeError):
        await db.touch_session(session.id, _now_iso())
    return AuthContext(user=user, session=session, clear_cookie=False)


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
    return os.getenv("OLLAMA_MODEL", "qwen3.5:4b")


def _metadata_enabled() -> bool:
    raw = os.getenv("TAGLENS_METADATA_ENABLED", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _metadata_dependency_report() -> dict:
    if dependency_report is None:
        return {"available": {}, "missing": ["metadata_module_unavailable"]}
    report = dependency_report()
    missing = [name for name, present in report.items() if not present]
    return {"available": report, "missing": missing}


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


def _generate_thumbnail_webp(
    image_bytes: bytes,
    *,
    max_size: tuple[int, int] = (480, 480),
    quality: int = 70,
) -> bytes:
    if Image is None:
        raise RuntimeError("Pillow is required for thumbnail generation")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.thumbnail(max_size)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=quality, optimize=True)
            thumbnail = buffer.getvalue()
    except UnidentifiedImageError as exc:
        raise ValueError("unsupported image payload") from exc
    if not thumbnail:
        raise ValueError("empty thumbnail generated")
    return thumbnail


def _photo_file_key(user_id: int, photo_id: int, filename: str) -> str:
    ext = pathlib.Path(filename).suffix.lower()
    return f"{user_id}/{photo_id}{ext}"


def _photo_thumbnail_key(user_id: int, photo_id: int) -> str:
    return f"{user_id}/{photo_id}_thumb.webp"


def _fetch_bucket_bytes(file_key: str) -> Optional[bytes]:
    if bucket is None:
        return None
    auth_token = bucket.get_download_authorization(
        file_key,
        valid_duration_in_seconds=300,
    )
    download_base = bucket.get_download_url("")
    signed_url = f"{download_base}{file_key}?Authorization={auth_token}"
    with urllib_request.urlopen(signed_url, timeout=30) as response:
        return response.read()


def _fetch_bucket_image_bytes(user_id: int, photo_id: int, filename: str) -> Optional[bytes]:
    file_key = _photo_file_key(user_id, photo_id, filename)
    return _fetch_bucket_bytes(file_key)


def _delete_bucket_file_by_name(file_key: str) -> None:
    if bucket is None:
        return
    try:
        info = bucket.get_file_info_by_name(file_key)
        file_id = getattr(info, "id_", None) or getattr(info, "file_id", None)
        if not file_id:
            raise RuntimeError("Backblaze file id missing for delete")
        bucket.delete_file_version(file_id, file_key)
    except Exception as exc:
        msg = str(exc).lower()
        if any(token in msg for token in ("not found", "not_present", "not present", "no such")):
            return
        # Fallback: try listing versions by name.
        try:
            versions = bucket.list_file_versions(file_key)
            for version in versions:
                file_id = getattr(version, "id_", None) or getattr(version, "file_id", None)
                file_name = getattr(version, "file_name", None) or file_key
                if file_id:
                    bucket.delete_file_version(file_id, file_name)
                    return
        except Exception as exc:
            msg = str(exc).lower()
            if any(token in msg for token in ("not found", "not_present", "not present", "no such")):
                return
            raise


def _photo_view_response(user_id: int, photo_id: int, record) -> Response:
    if record.image_data:
        return Response(
            status_code=200,
            headers={"content-type": record.content_type},
            description=record.image_data,
        )
    if bucket is None:
        return Response(status_code=503, headers={}, description="Storage unavailable")
    b2_key = _photo_file_key(user_id, photo_id, record.filename)
    auth_token = bucket.get_download_authorization(
        b2_key, valid_duration_in_seconds=300
    )
    download_base = bucket.get_download_url("")
    signed_url = f"{download_base}{b2_key}?Authorization={auth_token}"

    return Response(
        status_code=302,
        headers={"location": signed_url},
        description="",
    )


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


async def _process_image_metadata(
    *,
    image_id: int,
    user_id: int,
    filename: str,
    image_bytes: bytes,
) -> None:
    if not _metadata_enabled():
        return
    if extract_metadata_from_bytes is None or metadata_to_dict is None:
        logger.info(
            "metadata extraction skipped image_id=%s reason=deps_missing",
            image_id,
        )
        return
    try:
        result = await asyncio.to_thread(
            extract_metadata_from_bytes,
            image_bytes,
            filename=filename,
        )
    except Exception:
        logger.exception(
            "metadata extraction failed image_id=%s filename=%s",
            image_id,
            filename,
        )
        return
    if not result:
        logger.info(
            "metadata extraction skipped image_id=%s reason=empty_result",
            image_id,
        )
        return
    data = metadata_to_dict(result)
    try:
        await db.upsert_image_metadata(
            image_id=image_id,
            user_id=user_id,
            faces_json=json.dumps(data.get("faces") or []),
            ocr_text=str(data.get("ocr_text") or ""),
            caption=str(data.get("caption") or ""),
            lat=data.get("lat"),
            lon=data.get("lon"),
            loc_description=data.get("loc_description"),
            loc_city=data.get("loc_city"),
            loc_state=data.get("loc_state"),
            loc_country=data.get("loc_country"),
            make=data.get("make"),
            model=data.get("model"),
            iso=data.get("iso"),
            f_stop=data.get("f_stop"),
            shutter_speed=data.get("shutter_speed"),
            focal_length=data.get("focal_length"),
            width=data.get("width"),
            height=data.get("height"),
            file_size_mb=data.get("file_size_mb"),
            taken_at=data.get("taken_at"),
        )
        logger.info("metadata stored image_id=%s filename=%s", image_id, filename)
    except Exception:
        logger.exception(
            "metadata store failed image_id=%s filename=%s",
            image_id,
            filename,
        )


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


@app.get("/dashboard")
async def dashboard(request: Request) -> Response:
    """Private dashboard that requires a valid session cookie."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth
    context = auth
    user = context.user
    csrf_token, set_csrf = _get_or_create_csrf_token(request)
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
        "created_at": created_at,
    }

    response = jinja_template.render_template(
        "user_profile/UserProfile.html", request=request, user=user_dict
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
    content = "console.info('UserProfile.js placeholder loaded.');"
    return Response(
        status_code=200,
        headers={"content-type": "application/javascript; charset=utf-8"},
        description=content,
    )


@app.get("/favicon.ico")
async def favicon(_: Request) -> Response:
    favicon_path = static_dir / "favicon.svg"
    if not favicon_path.exists():
        return Response(status_code=404, headers={}, description="")
    return Response(
        status_code=200,
        headers={"content-type": "image/svg+xml"},
        description=favicon_path.read_text(encoding="utf-8"),
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
        return _json_response(
            {"error": "sort_by must be uploaded or taken"}, status=400
        )
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
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    if not _upload_limiter.is_allowed(_client_ip(request)):
        return _json_response({"error": "too many uploads, try again later"}, status=429)
    payload = _json_data(request)
    raw_filename = str(payload.get("filename", "")).strip()
    # Keep only the basename and strip unsafe characters
    filename = os.path.basename(raw_filename)
    filename = re.sub(r"[^\w.\-() ]", "_", filename).strip()
    if not filename:
        filename = "upload"
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
    MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        logger.warning(
            "upload rejected user_id=%s filename=%s reason=too_large bytes=%s",
            auth.user.id,
            filename,
            len(image_bytes),
        )
        return _json_response({"error": "file exceeds 20 MB limit"}, status=413)
    logger.info(
        "upload started user_id=%s filename=%s bytes=%s content_type=%s",
        auth.user.id,
        filename,
        len(image_bytes),
        content_type,
    )
    thumbnail_data: Optional[bytes] = None
    try:
        thumbnail_data = await asyncio.to_thread(_generate_thumbnail_webp, image_bytes)
    except Exception:
        logger.warning(
            "upload thumbnail generation skipped user_id=%s filename=%s",
            auth.user.id,
            filename,
            exc_info=True,
        )
    try:
        # 1. Generate caption
        description = await asyncio.to_thread(
            _generate_image_description_with_ollama,
            image_bytes,
            filename=filename,
        )
    except Exception:
        logger.exception(
            "upload caption generation failed user_id=%s filename=%s",
            auth.user.id,
            filename,
        )
        return _json_response({"error": "upload failed unexpectedly"}, status=500)

    try:
        # 2. Insert metadata first to get photo_id.
        # Keep blob data only when B2 is unavailable.
        image_blob = image_bytes if bucket is None else None
        thumbnail_blob = thumbnail_data if bucket is None else None
        saved = await db.create_image_metadata(
            filename=filename,
            faces_json="[]",
            ocr_text="",
            user_id=auth.user.id,
            ai_description=description,
            content_type=content_type,
            image_data=image_blob,
            thumbnail_data=thumbnail_blob,
            thumbnail_content_type="image/webp",
            taken_at=taken_at,
        )
    except Exception:
        logger.exception(
            "upload database write failed user_id=%s filename=%s",
            auth.user.id,
            filename,
        )
        return _json_response({"error": "upload failed unexpectedly"}, status=500)

    try:
        photo_id = saved.id
        if bucket is not None:
            ext = pathlib.Path(filename).suffix.lower()
            b2_key = f"{auth.user.id}/{photo_id}{ext}"
            await asyncio.to_thread(
                bucket.upload_bytes,
                image_bytes,
                b2_key,
                content_type=content_type,
            )
            if thumbnail_data:
                thumb_key = _photo_thumbnail_key(auth.user.id, photo_id)
                await asyncio.to_thread(
                    bucket.upload_bytes,
                    thumbnail_data,
                    thumb_key,
                    content_type="image/webp",
                )
    except Exception:
        logger.exception(
            "upload file storage failed user_id=%s photo_id=%s filename=%s",
            auth.user.id,
            saved.id,
            filename,
        )
        try:
            await db.delete_image_for_user(saved.id, auth.user.id)
            logger.info("rolled back orphaned DB record photo_id=%s", saved.id)
        except Exception:
            logger.exception("rollback failed for photo_id=%s", saved.id)
        return _json_response({"error": "upload failed unexpectedly"}, status=500)
    logger.info(
        "upload completed user_id=%s photo_id=%s filename=%s",
        auth.user.id,
        saved.id,
        filename,
    )
    asyncio.create_task(
        _process_image_metadata(
            image_id=saved.id,
            user_id=auth.user.id,
            filename=filename,
            image_bytes=image_bytes,
        )
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

    if record.image_data:
        return _json_response(
            {
                "filename": record.filename,
                "content_type": record.content_type,
                "image_base64": base64.b64encode(record.image_data).decode("utf-8"),
            }
        )
    if bucket is None:
        return _json_response({"error": "file storage unavailable"}, status=503)
    file_key = _photo_file_key(auth.user.id, photo_id, record.filename)
    auth_token = bucket.get_download_authorization(
        file_key,
        valid_duration_in_seconds=300,
    )
    download_base = bucket.get_download_url("")
    signed_url = f"{download_base}{file_key}?Authorization={auth_token}"
    return _json_response(
        {
            "url": signed_url,
            "filename": record.filename,
            "content_type": record.content_type,
        }
    )


@app.get("/api/photos/metadata")
async def photo_metadata_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)

    raw_photo_id = str(request.query_params.get("photo_id", "")).strip()
    if not raw_photo_id.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(raw_photo_id)

    record = await db.fetch_image_metadata_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "metadata not found"}, status=404)

    try:
        faces = json.loads(record.faces_json or "[]")
    except json.JSONDecodeError:
        faces = []

    return _json_response(
        {
            "image_id": record.image_id,
            "faces": faces,
            "ocr_text": record.ocr_text,
            "caption": record.caption,
            "lat": record.lat,
            "lon": record.lon,
            "loc_description": record.loc_description,
            "loc_city": record.loc_city,
            "loc_state": record.loc_state,
            "loc_country": record.loc_country,
            "make": record.make,
            "model": record.model,
            "iso": record.iso,
            "f_stop": record.f_stop,
            "shutter_speed": record.shutter_speed,
            "focal_length": record.focal_length,
            "width": record.width,
            "height": record.height,
            "file_size_mb": record.file_size_mb,
            "taken_at": record.taken_at,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
    )


@app.get("/api/metadata/deps")
async def metadata_deps_api(_: Request) -> Response:
    report = _metadata_dependency_report()
    return _json_response(
        {
            "enabled": _metadata_enabled(),
            "missing": report["missing"],
            "available": report["available"],
        }
    )

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

    return _photo_view_response(auth.user.id, photo_id, record)

@app.get("/api/photos/thumb")
async def view_photo_thumbnail(request: Request) -> Response:
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

    if record.thumbnail_data:
        if bucket is not None:
            try:
                thumb_key = _photo_thumbnail_key(auth.user.id, photo_id)
                await asyncio.to_thread(
                    bucket.upload_bytes,
                    record.thumbnail_data,
                    thumb_key,
                    content_type=record.thumbnail_content_type or "image/webp",
                )
                await db.clear_image_thumbnail(photo_id, auth.user.id)
            except Exception:
                logger.warning(
                    "thumbnail migrate failed user_id=%s photo_id=%s",
                    auth.user.id,
                    photo_id,
                    exc_info=True,
                )
        return Response(
            status_code=200,
            headers={
                "content-type": record.thumbnail_content_type or "image/webp",
                "cache-control": "private, max-age=86400",
            },
            description=record.thumbnail_data,
        )
    if bucket is not None:
        try:
            thumb_key = _photo_thumbnail_key(auth.user.id, photo_id)
            thumbnail_data = await asyncio.to_thread(_fetch_bucket_bytes, thumb_key)
            if thumbnail_data:
                return Response(
                    status_code=200,
                    headers={
                        "content-type": "image/webp",
                        "cache-control": "private, max-age=86400",
                    },
                    description=thumbnail_data,
                )
        except Exception:
            logger.warning(
                "thumbnail fetch failed user_id=%s photo_id=%s",
                auth.user.id,
                photo_id,
                exc_info=True,
            )
    image_bytes: Optional[bytes] = record.image_data
    if image_bytes is None:
        try:
            image_bytes = await asyncio.to_thread(
                _fetch_bucket_image_bytes,
                auth.user.id,
                photo_id,
                record.filename,
            )
        except Exception:
            logger.warning(
                "thumbnail source fetch failed user_id=%s photo_id=%s",
                auth.user.id,
                photo_id,
                exc_info=True,
            )
            image_bytes = None

    if image_bytes:
        try:
            thumbnail_data = await asyncio.to_thread(
                _generate_thumbnail_webp, image_bytes
            )
            if bucket is not None:
                try:
                    thumb_key = _photo_thumbnail_key(auth.user.id, photo_id)
                    await asyncio.to_thread(
                        bucket.upload_bytes,
                        thumbnail_data,
                        thumb_key,
                        content_type="image/webp",
                    )
                except Exception:
                    logger.warning(
                        "thumbnail upload failed user_id=%s photo_id=%s",
                        auth.user.id,
                        photo_id,
                        exc_info=True,
                    )
            else:
                await db.update_image_thumbnail(
                    photo_id,
                    auth.user.id,
                    thumbnail_data,
                    "image/webp",
                )
            return Response(
                status_code=200,
                headers={
                    "content-type": "image/webp",
                    "cache-control": "private, max-age=86400",
                },
                description=thumbnail_data,
            )
        except Exception:
            logger.warning(
                "thumbnail generation failed user_id=%s photo_id=%s",
                auth.user.id,
                photo_id,
                exc_info=True,
            )
    return _photo_view_response(auth.user.id, photo_id, record)


@app.delete("/api/photos")
async def delete_photo_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
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
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        logger.warning(
            "delete rejected user_id=%s photo_id=%s reason=not_found",
            auth.user.id,
            photo_id,
        )
        return _json_response({"error": "photo not found"}, status=404)
    if bucket is not None:
        try:
            image_key = _photo_file_key(auth.user.id, photo_id, record.filename)
            await asyncio.to_thread(_delete_bucket_file_by_name, image_key)
            thumb_key = _photo_thumbnail_key(auth.user.id, photo_id)
            await asyncio.to_thread(_delete_bucket_file_by_name, thumb_key)
        except Exception:
            logger.exception(
                "delete failed user_id=%s photo_id=%s reason=b2_error",
                auth.user.id,
                photo_id,
            )
            return _json_response({"error": "delete failed unexpectedly"}, status=500)
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
    if not _login_limiter.is_allowed(_client_ip(request)):
        return _html_response("<p>Too many login attempts. Please try again later.</p>", status=429)
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
            messages=errors,
        )

        response = Response(
            description=template_response.description,
            status_code=200,
            headers=template_response.headers,
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
            messages=["Invalid credentials."],
        )

        response = Response(
            description=template_response.description,
            status_code=401,
            headers=template_response.headers,
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
        _utc_now() + timedelta(seconds=session_expiration())
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
        csrf_token=csrf_token,
    )
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
    if not _register_limiter.is_allowed(_client_ip(request)):
        return _html_response("<p>Too many registration attempts. Please try again later.</p>", status=429)
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
            messages=errors,
        )

        response = Response(
            description=template_response.description,
            status_code=200,
            headers=template_response.headers,
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
            messages=errors,
        )

        response = Response(
            description=template_response.description,
            status_code=200,
            headers=template_response.headers,
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


if __name__ == "__main__":
    app.start(_check_port=False)
