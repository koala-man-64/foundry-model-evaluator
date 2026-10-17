"""Runs: preview, creation, progress, history, cancellation, explicit retries, manual scores."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from ...domain.generation import GenerationSettings
from ...execution.runs import RunPreview, RunRequest
from ...execution.totals import CostTotal, combine_totals, total_costs
from ...grading.checks import CheckOutcome, RegexBudget, run_checks
from ...judging.service import JudgeAggregate, JudgeScore
from ...money import Money
from ...pricing.ratecard import RateCard
from ...pricing.snapshots import load_snapshot
from ...repos.runs import Attempt, ManualScore, Run, RunPrompt
from ...types import AttemptStatus
from ..deps import IdempotencyKey, ServicesDep

router = APIRouter(prefix="/api", tags=["runs"])


class RunCandidateOut(BaseModel):
    id: str
    position: int
    label: str
    profile_id: str
    profile_name: str
    endpoint_name: str
    api_style: str
    deployment: str
    model_name: str
    settings: GenerationSettings
    candidate_key: str
    price_snapshot_id: str


class BudgetOut(BaseModel):
    limit: Money
    committed: Money


class RunDetail(BaseModel):
    run: Run
    prompts: list[RunPrompt]
    candidates: list[RunCandidateOut]
    attempts: list[Attempt]
    manual_scores: dict[str, ManualScore]
    generation_cost: CostTotal
    judging_cost: CostTotal
    combined_cost: CostTotal
    active: bool
    budget: BudgetOut | None
    # The latest succeeded judge result per attempt (what the run view and the export show).
    judge_scores: dict[str, JudgeScore] = Field(default_factory=dict)
    # What comparisons and calibration use instead: every evaluation on its prompt and trial's basis.
    judge_aggregates: dict[str, JudgeAggregate] = Field(default_factory=dict)
    evaluation_counts: dict[str, int] = Field(default_factory=dict)
    # Deterministic checks on each succeeded answer whose prompt has checks (computed, free).
    check_results: dict[str, CheckOutcome] = Field(default_factory=dict)


class RunSummary(BaseModel):
    run: Run
    candidate_labels: list[str]
    counts: dict[str, int]
    generation_cost: CostTotal
    judging_cost: CostTotal
    combined_cost: CostTotal


class Progress(BaseModel):
    status: str
    counts: dict[str, int]
    total: int
    active: bool
    generation_cost: CostTotal


class CreatedRun(BaseModel):
    run_id: str
    created: bool


class RetryOut(BaseModel):
    attempt_ids: list[str]


class RawOut(BaseModel):
    request_body: dict[str, Any]
    raw_response: Any
    raw_usage: Any


class ManualScoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=5)
    notes: str = Field(default="", max_length=5000)


class SnapshotOut(BaseModel):
    meta: dict[str, Any]
    card: RateCard


def _candidate_out(c: Any) -> RunCandidateOut:
    snap = c.profile_snapshot
    return RunCandidateOut(
        id=c.id,
        position=c.position,
        label=c.label,
        profile_id=c.profile_id,
        profile_name=snap.get("name", ""),
        endpoint_name=(snap.get("endpoint") or {}).get("name", ""),
        api_style=snap.get("api_style", ""),
        deployment=snap.get("deployment", ""),
        model_name=snap.get("model_name", ""),
        settings=c.settings,
        candidate_key=c.candidate_key,
        price_snapshot_id=c.price_snapshot_id,
    )


def judging_total(services: ServicesDep, run_id: str, currency: str) -> CostTotal:
    return services.judge.judging_total(run_id, currency)


def run_detail(services: ServicesDep, run_id: str, regex_budget: RegexBudget | None = None) -> RunDetail:
    run = services.runs.get(run_id)
    currency = services.runs.run_currency(run_id, services.settings.get().currency)
    attempts = services.runs.attempts(run_id)
    generation = total_costs([a.cost for a in attempts], currency)
    judging = judging_total(services, run_id, currency)
    budget = (
        BudgetOut(limit=run.budget, committed=services.engine.committed(run_id)) if run.budget is not None else None
    )
    evaluation_counts: dict[str, int] = {}
    for evaluation in services.evaluations.list_for_run(run_id):
        evaluation_counts[evaluation.status] = evaluation_counts.get(evaluation.status, 0) + 1
    prompts = services.runs.prompts(run_id)
    checks = {p.id: p.checks for p in prompts if p.checks}
    pattern_budget = regex_budget or RegexBudget()
    check_results = {
        a.id: run_checks(checks[a.prompt_id], a.output_text or "", budget=pattern_budget)
        for a in attempts
        if a.status is AttemptStatus.SUCCEEDED and a.prompt_id in checks
    }
    return RunDetail(
        run=run,
        prompts=prompts,
        candidates=[_candidate_out(c) for c in services.runs.candidates(run_id)],
        attempts=attempts,
        manual_scores=services.runs.manual_scores(run_id),
        generation_cost=generation,
        judging_cost=judging,
        combined_cost=combine_totals(generation, judging),
        active=services.engine.is_active(run_id),
        budget=budget,
        judge_scores=services.judge.scores_for_run(run_id),
        judge_aggregates=services.judge.aggregates_for_run(run_id),
        evaluation_counts=evaluation_counts,
        check_results=check_results,
    )


@router.post("/runs/preview", response_model=RunPreview)
def preview_run(payload: RunRequest, services: ServicesDep) -> RunPreview:
    return services.run_service.preview(payload)


@router.post("/runs", response_model=CreatedRun, status_code=201)
def create_run(payload: RunRequest, key: IdempotencyKey, services: ServicesDep, response: Response) -> CreatedRun:
    run_id, created = services.run_service.create(payload, key)
    if created:
        services.engine.start(run_id)
    else:
        response.status_code = 200
    return CreatedRun(run_id=run_id, created=created)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    services: ServicesDep, kind: str | None = None, suite_id: str | None = None, limit: int = 100
) -> list[RunSummary]:
    default_currency = services.settings.get().currency
    summaries: list[RunSummary] = []
    for run in services.runs.list_runs(limit=min(max(limit, 1), 500), kind=kind, suite_id=suite_id):
        currency = services.runs.run_currency(run.id, default_currency)
        attempts = services.runs.attempts(run.id)
        generation = total_costs([a.cost for a in attempts], currency)
        judging = judging_total(services, run.id, currency)
        counts: dict[str, int] = {}
        for attempt in attempts:
            counts[attempt.status.value] = counts.get(attempt.status.value, 0) + 1
        summaries.append(
            RunSummary(
                run=run,
                candidate_labels=[c.label for c in services.runs.candidates(run.id)],
                counts=counts,
                generation_cost=generation,
                judging_cost=judging,
                combined_cost=combine_totals(generation, judging),
            )
        )
    return summaries


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, services: ServicesDep) -> RunDetail:
    return run_detail(services, run_id)


@router.get("/runs/{run_id}/progress", response_model=Progress)
def get_progress(run_id: str, services: ServicesDep) -> Progress:
    run = services.runs.get(run_id)
    attempts = services.runs.attempts(run_id)
    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.status.value] = counts.get(attempt.status.value, 0) + 1
    return Progress(
        status=run.status.value,
        counts=counts,
        total=len(attempts),
        active=services.engine.is_active(run_id),
        generation_cost=total_costs(
            [a.cost for a in attempts], services.runs.run_currency(run_id, services.settings.get().currency)
        ),
    )


@router.post("/runs/{run_id}/cancel", response_model=Progress)
async def cancel_run(run_id: str, services: ServicesDep) -> Progress:
    services.runs.get(run_id)
    await services.engine.cancel(run_id)
    return get_progress(run_id, services)


@router.post("/runs/{run_id}/retry-failed", response_model=RetryOut)
def retry_failed(run_id: str, key: IdempotencyKey, services: ServicesDep) -> RetryOut:
    services.runs.get(run_id)
    retryable = {AttemptStatus.FAILED, AttemptStatus.CANCELLED, AttemptStatus.INTERRUPTED, AttemptStatus.SKIPPED_BUDGET}

    def work(conn: sqlite3.Connection) -> list[str]:
        attempts = services.runs.attempts(run_id)
        retried = {a.retry_of for a in attempts if a.retry_of}
        # A no-answer response (tool call, empty text) was billed and would most likely repeat with the
        # same body, so the bulk action leaves it to an explicit single retry.
        return [
            services.run_service.create_retry(a.id, conn)
            for a in attempts
            if a.status in retryable and a.id not in retried and a.error_type != "no_answer"
        ]

    new_ids, created = services.action_keys.run_once(key, f"retry-failed:{run_id}", work)
    if created and new_ids:
        services.engine.start(run_id, new_ids)
    return RetryOut(attempt_ids=new_ids)


@router.post("/attempts/{attempt_id}/retry", response_model=RetryOut)
def retry_attempt(attempt_id: str, key: IdempotencyKey, services: ServicesDep) -> RetryOut:
    attempt = services.runs.attempt(attempt_id)
    new_ids, created = services.action_keys.run_once(
        key, f"retry:{attempt_id}", lambda conn: [services.run_service.create_retry(attempt_id, conn)]
    )
    if created:
        services.engine.start(attempt.run_id, new_ids)
    return RetryOut(attempt_ids=new_ids)


@router.get("/attempts/{attempt_id}/raw", response_model=RawOut)
def attempt_raw(attempt_id: str, services: ServicesDep) -> RawOut:
    attempt = services.runs.attempt(attempt_id)
    raw_response, raw_usage = services.runs.raw_response(attempt_id)
    return RawOut(request_body=attempt.request_body, raw_response=raw_response, raw_usage=raw_usage)


@router.put("/attempts/{attempt_id}/manual-score", response_model=ManualScore)
def put_manual_score(attempt_id: str, payload: ManualScoreIn, services: ServicesDep) -> ManualScore:
    return services.runs.set_manual_score(attempt_id, payload.score, payload.notes)


@router.delete("/attempts/{attempt_id}/manual-score", status_code=204)
def delete_manual_score(attempt_id: str, services: ServicesDep) -> Response:
    services.runs.delete_manual_score(attempt_id)
    return Response(status_code=204)


@router.get("/price-snapshots/{snapshot_id}", response_model=SnapshotOut)
def get_snapshot(snapshot_id: str, services: ServicesDep) -> SnapshotOut:
    with services.db.connect() as conn:
        card, meta = load_snapshot(conn, snapshot_id)
    return SnapshotOut(meta=meta, card=card)
