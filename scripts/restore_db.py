"""
SQLite restore helper for TagLens.

Restores a previously created backup into a target DB path.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from database import DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a TagLens SQLite backup.")
    parser.add_argument("--backup", type=Path, required=True, help="Backup .db file path.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DB_PATH,
        help="Target SQLite path to restore into (default: TAGLENS_DB_PATH or data/users.db).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target DB if it already exists.",
    )
    args = parser.parse_args()

    backup: Path = args.backup
    target: Path = args.db_path
    if not backup.exists():
        raise SystemExit(f"Backup file not found: {backup}")
    if target.exists() and not args.force:
        raise SystemExit(
            f"Refusing to overwrite existing DB without --force: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
    print(f"restored={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

