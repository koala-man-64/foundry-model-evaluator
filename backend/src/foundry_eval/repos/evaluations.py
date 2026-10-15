"""Persistence for judge evaluations (kept separately from attempts and manual scores)."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from ..credentials.redaction import sanitize_error_text
from ..db.database import Database
from ..domain.generation import GenerationSettings
from ..judging.parse import ParseResult
from ..money import Money, to_decimal, to_text
from ..pricing.calculator import CostEstimate
from ..providers.base import ProviderResult
from ..timeutil import iso_now, new_id
from ..usage.facts import UsageFacts
from .endpoints import NotFoundError
from .runs import scrub, scrub_json

EvaluationStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "malformed",
    "failed",
    "rejected_oversized",
    "cancelled",
    "interrupted",
    "skipped_budget",
]
ACTIVE = ("queued", "running")


class OrderEntry(BaseModel):
    label: str
    attempt_id: str
    candidate_id: str


class CriterionScore(BaseModel):
    key: str
    score: int
    explanation: str


class EvaluationCandidate(BaseModel):
    label: str
    position: int
    attempt_id: str
    candidate_id: str
    judge_overall: float
    weighted_overall: float
    explanation: str
    criteria: list[CriterionScore]


class Evaluation(BaseModel):
    id: str
    run_id: str
    prompt_id: str
    trial_index: int
    batch_id: str
    trigger: Literal["manual", "auto"]
    status: EvaluationStatus
    judge_profile_id: str
    judge_profile_snapshot: dict[str, Any]
    judge_settings: GenerationSettings
    rubric_version_id: str
    price_snapshot_id: str
    structured_output: bool
    judge_is_participant: bool
    participant_reasons: list[str]
    order: list[OrderEntry]
    input_estimate_tokens: int
    input_limit_tokens: int
    request_sent_at: str | None
    started_at: str | None
    finished_at: str | None
    latency_ms: int | None
    finish_reason: str | None
    raw_finish_reason: str | None
    response_model: str | None
    output_text: str | None
    usage: UsageFacts | None
    parse_errors: list[str]
    preferred: str | None
    preferred_candidate_id: str | None
    preference_explanation: str | None
    cost: CostEstimate | None
    cost_total: Money | None
    error_type: str | None
    error_message: str | None
    created_at: str
    candidates: list[EvaluationCandidate]
    budget_reserved: Money | None = None


def _cost_columns(cost: CostEstimate) -> tuple[str, str, str | None, str, str | None]:
    return (
        cost.model_dump_json(),
        cost.status,
        to_text(cost.total) if cost.total is not None else None,
        to_text(cost.lower),
        to_text(cost.upper) if cost.upper is not None else None,
    )


class EvaluationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ reads
    def _load(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Evaluation:
        candidate_rows = conn.execute(
            "SELECT * FROM evaluation_candidates WHERE evaluation_id = ? ORDER BY position", (row["id"],)
        ).fetchall()
        criteria_rows = conn.execute(
            "SELECT * FROM evaluation_criteria WHERE evaluation_id = ?", (row["id"],)
        ).fetchall()
        criteria: dict[str, list[CriterionScore]] = {}
        for c in criteria_rows:
            criteria.setdefault(c["label"], []).append(
                CriterionScore(key=c["criterion_key"], score=c["score"], explanation=c["explanation"])
            )
        return Evaluation(
            id=row["id"],
            run_id=row["run_id"],
            prompt_id=row["prompt_id"],
            trial_index=row["trial_index"],
            batch_id=row["batch_id"],
            trigger=row["trigger"],
            status=row["status"],
            judge_profile_id=row["judge_profile_id"],
            judge_profile_snapshot=json.loads(row["judge_profile_snapshot_json"]),
            judge_settings=GenerationSettings.model_validate_json(row["judge_settings_json"]),
            rubric_version_id=row["rubric_version_id"],
            price_snapshot_id=row["price_snapshot_id"],
            structured_output=bool(row["structured_output"]),
            judge_is_participant=bool(row["judge_is_participant"]),
            participant_reasons=json.loads(row["participant_reasons_json"]),
            order=[OrderEntry.model_validate(o) for o in json.loads(row["order_json"])],
            input_estimate_tokens=row["input_estimate_tokens"],
            input_limit_tokens=row["input_limit_tokens"],
            request_sent_at=row["request_sent_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            latency_ms=row["latency_ms"],
            finish_reason=row["finish_reason"],
            raw_finish_reason=row["raw_finish_reason"],
            response_model=row["response_model"],
            output_text=row["output_text"],
            usage=UsageFacts.model_validate_json(row["usage_json"]) if row["usage_json"] else None,
            parse_errors=json.loads(row["parse_errors_json"]),
            preferred=row["preferred"],
            preferred_candidate_id=row["preferred_candidate_id"],
            preference_explanation=row["preference_explanation"],
            cost=CostEstimate.model_validate_json(row["cost_json"]) if row["cost_json"] else None,
            cost_total=to_decimal(row["cost_total"]) if row["cost_total"] is not None else None,
            error_type=row["error_type"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            budget_reserved=to_decimal(row["budget_reserved"]) if row["budget_reserved"] is not None else None,
            candidates=[
                EvaluationCandidate(
                    label=c["label"],
                    position=c["position"],
                    attempt_id=c["attempt_id"],
                    candidate_id=c["candidate_id"],
                    judge_overall=c["judge_overall"],
                    weighted_overall=c["weighted_overall"],
                    explanation=c["explanation"],
                    criteria=criteria.get(c["label"], []),
                )
                for c in candidate_rows
            ],
        )

    def get(self, evaluation_id: str) -> Evaluation:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"evaluation {evaluation_id} not found")
            return self._load(conn, row)

    def list_for_run(self, run_id: str) -> list[Evaluation]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evaluations WHERE run_id = ? ORDER BY created_at, trial_index", (run_id,)
            ).fetchall()
            return [self._load(conn, row) for row in rows]

    def list_for_runs(self, run_ids: list[str]) -> list[Evaluation]:
        return [e for run_id in run_ids for e in self.list_for_run(run_id)]

    def ids_for_key(self, idempotency_key: str) -> list[str]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM evaluations WHERE idempotency_key = ? ORDER BY created_at", (idempotency_key,)
            ).fetchall()
        return [r["id"] for r in rows]

    def has_trigger(self, run_id: str, trigger: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM evaluations WHERE run_id = ? AND trigger = ? LIMIT 1", (run_id, trigger)
            ).fetchone()
        return row is not None

    def spending_for_run(self, run_id: str) -> list[tuple[str | None, CostEstimate | None, Decimal | None]]:
        """(request_sent_at, cost, budget_reserved) of every evaluation, for budget accounting."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT request_sent_at, cost_json, budget_reserved FROM evaluations WHERE run_id = ?", (run_id,)
            ).fetchall()
        return [
            (
                r["request_sent_at"],
                CostEstimate.model_validate_json(r["cost_json"]) if r["cost_json"] else None,
                to_decimal(r["budget_reserved"]) if r["budget_reserved"] is not None else None,
            )
            for r in rows
        ]

    def costs_for_run(self, run_id: str) -> list[CostEstimate | None]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT cost_json FROM evaluations WHERE run_id = ?", (run_id,)).fetchall()
        return [CostEstimate.model_validate_json(r["cost_json"]) if r["cost_json"] else None for r in rows]

    def raw(self, evaluation_id: str) -> tuple[dict[str, Any], Any, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT request_json, raw_response_json, raw_usage_json FROM evaluations WHERE id = ?",
                (evaluation_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"evaluation {evaluation_id} not found")
        return (
            json.loads(row["request_json"]),
            json.loads(row["raw_response_json"]) if row["raw_response_json"] else None,
            json.loads(row["raw_usage_json"]) if row["raw_usage_json"] else None,
        )

    # ------------------------------------------------------------------ writes
    def insert(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        prompt_id: str,
        trial_index: int,
        batch_id: str,
        idempotency_key: str,
        trigger: str,
        judge_profile_id: str,
        judge_profile_snapshot: dict[str, Any],
        judge_settings: GenerationSettings,
        rubric_version_id: str,
        price_snapshot_id: str,
        structured_output: bool,
        participant_reasons: list[str],
        order: list[OrderEntry],
        input_estimate_tokens: int,
        input_limit_tokens: int,
        request_body: dict[str, Any],
        rejected: tuple[str, CostEstimate] | None = None,
    ) -> str:
        evaluation_id = new_id()
        now = iso_now()
        status = "rejected_oversized" if rejected else "queued"
        conn.execute(
            "INSERT INTO evaluations (id, run_id, prompt_id, trial_index, batch_id, idempotency_key, trigger, status,"
            " judge_profile_id, judge_profile_snapshot_json, judge_settings_json, rubric_version_id,"
            " price_snapshot_id, structured_output, judge_is_participant, participant_reasons_json, order_json,"
            " input_estimate_tokens, input_limit_tokens, request_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evaluation_id,
                run_id,
                prompt_id,
                trial_index,
                batch_id,
                idempotency_key,
                trigger,
                status,
                judge_profile_id,
                json.dumps(judge_profile_snapshot, sort_keys=True),
                judge_settings.model_dump_json(),
                rubric_version_id,
                price_snapshot_id,
                int(structured_output),
                int(bool(participant_reasons)),
                json.dumps(participant_reasons),
                json.dumps([o.model_dump() for o in order]),
                input_estimate_tokens,
                input_limit_tokens,
                scrub_json(request_body),
                now,
            ),
        )
        if rejected:
            message, cost = rejected
            conn.execute(
                "UPDATE evaluations SET finished_at = ?, error_type = 'rejected_oversized', error_message = ?,"
                " cost_json = ?, cost_status = ?, cost_total = ?, cost_lower = ?, cost_upper = ? WHERE id = ?",
                (now, message, *_cost_columns(cost), evaluation_id),
            )
        return evaluation_id

    def mark_sent(self, evaluation_id: str, reserved: Decimal | None = None) -> None:
        now = iso_now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE evaluations SET status = 'running', started_at = ?, request_sent_at = ?, budget_reserved = ?"
                " WHERE id = ?",
                (now, now, to_text(reserved) if reserved is not None else None, evaluation_id),
            )

    def finish_not_sent(self, evaluation_id: str, status: str, message: str, currency: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE evaluations SET status = ?, finished_at = ?, error_type = ?, error_message = ?, cost_json = ?,"
                " cost_status = ?, cost_total = ?, cost_lower = ?, cost_upper = ? WHERE id = ?",
                (status, iso_now(), status, message, *_cost_columns(CostEstimate.not_sent(currency)), evaluation_id),
            )

    def finish_unsent_or_sent(self, evaluation_id: str, status: str, message: str) -> None:
        """Terminal state after cancellation/interruption/unexpected error, in the snapshot's currency."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT e.request_sent_at, e.budget_reserved, s.currency FROM evaluations e"
                " JOIN price_snapshots s ON s.id = e.price_snapshot_id WHERE e.id = ?",
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return
        currency = str(row["currency"])
        if row["request_sent_at"] is None:
            self.finish_not_sent(evaluation_id, status, message, currency)
            return
        reserved = to_decimal(row["budget_reserved"]) if row["budget_reserved"] is not None else None
        cost = CostEstimate.usage_missing(currency, "request was sent but no usage was received", reserved)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE evaluations SET status = ?, finished_at = ?, error_type = ?, error_message = ?, cost_json = ?,"
                " cost_status = ?, cost_total = ?, cost_lower = ?, cost_upper = ? WHERE id = ?",
                (status, iso_now(), status, message, *_cost_columns(cost), evaluation_id),
            )

    def finish(
        self,
        evaluation_id: str,
        *,
        status: str,
        result: ProviderResult,
        usage: UsageFacts | None,
        cost: CostEstimate,
        parsed: ParseResult | None,
        order: list[OrderEntry],
        error_message: str | None,
    ) -> None:
        by_label = {o.label: o for o in order}
        preferred_candidate = (
            by_label[parsed.preferred].candidate_id
            if parsed is not None and parsed.ok and parsed.preferred in by_label
            else None
        )
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE evaluations SET status = ?, finished_at = ?, latency_ms = ?, ttft_ms = ?, streamed = ?,"
                " http_status = ?, request_id = ?, finish_reason = ?, raw_finish_reason = ?, response_model = ?,"
                " service_tier = ?, output_text = ?, raw_response_json = ?, raw_usage_json = ?, usage_json = ?,"
                " parse_errors_json = ?, preferred = ?, preferred_candidate_id = ?, preference_explanation = ?,"
                " cost_json = ?, cost_status = ?, cost_total = ?, cost_lower = ?, cost_upper = ?, error_type = ?,"
                " error_message = ?, request_json = ? WHERE id = ?",
                (
                    status,
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
                    scrub_json(result.response_json),
                    scrub_json(result.raw_usage),
                    usage.model_dump_json() if usage else None,
                    json.dumps(parsed.errors if parsed else []),
                    parsed.preferred if parsed and parsed.ok else None,
                    preferred_candidate,
                    parsed.preference_explanation if parsed and parsed.ok else None,
                    *_cost_columns(cost),
                    result.error_type if status != "malformed" else "malformed",
                    sanitize_error_text(error_message),
                    scrub_json(result.request_body),
                    evaluation_id,
                ),
            )
            if parsed is None or not parsed.ok:
                return
            for position, candidate in enumerate(parsed.candidates):
                entry = by_label[candidate.label]
                conn.execute(
                    "INSERT INTO evaluation_candidates (evaluation_id, label, position, attempt_id, candidate_id,"
                    " judge_overall, weighted_overall, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        evaluation_id,
                        candidate.label,
                        position,
                        entry.attempt_id,
                        entry.candidate_id,
                        candidate.judge_overall,
                        candidate.weighted_overall,
                        candidate.explanation,
                    ),
                )
                for key, score in candidate.scores.items():
                    conn.execute(
                        "INSERT INTO evaluation_criteria (evaluation_id, label, criterion_key, score, explanation)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (evaluation_id, candidate.label, key, score, candidate.criterion_explanations.get(key, "")),
                    )

    def recover_interrupted(self) -> int:
        """Mark evaluations left unfinished by a previous process as interrupted (never replayed)."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM evaluations WHERE status IN (?, ?)", ACTIVE).fetchall()
        for row in rows:
            self.finish_unsent_or_sent(
                row["id"],
                "interrupted",
                "The app stopped before this evaluation finished; it was not replayed.",
            )
        return len(rows)
