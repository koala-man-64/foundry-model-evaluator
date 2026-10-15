"""Persistence for rubrics and their immutable versions."""

from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel

from ..db.database import Database
from ..judging.rubric import RubricDefinition
from ..timeutil import iso_now, new_id
from .endpoints import ConflictError, NotFoundError


class RubricVersion(BaseModel):
    id: str
    rubric_id: str
    version: int
    definition: RubricDefinition
    created_at: str


class Rubric(BaseModel):
    id: str
    name: str
    is_default: bool
    archived: bool
    latest: RubricVersion
    version_count: int
    created_at: str
    updated_at: str


def _version(row: sqlite3.Row) -> RubricVersion:
    return RubricVersion(
        id=row["id"],
        rubric_id=row["rubric_id"],
        version=row["version"],
        definition=RubricDefinition(
            scale_min=row["scale_min"],
            scale_max=row["scale_max"],
            criteria=json.loads(row["criteria_json"]),
            instructions=row["instructions"],
        ),
        created_at=row["created_at"],
    )


class RubricRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _rubric(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Rubric:
        versions = conn.execute(
            "SELECT * FROM rubric_versions WHERE rubric_id = ? ORDER BY version DESC", (row["id"],)
        ).fetchall()
        return Rubric(
            id=row["id"],
            name=row["name"],
            is_default=bool(row["is_default"]),
            archived=bool(row["archived"]),
            latest=_version(versions[0]),
            version_count=len(versions),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_all(self, *, include_archived: bool = False) -> list[Rubric]:
        query = "SELECT * FROM rubrics" + ("" if include_archived else " WHERE archived = 0")
        query += " ORDER BY is_default DESC, name"
        with self.db.connect() as conn:
            return [self._rubric(conn, row) for row in conn.execute(query).fetchall()]

    def get(self, rubric_id: str) -> Rubric:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM rubrics WHERE id = ?", (rubric_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"rubric {rubric_id} not found")
            return self._rubric(conn, row)

    def versions(self, rubric_id: str) -> list[RubricVersion]:
        self.get(rubric_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rubric_versions WHERE rubric_id = ? ORDER BY version DESC", (rubric_id,)
            ).fetchall()
        return [_version(r) for r in rows]

    def version(self, version_id: str) -> RubricVersion:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM rubric_versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"rubric version {version_id} not found")
        return _version(row)

    def create(self, name: str, definition: RubricDefinition) -> Rubric:
        rubric_id = new_id()
        now = iso_now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO rubrics (id, name, is_default, archived, created_at, updated_at)"
                " VALUES (?, ?, 0, 0, ?, ?)",
                (rubric_id, name.strip(), now, now),
            )
            self._insert_version(conn, rubric_id, 1, definition)
        return self.get(rubric_id)

    def add_version(self, rubric_id: str, definition: RubricDefinition) -> RubricVersion:
        self.get(rubric_id)
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT max(version) AS v FROM rubric_versions WHERE rubric_id = ?", (rubric_id,)
            ).fetchone()["v"]
            version_id = self._insert_version(conn, rubric_id, int(current) + 1, definition)
            conn.execute("UPDATE rubrics SET updated_at = ? WHERE id = ?", (iso_now(), rubric_id))
        return self.version(version_id)

    def update(self, rubric_id: str, *, name: str | None = None, archived: bool | None = None) -> Rubric:
        rubric = self.get(rubric_id)
        if archived and rubric.is_default:
            raise ConflictError("The default rubric cannot be archived.")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE rubrics SET name = ?, archived = ?, updated_at = ? WHERE id = ?",
                (
                    name.strip() if name and name.strip() else rubric.name,
                    int(rubric.archived if archived is None else archived),
                    iso_now(),
                    rubric_id,
                ),
            )
        return self.get(rubric_id)

    @staticmethod
    def _insert_version(conn: sqlite3.Connection, rubric_id: str, version: int, definition: RubricDefinition) -> str:
        version_id = new_id()
        conn.execute(
            "INSERT INTO rubric_versions (id, rubric_id, version, scale_min, scale_max, criteria_json, instructions,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                rubric_id,
                version,
                definition.scale_min,
                definition.scale_max,
                json.dumps([c.model_dump() for c in definition.criteria]),
                definition.instructions,
                iso_now(),
            ),
        )
        return version_id
