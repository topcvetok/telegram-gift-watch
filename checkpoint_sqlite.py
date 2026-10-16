#!/usr/bin/env python3
"""Move committed SQLite WAL records into the main database before caching."""

import sqlite3
from pathlib import Path


DATABASES = (Path("state.sqlite3"), Path("settings.sqlite3"))


def checkpoint(path):
    path = Path(path)
    if not path.exists():
        return False
    with sqlite3.connect(path, timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise RuntimeError(f"Не удалось сохранить журнал SQLite: {path}")
    return True


def main():
    for path in DATABASES:
        if checkpoint(path):
            print(f"SQLite сохранён: {path}")


if __name__ == "__main__":
    main()
