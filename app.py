"""
TagLens web app entrypoint.

Purpose:
    Defines the Robyn application, HTTP routes, and shared runtime services
    (DB connection, rate limits, metadata pipeline, and Backblaze integration).

Authorship (git history, mapped to real names):
    Daniel (l33tdaniel), Srihari (dimes130)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Iterable, Optional, Tuple
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
from database import Database, JobRecord, SessionRecord, UserRecord
from scripts.insightface_tagging import (
    detect_and_tag_faces_for_user,
    public_faces_payload,
)
from scripts.upload_metadata import extract_upload_metadata
import aiosqlite

app = Robyn(__file__)
logger = logging.getLogger(__name__)

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


# Per-route limiters: tuned to keep basic abuse in check without penalizing
# normal users during bursts of activity.
_login_limiter = RateLimiter(max_requests=10, window_seconds=60)
_register_limiter = RateLimiter(max_requests=5, window_seconds=60)
_upload_limiter = RateLimiter(max_requests=30, window_seconds=60)

# Singletons used by every request to avoid repeated setup overhead.
db = Database()

# Backblaze B2 credentials (optional). When missing, the app will operate
# purely on the local sqlite DB without remote storage.
KEY_ID = os.getenv("KEY_ID")
APP_KEY = os.getenv("APP_KEY") or os.getenv("API_KEY")
BUCKET_NAME = os.getenv("BUCKET_NAME")

bucket = None


def _env_truthy(name: str) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


ASYNC_PROCESSING = _env_truthy("TAGLENS_ASYNC_PROCESSING")
IMAGE_PROCESS_JOB_KIND = "process_image"
ENABLE_RETENTION = _env_truthy("TAGLENS_ENABLE_RETENTION")
DIRECT_B2_UPLOAD = _env_truthy("TAGLENS_DIRECT_B2_UPLOAD")

_signed_url_cache: dict[str, tuple[float, str]] = {}
_signed_url_lock = threading.Lock()


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
    if ASYNC_PROCESSING:
        asyncio.create_task(_job_worker_loop())
    if ENABLE_RETENTION:
        asyncio.create_task(_retention_worker_loop())


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
    """Normalize Robyn request body into bytes."""
    raw_body = request.body
    if isinstance(raw_body, (bytes, bytearray)):
        return bytes(raw_body)
    if isinstance(raw_body, list):
        return bytes(raw_body)
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    return b""


def _json_data(request: Request) -> dict:
    """Parse JSON body into a dict; return empty dict on failure."""
    body = _raw_body_bytes(request)
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 format."""
    return _utc_now().isoformat()


def _utc_now() -> datetime:
    """UTC datetime helper used across sessions and metadata."""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Coerce naive datetimes to UTC, otherwise convert to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_expired(expires_at: str) -> bool:
    """Return True if a stored ISO timestamp is in the past."""
    try:
        return _as_utc(datetime.fromisoformat(expires_at)) <= _utc_now()
    except ValueError:
        return True


def _get_or_create_csrf_token(request: Request) -> Tuple[str, bool]:
    """Return CSRF token + whether it needs to be set in a cookie."""
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


def _is_test_env() -> bool:
    return os.getenv("ROBYN_ENV", "").strip().lower() == "test"


def _allow_rate_limited_request(limiter: RateLimiter, request: Request) -> bool:
    # Integration tests run many requests from one local IP in a tight loop.
    if _is_test_env():
        return True
    return limiter.is_allowed(_client_ip(request))


def _cookie_header(name: str, value: str, settings: dict) -> str:
    """Build a Set-Cookie header value from common settings."""
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
    """Append a Set-Cookie header without clobbering existing cookies."""
    response.headers.append("Set-Cookie", _cookie_header(name, value, settings))


def _set_csrf_cookie(response: Response, token: str) -> None:
    """Set CSRF cookie using standard settings."""
    _append_set_cookie(response, CSRF_COOKIE_NAME, token, csrf_cookie_settings())


def _set_session_cookie(response: Response, token: str) -> None:
    """Set session cookie using standard settings."""
    _append_set_cookie(response, SESSION_COOKIE_NAME, token, cookie_settings())


def _clear_session_cookie(response: Response) -> None:
    """Expire the session cookie immediately."""
    _append_set_cookie(response, SESSION_COOKIE_NAME, "", cookie_clear_settings())


def _apply_common_cookies(
    response: Response,
    *,
    clear_session: bool = False,
    csrf_token: Optional[str] = None,
    set_csrf: bool = False,
) -> None:
    """Apply session/CSRF cookie mutations in a consistent order."""
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
    links = ['<a href="/">Home</a>']
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
    """Return a JSON response with a UTF-8 content-type."""
    return Response(
        status_code=status,
        headers={"content-type": "application/json; charset=utf-8"},
        description=json.dumps(payload),
    )


def _ollama_endpoint() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "qwen3.5:2b")


def _extract_metadata_from_bytes(image_bytes: bytes, filename: str = "") -> dict:
    """Extract EXIF, dimensions, OCR, and faces from raw image bytes."""
    from PIL.ExifTags import GPSTAGS

    data: dict = {
        "width": None, "height": None, "file_size_mb": None,
        "make": None, "model": None, "taken_at": None,
        "iso": None, "f_stop": None, "shutter_speed": None, "focal_length": None,
        "lat": None, "lon": None,
        "loc_description": None, "loc_city": None, "loc_state": None, "loc_country": None,
        "ocr_text": "", "caption": "", "faces": [],
    }
    if not image_bytes or Image is None:
        return data

    data["file_size_mb"] = round(len(image_bytes) / (1024 * 1024), 2)

    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception:
        return data

    data["width"], data["height"] = img.size

    # EXIF
    try:
        exif = img.getexif()
        exif_ifd = exif.get_ifd(34665)
        gps_info = exif.get_ifd(34853)

        data["make"] = exif.get(271) or None
        data["model"] = exif.get(272) or None
        raw_date = exif.get(36867) or exif.get(306)
        if raw_date:
            raw_text = str(raw_date).strip()
            for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    data["taken_at"] = datetime.strptime(raw_text, fmt).isoformat()
                    break
                except ValueError:
                    continue

        iso = exif_ifd.get(34855)
        data["iso"] = int(iso) if iso else None
        f_stop = exif_ifd.get(33437)
        data["f_stop"] = float(f_stop) if f_stop else None
        focal = exif_ifd.get(37386)
        data["focal_length"] = float(focal) if focal else None
        exposure = exif_ifd.get(33434)
        if exposure and isinstance(exposure, (int, float)) and 0 < exposure < 1:
            data["shutter_speed"] = f"1/{int(1/exposure)}"
        elif exposure:
            data["shutter_speed"] = str(exposure)

        # GPS
        if gps_info and len(gps_info) > 1:
            raw_gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}
            lat_val = raw_gps.get("GPSLatitude")
            lon_val = raw_gps.get("GPSLongitude")
            if lat_val and lon_val:
                try:
                    to_dec = lambda v: float(v[0]) + float(v[1]) / 60.0 + float(v[2]) / 3600.0
                    lat = to_dec(lat_val)
                    lon = to_dec(lon_val)
                    if raw_gps.get("GPSLatitudeRef") == "S":
                        lat = -lat
                    if raw_gps.get("GPSLongitudeRef") == "W":
                        lon = -lon
                    data["lat"] = lat
                    data["lon"] = lon
                except (ZeroDivisionError, TypeError, ValueError):
                    pass
    except Exception:
        pass

    # OCR
    try:
        extracted = extract_upload_metadata(image_bytes)
        data["ocr_text"] = extracted.ocr_text or ""
    except Exception:
        pass

    # Faces — try insightface first, fall back to OpenCV Haar cascade
    try:
        from scripts.insightface_tagging import _detect_faces_with_insightface
        detected = _detect_faces_with_insightface(image_bytes)
        if detected:
            data["faces"] = [
                {"x": f["x"], "y": f["y"], "w": f["w"], "h": f["h"]}
                for f in detected
            ]
            logger.info("insightface detected %d faces", len(detected))
    except Exception:
        logger.info("insightface unavailable, using Haar cascade fallback")
    if not data["faces"]:
        try:
            import cv2 as _cv2
            import numpy as np
            rgb_img = img.convert("RGB")
            rgb = np.array(rgb_img)
            gray = _cv2.cvtColor(rgb, _cv2.COLOR_RGB2GRAY)
            logger.info("haar cascade input: %dx%d gray shape=%s", rgb_img.width, rgb_img.height, gray.shape)
            img_h, img_w = gray.shape[:2]
            min_face = max(30, min(img_h, img_w) // 10)
            for cascade_name in ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml"):
                cascade = _cv2.CascadeClassifier(_cv2.data.haarcascades + cascade_name)
                rects = cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_face, min_face)
                )
                logger.info("haar %s: %d faces detected", cascade_name, len(rects))
                if len(rects) > 0:
                    data["faces"] = [
                        {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
                        for (x, y, w, h) in rects
                    ]
                    break
        except Exception:
            logger.exception("haar cascade face detection failed")

    # Caption (Ollama)
    try:
        caption = _generate_image_description_with_ollama(image_bytes, filename=filename)
        data["caption"] = caption or ""
    except Exception:
        pass

    return data


async def _process_image_metadata(
    *,
    image_id: int,
    user_id: int,
    filename: str,
    image_bytes: bytes,
) -> None:
    try:
        data = await asyncio.to_thread(
            _extract_metadata_from_bytes, image_bytes, filename,
        )
    except Exception:
        logger.exception(
            "metadata extraction failed image_id=%s filename=%s", image_id, filename,
        )
        return

    # Run face tagging with identity matching (insightface); keep Haar results as fallback
    try:
        faces = await detect_and_tag_faces_for_user(user_id, image_bytes, db)
        if faces:
            data["faces"] = faces
    except Exception:
        logger.warning("face tagging failed image_id=%s", image_id, exc_info=True)

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
        caption = str(data.get("caption") or "")
        if caption:
            await db.update_image_description(image_id, user_id, caption)
        await db.populate_fts_for_image(image_id, user_id)
    except Exception:
        logger.exception(
            "metadata store failed image_id=%s filename=%s", image_id, filename,
        )


def _metadata_response_dict(record) -> dict:
    try:
        faces = json.loads(record.faces_json or "[]")
    except json.JSONDecodeError:
        faces = []
    return {
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


def _normalize_content_type(content_type: str, filename: str) -> str:
    """Normalize content type; fall back to filename inference."""
    candidate = (content_type or "").strip().lower()
    if candidate.startswith("image/"):
        return candidate
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "application/octet-stream"


def _parse_taken_at(value: object) -> Optional[str]:
    """Normalize a user-provided timestamp into ISO-8601."""
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
    """Create a WEBP thumbnail from raw image bytes."""
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
    """Return a stable storage key for the original upload."""
    ext = pathlib.Path(filename).suffix.lower()
    return f"{user_id}/{photo_id}{ext}"


def _fetch_bucket_image_bytes(user_id: int, photo_id: int, filename: str) -> Optional[bytes]:
    """Fetch original image bytes from Backblaze for a photo."""
    if bucket is None:
        return None
    file_key = _photo_file_key(user_id, photo_id, filename)
    signed_url = _get_cached_signed_url(file_key, valid_duration_in_seconds=300)
    with urllib_request.urlopen(signed_url, timeout=30) as response:
        return response.read()


def _get_cached_signed_url(file_key: str, *, valid_duration_in_seconds: int) -> str:
    if bucket is None:
        raise RuntimeError("Storage unavailable")
    now = time.monotonic()
    with _signed_url_lock:
        cached = _signed_url_cache.get(file_key)
        if cached and cached[0] > now:
            return cached[1]
    auth_token = bucket.get_download_authorization(
        file_key,
        valid_duration_in_seconds=valid_duration_in_seconds,
    )
    download_base = bucket.get_download_url("")
    signed_url = f"{download_base}{file_key}?Authorization={auth_token}"
    # Cache slightly shorter than token lifetime to avoid edge-of-expiry failures.
    cache_expires = now + max(0, valid_duration_in_seconds - 15)
    with _signed_url_lock:
        _signed_url_cache[file_key] = (cache_expires, signed_url)
    return signed_url


def _photo_view_response(
    user_id: int,
    photo_id: int,
    record,
    *,
    allow_thumbnail_fallback: bool = False,
) -> Response:
    """Return a response that serves the original image or redirects to B2."""
    if record.image_data:
        return Response(
            status_code=200,
            headers={"content-type": record.content_type},
            description=record.image_data,
        )
    if allow_thumbnail_fallback and record.thumbnail_data:
        return Response(
            status_code=200,
            headers={
                "content-type": record.thumbnail_content_type or "image/webp",
                "cache-control": "private, max-age=86400",
            },
            description=record.thumbnail_data,
        )
    if bucket is None:
        return Response(status_code=503, headers={}, description="Storage unavailable")
    b2_key = _photo_file_key(user_id, photo_id, record.filename)
    signed_url = _get_cached_signed_url(b2_key, valid_duration_in_seconds=300)

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
    """Generate a short caption using the local Ollama image model."""
    endpoint = f"{_ollama_endpoint()}/api/generate"
    logger.info("ollama caption request endpoint=%s model=%s filename=%s", endpoint, _ollama_model(), filename)
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
            logger.info("ollama raw response filename=%s body=%.200s", filename, raw)
            parsed = json.loads(raw) if raw else {}
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("ollama caption failed filename=%s error=%s", filename, exc)
        return ""
    if not isinstance(parsed, dict):
        logger.warning("ollama unexpected response type filename=%s type=%s", filename, type(parsed))
        return ""
    description = (parsed.get("response") or "").strip()
    logger.info("ollama caption result filename=%s description=%.100s", filename, description)
    if not description:
        return ""
    return f"{description}"

def _generate_ocr_with_ollama(
    image_bytes: bytes,
    *,
    filename: str,
) -> str:
    """Generate a short caption using the local Ollama image model."""
    endpoint = f"{_ollama_endpoint()}/api/generate"
    logger.info("ollama ocr request endpoint=%s model=%s filename=%s", endpoint, _ollama_model(), filename)
    payload = {
        "model": _ollama_model(),
        "prompt": (
            "Extract all visible text from this image exactly as it appears. "
            "Return only the raw text, no commentary, no formatting."
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
            logger.info("ollama raw response filename=%s body=%.200s", filename, raw)
            parsed = json.loads(raw) if raw else {}
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("ollama ocr failed filename=%s error=%s", filename, exc)
        return ""
    if not isinstance(parsed, dict):
        logger.warning("ollama ocr unexpected response type filename=%s type=%s", filename, type(parsed))
        return ""
    description = (parsed.get("response") or "").strip()
    logger.info("ollama ocr result filename=%s description=%.100s", filename, description)
    if not description:
        return ""
    return f"{description}"


async def _process_image_job(job: JobRecord) -> None:
    payload: dict = {}
    try:
        payload = json.loads(job.payload_json) if job.payload_json else {}
    except json.JSONDecodeError:
        payload = {}

    record = await db.fetch_image_for_user(job.image_id, job.user_id)
    if not record:
        raise RuntimeError("image not found")

    image_bytes: Optional[bytes] = record.image_data
    if image_bytes is None:
        image_bytes = await asyncio.to_thread(
            _fetch_bucket_image_bytes,
            job.user_id,
            job.image_id,
            record.filename,
        )
    if not image_bytes:
        raise RuntimeError("image bytes unavailable")

    faces_json: Optional[str] = None
    ocr_text: Optional[str] = None
    ai_description: Optional[str] = None
    taken_at: Optional[str] = None
    thumbnail_data: Optional[bytes] = None

    if payload.get("do_ocr") is True:
        try:
            extracted = await asyncio.to_thread(extract_upload_metadata, image_bytes)
            ocr_text = extracted.ocr_text
            if record.taken_at is None and extracted.taken_at is not None:
                taken_at = extracted.taken_at
        except Exception:
            logger.warning(
                "job metadata extraction skipped user_id=%s image_id=%s",
                job.user_id,
                job.image_id,
                exc_info=True,
            )

    if payload.get("do_ai") is True:
        try:
            ai_description = await asyncio.to_thread(
                _generate_image_description_with_ollama,
                image_bytes,
                filename=record.filename,
            )
        except Exception:
            logger.warning(
                "job caption generation skipped user_id=%s image_id=%s",
                job.user_id,
                job.image_id,
                exc_info=True,
            )

    if payload.get("do_faces") is True:
        try:
            faces = await detect_and_tag_faces_for_user(job.user_id, image_bytes, db)
            faces_json = json.dumps(faces)
        except Exception:
            logger.warning(
                "job face detection/tagging skipped user_id=%s image_id=%s",
                job.user_id,
                job.image_id,
                exc_info=True,
            )

    if payload.get("do_thumb") is True:
        try:
            thumbnail_data = await asyncio.to_thread(_generate_thumbnail_webp, image_bytes)
        except Exception:
            logger.warning(
                "job thumbnail generation skipped user_id=%s image_id=%s",
                job.user_id,
                job.image_id,
                exc_info=True,
            )

    if thumbnail_data:
        try:
            await db.update_image_thumbnail(
                job.image_id,
                job.user_id,
                thumbnail_data,
                "image/webp",
            )
        except Exception:
            logger.warning(
                "job thumbnail update skipped user_id=%s image_id=%s",
                job.user_id,
                job.image_id,
                exc_info=True,
            )

    await db.update_image_processing_fields(
        image_id=job.image_id,
        user_id=job.user_id,
        faces_json=faces_json,
        ocr_text=ocr_text,
        ai_description=ai_description,
        taken_at=taken_at,
    )


async def _job_worker_loop() -> None:
    logger.info("job worker enabled kind=%s", IMAGE_PROCESS_JOB_KIND)
    while True:
        job = await db.claim_next_job(kind=IMAGE_PROCESS_JOB_KIND)
        if not job:
            await asyncio.sleep(0.5)
            continue
        try:
            await _process_image_job(job)
            await db.complete_job(job.id)
        except Exception as exc:
            logger.exception("job failed id=%s kind=%s", job.id, job.kind)
            await db.fail_job(job.id, str(exc))


async def _retention_worker_loop() -> None:
    logger.info("retention worker enabled")
    while True:
        try:
            await _run_retention_cleanup_once()
        except Exception:
            logger.exception("retention cleanup failed")
        # Run periodically; keep lightweight.
        await asyncio.sleep(6 * 60 * 60)


async def _run_retention_cleanup_once() -> None:
    users = await db.list_users_with_retention()
    if not users:
        return
    now = _utc_now()
    for user_id, retention_days in users:
        cutoff = (now - timedelta(days=int(retention_days))).isoformat()
        while True:
            refs = await db.list_image_file_refs_older_than(
                user_id=user_id,
                cutoff_iso=cutoff,
                limit=200,
            )
            if not refs:
                break
            image_ids = [image_id for image_id, _ in refs]
            deleted = await db.delete_images_by_ids(user_id=user_id, image_ids=image_ids)
            if bucket is not None:
                for image_id, filename in refs:
                    try:
                        file_key = _photo_file_key(user_id, image_id, filename)
                        file_version = await asyncio.to_thread(bucket.get_file_info_by_name, file_key)
                        await asyncio.to_thread(
                            bucket.delete_file_version,
                            file_version.id_,
                            file_version.file_name,
                        )
                    except Exception:
                        logger.info(
                            "retention storage cleanup skipped user_id=%s image_id=%s",
                            user_id,
                            image_id,
                        )
            logger.info(
                "retention cleanup user_id=%s deleted=%s cutoff=%s",
                user_id,
                deleted,
                cutoff,
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
    """Placeholder script endpoint (legacy client hook)."""
    content = "console.info('UserProfile.js placeholder loaded.');"
    return Response(
        status_code=200,
        headers={"content-type": "application/javascript; charset=utf-8"},
        description=content,
    )


@app.get("/favicon.ico")
async def favicon(_: Request) -> Response:
    """Serve the SVG favicon through a conventional .ico route."""
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
    raw_limit = request.query_params.get("limit", None)
    raw_offset = request.query_params.get("offset", None)
    limit: Optional[int] = None
    offset = 0
    if sort_by not in {"uploaded", "taken"}:
        return _json_response(
            {"error": "sort_by must be uploaded or taken"}, status=400
        )
    if order not in {"asc", "desc"}:
        return _json_response({"error": "order must be asc or desc"}, status=400)
    if raw_limit is not None:
        raw_limit_str = str(raw_limit).strip()
        if not raw_limit_str.isdigit():
            return _json_response({"error": "limit must be an integer"}, status=400)
        limit = int(raw_limit_str)
        if limit < 1 or limit > 500:
            return _json_response({"error": "limit must be 1..500"}, status=400)
    if raw_offset is not None:
        raw_offset_str = str(raw_offset).strip()
        if not raw_offset_str.isdigit():
            return _json_response({"error": "offset must be an integer"}, status=400)
        offset = int(raw_offset_str)
    images = await db.list_images_for_user(
        user.id, sort_by=sort_by, order=order, limit=limit, offset=offset
    )
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
                    "faces": public_faces_payload(record.faces_json),
                }
                for record in images
            ],
        }
    )

@app.get("/search")
async def search_page(request: Request) -> Response:
    """AI search page for photos."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth

    context = auth
    user = context.user
    csrf_token, set_csrf = _get_or_create_csrf_token(request)

    response = jinja_template.render_template(
        "search/Search.html",  # 👈 make sure this file exists
        request=request,
        title="Search",
        user=user,
    )

    _apply_common_cookies(
        response,
        clear_session=context.clear_cookie,
        csrf_token=csrf_token,
        set_csrf=set_csrf,
    )

    return response


def _payload_optional_bool(payload: dict, key: str) -> Optional[bool]:
    if key not in payload:
        return None
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean")


@app.get("/api/settings/privacy")
async def privacy_settings_get(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    settings = await db.ensure_user_settings(auth.user.id)
    return _json_response(
        {
            "ai_descriptions_enabled": bool(settings.ai_descriptions_enabled),
            "ocr_enabled": bool(settings.ocr_enabled),
            "face_recognition_enabled": bool(settings.face_recognition_enabled),
            "store_originals_enabled": bool(settings.store_originals_enabled),
            "retention_days": settings.retention_days,
        }
    )


@app.put("/api/settings/privacy")
async def privacy_settings_put(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    payload = _json_data(request)
    try:
        ai_enabled = _payload_optional_bool(payload, "ai_descriptions_enabled")
        ocr_enabled = _payload_optional_bool(payload, "ocr_enabled")
        faces_enabled = _payload_optional_bool(payload, "face_recognition_enabled")
        originals_enabled = _payload_optional_bool(payload, "store_originals_enabled")
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)
    retention_days = payload.get("retention_days", None)
    if retention_days is not None:
        if retention_days == "":
            retention_days = None
        if retention_days is not None:
            if isinstance(retention_days, str) and retention_days.strip().isdigit():
                retention_days = int(retention_days.strip())
            if not isinstance(retention_days, int) or retention_days < 1:
                return _json_response(
                    {"error": "retention_days must be a positive integer"},
                    status=400,
                )
    settings = await db.update_user_settings(
        auth.user.id,
        ai_descriptions_enabled=ai_enabled,
        ocr_enabled=ocr_enabled,
        face_recognition_enabled=faces_enabled,
        store_originals_enabled=originals_enabled,
        retention_days=retention_days,
    )
    return _json_response(
        {
            "ai_descriptions_enabled": bool(settings.ai_descriptions_enabled),
            "ocr_enabled": bool(settings.ocr_enabled),
            "face_recognition_enabled": bool(settings.face_recognition_enabled),
            "store_originals_enabled": bool(settings.store_originals_enabled),
            "retention_days": settings.retention_days,
        }
    )


async def _handle_photo_upload(
    *,
    request: Request,
    auth: AuthContext,
    filename: str,
    image_bytes: bytes,
    content_type: str,
    taken_at: Optional[str],
) -> Response:
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

    user_settings = await db.ensure_user_settings(auth.user.id)
    do_ocr = bool(user_settings.ocr_enabled)
    do_ai = bool(user_settings.ai_descriptions_enabled)
    do_faces = bool(user_settings.face_recognition_enabled)
    store_originals = bool(user_settings.store_originals_enabled)

    ocr_text = ""
    make = model = shutter = loc_desc = city = state = country = None
    iso = None
    f_stop = focal = lat = lon = None
    if do_ocr and not ASYNC_PROCESSING:
        try:
            extracted = await asyncio.to_thread(extract_upload_metadata, image_bytes)
            if taken_at is None:
                taken_at = extracted.taken_at
            ocr_text = extracted.ocr_text
            make = extracted.make
            model = extracted.model
            iso = extracted.iso
            f_stop = extracted.f_stop
            shutter = extracted.shutter
            focal = extracted.focal
            lat = extracted.lat
            lon = extracted.lon
            loc_desc = extracted.loc_desc
            city = extracted.loc_city
            state = extracted.loc_state
            country = extracted.loc_country
        except Exception:
            logger.warning(
                "upload metadata extraction skipped user_id=%s filename=%s",
                auth.user.id,
                filename,
                exc_info=True,
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

    description = ""
    if do_ai and not ASYNC_PROCESSING:
        try:
            description = await asyncio.to_thread(
                _generate_image_description_with_ollama,
                image_bytes,
                filename=filename,
            )
        except Exception:
            logger.warning(
                "upload caption generation skipped user_id=%s filename=%s",
                auth.user.id,
                filename,
                exc_info=True,
            )

    faces_json = "[]"
    if do_faces and not ASYNC_PROCESSING:
        try:
            faces = await detect_and_tag_faces_for_user(auth.user.id, image_bytes, db)
            faces_json = json.dumps(faces)
        except Exception:
            logger.warning(
                "upload face detection/tagging skipped user_id=%s filename=%s",
                auth.user.id,
                filename,
                exc_info=True,
            )

    try:
        # Insert metadata first to get photo_id.
        # Keep blob data only when B2 is unavailable.
        image_blob = image_bytes if (bucket is None and store_originals) else None
        saved = await db.create_image_metadata(
            filename=filename,
            faces_json=faces_json,
            ocr_text=ocr_text,
            user_id=auth.user.id,
            ai_description=description,
            content_type=content_type,
            image_data=image_blob,
            thumbnail_data=thumbnail_data,
            thumbnail_content_type="image/webp",
            taken_at=taken_at,
            make=make,
            model=model,
            iso=iso,
            f_stop=f_stop,
            shutter=shutter,
            focal=focal,
            lat=lat,
            lon=lon,
            loc_desc=loc_desc,
            city=city,
            state=state,
            country=country,
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
        if bucket is not None and store_originals:
            ext = pathlib.Path(filename).suffix.lower()
            b2_key = f"{auth.user.id}/{photo_id}{ext}"
            await asyncio.to_thread(
                bucket.upload_bytes,
                image_bytes,
                b2_key,
                content_type=content_type,
            )
        if ASYNC_PROCESSING and (do_ocr or do_ai or do_faces):
            await db.enqueue_job(
                user_id=auth.user.id,
                image_id=photo_id,
                kind=IMAGE_PROCESS_JOB_KIND,
                payload_json=json.dumps(
                    {"do_ocr": do_ocr, "do_ai": do_ai, "do_faces": do_faces},
                    separators=(",", ":"),
                ),
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
    return _json_response(
        {
            "id": saved.id,
            "filename": saved.filename,
            "created_at": saved.created_at,
            "taken_at": saved.taken_at,
            "description": saved.ai_description,
            "faces": public_faces_payload(saved.faces_json),
            "processing": ASYNC_PROCESSING and (do_ocr or do_ai or do_faces),
            "stored_original": store_originals,
        },
        status=201,
    )


@app.post("/api/photos/raw")
async def upload_photo_raw_api(request: Request) -> Response:
    """Upload photo bytes directly (no base64) via request body."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    if not _allow_rate_limited_request(_upload_limiter, request):
        return _json_response({"error": "too many uploads, try again later"}, status=429)

    raw_filename = str(request.query_params.get("filename", "")).strip()
    filename = os.path.basename(raw_filename)
    filename = re.sub(r"[^\w.\-() ]", "_", filename).strip()
    if not filename:
        filename = "upload"
    taken_at = _parse_taken_at(request.query_params.get("taken_at"))
    content_type = _normalize_content_type(
        str(request.headers.get("content-type", "")),
        filename,
    )
    image_bytes = _raw_body_bytes(request)
    return await _handle_photo_upload(
        request=request,
        auth=auth,
        filename=filename,
        image_bytes=image_bytes,
        content_type=content_type,
        taken_at=taken_at,
    )


def _b2_get_upload_url() -> tuple[str, str]:
    if bucket is None:
        raise RuntimeError("Storage unavailable")
    api = bucket.api
    api_url = api.account_info.get_api_url()
    account_auth_token = api.account_info.get_account_auth_token()
    result = api.session.raw_api.get_upload_url(api_url, account_auth_token, bucket.id_)
    return str(result["uploadUrl"]), str(result["authorizationToken"])


@app.post("/api/uploads/b2/init")
async def b2_upload_init_api(request: Request) -> Response:
    """Initialize a browser-direct upload to Backblaze B2 (requires CORS on bucket)."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    if not _allow_rate_limited_request(_upload_limiter, request):
        return _json_response({"error": "too many uploads, try again later"}, status=429)
    if not DIRECT_B2_UPLOAD:
        return _json_response({"error": "direct uploads are disabled"}, status=403)
    if bucket is None:
        return _json_response({"error": "file storage unavailable"}, status=503)

    user_settings = await db.ensure_user_settings(auth.user.id)
    if not bool(user_settings.store_originals_enabled):
        return _json_response({"error": "store originals is disabled in privacy settings"}, status=400)

    payload = _json_data(request)
    raw_filename = str(payload.get("filename", "")).strip()
    filename = os.path.basename(raw_filename)
    filename = re.sub(r"[^\w.\-() ]", "_", filename).strip()
    if not filename:
        filename = "upload"
    taken_at = _parse_taken_at(payload.get("taken_at"))
    content_type = _normalize_content_type(str(payload.get("content_type", "")), filename)

    saved = await db.create_image_metadata(
        filename=filename,
        faces_json="[]",
        ocr_text="",
        user_id=auth.user.id,
        ai_description="",
        content_type=content_type,
        image_data=None,
        thumbnail_data=None,
        thumbnail_content_type="image/webp",
        taken_at=taken_at,
    )
    file_key = _photo_file_key(auth.user.id, saved.id, saved.filename)
    try:
        upload_url, upload_token = await asyncio.to_thread(_b2_get_upload_url)
    except Exception:
        logger.exception("b2 upload init failed user_id=%s photo_id=%s", auth.user.id, saved.id)
        await db.delete_image_for_user(saved.id, auth.user.id)
        return _json_response({"error": "could not initialize upload"}, status=500)
    return _json_response(
        {
            "photo_id": saved.id,
            "file_key": file_key,
            "upload_url": upload_url,
            "authorization_token": upload_token,
            "content_type": content_type,
        },
        status=201,
    )


@app.post("/api/uploads/b2/complete")
async def b2_upload_complete_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    if not DIRECT_B2_UPLOAD:
        return _json_response({"error": "direct uploads are disabled"}, status=403)
    payload = _json_data(request)
    photo_id_raw = str(payload.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(photo_id_raw)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)

    user_settings = await db.ensure_user_settings(auth.user.id)
    do_ocr = bool(user_settings.ocr_enabled)
    do_ai = bool(user_settings.ai_descriptions_enabled)
    do_faces = bool(user_settings.face_recognition_enabled)
    await db.enqueue_job(
        user_id=auth.user.id,
        image_id=photo_id,
        kind=IMAGE_PROCESS_JOB_KIND,
        payload_json=json.dumps(
            {"do_ocr": do_ocr, "do_ai": do_ai, "do_faces": do_faces, "do_thumb": True},
            separators=(",", ":"),
        ),
    )
    return _json_response({"status": "queued", "photo_id": photo_id})


@app.post("/api/photos")
async def upload_photo_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    if not _allow_rate_limited_request(_upload_limiter, request):
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
    return await _handle_photo_upload(
        request=request,
        auth=auth,
        filename=filename,
        image_bytes=image_bytes,
        content_type=content_type,
        taken_at=taken_at,
    )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_expires_in_seconds(payload: dict) -> Optional[int]:
    raw = payload.get("expires_in_seconds", None)
    if raw is None or raw == "":
        return None
    if isinstance(raw, int):
        seconds = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        seconds = int(raw.strip())
    else:
        raise ValueError("expires_in_seconds must be an integer")
    if seconds < 60 or seconds > 60 * 60 * 24 * 30:
        raise ValueError("expires_in_seconds must be 60..2592000 (30 days)")
    return seconds


@app.post("/api/photos/share")
async def create_share_link_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    payload = _json_data(request)
    photo_id_raw = str(payload.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(photo_id_raw)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    try:
        expires_in = _parse_expires_in_seconds(payload)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)
    expires_at = None
    if expires_in is None:
        expires_at = (_utc_now() + timedelta(days=1)).isoformat()
    else:
        expires_at = (_utc_now() + timedelta(seconds=expires_in)).isoformat()
    token = secrets.token_urlsafe(32)
    token_hash = _sha256_hex(token)
    token_prefix = token[:8]
    try:
        share = await db.create_photo_share(
            image_id=photo_id,
            token_hash=token_hash,
            token_prefix=token_prefix,
            expires_at=expires_at,
        )
    except Exception:
        logger.exception("share create failed user_id=%s photo_id=%s", auth.user.id, photo_id)
        return _json_response({"error": "could not create share link"}, status=500)
    return _json_response(
        {
            "photo_id": photo_id,
            "token_prefix": share.token_prefix,
            "expires_at": share.expires_at,
            "share_url": f"/s?token={token}",
        },
        status=201,
    )


@app.post("/api/photos/recaption")
async def recaption_photo_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)

    body = _json_data(request)
    photo_id = body.get("photo_id")
    if not isinstance(photo_id, int):
        return _json_response({"error": "photo_id (int) required"}, status=400)

    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)

    image_bytes = record.image_data
    if not image_bytes:
        image_bytes = await asyncio.to_thread(
            _fetch_bucket_image_bytes, auth.user.id, photo_id, record.filename
        )
    if not image_bytes:
        return _json_response({"error": "image data unavailable"}, status=404)

    await _process_image_metadata(
        image_id=photo_id,
        user_id=auth.user.id,
        filename=record.filename,
        image_bytes=image_bytes,
    )

    meta = await db.fetch_image_metadata_for_user(photo_id, auth.user.id)
    if not meta:
        return _json_response({"error": "metadata generation failed"}, status=500)

    return _json_response({"ok": True, "metadata": _metadata_response_dict(meta)})


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
    if record:
        return _json_response(_metadata_response_dict(record))

    # Fallback: build metadata from the images table + image dimensions
    img_record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not img_record:
        return _json_response({"error": "photo not found"}, status=404)

    faces = public_faces_payload(img_record.faces_json)
    width, height = None, None
    try:
        image_bytes = img_record.image_data
        if not image_bytes and bucket is not None:
            image_bytes = await asyncio.to_thread(
                _fetch_bucket_image_bytes, auth.user.id, photo_id, img_record.filename
            )
        if image_bytes and Image is not None:
            with Image.open(io.BytesIO(image_bytes)) as pil_img:
                width, height = pil_img.size
    except Exception:
        pass

    return _json_response({
        "image_id": photo_id,
        "faces": faces,
        "ocr_text": img_record.ocr_text or "",
        "caption": img_record.ai_description or "",
        "width": width,
        "height": height,
        "file_size_mb": round(len(img_record.image_data) / (1024 * 1024), 2) if img_record.image_data else None,
        "taken_at": img_record.taken_at,
        "created_at": img_record.created_at,
        "lat": None, "lon": None,
        "loc_description": None, "loc_city": None, "loc_state": None, "loc_country": None,
        "make": None, "model": None, "iso": None, "f_stop": None,
        "shutter_speed": None, "focal_length": None,
        "updated_at": None,
    })


@app.get("/api/photos/shares")
async def list_share_links_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    photo_id_raw = str(request.query_params.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(photo_id_raw)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    shares = await db.list_photo_shares_for_image(photo_id)
    return _json_response(
        {
            "photo_id": photo_id,
            "shares": [
                {
                    "token_prefix": s.token_prefix,
                    "created_at": s.created_at,
                    "expires_at": s.expires_at,
                    "revoked_at": s.revoked_at,
                }
                for s in shares
            ],
        }
    )


@app.delete("/api/photos/share")
async def revoke_share_links_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    payload = _json_data(request)
    photo_id_raw = str(payload.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(photo_id_raw)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    token_prefix = payload.get("token_prefix", None)
    if token_prefix is not None:
        token_prefix = str(token_prefix).strip()
        if not token_prefix:
            token_prefix = None
    changed = await db.revoke_photo_shares(image_id=photo_id, token_prefix=token_prefix)
    return _json_response({"photo_id": photo_id, "revoked": changed})


@app.get("/s")
async def shared_photo_view(request: Request) -> Response:
    """Public view route for share links: /s?token=..."""
    token = str(request.query_params.get("token", "")).strip()
    if not token:
        return Response(status_code=400, headers={}, description="Missing token")
    share = await db.fetch_photo_share_by_token_hash(_sha256_hex(token))
    if not share:
        return Response(status_code=404, headers={}, description="Not found")
    if share.revoked_at is not None:
        return Response(status_code=404, headers={}, description="Not found")
    if share.expires_at and _as_utc(datetime.fromisoformat(share.expires_at)) <= _utc_now():
        return Response(status_code=404, headers={}, description="Not found")
    record = await db.fetch_image_by_id(share.image_id)
    if not record:
        return Response(status_code=404, headers={}, description="Not found")
    if record.user_id is None and not record.image_data and not record.thumbnail_data:
        return Response(status_code=404, headers={}, description="Not found")
    owner_id = int(record.user_id) if record.user_id is not None else 0
    return _photo_view_response(owner_id, record.id, record, allow_thumbnail_fallback=True)


@app.post("/api/photos/acl/grant")
async def grant_photo_acl_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    payload = _json_data(request)
    photo_id_raw = str(payload.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(photo_id_raw)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    email = str(payload.get("email", "")).strip().lower()
    if not email:
        return _json_response({"error": "email is required"}, status=400)
    user = await db.fetch_user_by_email(email)
    if not user:
        return _json_response({"error": "user not found"}, status=404)
    if user.id == auth.user.id:
        return _json_response({"error": "cannot share with yourself"}, status=400)
    await db.grant_photo_acl(image_id=photo_id, grantee_user_id=user.id)
    acl = await db.list_photo_acl_with_users(image_id=photo_id)
    return _json_response({"photo_id": photo_id, "acl": acl})


@app.delete("/api/photos/acl/revoke")
async def revoke_photo_acl_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    if not _verify_api_csrf(request):
        return _json_response({"error": "CSRF validation failed"}, status=403)
    payload = _json_data(request)
    photo_id_raw = str(payload.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(photo_id_raw)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    grantee_raw = str(payload.get("grantee_user_id", "")).strip()
    if not grantee_raw.isdigit():
        return _json_response({"error": "grantee_user_id must be an integer"}, status=400)
    grantee_user_id = int(grantee_raw)
    changed = await db.revoke_photo_acl(image_id=photo_id, grantee_user_id=grantee_user_id)
    acl = await db.list_photo_acl_with_users(image_id=photo_id)
    return _json_response({"photo_id": photo_id, "revoked": changed, "acl": acl})


@app.get("/api/photos/acl")
async def list_photo_acl_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    photo_id_raw = str(request.query_params.get("photo_id", "")).strip()
    if not photo_id_raw.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(photo_id_raw)
    record = await db.fetch_image_for_user(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    acl = await db.list_photo_acl_with_users(image_id=photo_id)
    return _json_response({"photo_id": photo_id, "acl": acl})


@app.get("/api/photos/shared-with-me")
async def shared_with_me_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    raw_limit = str(request.query_params.get("limit", "100")).strip()
    raw_offset = str(request.query_params.get("offset", "0")).strip()
    if not raw_limit.isdigit() or not raw_offset.isdigit():
        return _json_response({"error": "limit/offset must be integers"}, status=400)
    limit = min(500, max(1, int(raw_limit)))
    offset = max(0, int(raw_offset))
    images = await db.list_shared_images_for_user(auth.user.id, limit=limit, offset=offset)
    return _json_response(
        {
            "photos": [
                {
                    "id": record.id,
                    "owner_user_id": record.user_id,
                    "filename": record.filename,
                    "created_at": record.created_at,
                    "taken_at": record.taken_at,
                    "description": record.ai_description,
                }
                for record in images
            ]
        }
    )


@app.get("/api/photos/info")
async def photo_info_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    raw_photo_id = str(request.query_params.get("photo_id", "")).strip()
    if not raw_photo_id.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(raw_photo_id)
    record = await db.fetch_image_for_access(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    return _json_response(
        {
            "id": record.id,
            "filename": record.filename,
            "created_at": record.created_at,
            "taken_at": record.taken_at,
            "description": record.ai_description,
            "ocr_text": record.ocr_text,
            "faces": public_faces_payload(record.faces_json),
            "thumbnail_present": bool(record.thumbnail_data),
            "original_present": bool(record.image_data) or (bucket is not None and record.user_id is not None),
        }
    )


@app.get("/api/photos/status")
async def photo_status_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)
    raw_photo_id = str(request.query_params.get("photo_id", "")).strip()
    if not raw_photo_id.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)
    photo_id = int(raw_photo_id)
    record = await db.fetch_image_for_access(photo_id, auth.user.id)
    if not record:
        return _json_response({"error": "photo not found"}, status=404)
    job = await db.fetch_latest_job_for_image(photo_id, kind=IMAGE_PROCESS_JOB_KIND)
    if not job:
        return _json_response({"photo_id": photo_id, "status": "none"})
    return _json_response(
        {
            "photo_id": photo_id,
            "status": job.status,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "error": job.error,
        }
    )


@app.get("/api/photos/search")
async def search_photos_api(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)

    query = str(request.query_params.get("q", "")).strip().lower()

    if not query:
        return _json_response({"photos": []})

    images = await db.list_images_for_user(auth.user.id)

    results = []
    for record in images:
        searchable_text = " ".join([
            record.filename or "",
            record.ai_description or "",
            record.ocr_text or ""
        ]).lower()

        if query in searchable_text:
            results.append({
                "id": record.id,
                "filename": record.filename,
                "description": record.ai_description,
                "ocr_text": record.ocr_text,
                "faces": public_faces_payload(record.faces_json),
                "created_at": record.created_at,
                "taken_at": record.taken_at,
            })

    return _json_response({"photos": results})


@app.get("/api/photos/download")
async def download_photo_api(request: Request) -> Response:
    """Return base64 or signed URL for a stored photo."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return _json_response({"error": "authentication required"}, status=401)

    raw_photo_id = str(request.query_params.get("photo_id", "")).strip()
    if not raw_photo_id.isdigit():
        return _json_response({"error": "photo_id must be an integer"}, status=400)

    photo_id = int(raw_photo_id)

    record = await db.fetch_image_for_access(photo_id, auth.user.id)
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
    owner_id = int(record.user_id) if record.user_id is not None else auth.user.id
    file_key = _photo_file_key(owner_id, photo_id, record.filename)
    signed_url = _get_cached_signed_url(file_key, valid_duration_in_seconds=300)
    return _json_response(
        {
            "url": signed_url,
            "filename": record.filename,
            "content_type": record.content_type,
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
    record = await db.fetch_image_for_access(photo_id, auth.user.id)
    if not record:
        return Response(status_code=404, headers={}, description="Not found")

    owner_id = int(record.user_id) if record.user_id is not None else auth.user.id
    return _photo_view_response(owner_id, photo_id, record)

@app.get("/api/photos/thumb")
async def view_photo_thumbnail(request: Request) -> Response:
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth

    raw_photo_id = request.query_params.get("photo_id", None)
    if not raw_photo_id or not raw_photo_id.isdigit():
        return Response(status_code=400, headers={}, description="Invalid photo_id")

    photo_id = int(raw_photo_id)
    record = await db.fetch_image_for_access(photo_id, auth.user.id)
    if not record:
        return Response(status_code=404, headers={}, description="Not found")

    if record.thumbnail_data:
        return Response(
            status_code=200,
            headers={
                "content-type": record.thumbnail_content_type or "image/webp",
                "cache-control": "private, max-age=86400",
            },
            description=record.thumbnail_data,
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
    owner_id = int(record.user_id) if record.user_id is not None else auth.user.id
    return _photo_view_response(owner_id, photo_id, record)


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
        return _json_response({"error": "photo not found"}, status=404)
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
    if bucket is not None and record.user_id is not None:
        try:
            file_key = _photo_file_key(auth.user.id, photo_id, record.filename)
            file_version = await asyncio.to_thread(bucket.get_file_info_by_name, file_key)
            await asyncio.to_thread(
                bucket.delete_file_version,
                file_version.id_,
                file_version.file_name,
            )
        except Exception:
            logger.warning(
                "delete storage cleanup failed user_id=%s photo_id=%s",
                auth.user.id,
                photo_id,
                exc_info=True,
            )
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
    if not _allow_rate_limited_request(_login_limiter, request):
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
    if not _allow_rate_limited_request(_register_limiter, request):
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
