# TagLens Authentication Service

This repository contains a Robyn-based web app for user authentication, profile pages, and photo metadata storage.

## What this app does
- Username/password auth with hashed passwords (`passlib[bcrypt]`).
- Session cookie auth with server-side session storage in SQLite.
- CSRF protection for form-based auth actions.
- Protected routes (`/dashboard`, `/profile`, `/api/*`) and public routes (`/`, `/public`, `/register`, `/login`).
- Photo metadata APIs with optional AI description generation.

## Quick start (local)
1. Create and activate a virtual environment:
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

## Running checks
Run the repository checks script:
```bash
./scripts/check.sh
```

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
- `GET /public`
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
