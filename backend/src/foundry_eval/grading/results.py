"""Session-only pattern results, with a small page cache and temporary disk backing.

Completed checks must survive a view's time limit even when a run exceeds an in-memory
LRU's capacity. An anonymous SQLite database retains them until this process closes it,
spilling pages to the OS temporary directory rather than growing a Python dictionary.
It stores only digests and outcomes, never answer text, and is separate from app data.
"""

from __future__ import annotations

import sqlite3
import threading

type Result = tuple[bool | None, str, float | None]


class PatternResults:
    def __init__(self) -> None:
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        # Called with the lock held. An empty filename creates a private temporary
        # database deleted on close, not an unbounded :memory: database.
        db = sqlite3.connect("", check_same_thread=False)
        try:
            db.execute("PRAGMA cache_size = -2048")  # about 2 MiB of page cache
            db.execute(
                "CREATE TABLE results (key BLOB PRIMARY KEY, passed INTEGER, detail TEXT NOT NULL, expires REAL)"
            )
        except sqlite3.Error:
            db.close()
            raise
        self._db = db
        return db

    def get(self, key: bytes) -> Result | None:
        with self._lock:
            if self._db is None:
                return None
            row = self._db.execute("SELECT passed, detail, expires FROM results WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            return (
                None if row[0] is None else bool(row[0]),
                str(row[1]),
                None if row[2] is None else float(row[2]),
            )

    def put(self, key: bytes, value: Result) -> None:
        with self._lock:
            db = self._db if self._db is not None else self._connect()
            # No commit is needed: only this connection uses the disposable results.
            db.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)", (key, *value))

    def clear(self) -> None:
        with self._lock:
            db, self._db = self._db, None
            if db is not None:
                db.close()
