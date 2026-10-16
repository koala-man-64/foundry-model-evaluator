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
- Failed attempts whose provider reported usage were billed; they are counted per candidate because
  their cost is in the run totals but, as failures, not in the averages.
- Failures are counted over all cells, for finished attempts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from ..db.database import Database
from ..judging.service import JudgeService, judge_scores, latest_attempts, latest_evaluations
from ..money import Money, add, div
from ..repos.endpoints import NotFoundError
from ..repos.evaluations import Evaluation, EvaluationRepository
from ..repos.runs import Attempt, RunRepository
from ..types import AttemptStatus

Cell = tuple[str, int]

_FAILED = frozenset(
    {AttemptStatus.FAILED, AttemptStatus.CANCELLED, AttemptStatus.INTERRUPTED, AttemptStatus.SKIPPED_BUDGET}
)
_PENDING = frozenset({AttemptStatus.QUEUED, AttemptStatus.RUNNING})


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
    ):
        mean = _mean(values)
        if mean is not None:
            target[cell] = mean
    bases = {o.judge_basis for o in observations if o.judge is not None and o.judge_basis is not None}
    if bases:
        into.judge_bases[cell] = bases


# Judge settings that change how answers are scored. Output limits, stop sequences, display options,
# the seed and prompt caching do not, so re-judging with a larger max_output_tokens keeps the basis.
_SCORING_SETTINGS = ("temperature", "top_p", "top_k", "effort", "thinking_mode", "thinking_budget_tokens")
_JUDGE_IDENTITY = ("deployment", "model_name", "model_version", "api_style")


def judge_basis(evaluation: Evaluation) -> str:
    """What must match for two judge scores to be comparable: rubric version, judge and scoring settings."""
    snapshot = evaluation.judge_profile_snapshot
    settings = evaluation.judge_settings.model_dump()
    parts = [evaluation.rubric_version_id, evaluation.judge_profile_id]
    parts += [str(snapshot.get(name, "")) for name in _JUDGE_IDENTITY]
    parts.append(f"structured={evaluation.structured_output}")
    parts += [f"{name}={settings.get(name)!r}" for name in _SCORING_SETTINGS]
    return "|".join(parts)


def _win_outcome(evaluation: Evaluation, mine: list[str]) -> float:
    if evaluation.preferred == "tie":
        return len(mine) / len(evaluation.candidates)
    return 1.0 if evaluation.preferred in mine else 0.0


class ComparisonService:
    def __init__(self, db: Database, judge: JudgeService) -> None:
        self.runs = RunRepository(db)
        self.evaluations = EvaluationRepository(db)
        self.judge = judge

    def compare(self, run_ids: list[str], candidate_keys: list[str] | None, currency: str) -> Comparison:
        notes: list[str] = []
        data: dict[str, _CandidateData] = {}
        titles: dict[Cell, str] = {}
        evaluations: list[tuple[Cell, Evaluation, dict[str, str]]] = []
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
                )
                item.slots.append(observation)
                item.by_cell.setdefault(cell, []).append(observation)
            key_by_candidate = {c.id: c.candidate_key for c in candidates.values()}
            for evaluation in latest_evaluations(run_evaluations).values():
                prompt = prompts[evaluation.prompt_id]
                labels = {c.label: key_by_candidate[c.candidate_id] for c in evaluation.candidates}
                evaluations.append(((prompt.content_hash, evaluation.trial_index), evaluation, labels))

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
        for cell, evaluation, labels in evaluations:
            if cell not in matched_set or len(evaluation.candidates) < 2 or evaluation.preferred is None:
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
        wins = _common_values({k: v.wins for k, v in values.items()}, matched)
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
            candidates=results,
            cells=cell_rows,
            notes=notes,
        )
