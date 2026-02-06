# TagLens Authentication Service

This project is a small Robyn/uvicorn application that demonstrates a secure username/password flow.

**Author: Daniel Neugent**

## Features
- `robyn` + `uvicorn` (the `uv` server you requested) runs the asynchronous ASGI app.
- SQLite stores user records with hashed-and-salted passwords using `passlib[bcrypt]`.
- Auth-protected routes (`/dashboard`, `/profile`) redirect to `/login` when there is no session cookie.
- Public routes (`/`, `/public`, `/register`, `/login`) remain open to everyone.
- Session cookies are signed via `itsdangerous` and default to `httponly`, `samesite=lax`, and configurable `secure` mode.
- No SQL is interpolated directly; every database query is parameterized through `aiosqlite`.
- HTML rendering escapes user supplied values with `markupsafe`.

## Setup
1. (Optional but recommended) create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
2. Install the dependencies:
   ```bash
   uv install -r requirements.txt
   ```
3. Export a fixed signing secret before starting the server. Without it, the app will generate a temporary secret and warn you on the console:
   ```bash
   export ROBYN_SECRET_KEY="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY")"
   ```
   Optionally set `ROBYN_SECURE_COOKIES=1` if you deploy behind HTTPS and want secure cookies.
4. Run the app with uvicorn:
   ```bash
   uvicorn app:app --reload --host 0.0.0.0 --port 8000
   ```

## Routes
- `GET /` – landing page with the current authentication status.
- `GET /public` – a publicly accessible page.
- `GET /register` + `POST /register` – create a new username/email + password.
- `GET /login` + `POST /login` – authenticate and receive a signed session cookie.
- `GET /dashboard`, `GET /profile` – require an active session cookie and redirect to `/login` if missing.
- `GET /logout` – clears the session cookie and sends you back to `/`.

## Security notes
- Every response that renders user-provided data escapes the values through `markupsafe.escape`.
- Database calls use parameterized SQL (no string interpolation) with `aiosqlite`.
- Password hashing uses `passlib`'s vetted `bcrypt` algorithm.
- Sessions are signed and time-limited through `itsdangerous.URLSafeTimedSerializer`.

## Development tips
- The SQLite file lives under `data/users.db`; it is ignored by `.gitignore`.
- To reset the database, stop the server and delete `data/users.db` before restarting.
- You can toggle secure cookies by setting `ROBYN_SECURE_COOKIES=1` in the environment (remember to run behind HTTPS when secure cookies are enabled).
