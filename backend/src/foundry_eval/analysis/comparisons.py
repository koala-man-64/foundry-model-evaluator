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
- Failures are counted over all cells, for finished attempts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from ..db.database import Database
from ..judging.service import JudgeService, latest_attempts
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
    runs: int
    attempts: int
    succeeded: int
    failures: int
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


@dataclass
class _CandidateData:
    key: str
    label: str
    profile_name: str
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
    cost_excluded: int = 0
    cost_other_currency: int = 0


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


def _common_values[T](per_cell: dict[str, dict[Cell, T]], matched: list[Cell]) -> dict[str, list[T]]:
    """Each candidate's cell values over the metric's common cells (none for a candidate without any)."""
    participants = [key for key, values in per_cell.items() if values]
    common = [cell for cell in matched if all(cell in per_cell[key] for key in participants)]
    return {key: [per_cell[key][cell] for cell in common] if key in participants else [] for key in per_cell}


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
            judge_scores = self.judge.scores_for_run(run_id)
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
                    )
                    order.append(candidate.candidate_key)
                item.runs.add(run.id)
                score = manual.get(attempt.id)
                judged = judge_scores.get(attempt.id)
                observation = _Observation(
                    attempt=attempt,
                    run_id=run.id,
                    label=candidate.label,
                    manual=score.score if score else None,
                    judge=judged.weighted_overall if judged else None,
                )
                item.slots.append(observation)
                item.by_cell.setdefault(cell, []).append(observation)
            key_by_candidate = {c.id: c.candidate_key for c in candidates.values()}
            latest_eval: dict[tuple[str, int], Evaluation] = {}
            for evaluation in self.evaluations.list_for_run(run_id):
                if evaluation.status == "succeeded":
                    slot = (evaluation.prompt_id, evaluation.trial_index)
                    if slot not in latest_eval or evaluation.created_at >= latest_eval[slot].created_at:
                        latest_eval[slot] = evaluation
            for evaluation in latest_eval.values():
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
                    runs=len(item.runs),
                    attempts=len(item.slots),
                    succeeded=sum(1 for o in item.slots if o.attempt.status is AttemptStatus.SUCCEEDED),
                    failures=sum(1 for o in item.slots if o.attempt.status in _FAILED),
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
            candidates=results,
            cells=cell_rows,
            notes=notes,
        )
