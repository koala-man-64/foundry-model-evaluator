"""Idempotency keys for paid actions other than run creation (explicit retries)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from ..db.database import Database
from ..timeutil import iso_now
from .endpoints import ConflictError


class ActionKeyRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def run_once(
        self, key: str, action: str, work: Callable[[sqlite3.Connection], list[str]]
    ) -> tuple[list[str], bool]:
        """Run ``work`` once per key. Returns (result, created).

        The check, the work and the key are written in one write transaction (BEGIN IMMEDIATE), so
        concurrent requests with the same key wait for the first and then get its result.
        """
        with self.db.transaction() as conn:
            row = conn.execute("SELECT action, result_json FROM action_keys WHERE key = ?", (key,)).fetchone()
            if row is not None:
                if row["action"] != action:
                    raise ConflictError("This Idempotency-Key was already used for a different action.")
                result: list[str] = json.loads(row["result_json"])
                return result, False
            result = work(conn)
            conn.execute(
                "INSERT INTO action_keys (key, action, result_json, created_at) VALUES (?, ?, ?, ?)",
                (key, action, json.dumps(result), iso_now()),
            )
            return result, True
