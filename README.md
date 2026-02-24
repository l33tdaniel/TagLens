# TagLens Authentication Service

This project is a small Robyn application that demonstrates a secure username/password flow.

**Author: Daniel Neugent**

## Features
- `robyn` runs the asynchronous web server.
- SQLite stores user records with hashed-and-salted passwords using `passlib[bcrypt]`.
- Auth-protected routes (`/dashboard`, `/profile`) redirect to `/login` when there is no session cookie.
- Authenticated photo uploads can call Ollama to generate a short AI description per image.
- Public routes (`/`, `/public`, `/register`, `/login`) remain open to everyone.
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
2. Install the dependencies (runtime and tooling):
   ```bash
   uv install -r requirements.txt requirements-dev.txt
   ```
3. Optionally set `ROBYN_SECURE_COOKIES=1` if you deploy behind HTTPS and want secure cookies.
4. Use the automated checks to ensure the code base compiles and is linted:
   ```bash
   ./scripts/check.sh
   ```
5. Run the app:
   ```bash
   ROBYN_HOST=0.0.0.0 ROBYN_PORT=8000 ./start_server.sh
   ```
### Running image metadata script (Windows)
Activate your virtual environment first to ensure dependencies are available:
```powershell
.\.venv\Scripts\Activate.ps1
python scripts\metadata.py "C:\\path\\to\\image.jpg"
```

   By default `./start_server.sh` launches Robyn without `--dev` because the bundled reloader is currently crashing with `RuntimeError: threads can only be started once`. Set `DEV_MODE=1` (or `DEV_MODE=true`) if you need the hot-reload behavior and can tolerate the occasional `--dev` panic until the upstream issue is resolved.

## Routes
- `GET /` – landing page with the current authentication status.
- `GET /public` – a publicly accessible page.
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

### Windows OCR setup
- OCR uses Tesseract; install it from: https://github.com/UB-Mannheim/tesseract/wiki
- After installation, ensure `tesseract.exe` is in your `PATH` or set `pytesseract.pytesseract.tesseract_cmd` to the installed binary location.

### Try it
```bash
python scripts/metadata.py
```
The script prints face bounding boxes and a preview of extracted text if Tesseract is available.
