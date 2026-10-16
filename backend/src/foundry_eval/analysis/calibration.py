"""How well the judge agrees with your own scores, per judge basis (rubric version, judge, settings).

The manual score is one person's holistic 1-5 rating, not ground truth, and the judge scores on its
rubric's scale. Judge scores are therefore rescaled to 1-5 first, and agreement is reported as rank
correlation (Spearman, with ties ranked by their average), the share of answers within one point, the
mean signed difference (judge minus you) and how often the judge preferred the answer you scored
highest. Evaluations in which the judge also produced one of the answers are left out and counted.
Nothing here costs anything: it only reads scores already stored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pydantic import BaseModel

from ..db.database import Database
from ..judging.service import latest_attempts, latest_evaluations
from ..repos.endpoints import NotFoundError
from ..repos.evaluations import Evaluation, EvaluationRepository
from ..repos.rubrics import RubricRepository
from ..repos.runs import RunRepository
from .comparisons import judge_basis

MIN_PAIRS = 5  # below this a rank correlation is noise, so it is not reported


class CalibrationGroup(BaseModel):
    judge: str
    rubric: str
    scale_min: int
    scale_max: int
    pairs: int
    spearman: float | None
    spearman_low: float | None
    spearman_high: float | None
    within_one: float | None
    mean_bias: float | None
    top_pick_agree: int
    top_pick_total: int
    participant_pairs_excluded: int


class Calibration(BaseModel):
    run_ids: list[str]
    groups: list[CalibrationGroup]
    min_pairs: int
    notes: list[str]


@dataclass
class _Group:
    judge: str
    rubric: str
    scale_min: int
    scale_max: int
    pairs: list[tuple[float, float]] = field(default_factory=list)  # (manual, judge rescaled to 1-5)
    top_agree: int = 0
    top_total: int = 0
    excluded: int = 0


def average_ranks(values: list[float]) -> list[float]:
    """1-based ranks; tied values share the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        rank = (start + end) / 2 + 1
        for position in range(start, end + 1):
            ranks[order[position]] = rank
        start = end + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rank correlation, or None when either side has no variation."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx, ry = average_ranks(xs), average_ranks(ys)
    mx, my = math.fsum(rx) / len(rx), math.fsum(ry) / len(ry)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sxx = math.fsum((a - mx) ** 2 for a in rx)
    syy = math.fsum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman_interval(rho: float, n: int) -> tuple[float, float] | None:
    """Approximate 95% interval via Fisher's z with the Fieller-Hartley-Pearson variance for Spearman."""
    if n < MIN_PAIRS:
        return None
    clipped = max(min(rho, 0.999999), -0.999999)
    z = math.atanh(clipped)
    half = 1.96 * math.sqrt(1.06 / (n - 3))
    return math.tanh(z - half), math.tanh(z + half)


def rescale(score: float, scale_min: int, scale_max: int) -> float:
    """A judge score on its rubric scale, mapped linearly onto the 1-5 manual scale."""
    return 1 + 4 * (score - scale_min) / (scale_max - scale_min)


class CalibrationService:
    def __init__(self, db: Database) -> None:
        self.runs = RunRepository(db)
        self.evaluations = EvaluationRepository(db)
        self.rubrics = RubricRepository(db)

    def calibrate(self, run_ids: list[str]) -> Calibration:
        groups: dict[str, _Group] = {}
        for run_id in dict.fromkeys(run_ids):
            self.runs.get(run_id)
            current = {a.id for a in latest_attempts(self.runs.attempts(run_id)).values()}
            manual = {k: v for k, v in self.runs.manual_scores(run_id).items() if k in current}
            for evaluation in latest_evaluations(self.evaluations.list_for_run(run_id)).values():
                group = groups.get(judge_basis(evaluation))
                if group is None:
                    group = groups[judge_basis(evaluation)] = self._group(evaluation)
                scored = [c for c in evaluation.candidates if c.attempt_id in manual]
                if evaluation.judge_is_participant:
                    group.excluded += len(scored)
                    continue
                for candidate in scored:
                    judged = rescale(candidate.weighted_overall, group.scale_min, group.scale_max)
                    group.pairs.append((float(manual[candidate.attempt_id].score), judged))
                if len(evaluation.candidates) >= 2 and len(scored) == len(evaluation.candidates):
                    best = max(manual[c.attempt_id].score for c in scored)
                    top = [c for c in scored if manual[c.attempt_id].score == best]
                    if len(top) == 1 and evaluation.preferred not in (None, "tie"):
                        group.top_total += 1
                        group.top_agree += int(evaluation.preferred == top[0].label)
        notes = [
            "Your manual scores are one person's view, not ground truth.",
            "Judge scores are rescaled from the rubric's scale to 1-5 before they are compared with yours.",
        ]
        if any(g.excluded for g in groups.values()):
            notes.append("Evaluations in which the judge also produced an answer are left out.")
        return Calibration(
            run_ids=list(dict.fromkeys(run_ids)),
            groups=[self._summary(g) for g in groups.values()],
            min_pairs=MIN_PAIRS,
            notes=notes,
        )

    def _group(self, evaluation: Evaluation) -> _Group:
        snapshot = evaluation.judge_profile_snapshot
        judge = str(snapshot.get("name") or snapshot.get("deployment") or evaluation.judge_profile_id)
        try:
            version = self.rubrics.version(evaluation.rubric_version_id)
            rubric = f"{self.rubrics.get(version.rubric_id).name} v{version.version}"
            scale = (version.definition.scale_min, version.definition.scale_max)
        except NotFoundError:
            rubric, scale = "Unknown rubric", (1, 5)
        return _Group(judge=judge, rubric=rubric, scale_min=scale[0], scale_max=scale[1])

    @staticmethod
    def _summary(group: _Group) -> CalibrationGroup:
        n = len(group.pairs)
        manual = [p[0] for p in group.pairs]
        judged = [p[1] for p in group.pairs]
        rho = spearman(manual, judged) if n >= MIN_PAIRS else None
        interval = spearman_interval(rho, n) if rho is not None else None
        return CalibrationGroup(
            judge=group.judge,
            rubric=group.rubric,
            scale_min=group.scale_min,
            scale_max=group.scale_max,
            pairs=n,
            spearman=rho,
            spearman_low=interval[0] if interval else None,
            spearman_high=interval[1] if interval else None,
            within_one=sum(1 for m, j in group.pairs if abs(j - m) <= 1) / n if n else None,
            mean_bias=math.fsum(j - m for m, j in group.pairs) / n if n else None,
            top_pick_agree=group.top_agree,
            top_pick_total=group.top_total,
            participant_pairs_excluded=group.excluded,
        )
