"""SQLite access: one short-lived connection per unit of work, WAL mode, STRICT tables."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

MIN_SQLITE_VERSION = (3, 37, 0)  # STRICT tables


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 15000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def migrate(self) -> int:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old; version 3.37 or newer is required.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrations = _migrations()
        known = migrations[-1][0] if migrations else 0
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current > known:
                raise RuntimeError(
                    f"The database at {self.path} uses schema version {current}, but this version of the app only "
                    f"knows versions up to {known}. It was opened by a newer version; update the app instead of "
                    "running an older one against it."
                )
            for version, sql in migrations:
                if version <= current:
                    continue
                conn.execute("PRAGMA foreign_keys = OFF")
                try:
                    conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
                    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if fk_errors:
                        raise RuntimeError(f"Foreign key check failed after migration {version}: {fk_errors}")
                finally:
                    conn.execute("PRAGMA foreign_keys = ON")
                current = version
            return current


def _migrations() -> list[tuple[int, str]]:
    package = resources.files("foundry_eval.db") / "migrations"
    found: list[tuple[int, str]] = []
    for entry in package.iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            found.append((int(name[:4]), entry.read_text(encoding="utf-8")))
    return sorted(found)
