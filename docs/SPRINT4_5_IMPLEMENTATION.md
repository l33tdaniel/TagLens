# Sprint 4.5 – Privacy, Performance, Backups (Implementation Plan)

This document tracks the concrete, PR-sized work to add:
- Privacy controls (per-user + per-photo)
- Performance improvements (async/background + less data transfer)
- Backups (repeatable, tested restore path)

## Phase 0: Baseline (now)
- Confirm current architecture + data flows:
  - Robyn app (`app.py`) + SQLite persistence (`database.py`)
  - Images stored in Backblaze B2 when configured; otherwise stored as BLOBs in SQLite.
  - Upload path currently performs OCR/EXIF, thumbnail generation, AI caption, and face tagging in-request.
- Define privacy knobs (initial):
  - Per-user toggles: AI descriptions, OCR, face recognition.
  - Later: retention window, store originals on/off, share links + ACL.

## Phase 1: Schema + settings foundation
- Add `user_settings` table (per-user privacy flags + future knobs).
- Add basic DB indexes for common queries:
  - `images(user_id, created_at)`
  - `images(user_id, taken_at)`
- Add API to read/update privacy settings.
- Enforce settings during upload:
  - If OCR disabled → skip OCR extraction.
  - If AI disabled → skip AI caption generation.
  - If faces disabled → skip face tagging.
  - AI failures should not fail uploads (best-effort).

## Phase 2: Per-photo privacy (share controls)
- Add share-link model:
  - `photo_shares` with hashed token, expiry, and revoke.
  - Public route `/s/<token>` to view/download.
  - Share-link creation/revoke endpoints under auth.
- Optional: direct user-to-user ACL (`photo_acl`) for “shared with user X”.

## Phase 3: Performance (big wins)
- Move heavy work off the request path:
  - Create a `jobs` table + background worker task (Robyn startup) OR external queue.
  - Upload returns quickly with minimal metadata; background pipeline fills in OCR/faces/AI.
- Reduce payload sizes:
  - Move from base64 uploads/downloads to direct-to-B2 signed URLs.
  - Prefer thumbnails by default; on-demand original.

## Phase 4: Backups + DR
- Add scripts:
  - `scripts/backup_db.py` – SQLite online backup to timestamped file
  - `scripts/restore_db.py` – restore from backup into target DB
- Add runbook:
  - What to back up (SQLite file + B2 objects + config)
  - Rotation policy (daily/monthly) + restore test cadence

