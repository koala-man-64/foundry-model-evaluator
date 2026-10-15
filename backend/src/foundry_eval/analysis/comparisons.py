"""Compare candidates across runs on matching prompts and trials, with visible coverage.

A cell is (prompt content hash, trial index). Candidates are identified by their candidate key
(pricing identity + exact settings), so the same candidate can be followed across runs of a suite.
Averages use only the cells where every selected candidate has a usable (succeeded) attempt; each
cell counts once. Failures are counted over all cells.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from ..db.database import Database
from ..judging.service import JudgeService, latest_attempts
from ..money import Money
from ..repos.endpoints import NotFoundError
from ..repos.evaluations import Evaluation, EvaluationRepository
from ..repos.runs import Attempt, RunRepository
from ..types import AttemptStatus

Cell = tuple[str, int]


class ComparisonCandidate(BaseModel):
    candidate_key: str
    label: str
    profile_name: str
    runs: int
    attempts: int
    succeeded: int
    failures: int
    covered_cells: int
    avg_cost: Money | None
    cost_cells: int
    cost_excluded: int
    avg_latency_ms: float | None
    avg_input_tokens: float | None
    avg_output_tokens: float | None
    avg_reasoning_tokens: float | None
    avg_manual_score: float | None
    manual_scored: int
    avg_judge_score: float | None
    judge_scored: int
    judge_wins: float
    judge_comparisons: int
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


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _decimal_mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _succeeded(observations: list[_Observation]) -> bool:
    return any(o.attempt.status is AttemptStatus.SUCCEEDED for o in observations)


def _complete_cost(attempt: Attempt) -> Decimal | None:
    cost = attempt.cost
    if cost is None or cost.status != "complete" or cost.total is None:
        return None
    return cost.total


def _usage_values(observations: list[_Observation], name: str) -> list[float]:
    values: list[float] = []
    for observation in observations:
        usage = observation.attempt.usage
        value = getattr(usage, name) if usage is not None else None
        if value is not None:
            values.append(float(value))
    return values


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

        cells = sorted({cell for item in data.values() for cell in item.by_cell})
        usable = {key: {cell for cell, obs in item.by_cell.items() if _succeeded(obs)} for key, item in data.items()}
        matched = {cell for cell in cells if all(cell in usable[key] for key in data)}
        if len(run_ids) > 1:
            notes.append("Cells with the same prompt text and trial number are matched across runs.")
        results: list[ComparisonCandidate] = []
        for key in order:
            item = data[key]
            costs: list[Decimal] = []
            excluded = 0
            latency: list[float] = []
            inputs: list[float] = []
            outputs: list[float] = []
            reasoning: list[float] = []
            manual_scores: list[float] = []
            judge_scores_list: list[float] = []
            custom = False
            for cell in sorted(matched):
                observations = [o for o in item.by_cell.get(cell, []) if o.attempt.status is AttemptStatus.SUCCEEDED]
                cell_costs = [c for c in (_complete_cost(o.attempt) for o in observations) if c is not None]
                excluded += len(observations) - len(cell_costs)
                mean_cost = _decimal_mean(cell_costs)
                if mean_cost is not None:
                    costs.append(mean_cost)
                custom = custom or any(
                    line.custom_rate for o in observations if o.attempt.cost for line in o.attempt.cost.lines
                )
                for target, values in (
                    (latency, [float(o.attempt.latency_ms) for o in observations if o.attempt.latency_ms is not None]),
                    (inputs, _usage_values(observations, "input_total")),
                    (outputs, _usage_values(observations, "output_total")),
                    (reasoning, _usage_values(observations, "reasoning")),
                    (manual_scores, [float(o.manual) for o in observations if o.manual is not None]),
                    (judge_scores_list, [o.judge for o in observations if o.judge is not None]),
                ):
                    mean = _mean(values)
                    if mean is not None:
                        target.append(mean)
            wins = 0.0
            comparisons = 0
            for cell, evaluation, labels in evaluations:
                if cell not in matched or len(evaluation.candidates) < 2:
                    continue
                mine = [label for label, candidate_key in labels.items() if candidate_key == key]
                if not mine:
                    continue
                comparisons += 1
                if evaluation.preferred == "tie":
                    wins += 0.5
                elif evaluation.preferred in mine:
                    wins += 1
            avg_cost = _decimal_mean(costs)
            results.append(
                ComparisonCandidate(
                    candidate_key=key,
                    label=item.label,
                    profile_name=item.profile_name,
                    runs=len(item.runs),
                    attempts=len(item.slots),
                    succeeded=sum(1 for o in item.slots if o.attempt.status is AttemptStatus.SUCCEEDED),
                    failures=sum(1 for o in item.slots if o.attempt.status is not AttemptStatus.SUCCEEDED),
                    covered_cells=len(usable[key]),
                    avg_cost=avg_cost,
                    cost_cells=len(costs),
                    cost_excluded=excluded,
                    avg_latency_ms=_mean(latency),
                    avg_input_tokens=_mean(inputs),
                    avg_output_tokens=_mean(outputs),
                    avg_reasoning_tokens=_mean(reasoning),
                    avg_manual_score=_mean(manual_scores),
                    manual_scored=len(manual_scores),
                    avg_judge_score=_mean(judge_scores_list),
                    judge_scored=len(judge_scores_list),
                    judge_wins=wins,
                    judge_comparisons=comparisons,
                    win_rate=wins / comparisons if comparisons else None,
                    custom_rates=custom,
                )
            )
        cell_rows = [
            ComparisonCell(
                content_hash=cell[0],
                prompt_title=titles.get(cell, ""),
                trial_index=cell[1],
                matched=cell in matched,
                missing=[data[key].label for key in order if cell not in usable[key]],
            )
            for cell in cells
        ]
        if not data:
            raise NotFoundError("No attempts found for the selected runs and candidates.")
        return Comparison(
            run_ids=list(dict.fromkeys(run_ids)),
            currency=currency,
            cells_total=len(cells),
            cells_matched=len(matched),
            candidates=results,
            cells=cell_rows,
            notes=notes,
        )
