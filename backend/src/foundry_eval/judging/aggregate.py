"""Judge evidence per (prompt, trial): every succeeded evaluation counts, on one scoring basis.

Re-judging adds evidence instead of replacing it. For each (prompt, trial) of a run, the succeeded
evaluations of its current answers are grouped by the answers they judged and by judge basis (rubric
version, judge and the settings that change how it scores). Only evaluations of the same answers on
the same basis are averaged together, so the preference shares of one group always sum to 1. One
group is used per (prompt, trial): the one that judged the most answers (normally every current
answer: the set only grows when a failed answer is retried), then the basis with the most
evaluations, then the one judged most recently (then a fixed order, so the choice never depends on
read order). Each answer's judge score is the mean of its weighted overall scores over that group,
and its win share the mean of its preference outcomes (1 when preferred, 1/n each on a tie, 0
otherwise). Evaluations of fewer answers or on another basis are left out and counted, never mixed
in, and so are evaluations of an answer that was retried since (it is no longer shown).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..domain.generation import GenerationSettings
from ..repos.evaluations import Evaluation

Slot = tuple[str, int]  # (prompt id, trial index) within one run

# Judge settings that change how answers are scored. Output limits, stop sequences, display options,
# the seed and prompt caching do not, so re-judging with a larger max_output_tokens keeps the basis.
SCORING_SETTINGS = ("temperature", "top_p", "top_k", "effort", "thinking_mode", "thinking_budget_tokens")
JUDGE_IDENTITY = ("deployment", "model_name", "model_version", "api_style")


def basis_of(
    rubric_version_id: str,
    judge_profile_id: str,
    snapshot: dict[str, object],
    structured_output: bool,
    settings: GenerationSettings,
) -> str:
    """The basis text from its parts, so a planned evaluation can be checked before it exists."""
    values = settings.model_dump()
    parts = [rubric_version_id, judge_profile_id]
    parts += [str(snapshot.get(name, "")) for name in JUDGE_IDENTITY]
    parts.append(f"structured={structured_output}")
    parts += [f"{name}={values.get(name)!r}" for name in SCORING_SETTINGS]
    return "|".join(parts)


def judge_basis(evaluation: Evaluation) -> str:
    """What must match for two judge scores to be comparable: rubric version, judge and scoring settings."""
    return basis_of(
        evaluation.rubric_version_id,
        evaluation.judge_profile_id,
        evaluation.judge_profile_snapshot,
        evaluation.structured_output,
        evaluation.judge_settings,
    )


def win_outcome(evaluation: Evaluation, labels: list[str]) -> float | None:
    """The share of one evaluation's preference won by these labels, or None if it compared nothing."""
    if len(evaluation.candidates) < 2 or evaluation.preferred is None:
        return None
    if evaluation.preferred == "tie":
        return len(labels) / len(evaluation.candidates)
    return 1.0 if evaluation.preferred in labels else 0.0


@dataclass(frozen=True)
class AnswerJudgement:
    """One answer's judge evidence on its (prompt, trial)'s basis."""

    attempt_id: str
    candidate_id: str
    mean: float  # weighted overall score, averaged over the evaluations that scored this answer
    evaluations: int
    evaluation_ids: tuple[str, ...]
    spread: float | None  # highest minus lowest score; None from one evaluation (unknown, not zero)
    win_share: float | None  # mean preference share over the evaluations that compared answers
    win_evaluations: int


@dataclass(frozen=True)
class SlotJudgement:
    """The judge evidence used for one (prompt, trial) of a run."""

    prompt_id: str
    trial_index: int
    basis: str
    rubric_version_id: str
    evaluations: tuple[Evaluation, ...]  # the chosen group (same answers, same basis), oldest first
    answers: dict[str, AnswerJudgement]  # by attempt id
    other_basis: int  # succeeded evaluations of the same answers on another basis (left out)
    other_answers: int = 0  # succeeded evaluations of fewer (or other) current answers (left out)

    @property
    def left_out(self) -> int:
        return self.other_basis + self.other_answers

    @property
    def spread(self) -> float | None:
        """The largest spread of any answer; None when no answer was judged more than once."""
        spreads = [a.spread for a in self.answers.values() if a.spread is not None]
        return max(spreads) if spreads else None


@dataclass(frozen=True)
class RunJudgements:
    slots: dict[Slot, SlotJudgement]
    # Succeeded evaluations left out because an answer they judged was retried since, per slot.
    stale: dict[Slot, int] = field(default_factory=dict)

    def answers(self) -> dict[str, tuple[AnswerJudgement, SlotJudgement]]:
        return {a.attempt_id: (a, s) for s in self.slots.values() for a in s.answers.values()}


def answers_judged(evaluation: Evaluation) -> frozenset[str]:
    """The attempts an evaluation judged (whatever order it showed them in)."""
    return frozenset(entry.attempt_id for entry in evaluation.order)


Group = tuple[frozenset[str], str]  # (answers judged, judge basis)


def choose_group(evaluations: list[Evaluation]) -> Group:
    """The most answers judged, then the most evaluations, then the most recent, then a fixed order."""
    groups: dict[Group, list[Evaluation]] = {}
    for evaluation in evaluations:
        groups.setdefault((answers_judged(evaluation), judge_basis(evaluation)), []).append(evaluation)
    return max(
        groups,
        key=lambda g: (len(g[0]), len(groups[g]), max(e.created_at for e in groups[g]), g[1], sorted(g[0])),
    )


def choose_basis(evaluations: list[Evaluation]) -> str:
    """The judge basis of the group ``choose_group`` picks."""
    return choose_group(evaluations)[1]


def aggregate_answers(evaluations: list[Evaluation]) -> dict[str, AnswerJudgement]:
    """Each answer's mean score, spread and win share over these evaluations (one basis)."""
    scores: dict[str, list[float]] = {}
    wins: dict[str, list[float]] = {}
    candidate_ids: dict[str, str] = {}
    sources: dict[str, list[str]] = {}
    for evaluation in evaluations:
        for candidate in evaluation.candidates:
            candidate_ids[candidate.attempt_id] = candidate.candidate_id
            scores.setdefault(candidate.attempt_id, []).append(candidate.weighted_overall)
            sources.setdefault(candidate.attempt_id, []).append(evaluation.id)
            outcome = win_outcome(evaluation, [candidate.label])
            if outcome is not None:
                wins.setdefault(candidate.attempt_id, []).append(outcome)
    return {
        attempt_id: AnswerJudgement(
            attempt_id=attempt_id,
            candidate_id=candidate_ids[attempt_id],
            mean=sum(values) / len(values),
            evaluations=len(values),
            evaluation_ids=tuple(sources[attempt_id]),
            spread=max(values) - min(values) if len(values) > 1 else None,
            win_share=sum(wins[attempt_id]) / len(wins[attempt_id]) if attempt_id in wins else None,
            win_evaluations=len(wins.get(attempt_id, [])),
        )
        for attempt_id, values in scores.items()
    }


def is_current(evaluation: Evaluation, current_attempts: set[str] | None) -> bool:
    """Whether every answer the evaluation judged is still the newest attempt of its slot."""
    return current_attempts is None or all(entry.attempt_id in current_attempts for entry in evaluation.order)


def by_slot(evaluations: Iterable[Evaluation]) -> dict[Slot, list[Evaluation]]:
    grouped: dict[Slot, list[Evaluation]] = {}
    for evaluation in evaluations:
        grouped.setdefault((evaluation.prompt_id, evaluation.trial_index), []).append(evaluation)
    return grouped


def judge_run(evaluations: Iterable[Evaluation], current_attempts: set[str] | None = None) -> RunJudgements:
    """The judge evidence of one run's evaluations, one basis per (prompt, trial).

    ``current_attempts`` are the newest attempt ids of the run; evaluations that judged an older
    attempt are left out and counted. None keeps every succeeded evaluation.
    """
    slots: dict[Slot, SlotJudgement] = {}
    stale: dict[Slot, int] = {}
    succeeded = [e for e in evaluations if e.status == "succeeded"]
    for slot, items in by_slot(succeeded).items():
        usable = [e for e in items if is_current(e, current_attempts)]
        if len(usable) < len(items):
            stale[slot] = len(items) - len(usable)
        if not usable:
            continue
        answers, basis = choose_group(usable)
        chosen = sorted(
            (e for e in usable if answers_judged(e) == answers and judge_basis(e) == basis),
            key=lambda e: (e.created_at, e.id),
        )
        other_answers = sum(1 for e in usable if answers_judged(e) != answers)
        slots[slot] = SlotJudgement(
            prompt_id=slot[0],
            trial_index=slot[1],
            basis=basis,
            rubric_version_id=chosen[0].rubric_version_id,
            evaluations=tuple(chosen),
            answers=aggregate_answers(chosen),
            other_basis=len(usable) - len(chosen) - other_answers,
            other_answers=other_answers,
        )
    return RunJudgements(slots=slots, stale=stale)
