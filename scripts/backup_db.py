"""
SQLite backup helper for TagLens.

Creates a consistent backup using sqlite3's online backup API.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3

from database import DB_PATH


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a TagLens SQLite backup.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DB_PATH,
        help="Path to the SQLite database (default: TAGLENS_DB_PATH or data/users.db).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("backups"),
        help="Directory to write backup files into (default: ./backups).",
    )
    args = parser.parse_args()

    db_path: Path = args.db_path
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"taglens-{ts}.db"

    with sqlite3.connect(db_path) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)

    print(str(dest))
    print(f"sha256={_sha256(dest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

