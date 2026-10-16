"""How well the judge agrees with your own scores, per judge basis (rubric version, judge, settings).

The manual score is one person's holistic 1-5 rating, not ground truth, and the judge scores on its
rubric's scale. Judge scores are therefore rescaled to 1-5 first, and agreement is reported as rank
correlation (Spearman, with ties ranked by their average), the share of answers within one point, the
mean signed difference (judge minus you) and how often the judge preferred the answer you scored
highest. Evaluations in which the judge also produced one of the answers are left out and counted.
Nothing here costs anything: it only reads scores already stored.

Every succeeded evaluation of an answer counts: within each judge basis, an answer judged several
times is compared with you once, at its mean score, and the judge's pick for a prompt and trial is
the answer with the highest mean preference share (no pick when they are level, for example when
two evaluations preferred different answers). As in comparisons, only evaluations of the same
answers are averaged together (those that judged the most answers, then the most evaluations, then
the most recent). Each basis has its own row, so nothing is left out for using another basis here;
evaluations of answers that were retried since are left out.

Position swaps: a swap pass re-judges the same answers with the presentation order reversed. For
each pair (an evaluation and its swap, both succeeded, on the same basis) the judge either kept its
preference (*consistent*: the same answer, or a tie both times), moved it to another answer
(*flip*: in a two-answer comparison it preferred the same position both times), or tied only once.
The flip share is the position-bias statistic; it is reported with its counts, since a handful of
pairs says little. Pairs follow the same exclusions as the scores (the judge also produced an
answer, or an answer was retried since). With an odd number of answers the middle one keeps its
position when the order is reversed, so a pair whose first evaluation preferred it says nothing
about position and is left out.

Answers to the same prompt are related, so the interval for the rank correlation counts *prompts*,
not answers, as its sample size, and is shown only from ``MIN_INTERVAL_PROMPTS`` prompts.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from ..db.database import Database
from ..judging.aggregate import (
    SCORING_SETTINGS,
    aggregate_answers,
    answers_judged,
    by_slot,
    choose_group,
    is_current,
    judge_basis,
)
from ..judging.service import latest_attempts
from ..repos.endpoints import NotFoundError
from ..repos.evaluations import Evaluation, EvaluationRepository
from ..repos.rubrics import RubricRepository
from ..repos.runs import ManualScore, RunRepository

MIN_PAIRS = 5  # below this a rank correlation is noise, so it is not reported
MIN_INTERVAL_PROMPTS = 5


class CalibrationGroup(BaseModel):
    key: str  # stable id of the judge basis
    judge: str
    rubric: str
    settings: str  # the judge's scoring settings, so two rows for one judge and rubric can be told apart
    scale_min: int
    scale_max: int
    pairs: int
    prompts: int
    spearman: float | None
    spearman_low: float | None
    spearman_high: float | None
    within_one: float | None
    mean_bias: float | None
    top_pick_agree: int
    top_pick_total: int
    participant_pairs_excluded: int
    # Position swaps on this basis: pairs, how many moved the preference to another answer, and how
    # many kept it. The rates are None without pairs (unknown, not zero).
    swap_pairs: int = 0
    swap_flips: int = 0
    swap_consistent: int = 0
    flip_rate: float | None = None
    consistency_rate: float | None = None


class Calibration(BaseModel):
    run_ids: list[str]
    groups: list[CalibrationGroup]
    min_pairs: int
    notes: list[str]


@dataclass
class _Group:
    key: str
    judge: str
    rubric: str
    settings: str
    scale_min: int
    scale_max: int
    pairs: list[tuple[float, float]] = field(default_factory=list)  # (manual, judge rescaled to 1-5)
    prompts: set[str] = field(default_factory=set)  # content hashes of the prompts the pairs came from
    top_agree: int = 0
    top_total: int = 0
    excluded: int = 0
    swap_pairs: int = 0
    swap_flips: int = 0
    swap_consistent: int = 0


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
    """Approximate 95% interval via Fisher's z with the Fieller-Hartley-Pearson variance for Spearman.

    ``n`` is the number of independent units (prompts). No interval for perfect agreement, where the
    transform has no finite width.
    """
    if n < MIN_INTERVAL_PROMPTS or abs(rho) >= 0.999:
        return None
    clipped = max(min(rho, 0.999999), -0.999999)
    z = math.atanh(clipped)
    half = 1.96 * math.sqrt(1.06 / (n - 3))
    return math.tanh(z - half), math.tanh(z + half)


def _preferred_answer(evaluation: Evaluation) -> str | None:
    """The attempt the judge preferred, "tie" for a tie, None without a usable preference."""
    if evaluation.preferred == "tie":
        return "tie"
    return {o.label: o.attempt_id for o in evaluation.order}.get(evaluation.preferred or "")


SwapOutcome = Literal["consistent", "flip", "one_tie"]


def swap_outcome(source: Evaluation, swap: Evaluation) -> SwapOutcome | None:
    """How the judge's preference changed once the order was reversed; None when the pair says nothing.

    consistent: the same answer, or a tie both times. flip: another answer (with two answers, the same
    position both times). one_tie: a tie in only one of the two, which is neither. None: either has no
    preference, or the source preferred the middle answer of an odd count, which reversing never moves.
    """
    before, after = _preferred_answer(source), _preferred_answer(swap)
    if before is None or after is None:
        return None
    shown = [o.attempt_id for o in source.order]
    if len(shown) % 2 == 1 and before == shown[len(shown) // 2]:
        return None
    if before == after:
        return "consistent"
    return "one_tie" if "tie" in (before, after) else "flip"


def _add_slot(group: _Group, evaluations: list[Evaluation], manual: dict[str, ManualScore], prompt: str) -> None:
    """One (prompt, trial)'s evaluations on one basis: each answer once, at its mean score."""
    counted = [e for e in evaluations if not e.judge_is_participant]
    covered = {c.attempt_id for e in counted for c in e.candidates}
    left_out = {
        c.attempt_id for e in evaluations if e.judge_is_participant for c in e.candidates if c.attempt_id in manual
    }
    group.excluded += len(left_out - covered)
    answers = aggregate_answers(counted)
    scored = [a for a in answers.values() if a.attempt_id in manual]
    for answer in scored:
        judged = rescale(answer.mean, group.scale_min, group.scale_max)
        group.pairs.append((float(manual[answer.attempt_id].score), judged))
    if scored:
        group.prompts.add(prompt)
    if len(answers) < 2 or len(scored) < len(answers):
        return
    best = max(manual[a.attempt_id].score for a in scored)
    top = [a for a in scored if manual[a.attempt_id].score == best]
    shares = [a.win_share for a in scored if a.win_share is not None]
    if len(top) != 1 or len(shares) < len(scored):
        return
    # Shares are means of fractions such as 1/3, so "level" allows for rounding.
    picks = [a for a in scored if a.win_share is not None and max(shares) - a.win_share < 1e-9]
    if len(picks) == 1:
        group.top_total += 1
        group.top_agree += int(picks[0].attempt_id == top[0].attempt_id)


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
            hashes = {p.id: p.content_hash for p in self.runs.prompts(run_id)}
            manual = {k: v for k, v in self.runs.manual_scores(run_id).items() if k in current}
            succeeded = [e for e in self.evaluations.list_for_run(run_id) if e.status == "succeeded"]
            for (prompt_id, _trial), items in by_slot(e for e in succeeded if is_current(e, current)).items():
                by_basis: dict[str, list[Evaluation]] = {}
                for evaluation in items:
                    by_basis.setdefault(judge_basis(evaluation), []).append(evaluation)
                for basis, basis_items in by_basis.items():
                    group = groups.get(basis)
                    if group is None:
                        group = groups[basis] = self._group(basis_items[0])
                    answers, _basis = choose_group(basis_items)
                    same = [e for e in basis_items if answers_judged(e) == answers]
                    _add_slot(group, same, manual, hashes.get(prompt_id, prompt_id))
            self._add_swaps(groups, [e for e in succeeded if is_current(e, current) and not e.judge_is_participant])
        notes = [
            "Your manual scores are one person's view, not ground truth.",
            "Judge scores are rescaled from the rubric's scale to 1-5 before they are compared with yours.",
        ]
        notes.append(
            "The interval for rank agreement counts prompts, not answers, because answers to one prompt are related."
        )
        notes.append("An answer judged more than once on the same basis is compared with you once, at its mean score.")
        if any(g.excluded for g in groups.values()):
            notes.append("Evaluations in which the judge also produced an answer are left out.")
        if any(g.swap_pairs for g in groups.values()):
            notes.append(
                "Order swaps: a flip means the judge preferred another answer once the order was reversed, so its "
                "preference followed the position rather than the answer."
            )
        return Calibration(
            run_ids=list(dict.fromkeys(run_ids)),
            groups=[self._summary(g) for g in groups.values()],
            min_pairs=MIN_PAIRS,
            notes=notes,
        )

    def _add_swaps(self, groups: dict[str, _Group], succeeded: list[Evaluation]) -> None:
        """Pairs of an evaluation and its swap pass, both usable, counted on the source's basis."""
        by_id = {e.id: e for e in succeeded}
        for swap in succeeded:
            source = by_id.get(swap.swap_of or "")
            if source is None:
                continue  # not a swap, or its source did not succeed
            outcome = swap_outcome(source, swap)
            basis = judge_basis(source)
            if outcome is None or judge_basis(swap) != basis:
                continue  # no preference to compare, or not like for like (the app always keeps the basis)
            group = groups.get(basis)
            if group is None:
                group = groups[basis] = self._group(source)
            group.swap_pairs += 1
            group.swap_consistent += int(outcome == "consistent")
            group.swap_flips += int(outcome == "flip")

    def _group(self, evaluation: Evaluation) -> _Group:
        snapshot = evaluation.judge_profile_snapshot
        judge = str(snapshot.get("name") or snapshot.get("deployment") or evaluation.judge_profile_id)
        try:
            version = self.rubrics.version(evaluation.rubric_version_id)
            rubric = f"{self.rubrics.get(version.rubric_id).name} v{version.version}"
            scale = (version.definition.scale_min, version.definition.scale_max)
        except NotFoundError:
            rubric, scale = "Unknown rubric", (1, 5)
        basis = judge_basis(evaluation)
        provided = evaluation.judge_settings.provided()
        scoring = [f"{name}={provided[name]}" for name in SCORING_SETTINGS if name in provided]
        return _Group(
            key=hashlib.sha256(basis.encode()).hexdigest()[:12],
            judge=judge,
            rubric=rubric,
            settings=", ".join(scoring) or "default settings",
            scale_min=scale[0],
            scale_max=scale[1],
        )

    @staticmethod
    def _summary(group: _Group) -> CalibrationGroup:
        n = len(group.pairs)
        manual = [p[0] for p in group.pairs]
        judged = [p[1] for p in group.pairs]
        rho = spearman(manual, judged) if n >= MIN_PAIRS else None
        interval = spearman_interval(rho, len(group.prompts)) if rho is not None else None
        return CalibrationGroup(
            key=group.key,
            judge=group.judge,
            rubric=group.rubric,
            settings=group.settings,
            scale_min=group.scale_min,
            scale_max=group.scale_max,
            pairs=n,
            prompts=len(group.prompts),
            spearman=rho,
            spearman_low=interval[0] if interval else None,
            spearman_high=interval[1] if interval else None,
            within_one=sum(1 for m, j in group.pairs if abs(j - m) <= 1) / n if n else None,
            mean_bias=math.fsum(j - m for m, j in group.pairs) / n if n else None,
            top_pick_agree=group.top_agree,
            top_pick_total=group.top_total,
            participant_pairs_excluded=group.excluded,
            swap_pairs=group.swap_pairs,
            swap_flips=group.swap_flips,
            swap_consistent=group.swap_consistent,
            flip_rate=group.swap_flips / group.swap_pairs if group.swap_pairs else None,
            consistency_rate=group.swap_consistent / group.swap_pairs if group.swap_pairs else None,
        )
