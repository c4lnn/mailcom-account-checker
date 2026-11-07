from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .db import Database


def backup_database(destination: str | Path, source: Database | None = None) -> Path:
    source = source or Database()
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve() == source.path.resolve():
        raise ValueError("backup_destination_matches_database")
    with source.connect() as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the account console SQLite database")
    parser.add_argument("destination", help="Path to the backup database file")
    args = parser.parse_args()
    backup_database(args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
