"""Endpoint persistence. API keys are never stored here (only an opaque credential_ref)."""

from __future__ import annotations

import sqlite3

from ..db.database import Database
from ..domain.profiles import AuthStyle, Endpoint, host_identity, validate_base_url
from ..timeutil import iso_now, new_id


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


def _row(row: sqlite3.Row) -> Endpoint:
    return Endpoint(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        region=row["region"],
        auth_style=row["auth_style"],
        credential_ref=row["credential_ref"],
        key_set_at=row["key_set_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class EndpointRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, *, name: str, base_url: str, region: str, auth_style: AuthStyle) -> Endpoint:
        now = iso_now()
        endpoint_id = new_id()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO endpoints (id, name, base_url, region, auth_style, credential_ref, key_set_at,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    endpoint_id,
                    name.strip(),
                    validate_base_url(base_url),
                    region.strip(),
                    auth_style,
                    new_id(),
                    now,
                    now,
                ),
            )
        return self.get(endpoint_id)

    def get(self, endpoint_id: str) -> Endpoint:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"endpoint {endpoint_id} not found")
        return _row(row)

    def list(self) -> list[Endpoint]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM endpoints ORDER BY name COLLATE NOCASE").fetchall()
        return [_row(r) for r in rows]

    def update(
        self, endpoint_id: str, *, name: str, base_url: str, region: str, auth_style: AuthStyle
    ) -> tuple[Endpoint, bool]:
        """Update an endpoint. Returns (endpoint, host_changed). A host change rotates the
        credential reference so a stored key can never be sent to a different host."""
        current = self.get(endpoint_id)
        url = validate_base_url(base_url)
        host_changed = host_identity(url) != host_identity(current.base_url)
        credential_ref = new_id() if host_changed else current.credential_ref
        key_set_at = None if host_changed else current.key_set_at
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE endpoints SET name = ?, base_url = ?, region = ?, auth_style = ?, credential_ref = ?,"
                " key_set_at = ?, updated_at = ? WHERE id = ?",
                (name.strip(), url, region.strip(), auth_style, credential_ref, key_set_at, iso_now(), endpoint_id),
            )
        return self.get(endpoint_id), host_changed

    def mark_key_set(self, endpoint_id: str, is_set: bool) -> Endpoint:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE endpoints SET key_set_at = ?, updated_at = ? WHERE id = ?",
                (iso_now() if is_set else None, iso_now(), endpoint_id),
            )
        return self.get(endpoint_id)

    def delete(self, endpoint_id: str) -> Endpoint:
        endpoint = self.get(endpoint_id)
        try:
            with self._db.transaction() as conn:
                conn.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("This endpoint is used by model profiles; archive or move them first.") from exc
        return endpoint
