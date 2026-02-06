from typing import Iterable, Optional
from urllib.parse import urlencode

from markupsafe import escape
from robyn import Request, Response, Robyn

from auth import (
    SESSION_COOKIE_NAME,
    SessionManager,
    cookie_clear_settings,
    cookie_settings,
    hash_password,
    verify_password,
)
from database import Database, UserRecord
import aiosqlite

# Author: Daniel Neugent

app = Robyn(__file__)

# Singletons used by every request
db = Database()
sessions = SessionManager()


async def _ensure_database() -> None:
    """Prepare the sqlite file before handling the first request."""
    await db.initialize()


app.startup_handler(_ensure_database)


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


def _html_response(body: str, *, status: int = 200) -> Response:
    """Wrap an HTML body inside a minimal Robyn response."""
    return Response(
        status_code=status,
        headers={"content-type": "text/html; charset=utf-8"},
        description=body,
    )


def _build_nav(user: Optional[UserRecord]) -> str:
    """Render the shared navigation bar shown at the top of every page."""
    links = ['<a href="/">Home</a>', '<a href="/public">Public</a>']
    if user:
        links.extend(
            [
                '<a href="/dashboard">Dashboard</a>',
                '<a href="/profile">Profile</a>',
                '<a href="/logout">Log out</a>',
                f'<span class="status">Signed in as {escape(user.username)}</span>',
            ]
        )
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
    messages: Iterable[str] | None = None,
    message_kind: str = "info",
) -> str:
    """Wrap any page body inside a styled template with optional flash messages."""
    nav = _build_nav(user)
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
    nav {{ margin-bottom: 1rem; }}
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


async def _current_user(request: Request) -> Optional[UserRecord]:
    """Resolve the current user from the session cookie (if present)."""
    token = _get_cookie_value(request, SESSION_COOKIE_NAME)
    user_id = sessions.decode(token or "")
    if not user_id:
        return None
    return await db.fetch_user_by_id(user_id)


def _redirect(location: str) -> Response:
    """Send a 303 redirect to the user agent."""
    return Response(
        status_code=303,
        headers={"location": location},
        description="",
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


async def _ensure_authenticated(request: Request) -> Response | UserRecord:
    """Return the authenticated user or issue a login redirect if missing."""
    user = await _current_user(request)
    if user:
        return user
    target = _normalize_redirect_path(request.url.path)
    return _redirect_with_next("/login", query=urlencode({"next": target}))


def _login_form(next_path: str, *, messages: Iterable[str] | None = None) -> str:
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
        <button type=\"submit\">Sign in</button>
      </form>
    </section>
    """


def _register_form(
    values: dict[str, str] | None = None, *, messages: Iterable[str] | None = None
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
        <button type=\"submit\">Create account</button>
      </form>
    </section>
    """


@app.get("/")
async def home(request: Request) -> Response:
    """Landing page that always renders regardless of authentication state."""
    user = await _current_user(request)
    body = """
    <section>
      <p>TagLens keeps every sensitive credential hashed and salted before hitting the database.</p>
      <p>Navigate using the links above; authenticated areas require a session cookie.</p>
    </section>
    """
    return _html_response(
        _page_template(title="TagLens", body=body, user=user),
    )


@app.get("/public")
async def public_page(request: Request) -> Response:
    """Informational route accessible to unauthenticated visitors."""
    user = await _current_user(request)
    body = """
    <section>
      <h2>Public area</h2>
      <p>Anyone can reach this without signing in.</p>
    </section>
    """
    return _html_response(_page_template(title="Public", body=body, user=user))


@app.get("/dashboard")
async def dashboard(request: Request) -> Response:
    """Private dashboard that requires a valid session cookie."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth
    user = auth
    body = f"""
    <section>
      <h2>Dashboard</h2>
      <p>Welcome back, {escape(user.username)}.</p>
      <p>Your account was created on {escape(user.created_at)} UTC.</p>
    </section>
    """
    return _html_response(_page_template(title="Dashboard", body=body, user=user))


@app.get("/profile")
async def profile(request: Request) -> Response:
    """Show account metadata for the signed-in user."""
    auth = await _ensure_authenticated(request)
    if isinstance(auth, Response):
        return auth
    user = auth
    body = f"""
    <section>
      <h2>Account details</h2>
      <dl>
        <dt>Username</dt><dd>{escape(user.username)}</dd>
        <dt>Email</dt><dd>{escape(user.email)}</dd>
        <dt>Member since</dt><dd>{escape(user.created_at)} UTC</dd>
      </dl>
    </section>
    """
    return _html_response(_page_template(title="Profile", body=body, user=user))


@app.get("/login")
async def login_get(request: Request) -> Response:
    """Render the login form and show flash messages if redirected from registration."""
    user = await _current_user(request)
    if user:
        return _redirect("/dashboard")
    next_path = _normalize_redirect_path(request.query_params.get("next"))
    messages: list[str] = []
    if request.query_params.get("registered") == "1":
        messages.append("Account created. Please sign in.")
    return _html_response(
        _page_template(
            title="Sign in",
            body=_login_form(next_path, messages=messages),
            messages=None,
        )
    )


@app.post("/login")
async def login_post(request: Request) -> Response:
    """Validate credentials and issue a session token when authentication succeeds."""
    form = request.form_data or {}
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    next_path = _normalize_redirect_path(form.get("next"))
    errors = []
    if not email:
        errors.append("Email is required.")
    if not password:
        errors.append("Password is required.")
    if errors:
        return _html_response(
            _page_template(
                title="Sign in",
                body=_login_form(next_path, messages=errors),
                messages=None,
            )
        )
    user = await db.fetch_user_by_email(email)
    if not user or not verify_password(password, user.password_hash):
        return _html_response(
            _page_template(
                title="Sign in",
                body=_login_form(next_path, messages=["Invalid credentials."]),
                messages=None,
            )
        )
    # The session manager always produces a signed token tied to the user id.
    session = sessions.create_token(user.id)
    redirect_destination = next_path if next_path.strip() else "/dashboard"
    response = _redirect(redirect_destination)
    # Store the session in a httponly cookie so browsers send it automatically.
    response.set_cookie(SESSION_COOKIE_NAME, session.token, **cookie_settings())
    return response


@app.get("/register")
async def register_get(request: Request) -> Response:
    """Show the account creation form unless the user is already signed in."""
    user = await _current_user(request)
    if user:
        return _redirect("/dashboard")
    return _html_response(
        _page_template(title="Register", body=_register_form()),
    )


@app.post("/register")
async def register_post(request: Request) -> Response:
    """Process registration data, enforce validation, and persist new users."""
    form = request.form_data or {}
    username = (form.get("username") or "").strip()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    confirm = form.get("confirm_password") or ""
    # Keep track of every validation failure so we can redisplay them at once.
    errors = []
    if len(username) < 3:
        errors.append("Pick a username that is at least 3 characters.")
    if not email:
        errors.append("Email is required.")
    if len(password) < 8:
        errors.append("Passwords must be at least 8 characters.")
    if password != confirm:
        errors.append("Password confirmation does not match.")
    if errors:
        return _html_response(
            _page_template(
                title="Register",
                body=_register_form(
                    {"username": username, "email": email}, messages=errors
                ),
            )
        )
    try:
        password_hash = hash_password(password)
        await db.create_user(username, email, password_hash)
    except aiosqlite.IntegrityError:
        # The DB enforces uniqueness so a duplicate inserts will raise here.
        errors.append("That username or email is already registered.")
        return _html_response(
            _page_template(
                title="Register",
                body=_register_form(
                    {"username": username, "email": email}, messages=errors
                ),
            )
        )
    return _redirect_with_next("/login", query=urlencode({"registered": "1"}))


@app.get("/logout")
async def logout(request: Request) -> Response:
    """Clear the user's session cookie and return to the public landing page."""
    response = _redirect("/")
    response.set_cookie(SESSION_COOKIE_NAME, "", **cookie_clear_settings())
    return response


if __name__ == "__main__":
    app.start(_check_port=False)
