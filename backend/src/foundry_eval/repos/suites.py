"""Persistence for saved suites."""

from __future__ import annotations

import json
import sqlite3

from ..db.database import Database
from ..domain.generation import GenerationSettings
from ..domain.suites import Suite, SuiteCandidate, SuiteIn, SuiteJudge, SuitePrompt
from ..grading.checks import checks_json, load_checks
from ..timeutil import iso_now, new_id
from .endpoints import NotFoundError


class SuiteRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _load(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Suite:
        prompts = conn.execute(
            "SELECT * FROM suite_prompts WHERE suite_id = ? ORDER BY position", (row["id"],)
        ).fetchall()
        candidates = conn.execute(
            "SELECT * FROM suite_candidates WHERE suite_id = ? ORDER BY position", (row["id"],)
        ).fetchall()
        judge = SuiteJudge.model_validate_json(row["judge_config_json"]) if row["judge_config_json"] else None
        return Suite(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            trials=row["trials"],
            judge=judge,
            archived=bool(row["archived"]),
            prompts=[
                SuitePrompt(
                    id=p["id"],
                    position=p["position"],
                    title=p["title"],
                    system=p["system_text"],
                    user=p["user_text"],
                    reference=p["reference_text"],
                    checks=load_checks(p["checks_json"]),
                )
                for p in prompts
            ],
            candidates=[
                SuiteCandidate(
                    id=c["id"],
                    position=c["position"],
                    profile_id=c["profile_id"],
                    label=c["label"],
                    settings=GenerationSettings.model_validate_json(c["settings_json"]),
                )
                for c in candidates
            ],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, suite_id: str) -> Suite:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM suites WHERE id = ?", (suite_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"suite {suite_id} not found")
            return self._load(conn, row)

    def list(self, *, include_archived: bool = False) -> list[Suite]:
        query = "SELECT * FROM suites" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY name"
        with self.db.connect() as conn:
            return [self._load(conn, row) for row in conn.execute(query).fetchall()]

    def create(self, payload: SuiteIn) -> Suite:
        suite_id = new_id()
        now = iso_now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO suites (id, name, description, trials, judge_config_json, archived, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (suite_id, payload.name.strip(), payload.description, payload.trials, _judge_json(payload), now, now),
            )
            self._write_children(conn, suite_id, payload, existing_prompts=set(), existing_candidates=set())
        return self.get(suite_id)

    def update(self, suite_id: str, payload: SuiteIn) -> Suite:
        self.get(suite_id)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE suites SET name = ?, description = ?, trials = ?, judge_config_json = ?, updated_at = ?"
                " WHERE id = ?",
                (payload.name.strip(), payload.description, payload.trials, _judge_json(payload), iso_now(), suite_id),
            )
            prompts = {r["id"] for r in conn.execute("SELECT id FROM suite_prompts WHERE suite_id = ?", (suite_id,))}
            candidates = {
                r["id"] for r in conn.execute("SELECT id FROM suite_candidates WHERE suite_id = ?", (suite_id,))
            }
            self._write_children(conn, suite_id, payload, existing_prompts=prompts, existing_candidates=candidates)
        return self.get(suite_id)

    def _write_children(
        self,
        conn: sqlite3.Connection,
        suite_id: str,
        payload: SuiteIn,
        *,
        existing_prompts: set[str],
        existing_candidates: set[str],
    ) -> None:
        # Prompt and candidate ids stay stable across edits (runs refer to the prompt a copy came from).
        kept_prompts: set[str] = set()
        for position, prompt in enumerate(payload.prompts):
            if prompt.id and prompt.id in existing_prompts and prompt.id not in kept_prompts:
                conn.execute(
                    "UPDATE suite_prompts SET position = ?, title = ?, system_text = ?, user_text = ?,"
                    " reference_text = ?, checks_json = ? WHERE id = ?",
                    (
                        position,
                        prompt.title,
                        prompt.system,
                        prompt.user,
                        prompt.reference,
                        checks_json(prompt.checks),
                        prompt.id,
                    ),
                )
                kept_prompts.add(prompt.id)
            else:
                conn.execute(
                    "INSERT INTO suite_prompts (id, suite_id, position, title, system_text, user_text, reference_text,"
                    " checks_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        suite_id,
                        position,
                        prompt.title,
                        prompt.system,
                        prompt.user,
                        prompt.reference,
                        checks_json(prompt.checks),
                    ),
                )
        for stale in existing_prompts - kept_prompts:
            conn.execute("DELETE FROM suite_prompts WHERE id = ?", (stale,))
        kept_candidates: set[str] = set()
        for position, candidate in enumerate(payload.candidates):
            settings = candidate.settings.model_dump_json()
            label = candidate.label.strip() if candidate.label and candidate.label.strip() else None
            if candidate.id and candidate.id in existing_candidates and candidate.id not in kept_candidates:
                conn.execute(
                    "UPDATE suite_candidates SET position = ?, profile_id = ?, label = ?, settings_json = ?"
                    " WHERE id = ?",
                    (position, candidate.profile_id, label, settings, candidate.id),
                )
                kept_candidates.add(candidate.id)
            else:
                conn.execute(
                    "INSERT INTO suite_candidates (id, suite_id, position, profile_id, label, settings_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (new_id(), suite_id, position, candidate.profile_id, label, settings),
                )
        for stale in existing_candidates - kept_candidates:
            conn.execute("DELETE FROM suite_candidates WHERE id = ?", (stale,))

    def set_archived(self, suite_id: str, archived: bool) -> Suite:
        self.get(suite_id)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE suites SET archived = ?, updated_at = ? WHERE id = ?", (int(archived), iso_now(), suite_id)
            )
        return self.get(suite_id)

    def delete(self, suite_id: str) -> None:
        """Delete a suite. Its runs keep their own copies of the prompts and settings."""
        self.get(suite_id)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM suites WHERE id = ?", (suite_id,))


def _judge_json(payload: SuiteIn) -> str | None:
    if payload.judge is None or not payload.judge.enabled:
        return None
    return json.dumps(payload.judge.model_dump(mode="json"), sort_keys=True)
