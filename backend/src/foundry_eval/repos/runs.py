"""Persistence for runs, prompts, candidates, attempts and manual scores."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..credentials.redaction import REGISTRY, sanitize_error_text
from ..db.database import Database
from ..domain.generation import GenerationSettings
from ..money import Money, to_decimal, to_text
from ..pricing.calculator import CostEstimate
from ..timeutil import iso_now, new_id
from ..types import AttemptStatus, RunStatus
from ..usage.facts import UsageFacts
from .endpoints import NotFoundError

ACTIVE_ATTEMPT_STATUSES = (AttemptStatus.QUEUED.value, AttemptStatus.RUNNING.value)


class RunPrompt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    position: int
    suite_prompt_id: str | None
    title: str
    system_text: str
    user_text: str
    content_hash: str


class RunCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    position: int
    label: str
    profile_id: str
    profile_snapshot: dict[str, Any]
    settings: GenerationSettings
    candidate_key: str
    price_snapshot_id: str


class Attempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    candidate_id: str
    prompt_id: str
    trial_index: int
    retry_of: str | None
    status: AttemptStatus
    request_body: dict[str, Any]
    request_sent_at: str | None
    started_at: str | None
    finished_at: str | None
    latency_ms: int | None
    ttft_ms: int | None
    streamed: bool | None
    http_status: int | None
    request_id: str | None
    finish_reason: str | None
    raw_finish_reason: str | None
    response_model: str | None
    service_tier: str | None
    output_text: str | None
    reasoning_summary: str | None
    usage: UsageFacts | None
    cost: CostEstimate | None
    cost_status: str | None
    cost_total: Money | None
    cost_lower: Money | None
    cost_upper: Money | None
    error_type: str | None
    error_message: str | None
    created_at: str
    budget_reserved: Money | None = None


class Run(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    suite_id: str | None
    title: str
    status: RunStatus
    idempotency_key: str
    trials: int
    budget: Money | None
    judge_config: dict[str, Any] | None
    judge_error: str | None = None
    created_at: str
    started_at: str | None
    finished_at: str | None


class ManualScore(BaseModel):
    attempt_id: str
    score: int
    notes: str
    updated_at: str


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        kind=row["kind"],
        suite_id=row["suite_id"],
        title=row["title"],
        status=RunStatus(row["status"]),
        idempotency_key=row["idempotency_key"],
        trials=row["trials"],
        budget=to_decimal(row["budget"]) if row["budget"] is not None else None,
        judge_config=json.loads(row["judge_config_json"]) if row["judge_config_json"] else None,
        judge_error=row["judge_error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _prompt(row: sqlite3.Row) -> RunPrompt:
    return RunPrompt(
        id=row["id"],
        run_id=row["run_id"],
        position=row["position"],
        suite_prompt_id=row["suite_prompt_id"],
        title=row["title"],
        system_text=row["system_text"],
        user_text=row["user_text"],
        content_hash=row["content_hash"],
    )


def _candidate(row: sqlite3.Row) -> RunCandidate:
    return RunCandidate(
        id=row["id"],
        run_id=row["run_id"],
        position=row["position"],
        label=row["label"],
        profile_id=row["profile_id"],
        profile_snapshot=json.loads(row["profile_snapshot_json"]),
        settings=GenerationSettings.model_validate_json(row["settings_json"]),
        candidate_key=row["candidate_key"],
        price_snapshot_id=row["price_snapshot_id"],
    )


def _attempt(row: sqlite3.Row) -> Attempt:
    def money(key: str) -> Any:
        return to_decimal(row[key]) if row[key] is not None else None

    return Attempt(
        id=row["id"],
        run_id=row["run_id"],
        candidate_id=row["candidate_id"],
        prompt_id=row["prompt_id"],
        trial_index=row["trial_index"],
        retry_of=row["retry_of"],
        status=AttemptStatus(row["status"]),
        request_body=json.loads(row["request_json"]),
        request_sent_at=row["request_sent_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        latency_ms=row["latency_ms"],
        ttft_ms=row["ttft_ms"],
        streamed=None if row["streamed"] is None else bool(row["streamed"]),
        http_status=row["http_status"],
        request_id=row["request_id"],
        finish_reason=row["finish_reason"],
        raw_finish_reason=row["raw_finish_reason"],
        response_model=row["response_model"],
        service_tier=row["service_tier"],
        output_text=row["output_text"],
        reasoning_summary=row["reasoning_summary"],
        usage=UsageFacts.model_validate_json(row["usage_json"]) if row["usage_json"] else None,
        cost=CostEstimate.model_validate_json(row["cost_json"]) if row["cost_json"] else None,
        cost_status=row["cost_status"],
        cost_total=money("cost_total"),
        cost_lower=money("cost_lower"),
        cost_upper=money("cost_upper"),
        error_type=row["error_type"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        budget_reserved=money("budget_reserved"),
    )


def scrub(text: str | None) -> str | None:
    """Redact registered secrets from provider-supplied text before it is stored (defence in depth)."""
    return REGISTRY.redact(text) if text else text


def scrub_json(value: Any) -> str | None:
    return None if value is None else scrub(json.dumps(value))


def _cost_columns(cost: CostEstimate) -> tuple[str, str, str | None, str, str | None]:
    return (
        cost.model_dump_json(),
        cost.status,
        to_text(cost.total) if cost.total is not None else None,
        to_text(cost.lower),
        to_text(cost.upper) if cost.upper is not None else None,
    )


class RunRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ reads
    def get(self, run_id: str) -> Run:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id} not found")
        return _run(row)

    def by_idempotency_key(self, key: str) -> Run | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE idempotency_key = ?", (key,)).fetchone()
        return _run(row) if row else None

    def prompts(self, run_id: str) -> list[RunPrompt]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM run_prompts WHERE run_id = ? ORDER BY position", (run_id,)).fetchall()
        return [_prompt(r) for r in rows]

    def candidates(self, run_id: str) -> list[RunCandidate]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM run_candidates WHERE run_id = ? ORDER BY position", (run_id,)).fetchall()
        return [_candidate(r) for r in rows]

    def candidate(self, candidate_id: str) -> RunCandidate:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM run_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"candidate {candidate_id} not found")
        return _candidate(row)

    def prompt(self, prompt_id: str) -> RunPrompt:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM run_prompts WHERE id = ?", (prompt_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"prompt {prompt_id} not found")
        return _prompt(row)

    def attempts(self, run_id: str) -> list[Attempt]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY created_at, trial_index", (run_id,)
            ).fetchall()
        return [_attempt(r) for r in rows]

    def attempt(self, attempt_id: str) -> Attempt:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"attempt {attempt_id} not found")
        return _attempt(row)

    def raw_response(self, attempt_id: str) -> tuple[Any, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT raw_response_json, raw_usage_json FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"attempt {attempt_id} not found")
        return (
            json.loads(row["raw_response_json"]) if row["raw_response_json"] else None,
            json.loads(row["raw_usage_json"]) if row["raw_usage_json"] else None,
        )

    def attempt_currency(self, attempt_id: str) -> str:
        """The currency of the price snapshot an attempt is priced with."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT s.currency FROM attempts a JOIN run_candidates c ON c.id = a.candidate_id"
                " JOIN price_snapshots s ON s.id = c.price_snapshot_id WHERE a.id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("Attempt not found.")
        return str(row["currency"])

    def run_currency(self, run_id: str, default: str) -> str:
        """The currency a run was priced in (its price snapshots), so its totals survive a currency change."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT s.currency FROM run_candidates c JOIN price_snapshots s ON s.id = c.price_snapshot_id"
                " WHERE c.run_id = ? ORDER BY c.position LIMIT 1",
                (run_id,),
            ).fetchone()
        return str(row["currency"]) if row is not None else default

    def status_counts(self, run_id: str) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT status, count(*) AS n FROM attempts WHERE run_id = ? GROUP BY status", (run_id,)
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def list_runs(self, *, limit: int = 100, kind: str | None = None, suite_id: str | None = None) -> list[Run]:
        query = "SELECT * FROM runs"
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if suite_id:
            clauses.append("suite_id = ?")
            params.append(suite_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_run(r) for r in rows]

    # ------------------------------------------------------------------ writes
    def insert_run(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        title: str,
        idempotency_key: str,
        trials: int,
        budget: str | None,
        suite_id: str | None = None,
        judge_config: dict[str, Any] | None = None,
    ) -> str:
        run_id = new_id()
        conn.execute(
            "INSERT INTO runs (id, kind, suite_id, title, status, idempotency_key, trials, budget, judge_config_json,"
            " created_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
            (
                run_id,
                kind,
                suite_id,
                title,
                idempotency_key,
                trials,
                budget,
                json.dumps(judge_config) if judge_config else None,
                iso_now(),
            ),
        )
        return run_id

    def insert_prompt(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        position: int,
        *,
        title: str,
        system_text: str,
        user_text: str,
        content_hash: str,
        suite_prompt_id: str | None = None,
    ) -> str:
        prompt_id = new_id()
        conn.execute(
            "INSERT INTO run_prompts (id, run_id, position, suite_prompt_id, title, system_text, user_text,"
            " content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (prompt_id, run_id, position, suite_prompt_id, title, system_text, user_text, content_hash),
        )
        return prompt_id

    def insert_candidate(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        position: int,
        *,
        label: str,
        profile_id: str,
        profile_snapshot: dict[str, Any],
        settings: GenerationSettings,
        candidate_key: str,
        price_snapshot_id: str,
    ) -> str:
        candidate_id = new_id()
        conn.execute(
            "INSERT INTO run_candidates (id, run_id, position, label, profile_id, profile_snapshot_json, settings_json,"
            " candidate_key, price_snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                run_id,
                position,
                label,
                profile_id,
                json.dumps(profile_snapshot, sort_keys=True),
                settings.model_dump_json(),
                candidate_key,
                price_snapshot_id,
            ),
        )
        return candidate_id

    def insert_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        candidate_id: str,
        prompt_id: str,
        trial_index: int,
        request_body: dict[str, Any],
        retry_of: str | None = None,
    ) -> str:
        attempt_id = new_id()
        conn.execute(
            "INSERT INTO attempts (id, run_id, candidate_id, prompt_id, trial_index, retry_of, status, request_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (attempt_id, run_id, candidate_id, prompt_id, trial_index, retry_of, json.dumps(request_body), iso_now()),
        )
        return attempt_id

    def mark_run_started(self, run_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = 'running', started_at = coalesce(started_at, ?), finished_at = NULL"
                " WHERE id = ?",
                (iso_now(), run_id),
            )

    def set_judge_error(self, run_id: str, message: str | None) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET judge_error = ? WHERE id = ?", (message, run_id))

    def finish_run(self, run_id: str, status: RunStatus) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET status = ?, finished_at = ? WHERE id = ?", (status.value, iso_now(), run_id))

    def mark_sent(self, attempt_id: str, reserved: Decimal | None = None) -> None:
        """Record that the request is about to be sent (written before the HTTP call)."""
        now = iso_now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET status = 'running', started_at = ?, request_sent_at = ?, budget_reserved = ?"
                " WHERE id = ?",
                (now, now, to_text(reserved) if reserved is not None else None, attempt_id),
            )

    def finish_not_sent(self, attempt_id: str, status: AttemptStatus, message: str, currency: str) -> None:
        cost = CostEstimate.not_sent(currency)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET status = ?, finished_at = ?, error_type = ?, error_message = ?, cost_json = ?,"
                " cost_status = ?, cost_total = ?, cost_lower = ?, cost_upper = ? WHERE id = ?",
                (status.value, iso_now(), status.value, message, *_cost_columns(cost), attempt_id),
            )

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        status: AttemptStatus,
        result: Any,
        usage: UsageFacts | None,
        cost: CostEstimate,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET status = ?, finished_at = ?, latency_ms = ?, ttft_ms = ?, streamed = ?,"
                " http_status = ?, request_id = ?, finish_reason = ?, raw_finish_reason = ?, response_model = ?,"
                " service_tier = ?, output_text = ?, reasoning_summary = ?, raw_response_json = ?, raw_usage_json = ?,"
                " usage_json = ?, cost_json = ?, cost_status = ?, cost_total = ?, cost_lower = ?, cost_upper = ?,"
                " error_type = ?, error_message = ?, request_json = ? WHERE id = ?",
                (
                    status.value,
                    iso_now(),
                    result.latency_ms,
                    result.ttft_ms,
                    int(result.streamed),
                    result.http_status,
                    result.request_id,
                    result.finish_reason.value,
                    result.raw_finish_reason,
                    result.response_model,
                    result.service_tier,
                    scrub(result.output_text),
                    scrub(result.reasoning_summary),
                    scrub_json(result.response_json),
                    scrub_json(result.raw_usage),
                    usage.model_dump_json() if usage else None,
                    *_cost_columns(cost),
                    result.error_type,
                    sanitize_error_text(result.error_message),
                    scrub_json(result.request_body),
                    attempt_id,
                ),
            )

    def finish_unsent_or_sent(self, attempt_id: str, *, status: AttemptStatus, message: str) -> None:
        """Terminal state after cancellation/interruption/unexpected error, respecting whether it was sent.

        The estimate uses the currency of the attempt's price snapshot, whatever the app currency is now.
        """
        attempt = self.attempt(attempt_id)
        currency = self.attempt_currency(attempt_id)
        if attempt.request_sent_at is None:
            self.finish_not_sent(attempt_id, status, message, currency)
            return
        cost = CostEstimate.usage_missing(
            currency, "request was sent but no usage was received", attempt.budget_reserved
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET status = ?, finished_at = ?, error_type = ?, error_message = ?, cost_json = ?,"
                " cost_status = ?, cost_total = ?, cost_lower = ?, cost_upper = ? WHERE id = ?",
                (status.value, iso_now(), status.value, message, *_cost_columns(cost), attempt_id),
            )

    def recover_interrupted(self) -> int:
        """Mark unfinished work from a previous process as interrupted. Never replays it."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM attempts WHERE status IN (?, ?)", ACTIVE_ATTEMPT_STATUSES).fetchall()
        for row in rows:
            self.finish_unsent_or_sent(
                row["id"],
                status=AttemptStatus.INTERRUPTED,
                message="The app stopped before this attempt finished; it was not replayed.",
            )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status IN ('queued', 'running')",
                (iso_now(),),
            )
        return len(rows)

    # ------------------------------------------------------------------ manual scores
    def set_manual_score(self, attempt_id: str, score: int, notes: str) -> ManualScore:
        self.attempt(attempt_id)
        now = iso_now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO manual_scores (attempt_id, score, notes, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(attempt_id) DO UPDATE SET score = excluded.score, notes = excluded.notes,"
                " updated_at = excluded.updated_at",
                (attempt_id, score, notes, now),
            )
        return ManualScore(attempt_id=attempt_id, score=score, notes=notes, updated_at=now)

    def delete_manual_score(self, attempt_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM manual_scores WHERE attempt_id = ?", (attempt_id,))

    def manual_scores(self, run_id: str) -> dict[str, ManualScore]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT m.* FROM manual_scores m JOIN attempts a ON a.id = m.attempt_id WHERE a.run_id = ?", (run_id,)
            ).fetchall()
        return {
            r["attempt_id"]: ManualScore(
                attempt_id=r["attempt_id"], score=r["score"], notes=r["notes"], updated_at=r["updated_at"]
            )
            for r in rows
        }
