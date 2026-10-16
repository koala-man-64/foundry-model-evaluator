"""Compare candidates across runs on matching prompts and trials, with visible coverage.

A cell is (prompt content hash, trial index). Candidates are identified by their candidate key
(pricing identity + exact settings), so the same candidate can be followed across runs of a suite.

- A cell is *matched* when every selected candidate has a succeeded attempt in it.
- Each metric is averaged within a cell first, so a cell repeated across runs counts once. The cell
  means are then averaged over the metric's *common cells*: the matched cells in which every
  candidate that has the metric at all has a value. Averages shown side by side therefore cover the
  same prompts and trials, and each metric reports how many cells it used. A candidate with no
  value in any matched cell shows no average for that metric.
- Costs use complete estimates in the requested currency only; the others are counted as excluded.
- The judge win rate is averaged per cell in the same way. A tie gives each of the n judged answers
  1/n, so the shares of one evaluation always sum to 1.
- Judge scores and wins are comparable only when they share a scoring basis: the same rubric version,
  the same judge (profile, deployment, model, API style, structured output) and the same settings that
  change how it scores. If the cells that would be averaged mix bases, judge averages and win rates
  are withheld (with a note) instead of averaging different scales or judges together.
- Checks: the share of answers that pass every deterministic check of their prompt, averaged per cell
  like the scores, over the matched cells whose prompts have checks. It is a separate score source,
  never blended with manual or judge scores. An answer a check could not be evaluated on counts as
  unknown, not as a fail.
- A cell repeated across runs must have been graded the same way: a cell whose runs used different
  checks is left out of the checks metric, and one whose runs gave the judge different reference
  answers is left out of the judge metrics, each with a note.
- Failed attempts whose provider reported usage were billed; they are counted per candidate because
  their cost is in the run totals but, as failures, not in the averages.
- Failures are counted over all cells, for finished attempts only.
- Each average also gets a 95% interval across prompts (trials of a prompt are averaged first), from
  ``stats.MIN_PROMPTS`` prompts on. With a baseline candidate, each other candidate gets the paired
  difference to it over the prompts both have, with a verdict (higher / lower / cannot tell).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from ..db.database import Database
from ..grading.checks import checks_json, run_checks
from ..judging.service import JudgeService, judge_scores, latest_attempts, latest_evaluations
from ..money import Money, add, div
from ..repos.endpoints import NotFoundError
from ..repos.evaluations import Evaluation, EvaluationRepository
from ..repos.rubrics import RubricRepository
from ..repos.runs import Attempt, RunRepository
from ..types import AttemptStatus
from . import stats

Cell = tuple[str, int]

_FAILED = frozenset(
    {AttemptStatus.FAILED, AttemptStatus.CANCELLED, AttemptStatus.INTERRUPTED, AttemptStatus.SKIPPED_BUDGET}
)
_PENDING = frozenset({AttemptStatus.QUEUED, AttemptStatus.RUNNING})


class Interval(BaseModel):
    """95% interval for the mean across prompts (trials of a prompt are averaged first).

    ``mean`` is that per-prompt mean, the centre of the interval. It can differ from the cell average
    shown beside it when prompts have different numbers of matched trials.
    """

    mean: float
    low: float
    high: float
    prompts: int


class MoneyInterval(BaseModel):
    mean: Money
    low: Money
    high: Money
    prompts: int


class Difference(BaseModel):
    """Candidate minus baseline over the prompts both have. No interval below the minimum prompt count."""

    mean: float
    low: float | None
    high: float | None
    prompts: int
    verdict: stats.Verdict


class MoneyDifference(BaseModel):
    mean: Money
    low: Money | None
    high: Money | None
    prompts: int
    verdict: stats.Verdict


class BaselineDifferences(BaseModel):
    cost: MoneyDifference | None
    latency_ms: Difference | None
    manual: Difference | None
    judge: Difference | None
    win_rate: Difference | None
    checks: Difference | None = None


class ComparisonCandidate(BaseModel):
    candidate_key: str
    label: str
    profile_name: str
    # Who makes the model (the profile's publisher, else its API style), for colouring charts.
    publisher: str
    api_style: str
    runs: int
    attempts: int
    succeeded: int
    failures: int
    # Failures whose provider reported token usage, so they were billed (in the run totals, not the averages).
    failures_billed: int
    pending: int
    covered_cells: int
    avg_cost: Money | None
    cost_cells: int
    cost_excluded: int
    cost_other_currency: int
    avg_latency_ms: float | None
    latency_cells: int
    avg_input_tokens: float | None
    avg_output_tokens: float | None
    token_cells: int
    avg_reasoning_tokens: float | None
    reasoning_cells: int
    avg_manual_score: float | None
    manual_scored: int
    avg_judge_score: float | None
    judge_scored: int
    judge_wins: float
    judge_comparisons: int
    win_cells: int
    win_rate: float | None
    custom_rates: bool
    avg_checks_passed: float | None = None
    checks_cells: int = 0
    checks_interval: Interval | None = None
    cost_interval: MoneyInterval | None = None
    latency_interval: Interval | None = None
    manual_interval: Interval | None = None
    judge_interval: Interval | None = None
    win_rate_interval: Interval | None = None
    # Paired differences to the baseline candidate (none for the baseline itself or without one).
    vs_baseline: BaselineDifferences | None = None


class ComparisonCell(BaseModel):
    content_hash: str
    prompt_title: str
    trial_index: int
    matched: bool
    missing: list[str]


class Comparison(BaseModel):
    run_ids: list[str]
    currency: str
    cells_total: int
    cells_matched: int
    # Distinct judge scoring bases (rubric version, judge, judge settings) in the matched cells.
    # More than one means the judge averages and win rates are withheld.
    judge_bases: int
    # Distinct prompts among the matched cells, and the fewest prompts an interval needs.
    prompts_matched: int
    interval_min_prompts: int
    baseline: str | None
    # Whether any compared prompt has deterministic checks (the table shows the column only then).
    has_checks: bool = False
    candidates: list[ComparisonCandidate]
    cells: list[ComparisonCell]
    notes: list[str]


@dataclass
class _Observation:
    attempt: Attempt
    run_id: str
    label: str
    manual: int | None
    judge: float | None
    judge_basis: str | None = None
    checks: float | None = None  # 1.0 when the answer passes every check, 0.0 when not, None if unknown
    checks_fp: str | None = None  # which checks graded it (None: its prompt has no checks)
    reference_fp: str = ""  # which reference answer the judge saw for its prompt ("" for none)


@dataclass
class _CandidateData:
    key: str
    label: str
    profile_name: str
    publisher: str
    api_style: str
    runs: set[str] = field(default_factory=set)
    slots: list[_Observation] = field(default_factory=list)
    by_cell: dict[Cell, list[_Observation]] = field(default_factory=dict)


@dataclass(frozen=True)
class _CellCost:
    mean: Decimal
    custom: bool


@dataclass
class _CellValues:
    """Per-cell means of every metric for one candidate, over its matched cells that have a value."""

    cost: dict[Cell, _CellCost] = field(default_factory=dict)
    latency: dict[Cell, float] = field(default_factory=dict)
    tokens: dict[Cell, tuple[float, float]] = field(default_factory=dict)
    reasoning: dict[Cell, float] = field(default_factory=dict)
    manual: dict[Cell, float] = field(default_factory=dict)
    judge: dict[Cell, float] = field(default_factory=dict)
    checks: dict[Cell, float] = field(default_factory=dict)
    wins: dict[Cell, list[float]] = field(default_factory=dict)
    judge_bases: dict[Cell, set[str]] = field(default_factory=dict)
    win_bases: dict[Cell, set[str]] = field(default_factory=dict)
    cost_excluded: int = 0
    cost_other_currency: int = 0


_STYLE_PUBLISHERS = {"anthropic_messages": "Anthropic", "openai_responses": "OpenAI", "openai_chat": "Other"}


def _publisher(snapshot: dict[str, Any]) -> str:
    publisher = str(snapshot.get("publisher") or "").strip()
    return publisher or _STYLE_PUBLISHERS.get(str(snapshot.get("api_style", "")), "Other")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _decimal_mean(values: list[Decimal]) -> Decimal | None:
    return div(add(*values), Decimal(len(values))) if values else None


def _succeeded(observations: list[_Observation]) -> bool:
    return any(o.attempt.status is AttemptStatus.SUCCEEDED for o in observations)


def _usage_values(observations: list[_Observation], name: str) -> list[float]:
    values: list[float] = []
    for observation in observations:
        usage = observation.attempt.usage
        value = getattr(usage, name) if usage is not None else None
        if value is not None:
            values.append(float(value))
    return values


def _common_cells[T](per_cell: dict[str, dict[Cell, T]], matched: list[Cell]) -> list[Cell]:
    """The matched cells in which every candidate that has the metric at all has a value."""
    participants = [key for key, values in per_cell.items() if values]
    return [cell for cell in matched if all(cell in per_cell[key] for key in participants)]


def _common_values[T](per_cell: dict[str, dict[Cell, T]], matched: list[Cell]) -> dict[str, list[T]]:
    """Each candidate's cell values over the metric's common cells (none for a candidate without any)."""
    common = _common_cells(per_cell, matched)
    return {key: [values[cell] for cell in common] if values else [] for key, values in per_cell.items()}


def _cell_values(observations: list[_Observation], currency: str, into: _CellValues, cell: Cell) -> None:
    costs: list[Decimal] = []
    custom = False
    for observation in observations:
        cost = observation.attempt.cost
        if cost is not None and cost.currency != currency:
            into.cost_other_currency += 1
        elif cost is None or cost.status != "complete" or cost.total is None:
            into.cost_excluded += 1
        else:
            costs.append(cost.total)
            custom = custom or any(line.custom_rate for line in cost.lines)
    mean_cost = _decimal_mean(costs)
    if mean_cost is not None:
        into.cost[cell] = _CellCost(mean_cost, custom)
    pairs = [
        (float(u.input_total), float(u.output_total))
        for u in (o.attempt.usage for o in observations)
        if u is not None and u.input_total is not None and u.output_total is not None
    ]
    if pairs:
        into.tokens[cell] = (sum(p[0] for p in pairs) / len(pairs), sum(p[1] for p in pairs) / len(pairs))
    for target, values in (
        (into.latency, [float(o.attempt.latency_ms) for o in observations if o.attempt.latency_ms is not None]),
        (into.reasoning, _usage_values(observations, "reasoning")),
        (into.manual, [float(o.manual) for o in observations if o.manual is not None]),
        (into.judge, [o.judge for o in observations if o.judge is not None]),
        (into.checks, [o.checks for o in observations if o.checks is not None]),
    ):
        mean = _mean(values)
        if mean is not None:
            target[cell] = mean
    bases = {o.judge_basis for o in observations if o.judge is not None and o.judge_basis is not None}
    if bases:
        into.judge_bases[cell] = bases


# Judge settings that change how answers are scored. Output limits, stop sequences, display options,
# the seed and prompt caching do not, so re-judging with a larger max_output_tokens keeps the basis.
SCORING_SETTINGS = ("temperature", "top_p", "top_k", "effort", "thinking_mode", "thinking_budget_tokens")
_JUDGE_IDENTITY = ("deployment", "model_name", "model_version", "api_style")


def judge_basis(evaluation: Evaluation) -> str:
    """What must match for two judge scores to be comparable: rubric version, judge and scoring settings."""
    snapshot = evaluation.judge_profile_snapshot
    settings = evaluation.judge_settings.model_dump()
    parts = [evaluation.rubric_version_id, evaluation.judge_profile_id]
    parts += [str(snapshot.get(name, "")) for name in _JUDGE_IDENTITY]
    parts.append(f"structured={evaluation.structured_output}")
    parts += [f"{name}={settings.get(name)!r}" for name in SCORING_SETTINGS]
    return "|".join(parts)


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16] if text else ""


def _checks_value(passed: bool | None) -> float | None:
    return None if passed is None else float(passed)


def _win_outcome(evaluation: Evaluation, mine: list[str]) -> float:
    if evaluation.preferred == "tie":
        return len(mine) / len(evaluation.candidates)
    return 1.0 if evaluation.preferred in mine else 0.0


@dataclass
class _PromptValues:
    """One candidate's per-prompt means of each metric, over that metric's common cells."""

    cost: dict[str, Decimal]
    latency: dict[str, float]
    manual: dict[str, float]
    judge: dict[str, float]
    wins: dict[str, float]
    checks: dict[str, float]


def _prompt_values(values: dict[str, _CellValues], matched: list[Cell]) -> dict[str, _PromptValues]:
    cost_cells = _common_cells({k: v.cost for k, v in values.items()}, matched)
    latency_cells = _common_cells({k: v.latency for k, v in values.items()}, matched)
    manual_cells = _common_cells({k: v.manual for k, v in values.items()}, matched)
    judge_cells = _common_cells({k: v.judge for k, v in values.items()}, matched)
    win_cells = _common_cells({k: v.wins for k, v in values.items()}, matched)
    check_cells = _common_cells({k: v.checks for k, v in values.items()}, matched)
    result: dict[str, _PromptValues] = {}
    for key, value in values.items():
        result[key] = _PromptValues(
            cost=stats.decimal_prompt_means(stats.by_prompt(value.cost, cost_cells, lambda c: c.mean)),
            latency=stats.prompt_means(stats.by_prompt(value.latency, latency_cells, float)),
            manual=stats.prompt_means(stats.by_prompt(value.manual, manual_cells, float)),
            judge=stats.prompt_means(stats.by_prompt(value.judge, judge_cells, float)),
            wins=stats.prompt_means(stats.by_prompt(value.wins, win_cells, lambda o: sum(o) / len(o))),
            checks=stats.prompt_means(stats.by_prompt(value.checks, check_cells, float)),
        )
    return result


def _interval(values: dict[str, float], floor: float | None = None, ceiling: float | None = None) -> Interval | None:
    found = stats.mean_interval(list(values.values()), floor=floor, ceiling=ceiling)
    return Interval(mean=found.mean, low=found.low, high=found.high, prompts=found.n) if found else None


def _intervals(prompt: _PromptValues, judge_scale: tuple[float, float] | None) -> dict[str, Any]:
    cost = stats.decimal_mean_interval(list(prompt.cost.values()))
    scale_floor, scale_ceiling = judge_scale if judge_scale else (None, None)
    return {
        "cost_interval": (
            MoneyInterval(mean=cost.mean, low=cost.low, high=cost.high, prompts=cost.n) if cost else None
        ),
        "latency_interval": _interval(prompt.latency, floor=0.0),
        "manual_interval": _interval(prompt.manual, floor=1.0, ceiling=5.0),
        "judge_interval": _interval(prompt.judge, floor=scale_floor, ceiling=scale_ceiling),
        "win_rate_interval": _interval(prompt.wins, floor=0.0, ceiling=1.0),
        "checks_interval": _interval(prompt.checks, floor=0.0, ceiling=1.0),
    }


def _difference(candidate: dict[str, float], baseline: dict[str, float]) -> Difference | None:
    found = stats.paired_difference(candidate, baseline)
    if found is None:
        return None
    return Difference(mean=found.mean, low=found.low, high=found.high, prompts=found.n, verdict=found.verdict)


def _differences(candidate: _PromptValues, baseline: _PromptValues) -> BaselineDifferences:
    cost = stats.decimal_paired_difference(candidate.cost, baseline.cost)
    return BaselineDifferences(
        cost=(
            MoneyDifference(mean=cost.mean, low=cost.low, high=cost.high, prompts=cost.n, verdict=cost.verdict)
            if cost
            else None
        ),
        latency_ms=_difference(candidate.latency, baseline.latency),
        manual=_difference(candidate.manual, baseline.manual),
        judge=_difference(candidate.judge, baseline.judge),
        win_rate=_difference(candidate.wins, baseline.wins),
        checks=_difference(candidate.checks, baseline.checks),
    )


class ComparisonService:
    def __init__(self, db: Database, judge: JudgeService) -> None:
        self.runs = RunRepository(db)
        self.evaluations = EvaluationRepository(db)
        self.rubrics = RubricRepository(db)
        self.judge = judge

    def _judge_scale(self, rubric_version_id: str) -> tuple[float, float] | None:
        try:
            definition = self.rubrics.version(rubric_version_id).definition
        except NotFoundError:
            return None
        return float(definition.scale_min), float(definition.scale_max)

    def compare(
        self, run_ids: list[str], candidate_keys: list[str] | None, currency: str, baseline: str | None = None
    ) -> Comparison:
        notes: list[str] = []
        has_checks = False
        basis_rubrics: dict[str, str] = {}
        data: dict[str, _CandidateData] = {}
        titles: dict[Cell, str] = {}
        evaluations: list[tuple[Cell, Evaluation, dict[str, str], str]] = []
        order: list[str] = []
        for run_id in dict.fromkeys(run_ids):
            run = self.runs.get(run_id)
            prompts = {p.id: p for p in self.runs.prompts(run_id)}
            candidates = {c.id: c for c in self.runs.candidates(run_id)}
            manual = self.runs.manual_scores(run_id)
            # One read of the run's evaluations feeds the scores, the wins and their bases alike.
            run_evaluations = self.evaluations.list_for_run(run_id)
            scores = judge_scores(run_evaluations)
            bases = {e.id: judge_basis(e) for e in run_evaluations}
            basis_rubrics.update({judge_basis(e): e.rubric_version_id for e in run_evaluations})
            for (prompt_id, candidate_id, trial), attempt in latest_attempts(self.runs.attempts(run_id)).items():
                candidate = candidates[candidate_id]
                if candidate_keys and candidate.candidate_key not in candidate_keys:
                    continue
                prompt = prompts[prompt_id]
                cell = (prompt.content_hash, trial)
                titles.setdefault(cell, prompt.title or prompt.user_text[:60])
                item = data.get(candidate.candidate_key)
                if item is None:
                    item = data[candidate.candidate_key] = _CandidateData(
                        key=candidate.candidate_key,
                        label=candidate.label,
                        profile_name=str(candidate.profile_snapshot.get("name", "")),
                        publisher=_publisher(candidate.profile_snapshot),
                        api_style=str(candidate.profile_snapshot.get("api_style", "")),
                    )
                    order.append(candidate.candidate_key)
                item.runs.add(run.id)
                score = manual.get(attempt.id)
                judged = scores.get(attempt.id)
                observation = _Observation(
                    attempt=attempt,
                    run_id=run.id,
                    label=candidate.label,
                    manual=score.score if score else None,
                    judge=judged.weighted_overall if judged else None,
                    judge_basis=bases.get(judged.evaluation_id) if judged else None,
                    checks=(
                        _checks_value(run_checks(prompt.checks, attempt.output_text or "").passed)
                        if prompt.checks and attempt.status is AttemptStatus.SUCCEEDED
                        else None
                    ),
                    checks_fp=_fingerprint(checks_json(prompt.checks)) if prompt.checks else None,
                    reference_fp=_fingerprint(prompt.reference_text),
                )
                has_checks = has_checks or bool(prompt.checks)
                item.slots.append(observation)
                item.by_cell.setdefault(cell, []).append(observation)
            key_by_candidate = {c.id: c.candidate_key for c in candidates.values()}
            for evaluation in latest_evaluations(run_evaluations).values():
                prompt = prompts[evaluation.prompt_id]
                labels = {c.label: key_by_candidate[c.candidate_id] for c in evaluation.candidates}
                cell_key = (prompt.content_hash, evaluation.trial_index)
                evaluations.append((cell_key, evaluation, labels, _fingerprint(prompt.reference_text)))

        if not data:
            raise NotFoundError("No attempts found for the selected runs and candidates.")
        cells = sorted({cell for item in data.values() for cell in item.by_cell})
        usable = {key: {cell for cell, obs in item.by_cell.items() if _succeeded(obs)} for key, item in data.items()}
        matched = [cell for cell in cells if all(cell in usable[key] for key in data)]
        if len(run_ids) > 1:
            notes.append("Cells with the same prompt text and trial number are matched across runs.")
        values = {key: _CellValues() for key in order}
        for key in order:
            for cell in matched:
                observations = [o for o in data[key].by_cell[cell] if o.attempt.status is AttemptStatus.SUCCEEDED]
                _cell_values(observations, currency, values[key], cell)
        matched_set = set(matched)
        # A cell repeated across runs must have been graded the same way (see the module docstring).
        check_fps: dict[Cell, set[str | None]] = {}
        reference_fps: dict[Cell, set[str]] = {}
        for key in order:
            for cell in matched:
                for o in data[key].by_cell[cell]:
                    if o.attempt.status is AttemptStatus.SUCCEEDED and o.checks_fp is not None:
                        check_fps.setdefault(cell, set()).add(o.checks_fp)
                    if o.judge is not None:
                        reference_fps.setdefault(cell, set()).add(o.reference_fp)
        for cell, _evaluation, _labels, reference_fp in evaluations:
            if cell in matched_set:
                reference_fps.setdefault(cell, set()).add(reference_fp)
        mixed_checks = {cell for cell, fps in check_fps.items() if len(fps) > 1}
        mixed_references = {cell for cell, fps in reference_fps.items() if len(fps) > 1}
        for value in values.values():
            for cell in mixed_checks:
                value.checks.pop(cell, None)
            for cell in mixed_references:
                value.judge.pop(cell, None)
                value.judge_bases.pop(cell, None)
        if mixed_checks:
            notes.append(
                f"{len(mixed_checks)} cell{'s' if len(mixed_checks) != 1 else ''} used different checks in different "
                "runs and are left out of the checks pass rate."
            )
        if mixed_references:
            notes.append(
                f"{len(mixed_references)} cell{'s' if len(mixed_references) != 1 else ''} were judged with different "
                "reference answers in different runs and are left out of the judge scores and win rates."
            )
        for cell, evaluation, labels, _reference_fp in evaluations:
            if cell not in matched_set or cell in mixed_references:
                continue
            if len(evaluation.candidates) < 2 or evaluation.preferred is None:
                continue
            for key in dict.fromkeys(labels.values()):
                if key in values:
                    mine = [label for label, candidate_key in labels.items() if candidate_key == key]
                    values[key].wins.setdefault(cell, []).append(_win_outcome(evaluation, mine))
                    values[key].win_bases.setdefault(cell, set()).add(judge_basis(evaluation))
        # Only the cells that would be averaged decide whether the judge evidence is comparable.
        judge_cells = _common_cells({k: v.judge for k, v in values.items()}, matched)
        win_cells = _common_cells({k: v.wins for k, v in values.items()}, matched)
        judge_bases: set[str] = set()
        for value in values.values():
            for cell in judge_cells:
                judge_bases |= value.judge_bases.get(cell, set())
            for cell in win_cells:
                judge_bases |= value.win_bases.get(cell, set())
        if len(judge_bases) > 1:
            for value in values.values():
                value.judge.clear()
                value.wins.clear()
            notes.append(
                f"Judge scores in these cells come from {len(judge_bases)} different combinations of rubric "
                "version, judge and judge settings, so they are not comparable. Judge averages and win rates "
                "are not shown; compare runs judged the same way, or re-judge them with one rubric version "
                "and judge."
            )
        costs = _common_values({k: v.cost for k, v in values.items()}, matched)
        latency = _common_values({k: v.latency for k, v in values.items()}, matched)
        tokens = _common_values({k: v.tokens for k, v in values.items()}, matched)
        reasoning = _common_values({k: v.reasoning for k, v in values.items()}, matched)
        manual_scores = _common_values({k: v.manual for k, v in values.items()}, matched)
        judge_scores_by_key = _common_values({k: v.judge for k, v in values.items()}, matched)
        checks_by_key = _common_values({k: v.checks for k, v in values.items()}, matched)
        wins = _common_values({k: v.wins for k, v in values.items()}, matched)
        prompt_values = _prompt_values(values, matched)
        judge_scale = self._judge_scale(basis_rubrics[next(iter(judge_bases))]) if len(judge_bases) == 1 else None
        if baseline is not None and baseline not in values:
            notes.append("The selected baseline is not among the compared candidates, so no differences are shown.")
            baseline = None
        other_currency = sum(v.cost_other_currency for v in values.values())
        if other_currency:
            notes.append(
                f"{other_currency} cost estimate{'s' if other_currency != 1 else ''} in another currency "
                f"{'were' if other_currency != 1 else 'was'} excluded; this comparison uses {currency}."
            )
        results: list[ComparisonCandidate] = []
        for key in order:
            item = data[key]
            win_means = [sum(outcomes) / len(outcomes) for outcomes in wins[key]]
            judge_wins = sum(win_means)
            results.append(
                ComparisonCandidate(
                    candidate_key=key,
                    label=item.label,
                    profile_name=item.profile_name,
                    publisher=item.publisher,
                    api_style=item.api_style,
                    runs=len(item.runs),
                    attempts=len(item.slots),
                    succeeded=sum(1 for o in item.slots if o.attempt.status is AttemptStatus.SUCCEEDED),
                    failures=sum(1 for o in item.slots if o.attempt.status in _FAILED),
                    failures_billed=sum(
                        1 for o in item.slots if o.attempt.status in _FAILED and o.attempt.usage is not None
                    ),
                    pending=sum(1 for o in item.slots if o.attempt.status in _PENDING),
                    covered_cells=len(usable[key]),
                    avg_cost=_decimal_mean([c.mean for c in costs[key]]),
                    cost_cells=len(costs[key]),
                    cost_excluded=values[key].cost_excluded,
                    cost_other_currency=values[key].cost_other_currency,
                    avg_latency_ms=_mean(latency[key]),
                    latency_cells=len(latency[key]),
                    avg_input_tokens=_mean([t[0] for t in tokens[key]]),
                    avg_output_tokens=_mean([t[1] for t in tokens[key]]),
                    token_cells=len(tokens[key]),
                    avg_reasoning_tokens=_mean(reasoning[key]),
                    reasoning_cells=len(reasoning[key]),
                    avg_manual_score=_mean(manual_scores[key]),
                    manual_scored=len(manual_scores[key]),
                    avg_judge_score=_mean(judge_scores_by_key[key]),
                    judge_scored=len(judge_scores_by_key[key]),
                    judge_wins=judge_wins,
                    judge_comparisons=sum(len(outcomes) for outcomes in wins[key]),
                    win_cells=len(win_means),
                    win_rate=judge_wins / len(win_means) if win_means else None,
                    custom_rates=any(c.custom for c in costs[key]),
                    avg_checks_passed=_mean(checks_by_key[key]),
                    checks_cells=len(checks_by_key[key]),
                    **_intervals(prompt_values[key], judge_scale),
                    vs_baseline=(
                        _differences(prompt_values[key], prompt_values[baseline])
                        if baseline is not None and key != baseline
                        else None
                    ),
                )
            )
        cell_rows = [
            ComparisonCell(
                content_hash=cell[0],
                prompt_title=titles.get(cell, ""),
                trial_index=cell[1],
                matched=cell in matched_set,
                missing=[data[key].label for key in order if cell not in usable[key]],
            )
            for cell in cells
        ]
        return Comparison(
            run_ids=list(dict.fromkeys(run_ids)),
            currency=currency,
            cells_total=len(cells),
            cells_matched=len(matched),
            judge_bases=len(judge_bases),
            prompts_matched=len({cell[0] for cell in matched}),
            interval_min_prompts=stats.MIN_PROMPTS,
            baseline=baseline,
            has_checks=has_checks,
            candidates=results,
            cells=cell_rows,
            notes=notes,
        )
