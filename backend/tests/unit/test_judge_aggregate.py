"""Judge evidence aggregation: one basis per (prompt, trial), means, spreads, win shares, left-out counts."""

from __future__ import annotations

from itertools import count
from typing import Any

import pytest

from foundry_eval.analysis.calibration import swap_outcome
from foundry_eval.domain.generation import GenerationSettings
from foundry_eval.judging.aggregate import (
    aggregate_answers,
    choose_basis,
    judge_basis,
    judge_run,
    win_outcome,
)
from foundry_eval.repos.evaluations import Evaluation, EvaluationCandidate, OrderEntry

_ids = count(1)
JUDGE = {"deployment": "judge-d", "model_name": "judge-m", "model_version": None, "api_style": "openai_responses"}


def evaluation(
    scores: dict[str, float],
    *,
    preferred: str | None = None,
    created_at: str = "2026-10-16T10:00:00Z",
    rubric: str = "default-v1",
    settings: dict[str, Any] | None = None,
    status: str = "succeeded",
    prompt_id: str = "p1",
    trial_index: int = 1,
    snapshot: dict[str, Any] | None = None,
) -> Evaluation:
    """An evaluation of attempts ``scores`` (attempt id -> weighted overall), labelled A, B, ... in order."""
    labels = [chr(65 + i) for i in range(len(scores))]
    attempts = list(scores)
    by_label = dict(zip(labels, attempts, strict=True))
    preferred_label = "tie" if preferred == "tie" else next((lb for lb, a in by_label.items() if a == preferred), None)
    return Evaluation(
        id=f"e{next(_ids)}",
        run_id="run",
        prompt_id=prompt_id,
        trial_index=trial_index,
        batch_id="batch",
        trigger="manual",
        status=status,  # type: ignore[arg-type]
        judge_profile_id="judge",
        judge_profile_snapshot=snapshot or JUDGE,
        judge_settings=GenerationSettings.model_validate(settings or {}),
        rubric_version_id=rubric,
        price_snapshot_id="price",
        structured_output=True,
        judge_is_participant=False,
        participant_reasons=[],
        order=[OrderEntry(label=lb, attempt_id=a, candidate_id=f"c-{a}") for lb, a in by_label.items()],
        input_estimate_tokens=100,
        input_limit_tokens=1000,
        request_sent_at=created_at,
        started_at=created_at,
        finished_at=created_at,
        latency_ms=1,
        finish_reason="completed",
        raw_finish_reason="stop",
        response_model="judge-m",
        output_text="{}",
        usage=None,
        parse_errors=[],
        preferred=preferred_label,
        preferred_candidate_id=None,
        preference_explanation=None,
        cost=None,
        cost_total=None,
        error_type=None,
        error_message=None,
        created_at=created_at,
        candidates=(
            [
                EvaluationCandidate(
                    label=lb,
                    position=i,
                    attempt_id=a,
                    candidate_id=f"c-{a}",
                    judge_overall=scores[a],
                    weighted_overall=scores[a],
                    explanation="",
                    criteria=[],
                )
                for i, (lb, a) in enumerate(by_label.items())
            ]
            if status == "succeeded"
            else []
        ),
    )


def test_scores_are_averaged_per_answer_with_their_spread():
    first = evaluation({"x": 4.0, "y": 2.0}, preferred="x")
    second = evaluation({"x": 3.0, "y": 2.0}, preferred="y")
    answers = aggregate_answers([first, second])
    assert answers["x"].mean == pytest.approx(3.5) and answers["x"].evaluations == 2
    assert answers["x"].spread == pytest.approx(1.0)
    assert answers["y"].spread == pytest.approx(0.0)  # judged twice, same score: a measured zero
    # Preferred once each: the shares average to one half.
    assert answers["x"].win_share == pytest.approx(0.5) and answers["x"].win_evaluations == 2
    assert answers["y"].win_share == pytest.approx(0.5)


def test_one_evaluation_has_no_spread_rather_than_zero():
    [answer] = aggregate_answers([evaluation({"x": 4.0})]).values()
    assert answer.spread is None
    # A single answer was compared with nothing, so it has no win share either.
    assert answer.win_share is None and answer.win_evaluations == 0


def test_a_tie_shares_the_preference_and_a_missing_preference_counts_for_nothing():
    tie = evaluation({"x": 3.0, "y": 3.0, "z": 3.0}, preferred="tie")
    assert win_outcome(tie, ["A"]) == pytest.approx(1 / 3)
    assert win_outcome(tie, ["A", "B"]) == pytest.approx(2 / 3)
    unstated = evaluation({"x": 3.0, "y": 2.0}, preferred=None)
    assert win_outcome(unstated, ["A"]) is None
    answers = aggregate_answers([tie, unstated])
    assert answers["x"].win_share == pytest.approx(1 / 3) and answers["x"].win_evaluations == 1
    assert answers["x"].evaluations == 2  # the scores of both still count


def test_the_basis_with_the_most_evaluations_wins_then_the_most_recent():
    a1 = evaluation({"x": 4.0, "y": 3.0}, created_at="2026-10-16T09:00:00Z")
    a2 = evaluation({"x": 4.0, "y": 3.0}, created_at="2026-10-16T09:30:00Z")
    b1 = evaluation({"x": 2.0, "y": 1.0}, rubric="other-v1", created_at="2026-10-16T11:00:00Z")
    assert choose_basis([a1, b1, a2]) == judge_basis(a1)  # two beat one, even though one is newer
    assert choose_basis([a1, b1]) == judge_basis(b1)  # one each: the most recent
    # Equal counts and times: the basis text decides, so read order never changes the result.
    c1 = evaluation({"x": 1.0}, rubric="c-v1", created_at="2026-10-16T09:00:00Z")
    d1 = evaluation({"x": 1.0}, rubric="d-v1", created_at="2026-10-16T09:00:00Z")
    assert choose_basis([c1, d1]) == choose_basis([d1, c1])


def test_judge_run_uses_one_basis_per_slot_and_counts_what_it_leaves_out():
    a1 = evaluation({"x": 4.0, "y": 2.0}, preferred="x", created_at="2026-10-16T09:00:00Z")
    a2 = evaluation({"x": 3.0, "y": 3.0}, preferred="tie", created_at="2026-10-16T09:10:00Z")
    other = evaluation({"x": 9.0, "y": 1.0}, rubric="ten-v1", preferred="x", created_at="2026-10-16T12:00:00Z")
    failed = evaluation({"x": 1.0, "y": 1.0}, status="failed", created_at="2026-10-16T13:00:00Z")
    elsewhere = evaluation({"u": 5.0, "v": 1.0}, prompt_id="p2", preferred="u")
    judged = judge_run([a1, a2, other, failed, elsewhere])
    slot = judged.slots[("p1", 1)]
    assert slot.basis == judge_basis(a1) and [e.id for e in slot.evaluations] == [a1.id, a2.id]
    assert slot.other_basis == 1  # the newer evaluation on another rubric; the failed one is not evidence
    assert slot.answers["x"].mean == pytest.approx(3.5)
    assert slot.answers["x"].win_share == pytest.approx((1 + 0.5) / 2)
    assert slot.spread == pytest.approx(1.0)
    assert judged.slots[("p2", 1)].left_out == 0
    assert set(judged.answers()) == {"x", "y", "u", "v"}


def test_evaluations_of_answers_retried_since_are_left_out():
    old = evaluation({"x-old": 4.0, "y": 2.0}, preferred="x-old", created_at="2026-10-16T09:00:00Z")
    new = evaluation({"x-new": 3.0, "y": 3.0}, preferred="tie", created_at="2026-10-16T10:00:00Z")
    judged = judge_run([old, new], current_attempts={"x-new", "y"})
    slot = judged.slots[("p1", 1)]
    assert [e.id for e in slot.evaluations] == [new.id]
    assert "x-old" not in slot.answers and slot.answers["y"].evaluations == 1
    assert judged.stale == {("p1", 1): 1}
    # Only stale evidence: the slot has no judgement at all, but the count is kept.
    only_old = judge_run([old], current_attempts={"x-new", "y"})
    assert only_old.slots == {} and only_old.stale == {("p1", 1): 1}


def test_settings_that_do_not_change_scoring_keep_the_basis():
    plain = evaluation({"x": 1.0})
    longer = evaluation({"x": 1.0}, settings={"max_output_tokens": 4000, "seed": 7})
    warmer = evaluation({"x": 1.0}, settings={"temperature": 0.5})
    moved = evaluation({"x": 1.0}, snapshot={**JUDGE, "deployment": "judge-2"})
    assert judge_basis(plain) == judge_basis(longer)
    assert judge_basis(plain) != judge_basis(warmer)
    assert judge_basis(plain) != judge_basis(moved)


def test_a_swap_pair_is_consistent_a_flip_or_tied_only_once():
    source = evaluation({"x": 4.0, "y": 3.0}, preferred="x")
    assert swap_outcome(source, evaluation({"y": 4.0, "x": 3.0}, preferred="x")) == "consistent"
    assert swap_outcome(source, evaluation({"y": 4.0, "x": 3.0}, preferred="y")) == "flip"
    assert swap_outcome(source, evaluation({"y": 3.0, "x": 3.0}, preferred="tie")) == "one_tie"
    tied = evaluation({"x": 3.0, "y": 3.0}, preferred="tie")
    assert swap_outcome(tied, evaluation({"y": 3.0, "x": 3.0}, preferred="tie")) == "consistent"
    assert swap_outcome(source, evaluation({"y": 4.0, "x": 3.0}, preferred=None)) is None


def test_only_evaluations_of_the_same_answers_are_averaged_together():
    """An evaluation from before a failed answer was retried judged fewer answers: it is not mixed in."""
    before = evaluation({"a": 4.0, "b": 2.0}, preferred="a", created_at="2026-10-16T09:00:00Z")
    swapped = evaluation({"b": 4.0, "a": 2.0}, preferred="b", created_at="2026-10-16T09:05:00Z")
    after = evaluation({"a": 3.0, "b": 3.0, "c": 5.0}, preferred="c", created_at="2026-10-16T10:00:00Z")
    slot = judge_run([before, swapped, after]).slots[("p1", 1)]
    # The most answers first, even against two evaluations of fewer answers.
    assert [e.id for e in slot.evaluations] == [after.id]
    assert slot.other_answers == 2 and slot.other_basis == 0 and slot.left_out == 2
    shares = [a.win_share for a in slot.answers.values()]
    assert sum(s for s in shares if s is not None) == pytest.approx(1.0)
    assert slot.answers["a"].mean == pytest.approx(3.0) and slot.answers["a"].evaluation_ids == (after.id,)


def test_a_middle_answer_that_reversing_cannot_move_says_nothing_about_position():
    middle = evaluation({"x": 3.0, "y": 4.0, "z": 3.0}, preferred="y")
    assert swap_outcome(middle, evaluation({"z": 3.0, "y": 4.0, "x": 3.0}, preferred="y")) is None
    edge = evaluation({"x": 4.0, "y": 3.0, "z": 3.0}, preferred="x")
    assert swap_outcome(edge, evaluation({"z": 3.0, "y": 3.0, "x": 4.0}, preferred="x")) == "consistent"
    assert swap_outcome(edge, evaluation({"z": 4.0, "y": 3.0, "x": 3.0}, preferred="z")) == "flip"
