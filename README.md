# TagLens Authentication Service

This repository contains a Robyn-based web app for user authentication, profile pages, and photo metadata storage.

## What this app does
- Username/password auth with hashed passwords (`passlib[bcrypt]`).
- Session cookie auth with server-side session storage in SQLite.
- CSRF protection for form-based auth actions.
- Protected routes (`/dashboard`, `/profile`, `/api/*`) and public routes (`/`, `/register`, `/login`).
- Photo metadata APIs with optional AI description generation.

## Features
- `robyn` runs the asynchronous web server.
- SQLite stores user records with hashed-and-salted passwords using `passlib[bcrypt]`.
- Auth-protected routes (`/dashboard`, `/profile`) redirect to `/login` when there is no session cookie.
- Authenticated photo uploads can call Ollama to generate a short AI description per image.
- Public routes (`/`, `/register`, `/login`) remain open to everyone.
- Session cookies are opaque tokens stored server-side in SQLite and default to `httponly`, `samesite=lax`, and configurable `secure` mode.
- CSRF protection uses the double-submit cookie pattern for HTML forms.
- No SQL is interpolated directly; every database query is parameterized through `aiosqlite`.
- HTML rendering escapes user supplied values with `markupsafe`.

## Setup
1. (Optional but recommended) create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
2. Install core app dependencies:
   ```bash
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt -r requirements-dev.txt
   ```
3. Install media/metadata extras (currently required for app startup because `app.py` imports `scripts/metadata.py` at import time):
   ```bash
   python -m pip install -r arnav_requirements.txt
   ```
4. Set environment variables for Backblaze B2 (required by current startup path):
   ```bash
   export KEY_ID="..."
   export APP_KEY="..."
   export BUCKET_NAME="..."
   ```
5. Start the app:
   ```bash
   ./start_server.sh
   ```
   Default URL: `http://127.0.0.1:9009`

   By default `./start_server.sh` launches Robyn without `--dev` because the bundled reloader is currently crashing with `RuntimeError: threads can only be started once`. Set `DEV_MODE=1` (or `DEV_MODE=true`) if you need the hot-reload behavior and can tolerate the occasional `--dev` panic until the upstream issue is resolved.

## Routes
- `GET /` – landing page with the current authentication status.
- `GET /register` + `POST /register` – create a new username/email + password.
- `GET /login` + `POST /login` – authenticate and receive a session cookie.
- `GET /dashboard`, `GET /profile` – require an active session cookie and redirect to `/login` if missing.
- `GET /api/profile` – returns JSON profile metadata and uploaded photos (requires session cookie). Supports `sort_by=uploaded|taken` and `order=asc|desc`.
- `POST /api/photos` – accepts JSON `{ "filename": "...", "image_base64": "...", "content_type": "image/png", "taken_at": "ISO-8601" }`, stores metadata + binary payload, and adds an Ollama-generated description when available (requires session cookie).
- `GET /api/photos/download?photo_id=<id>` – returns JSON with the photo metadata and base64 image payload for a single photo (requires session cookie).
- `DELETE /api/photos` – requires JSON `{ "photo_id": <id>, "confirm_delete": true }` and permanently deletes the photo (requires session cookie).
- `POST /logout` – revokes the session cookie and sends you back to `/`.

## Security notes
- Every response that renders user-provided data escapes the values through `markupsafe.escape`.
- Database calls use parameterized SQL (no string interpolation) with `aiosqlite`.
- Password hashing uses `passlib`'s vetted `bcrypt` algorithm.
- Sessions are stored in SQLite with hashed tokens and server-side revocation.
- CSRF protection is enforced for login, registration, and logout.

## Development tips
- The SQLite file lives under `data/users.db`; it is ignored by `.gitignore`.
- Configure Ollama integration with:
  - `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`)
  - `OLLAMA_MODEL` (default `llava`)
- To reset the database, stop the server and delete `data/users.db` before restarting.
- You can toggle secure cookies by setting `ROBYN_SECURE_COOKIES=1` in the environment (remember to run behind HTTPS when secure cookies are enabled).

## Image Processing (Faces + OCR)
- A standalone script at `scripts/metadata.py` extracts EXIF, captions, face boxes, and OCR text from images (including HEIC).
- Install extra dependencies for this script using `arnav_requirements.txt`.

### OCR setup
- OCR uses EasyOCR, which is automatically installed via `arnav_requirements.txt`.
- No additional system dependencies required.

## Image storage
The program uses Backblaze to store image files. You can configure the app via `.env` (auto-loaded by `./start_server.sh`), for example:

```dotenv
HOST=127.0.0.1
PORT=9009
KEY_ID=your_key_id
API_KEY=your_api_key
BUCKET_NAME=your_bucket_name
```

`APP_KEY` is also accepted as an alias for `API_KEY`.

### Try it
```bash
./scripts/check.sh
```
The script prints face bounding boxes and extracted text from the image.

Or run each check directly:
```bash
source .venv/bin/activate
python -m compileall app.py auth.py database.py scripts tests
python -m black --check .
python -m ruff check .
python -m mypy app.py auth.py database.py
python -m pytest -q
```

## Running tests
- Fast unit-style tests only:
  ```bash
  source .venv/bin/activate
  python -m pytest -q tests/test_auth.py tests/test_database.py
  ```
- Integration tests:
  ```bash
  source .venv/bin/activate
  python -m pytest -q tests/test_app_integration.py
  ```

## Current quality snapshot (2026-02-28)
From a fresh run in this repo:
- `black --check .`: failing (11 files would be reformatted).
- `ruff check .`: failing (47 violations across app and scripts).
- `mypy app.py auth.py database.py`: failing (missing optional deps + nullable user access warnings).
- `pytest -q`: failing during collection because `scripts/database_test.py` imports `sqlite_vec`, which is not in base requirements.
- `pytest -q tests/test_auth.py tests/test_database.py`: passing (5 tests).
- `pytest -q tests`: integration tests currently fail because the Robyn process exits early when optional media dependencies are missing.

## Known gaps to fix
- `app.py` imports heavy/optional metadata modules at import time; this blocks startup in minimal environments.
- `scripts/database_test.py` is picked up by pytest collection but depends on non-core packages.
- Multiple wildcard imports in `scripts/` reduce type/lint quality.
- `app.py` is large and handles many concerns (routing, auth, storage, B2, metadata orchestration) in one file.

## Main routes
- `GET /` 
- `GET /register`, `POST /register`
- `GET /login`, `POST /login`
- `POST /logout`
- `GET /dashboard`
- `GET /profile`
- `GET /api/profile`
- `POST /api/photos`
- `GET /api/photos/download?photo_id=<id>`
- `DELETE /api/photos`

## Environment variables
- `ROBYN_HOST` (default `127.0.0.1`)
- `ROBYN_PORT` (default `9009`)
- `ROBYN_SECRET_KEY` (generated at startup if unset)
- `ROBYN_SECURE_COOKIES` (`1` for HTTPS deployments)
- `TAGLENS_DB_PATH` (optional sqlite file override)
- `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`)
- `OLLAMA_MODEL` (default `llava`)
- `KEY_ID`, `APP_KEY`, `BUCKET_NAME` (Backblaze B2)

## Notes
- `./start_server.sh` supports `DEV_MODE=1` to pass `--dev` to Robyn.
- The local SQLite database is stored under `data/` by default.
