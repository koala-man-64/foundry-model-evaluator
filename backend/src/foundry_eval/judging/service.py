"""Judge evaluations: anonymized, rubric-based judging of a run's succeeded responses.

Judging runs only on an explicit request or when a suite's automatic judging setting was frozen
into the run. Each evaluation covers one (prompt, trial) of a run, takes its own price snapshot,
shares the run's concurrency limit and budget, and is never retried or replayed automatically.

A position-swap pass is one more explicit request: it re-judges the answers of earlier evaluations
with their presentation order reversed, on the same judge, rubric version and settings, so each swap
pairs with the evaluation it mirrors (see ``analysis.calibration`` for the position-bias numbers).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..credentials.redaction import sanitize_error_text
from ..credentials.store import CredentialStore
from ..db.database import Database
from ..domain.generation import GenerationSettings
from ..domain.profiles import Endpoint, ModelProfile, host_identity
from ..domain.suites import SuiteJudge
from ..execution.budget import (
    body_input_upper_bound,
    estimate_input_tokens,
    input_token_upper_bound,
    worst_case_cost,
)
from ..execution.engine import Engine, NotSent, sent_cost
from ..execution.runs import profile_snapshot
from ..execution.totals import CostTotal, total_costs
from ..money import Money, add
from ..pricing.calculator import CostContext, CostEstimate, estimate_cost
from ..pricing.ratecard import CATEGORY_LABELS, RateCard, RateStatus
from ..pricing.service import PricingService
from ..pricing.snapshots import create_snapshot, load_snapshot
from ..providers.base import Prompt
from ..providers.request_builder import SettingsValidationError, build_body
from ..repos.endpoints import ConflictError, EndpointRepository, NotFoundError
from ..repos.evaluations import Evaluation, EvaluationRepository, OrderEntry
from ..repos.profiles import ProfileRepository
from ..repos.rubrics import RubricRepository, RubricVersion
from ..repos.runs import Attempt, RunCandidate, RunPrompt, RunRepository
from ..repos.settings import SettingsRepository
from ..timeutil import new_id
from ..types import AttemptStatus, FinishReason
from ..usage.normalizers import normalize_usage
from .aggregate import basis_of, is_current, judge_basis, judge_run
from .parse import parse_judge_output
from .prompt import assign_labels, build_judge_prompt, judge_schema, label_in_order, labels_for, new_nonce

log = logging.getLogger(__name__)


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judge_profile_id: str
    settings: GenerationSettings = Field(default_factory=GenerationSettings)
    rubric_id: str | None = None  # the rubric's latest version, unless rubric_version_id is given
    rubric_version_id: str | None = None
    prompt_id: str | None = None  # scope: None means every prompt of the run
    trial_index: int | None = Field(default=None, ge=1)  # scope: None means every trial


class SwapRequest(BaseModel):
    """A position-swap pass: re-judge answers with an earlier evaluation's presentation order reversed.

    The judge, rubric version and settings are those of ``evaluation_id``, so each swap pairs with the
    evaluation it mirrors on the same basis. It is a paid action that runs only on this explicit
    request, through the same preview, budget and idempotency checks as any judging, and it is never
    retried automatically.
    """

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    # evaluation: swap that evaluation only. run: in every (prompt, trial) of the run that has no swap
    # on the same basis yet, swap the most recent evaluation on that basis.
    scope: Literal["evaluation", "run"] = "evaluation"


class GroupPreview(BaseModel):
    prompt_id: str
    prompt_position: int
    prompt_title: str
    trial_index: int
    responses: int
    input_estimate_tokens: int
    input_limit_tokens: int
    oversized: bool
    swap_of: str | None = None  # a swap pass: the evaluation whose order this group reverses


class JudgePreview(BaseModel):
    ok: bool
    errors: list[str]
    warnings: list[str]
    judge_profile_name: str | None
    rubric_version_id: str | None
    rubric_name: str | None
    rubric_version: int | None
    structured_output: bool
    key_configured: bool
    participant_reasons: list[str]
    groups: list[GroupPreview]
    skipped_groups: int
    calls: int
    worst_case_total: Money | None
    unknown_rates: list[str]


class JudgeScore(BaseModel):
    """The latest successful judge result for one attempt (kept apart from manual scores).

    The run view and the run export show this single evaluation; comparisons and calibration use
    the aggregate instead (``JudgeAggregate``).
    """

    evaluation_id: str
    label: str
    weighted_overall: float
    judge_overall: float
    preferred: bool
    tie: bool
    judge_is_participant: bool


class JudgeAggregate(BaseModel):
    """What comparisons use for one answer: its judge evidence on its (prompt, trial)'s chosen basis."""

    mean_weighted_overall: float
    n_evaluations: int
    spread: float | None  # highest minus lowest weighted overall; None from one evaluation
    win_share: float | None  # mean preference share; None if no evaluation compared answers
    evaluation_ids: list[str]  # the evaluations of its (prompt, trial) that are averaged (same answers and basis)
    left_out: int  # succeeded evaluations of its (prompt, trial) that are not (another basis, fewer or retried answers)


class JudgeValidationError(ValueError):
    def __init__(self, preview: JudgePreview) -> None:
        super().__init__("; ".join(preview.errors) or "invalid judge request")
        self.preview = preview


@dataclass
class _Group:
    prompt: RunPrompt
    trial: int
    # (attempt_id, candidate_id, text): in candidate order, or for a swap pass in presentation order.
    responses: list[tuple[str, str, str]]
    swap_of: str | None = None


@dataclass
class _SwapPlan:
    request: JudgeRequest | None  # the source evaluation's judge, rubric version and settings
    basis: str | None
    groups: list[_Group]
    errors: list[str]
    warnings: list[str]
    skipped: int


@dataclass
class _Setup:
    profile: ModelProfile
    endpoint: Endpoint
    rubric: RubricVersion
    rubric_name: str
    card: RateCard
    structured: bool
    input_limit: int


def latest_attempts(attempts: list[Attempt]) -> dict[tuple[str, str, int], Attempt]:
    """The newest attempt per (prompt, candidate, trial); earlier ones were explicitly retried."""
    latest: dict[tuple[str, str, int], Attempt] = {}
    for attempt in attempts:
        key = (attempt.prompt_id, attempt.candidate_id, attempt.trial_index)
        current = latest.get(key)
        if current is None or attempt.created_at >= current.created_at:
            latest[key] = attempt
    return latest


def latest_evaluations(evaluations: list[Evaluation]) -> dict[tuple[str, int], Evaluation]:
    """The newest succeeded evaluation per (prompt, trial); earlier ones were re-judged."""
    latest: dict[tuple[str, int], Evaluation] = {}
    for evaluation in evaluations:
        if evaluation.status != "succeeded":
            continue
        key = (evaluation.prompt_id, evaluation.trial_index)
        current = latest.get(key)
        if current is None or evaluation.created_at >= current.created_at:
            latest[key] = evaluation
    return latest


def judge_scores(evaluations: list[Evaluation]) -> dict[str, JudgeScore]:
    """Latest successful judge result per attempt, from one read of a run's evaluations."""
    scores: dict[str, JudgeScore] = {}
    for evaluation in latest_evaluations(evaluations).values():
        for candidate in evaluation.candidates:
            scores[candidate.attempt_id] = JudgeScore(
                evaluation_id=evaluation.id,
                label=candidate.label,
                weighted_overall=candidate.weighted_overall,
                judge_overall=candidate.judge_overall,
                preferred=evaluation.preferred == candidate.label,
                tie=evaluation.preferred == "tie",
                judge_is_participant=evaluation.judge_is_participant,
            )
    return scores


def participant_reasons(judge: ModelProfile, endpoint: Endpoint, candidates: list[RunCandidate]) -> list[str]:
    """Why the judge may be judging its own output (shown as a warning and stored with the result)."""
    reasons: list[str] = []
    judge_host = host_identity(endpoint.base_url)
    judge_deployment = judge.deployment.strip().lower()
    judge_model = judge.model_name.strip().lower()
    for candidate in candidates:
        snap = candidate.profile_snapshot
        deployment = str(snap.get("deployment") or "").strip().lower()
        model = str(snap.get("model_name") or "").strip().lower()
        candidate_host = host_identity(str((snap.get("endpoint") or {}).get("base_url") or ""))
        if candidate.profile_id == judge.id:
            reasons.append(f"the judge uses the same model profile as {candidate.label}")
        elif deployment and deployment == judge_deployment:
            where = "deployment" if candidate_host == judge_host else "deployment name"
            reasons.append(f"the judge uses the same {where} as {candidate.label}")
        elif judge_model and model == judge_model:
            reasons.append(f"the judge is the same model ({judge.model_name}) as {candidate.label}")
    return reasons


class JudgeService:
    def __init__(
        self,
        db: Database,
        credentials: CredentialStore,
        pricing: PricingService,
        engine: Engine,
        settings: SettingsRepository,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.credentials = credentials
        self.pricing = pricing
        self.engine = engine
        self.settings = settings
        self.rng = rng  # None: secrets.SystemRandom (tests inject a seeded generator)
        self.runs = RunRepository(db)
        self.evaluations = EvaluationRepository(db)
        self.rubrics = RubricRepository(db)
        self.profiles = ProfileRepository(db)
        self.endpoints = EndpointRepository(db)

    # ------------------------------------------------------------------ scope
    def groups(self, run_id: str, prompt_id: str | None, trial_index: int | None) -> tuple[list[_Group], int]:
        """Groups to judge (with at least one succeeded response) and the number skipped."""
        run = self.runs.get(run_id)
        prompts = self.runs.prompts(run_id)
        candidates = self.runs.candidates(run_id)
        latest = latest_attempts(self.runs.attempts(run_id))
        groups: list[_Group] = []
        skipped = 0
        for prompt in prompts:
            if prompt_id is not None and prompt.id != prompt_id:
                continue
            for trial in range(1, run.trials + 1):
                if trial_index is not None and trial != trial_index:
                    continue
                responses: list[tuple[str, str, str]] = []
                for candidate in candidates:
                    attempt = latest.get((prompt.id, candidate.id, trial))
                    if attempt is not None and attempt.status is AttemptStatus.SUCCEEDED:
                        responses.append((attempt.id, candidate.id, attempt.output_text or ""))
                if responses:
                    groups.append(_Group(prompt=prompt, trial=trial, responses=responses))
                else:
                    skipped += 1
        return groups, skipped

    # ------------------------------------------------------------------ setup and preview
    def _resolve(self, request: JudgeRequest, errors: list[str]) -> _Setup | None:
        try:
            profile = self.profiles.get(request.judge_profile_id)
        except NotFoundError:
            errors.append("The judge model profile does not exist.")
            return None
        endpoint = self.endpoints.get(profile.endpoint_id)
        if profile.archived:
            errors.append(f"The judge model profile '{profile.name}' is archived.")
        try:
            if request.rubric_version_id:
                rubric = self.rubrics.version(request.rubric_version_id)
            else:
                rubric = self.rubrics.get(request.rubric_id or "default").latest
            rubric_name = self.rubrics.get(rubric.rubric_id).name
        except NotFoundError:
            errors.append("The rubric does not exist.")
            return None
        context = profile.context_window or self.settings.get().judge_input_limit_tokens
        input_limit = context - (request.settings.max_output_tokens or 0)
        if input_limit <= 0:
            errors.append("The judge's max output tokens leave no room for input in its context window.")
        return _Setup(
            profile=profile,
            endpoint=endpoint,
            rubric=rubric,
            rubric_name=rubric_name,
            card=self.pricing.resolve(profile, endpoint),
            structured=bool(profile.capabilities.structured_output),
            input_limit=input_limit,
        )

    def _judge_prompt(self, group: _Group, setup: _Setup, *, shuffle: bool) -> tuple[list[Any], Prompt, str]:
        if group.swap_of is not None:  # a swap pass: exactly the reversed order, never shuffled
            entries = label_in_order(group.responses)
        elif shuffle:
            entries = assign_labels(group.responses, self.rng)
        else:  # preview: the order does not change the size
            entries = assign_labels(group.responses, random.Random(0))  # noqa: S311 - not security relevant
        texts = [group.prompt.system_text, group.prompt.user_text, group.prompt.reference_text]
        nonce = new_nonce([*texts, *(e.text for e in entries)])
        prompt = build_judge_prompt(
            system_text=group.prompt.system_text,
            user_text=group.prompt.user_text,
            entries=entries,
            rubric=setup.rubric.definition,
            nonce=nonce,
            reference_text=group.prompt.reference_text,
        )
        return entries, prompt, nonce

    @staticmethod
    def _request_body(setup: _Setup, settings: GenerationSettings, prompt: Prompt, labels: list[str]) -> dict[str, Any]:
        """The exact judge request body; the preview bounds its cost with the same body the engine sends."""
        schema = judge_schema(labels, setup.rubric.definition) if setup.structured else None
        return build_body(setup.profile, settings, prompt, json_schema=schema)

    def preview(self, run_id: str, request: JudgeRequest) -> JudgePreview:
        groups, skipped = self.groups(run_id, request.prompt_id, request.trial_index)
        return self._preview(
            run_id, request, groups, skipped, empty_error="There are no succeeded responses to judge in this scope."
        )

    def _preview(
        self,
        run_id: str,
        request: JudgeRequest,
        groups: list[_Group],
        skipped: int,
        *,
        empty_error: str,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        expected_basis: str | None = None,
    ) -> JudgePreview:
        run = self.runs.get(run_id)
        errors = list(errors or [])
        warnings = list(warnings or [])
        if self.engine.generation_active(run_id):
            errors.insert(0, "The run is still generating responses; judge it when it finishes.")
        setup = self._resolve(request, errors)
        if not groups:
            errors.append(empty_error)
        if setup is not None and expected_basis is not None and self._planned_basis(setup, request) != expected_basis:
            errors.append(
                "The judge profile changed since that evaluation (deployment, model, API style or structured "
                "output), so a swap would not be comparable with it."
            )
        if setup is None:
            return JudgePreview(
                ok=False,
                errors=errors,
                warnings=warnings,
                judge_profile_name=None,
                rubric_version_id=None,
                rubric_name=None,
                rubric_version=None,
                structured_output=False,
                key_configured=False,
                participant_reasons=[],
                groups=[],
                skipped_groups=skipped,
                calls=0,
                worst_case_total=None,
                unknown_rates=[],
            )
        key_configured = setup.endpoint.auth_style == "local_cli" or self.credentials.has(setup.endpoint.credential_ref)
        if not key_configured:
            errors.append(f"No API key is stored for the judge's endpoint '{setup.endpoint.name}'.")
        reasons = participant_reasons(setup.profile, setup.endpoint, self.runs.candidates(run_id))
        if reasons:
            warnings.append("The judge also took part in this run: " + "; ".join(reasons) + ".")
        if not setup.structured:
            warnings.append(
                "This judge profile does not declare structured output, so the JSON format is requested in the "
                "instructions only; malformed output is kept for inspection."
            )
        unknown = [
            CATEGORY_LABELS[category] + (f" (> {tier.above_input_tokens} tokens)" if tier.above_input_tokens else "")
            for tier in setup.card.tiers
            for category, entry in tier.categories.items()
            if entry.status is RateStatus.UNKNOWN
        ]
        if unknown:
            warnings.append(f"Judge rates unknown for {', '.join(unknown)}; judging cost estimates will be incomplete.")
        previews: list[GroupPreview] = []
        worst_total: Decimal | None = Decimal(0)
        calls = 0
        dense = 0
        max_out_bound = request.settings.max_output_tokens or setup.profile.max_output_tokens
        for group in groups:
            entries, prompt, _nonce = self._judge_prompt(group, setup, shuffle=False)
            try:
                body: dict[str, Any] | None = self._request_body(
                    setup, request.settings, prompt, [e.label for e in entries]
                )
            except SettingsValidationError as exc:
                if not any(e.startswith("Judge settings:") for e in errors):
                    errors.extend(f"Judge settings: {issue.message}" for issue in exc.issues)
                body = None
            estimate = estimate_input_tokens(prompt.system, prompt.user)
            oversized = estimate > setup.input_limit
            previews.append(
                GroupPreview(
                    prompt_id=group.prompt.id,
                    prompt_position=group.prompt.position,
                    prompt_title=group.prompt.title or group.prompt.user_text[:60],
                    trial_index=group.trial,
                    responses=len(group.responses),
                    input_estimate_tokens=estimate,
                    input_limit_tokens=setup.input_limit,
                    oversized=oversized,
                    swap_of=group.swap_of,
                )
            )
            if oversized:
                continue
            calls += 1
            # The same bound the engine reserves before sending: the JSON body, schema included.
            upper = (
                body_input_upper_bound(body)
                if body is not None
                else input_token_upper_bound(prompt.system, prompt.user)
            )
            if upper > setup.input_limit:
                dense += 1
            worst, _reason = worst_case_cost(setup.card, upper, max_out_bound)
            worst_total = None if worst is None or worst_total is None else add(worst_total, worst)
        oversized_count = sum(1 for g in previews if g.oversized)
        if oversized_count:
            warnings.append(
                f"{oversized_count} of {len(previews)} evaluations exceed the judge's input limit; they will be "
                "recorded as rejected without sending anything (nothing is truncated)."
            )
        if dense:
            warnings.append(
                f"{dense} evaluation{'s' if dense != 1 else ''} fit the input limit by estimate only. Very "
                "token-dense text could still exceed the judge's context window; the provider would then reject "
                "the call, and it is recorded as rejected (nothing is truncated)."
            )
        if run.budget is not None and calls:
            run_currency = self.runs.run_currency(run_id, setup.card.currency)
            if worst_total is None:
                errors.append(
                    "This run has a budget: set the judge's max output tokens and complete its rates so each "
                    "call's cost is bounded."
                )
            elif setup.card.currency != run_currency:
                errors.append(
                    f"This run's budget is in {run_currency}, but the judge's rates are in {setup.card.currency}."
                )
            else:
                remaining = run.budget - self.engine.committed(run_id)
                if worst_total > remaining:
                    warnings.append(
                        f"The worst case ({worst_total} {run_currency}) is above the run's remaining budget "
                        f"({max(remaining, Decimal(0))} {run_currency}). Evaluations that do not fit are skipped "
                        "and cost nothing."
                    )
        return JudgePreview(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            judge_profile_name=setup.profile.name,
            rubric_version_id=setup.rubric.id,
            rubric_name=setup.rubric_name,
            rubric_version=setup.rubric.version,
            structured_output=setup.structured,
            key_configured=key_configured,
            participant_reasons=reasons,
            groups=previews,
            skipped_groups=skipped,
            calls=calls,
            worst_case_total=worst_total,
            unknown_rates=unknown,
        )

    # ------------------------------------------------------------------ creation
    def create(
        self, run_id: str, request: JudgeRequest, idempotency_key: str, *, trigger: Literal["manual", "auto"] = "manual"
    ) -> tuple[list[str], bool]:
        """Create evaluations (queued, or rejected when oversized). Returns (ids, created)."""
        existing = self._existing(idempotency_key, swap=False)
        if existing:
            return existing, False
        preview = self.preview(run_id, request)
        if not preview.ok:
            raise JudgeValidationError(preview)
        setup = self._setup_for_create(request, preview)
        groups, _skipped = self.groups(run_id, request.prompt_id, request.trial_index)
        return self._insert(run_id, request, setup, groups, idempotency_key, trigger)

    def _existing(self, idempotency_key: str, *, swap: bool) -> list[str]:
        """Evaluations already created with this key; a key used for the other kind of request conflicts."""
        ids = self.evaluations.ids_for_key(idempotency_key)
        if ids and (self.evaluations.get(ids[0]).swap_of is not None) != swap:
            raise ConflictError("This Idempotency-Key was already used for a different action.")
        return ids

    def _setup_for_create(self, request: JudgeRequest, preview: JudgePreview) -> _Setup:
        errors: list[str] = []
        setup = self._resolve(request, errors)
        if setup is None or errors:
            raise JudgeValidationError(preview.model_copy(update={"ok": False, "errors": errors}))
        return setup

    def _insert(
        self,
        run_id: str,
        request: JudgeRequest,
        setup: _Setup,
        groups: list[_Group],
        idempotency_key: str,
        trigger: Literal["manual", "auto"],
    ) -> tuple[list[str], bool]:
        reasons = participant_reasons(setup.profile, setup.endpoint, self.runs.candidates(run_id))
        snapshot_info = profile_snapshot(setup.profile, setup.endpoint)
        currency = setup.card.currency
        batch_id = new_id()
        ids: list[str] = []
        with self.db.transaction() as conn:
            # Checked again under the write lock: concurrent requests with one key create one batch.
            rows = conn.execute(
                "SELECT id, swap_of FROM evaluations WHERE idempotency_key = ? ORDER BY created_at", (idempotency_key,)
            ).fetchall()
            sources = [g.swap_of for g in groups if g.swap_of is not None]
            if rows:
                if (rows[0]["swap_of"] is not None) != bool(sources):
                    raise ConflictError("This Idempotency-Key was already used for a different action.")
                return [row["id"] for row in rows], False
            if sources:
                # A swap pass re-checks under the lock that no other request swapped its sources meanwhile.
                swapped = EvaluationRepository.swapped(conn, sources)
                groups = [g for g in groups if g.swap_of not in swapped]
                if not groups:
                    raise ConflictError("Every evaluation in this scope was swapped meanwhile; nothing was started.")
            price_snapshot_id = create_snapshot(conn, setup.profile, setup.endpoint, setup.card)
            for group in groups:
                entries, prompt, _nonce = self._judge_prompt(group, setup, shuffle=True)
                body = self._request_body(setup, request.settings, prompt, [e.label for e in entries])
                estimate = estimate_input_tokens(prompt.system, prompt.user)
                rejected: tuple[str, CostEstimate] | None = None
                if estimate > setup.input_limit:
                    rejected = (
                        f"The judge input is about {estimate:,} tokens (estimate), above the limit of "
                        f"{setup.input_limit:,} tokens (context window minus max output tokens). Nothing was sent "
                        "and nothing was truncated.",
                        CostEstimate.not_sent(currency),
                    )
                ids.append(
                    self.evaluations.insert(
                        conn,
                        run_id=run_id,
                        prompt_id=group.prompt.id,
                        trial_index=group.trial,
                        batch_id=batch_id,
                        idempotency_key=idempotency_key,
                        trigger=trigger,
                        judge_profile_id=setup.profile.id,
                        judge_profile_snapshot=snapshot_info,
                        judge_settings=request.settings,
                        rubric_version_id=setup.rubric.id,
                        price_snapshot_id=price_snapshot_id,
                        structured_output=setup.structured,
                        participant_reasons=reasons,
                        order=[
                            OrderEntry(label=e.label, attempt_id=e.attempt_id, candidate_id=e.candidate_id)
                            for e in entries
                        ],
                        input_estimate_tokens=estimate,
                        input_limit_tokens=setup.input_limit,
                        request_body=body,
                        rejected=rejected,
                        swap_of=group.swap_of,
                    )
                )
        return ids, True

    # ------------------------------------------------------------------ position-swap pass
    @staticmethod
    def _planned_basis(setup: _Setup, request: JudgeRequest) -> str:
        """The basis new evaluations with this setup would have (as judge_basis() reads it back)."""
        snapshot = profile_snapshot(setup.profile, setup.endpoint)
        return basis_of(setup.rubric.id, setup.profile.id, snapshot, setup.structured, request.settings)

    def _swap_plan(self, run_id: str, swap: SwapRequest) -> _SwapPlan:
        """The groups a swap pass would judge: each source evaluation's answers in reversed order."""
        self.runs.get(run_id)
        try:
            given = self.evaluations.get(swap.evaluation_id)
        except NotFoundError:
            given = None
        if given is None or given.run_id != run_id:
            return _SwapPlan(None, None, [], ["The evaluation to swap does not exist in this run."], [], 0)
        if given.status != "succeeded":
            return _SwapPlan(None, None, [], ["Only a succeeded evaluation can be swapped."], [], 0)
        request = JudgeRequest(
            judge_profile_id=given.judge_profile_id,
            settings=given.judge_settings,
            rubric_version_id=given.rubric_version_id,
        )
        basis = judge_basis(given)
        attempts = latest_attempts(self.runs.attempts(run_id))
        current = {a.id: a for a in attempts.values()}
        if swap.scope == "evaluation":
            sources = [given]
        else:
            sources = [
                e
                for e in self.evaluations.list_for_run(run_id)
                if e.status == "succeeded" and e.swap_of is None and judge_basis(e) == basis
            ]
        with self.db.connect() as conn:
            swapped = EvaluationRepository.swapped(conn, [e.id for e in sources])
        current_ids = set(current)
        # A run-wide pass swaps each (prompt, trial) once: one that already has a swap of its current answers
        # on this basis is done (a swap of answers retried since does not count).
        done = (
            {(e.prompt_id, e.trial_index) for e in sources if e.id in swapped and is_current(e, current_ids)}
            if swap.scope == "run"
            else set()
        )
        reasons = {"swap": 0, "single": 0, "retried": 0, "swapped": 0, "done": 0, "older": 0}
        chosen: dict[tuple[str, int], Evaluation] = {}
        for source in sources:
            if source.swap_of is not None:
                reasons["swap"] += 1
            elif len(source.order) < 2:
                reasons["single"] += 1
            elif not is_current(source, current_ids):
                reasons["retried"] += 1
            elif source.id in swapped:
                reasons["swapped"] += 1
            elif (source.prompt_id, source.trial_index) in done:
                reasons["done"] += 1
            else:
                slot = (source.prompt_id, source.trial_index)
                previous = chosen.get(slot)
                if previous is not None:
                    reasons["older"] += 1  # one swap per prompt and trial: the most recent evaluation
                if previous is None or (source.created_at, source.id) > (previous.created_at, previous.id):
                    chosen[slot] = source
        explained = {
            "swap": "it is itself a swap pass (swap the evaluation it mirrors instead)",
            "single": "it judged fewer than two answers, so there is no order to reverse",
            "retried": "an answer it judged was retried since",
            "swapped": "it already has a swap pass (queued, running or succeeded)",
            "done": "its prompt and trial already have a swap pass on this basis (swap it on its own if you want more)",
            "older": "a more recent evaluation of the same prompt and trial is swapped instead",
        }
        errors: list[str] = []
        warnings: list[str] = []
        if swap.scope == "evaluation":
            errors += [f"This evaluation cannot be swapped: {explained[k]}." for k, n in reasons.items() if n]
        else:
            left = [f"{n} because {explained[k]}" for k, n in reasons.items() if n]
            if left:
                warnings.append("Evaluations left out of this swap pass: " + "; ".join(left) + ".")
        prompts = {p.id: p for p in self.runs.prompts(run_id)}
        groups = [
            _Group(
                prompt=prompts[source.prompt_id],
                trial=source.trial_index,
                responses=[
                    (entry.attempt_id, entry.candidate_id, current[entry.attempt_id].output_text or "")
                    for entry in reversed(source.order)
                ],
                swap_of=source.id,
            )
            for source in sorted(chosen.values(), key=lambda e: (prompts[e.prompt_id].position, e.trial_index))
        ]
        return _SwapPlan(request, basis, groups, errors, warnings, sum(reasons.values()))

    def preview_swap(self, run_id: str, swap: SwapRequest) -> JudgePreview:
        plan = self._swap_plan(run_id, swap)
        if plan.request is None:
            return JudgePreview(
                ok=False,
                errors=plan.errors,
                warnings=[],
                judge_profile_name=None,
                rubric_version_id=None,
                rubric_name=None,
                rubric_version=None,
                structured_output=False,
                key_configured=False,
                participant_reasons=[],
                groups=[],
                skipped_groups=0,
                calls=0,
                worst_case_total=None,
                unknown_rates=[],
            )
        return self._preview(
            run_id,
            plan.request,
            plan.groups,
            plan.skipped,
            empty_error="There is nothing to swap in this scope.",
            errors=plan.errors,
            warnings=plan.warnings,
            expected_basis=plan.basis,
        )

    def create_swap(self, run_id: str, swap: SwapRequest, idempotency_key: str) -> tuple[list[str], bool]:
        """Create a swap pass (a paid action, only on this explicit request). Returns (ids, created)."""
        existing = self._existing(idempotency_key, swap=True)
        if existing:
            return existing, False
        preview = self.preview_swap(run_id, swap)
        if not preview.ok:
            raise JudgeValidationError(preview)
        plan = self._swap_plan(run_id, swap)
        assert plan.request is not None  # the preview was ok
        setup = self._setup_for_create(plan.request, preview)
        # The evaluations are written from this setup, so its basis is the one they will have.
        if self._planned_basis(setup, plan.request) != plan.basis:
            raise JudgeValidationError(
                preview.model_copy(
                    update={
                        "ok": False,
                        "errors": ["The judge profile changed since the preview; preview the swap again."],
                    }
                )
            )
        return self._insert(run_id, plan.request, setup, plan.groups, idempotency_key, "manual")

    def start(self, run_id: str, evaluation_ids: list[str]) -> None:
        for evaluation_id in evaluation_ids:
            if self.evaluations.get(evaluation_id).status == "queued":
                self.engine.track_side(run_id, self.run_evaluation(run_id, evaluation_id), f"judge-{evaluation_id}")

    # ------------------------------------------------------------------ execution
    async def run_evaluation(self, run_id: str, evaluation_id: str) -> None:
        try:
            await self._execute(run_id, evaluation_id)
        except asyncio.CancelledError:
            if self.engine.shutting_down:
                status, message = "interrupted", "The app stopped before this evaluation finished; it was not replayed."
            else:
                status, message = (
                    "cancelled",
                    "Cancelled. If the request had already been sent, the provider may still bill it.",
                )
            self._finish_if_active(evaluation_id, status, message)
            raise
        except Exception as exc:
            log.exception("evaluation %s failed unexpectedly", evaluation_id)
            message = sanitize_error_text(f"Internal error: {exc}") or "Internal error"
            self._finish_if_active(evaluation_id, "failed", message)
        finally:
            self.engine.finish_budget(run_id, evaluation_id)

    def _finish_if_active(self, evaluation_id: str, status: str, message: str) -> None:
        try:
            evaluation = self.evaluations.get(evaluation_id)
        except NotFoundError:
            return
        if evaluation.status in ("queued", "running"):
            self.evaluations.finish_unsent_or_sent(evaluation_id, status, message)

    async def _execute(self, run_id: str, evaluation_id: str) -> None:
        evaluation = self.evaluations.get(evaluation_id)
        if evaluation.status != "queued":
            return
        with self.db.connect() as conn:
            card, _meta = load_snapshot(conn, evaluation.price_snapshot_id)
        snapshot = dict(evaluation.judge_profile_snapshot)
        endpoint_info = snapshot.pop("endpoint")
        profile = ModelProfile.model_validate(snapshot)
        body, _raw, _usage = self.evaluations.raw(evaluation_id)
        outcome = await self.engine.send(
            run_id,
            evaluation_id,
            profile=profile,
            endpoint_info=endpoint_info,
            body=body,
            card=card,
            input_upper=body_input_upper_bound(body),
            max_output_tokens=evaluation.judge_settings.max_output_tokens or profile.max_output_tokens,
            mark_sent=functools.partial(self.evaluations.mark_sent, evaluation_id),
        )
        if isinstance(outcome, NotSent):
            self.evaluations.finish_not_sent(evaluation_id, outcome.status.value, outcome.message, card.currency)
            return
        result = outcome
        facts = normalize_usage(profile.api_style, result.raw_usage, service_tier=result.service_tier)
        if facts is None:
            reserved = self.evaluations.get(evaluation_id).budget_reserved
            cost = CostEstimate.usage_missing(card.currency, "usage not reported by the provider", reserved)
        else:
            cost = estimate_cost(
                facts, card, CostContext(requested_cache_ttl=evaluation.judge_settings.prompt_cache_ttl)
            )
        if not result.ok:
            status = "rejected_oversized" if result.finish_reason is FinishReason.CONTEXT_EXCEEDED else "failed"
            self.evaluations.finish(
                evaluation_id,
                status=status,
                result=result,
                usage=facts,
                cost=cost,
                parsed=None,
                order=evaluation.order,
                error_message=result.error_message,
            )
        else:
            rubric = self.rubrics.version(evaluation.rubric_version_id).definition
            parsed = parse_judge_output(
                result.output_text,
                [entry.label for entry in evaluation.order],
                rubric,
                stopped_at_limit=result.finish_reason is FinishReason.MAX_TOKENS,
            )
            self.evaluations.finish(
                evaluation_id,
                status="succeeded" if parsed.ok else "malformed",
                result=result,
                usage=facts,
                cost=cost,
                parsed=parsed,
                order=evaluation.order,
                error_message=None
                if parsed.ok
                else "The judge output did not match the expected format; see the problems and the raw output.",
            )
        self.engine.settle(run_id, evaluation_id, cost)

    # ------------------------------------------------------------------ automatic judging
    def freeze_suite_judge(self, judge: SuiteJudge) -> dict[str, Any]:
        """The suite's judging setting as frozen into a run (the rubric's current version)."""
        rubric = self.rubrics.get(judge.rubric_id)
        return {
            "judge_profile_id": judge.judge_profile_id,
            "settings": judge.settings.model_dump(mode="json", exclude_none=True),
            "rubric_version_id": rubric.latest.id,
        }

    def validate_frozen_config(self, config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            request = JudgeRequest(
                judge_profile_id=str(config.get("judge_profile_id") or ""),
                settings=GenerationSettings.model_validate(config.get("settings") or {}),
                rubric_version_id=str(config.get("rubric_version_id") or "") or None,
            )
        except ValueError as exc:
            return [f"Automatic judging: {exc}"]
        setup = self._resolve(request, errors)
        if setup is not None:
            if setup.endpoint.auth_style != "local_cli" and not self.credentials.has(setup.endpoint.credential_ref):
                errors.append(f"No API key is stored for the judge's endpoint '{setup.endpoint.name}'.")
            labels = labels_for(2)
            try:
                build_body(
                    setup.profile,
                    request.settings,
                    Prompt(user="check", system="check"),
                    json_schema=judge_schema(labels, setup.rubric.definition) if setup.structured else None,
                )
            except SettingsValidationError as exc:
                errors.extend(f"Judge settings: {issue.message}" for issue in exc.issues)
        return [f"Automatic judging: {e}" for e in errors]

    async def auto_judge(self, run_id: str) -> None:
        """Engine hook after generation: judge the run if its suite asked for it (only once)."""
        run = self.runs.get(run_id)
        config = run.judge_config
        if not config or self.engine.is_cancelled(run_id) or self.evaluations.has_trigger(run_id, "auto"):
            return
        try:
            request = JudgeRequest(
                judge_profile_id=str(config.get("judge_profile_id") or ""),
                settings=GenerationSettings.model_validate(config.get("settings") or {}),
                rubric_version_id=config.get("rubric_version_id"),
            )
            ids, _created = self.create(run_id, request, f"auto:{run_id}", trigger="auto")
        except (JudgeValidationError, NotFoundError, ValueError) as exc:
            self.runs.set_judge_error(run_id, f"Automatic judging did not start: {exc}")
            return
        queued = [i for i in ids if self.evaluations.get(i).status == "queued"]
        await asyncio.gather(*(self.run_evaluation(run_id, i) for i in queued))

    # ------------------------------------------------------------------ costs and summaries
    def judging_total(self, run_id: str, currency: str) -> CostTotal:
        return total_costs(self.evaluations.costs_for_run(run_id), currency)

    def spent(self, run_id: str) -> Decimal:
        total = Decimal(0)
        for sent_at, cost, reserved in self.evaluations.spending_for_run(run_id):
            total = add(total, sent_cost(sent_at, cost, reserved))
        return total

    def scores_for_run(self, run_id: str) -> dict[str, JudgeScore]:
        """Latest successful judge result per attempt."""
        return judge_scores(self.evaluations.list_for_run(run_id))

    def aggregates_for_run(self, run_id: str) -> dict[str, JudgeAggregate]:
        """The judge evidence comparisons use, per current attempt (see ``judging.aggregate``)."""
        current = {a.id for a in latest_attempts(self.runs.attempts(run_id)).values()}
        judged = judge_run(self.evaluations.list_for_run(run_id), current)
        return {
            attempt_id: JudgeAggregate(
                mean_weighted_overall=answer.mean,
                n_evaluations=answer.evaluations,
                spread=answer.spread,
                win_share=answer.win_share,
                evaluation_ids=[e.id for e in slot.evaluations],
                left_out=slot.left_out + judged.stale.get((slot.prompt_id, slot.trial_index), 0),
            )
            for attempt_id, (answer, slot) in judged.answers().items()
        }
