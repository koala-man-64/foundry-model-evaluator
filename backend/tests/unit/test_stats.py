"""Intervals across prompts, paired differences and judge calibration maths."""

from __future__ import annotations

import math
from decimal import Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from foundry_eval.analysis import stats
from foundry_eval.analysis.calibration import average_ranks, rescale, spearman, spearman_interval


def test_t_critical_uses_the_table_and_rounds_down_between_entries():
    assert stats.t_critical(1) == 12.706
    assert stats.t_critical(30) == 2.042
    assert stats.t_critical(45) == 2.021  # between 40 and 60: the wider value
    assert stats.t_critical(500) == 1.960
    with pytest.raises(ValueError):
        stats.t_critical(0)


def test_mean_interval_matches_a_hand_computation():
    interval = stats.mean_interval([1.0, 2.0, 3.0])
    assert interval is not None and interval.n == 3 and interval.mean == 2.0
    half = 4.303 * 1.0 / math.sqrt(3)  # s = 1, df = 2
    assert interval.low == pytest.approx(2 - half) and interval.high == pytest.approx(2 + half)


def test_too_few_prompts_give_no_interval():
    assert stats.mean_interval([4.0, 5.0]) is None
    assert stats.decimal_mean_interval([Decimal("0.1"), Decimal("0.2")]) is None


def test_bounds_clip_an_interval():
    interval = stats.mean_interval([1.0, 5.0, 5.0], floor=1.0, ceiling=5.0)
    assert interval is not None and interval.low == 1.0 and interval.high == 5.0


def test_decimal_interval_is_exact_and_never_below_zero():
    values = [Decimal("0.001"), Decimal("0.002"), Decimal("0.003")]
    interval = stats.decimal_mean_interval(values)
    assert interval is not None and interval.mean == Decimal("0.002")
    with localcontext() as ctx:
        ctx.prec = 40
        half = Decimal("4.303") * (Decimal("0.000001") / 3).sqrt()
        assert interval.high == Decimal("0.002") + half
    assert interval.low == max(Decimal("0.002") - half, Decimal(0)) == Decimal(0)


@pytest.mark.parametrize(
    ("candidate", "verdict"),
    [
        ({"a": 5, "b": 5, "c": 4, "d": 5}, "higher"),
        ({"a": 1, "b": 1, "c": 2, "d": 1}, "lower"),
        ({"a": 4, "b": 2, "c": 3, "d": 1}, "cannot_tell"),
    ],
)
def test_paired_difference_verdicts(candidate, verdict):
    baseline = {"a": 3.0, "b": 3.0, "c": 3.0, "d": 3.0, "only-in-baseline": 1.0}
    found = stats.paired_difference({k: float(v) for k, v in candidate.items()}, baseline)
    assert found is not None and found.n == 4 and found.verdict == verdict


def test_paired_difference_needs_shared_prompts():
    assert stats.paired_difference({"a": 1.0}, {"b": 1.0}) is None
    small = stats.paired_difference({"a": 5.0, "b": 5.0}, {"a": 1.0, "b": 1.0})
    assert small is not None and small.mean == 4.0 and small.low is None and small.verdict == "insufficient_data"


def test_decimal_paired_difference():
    found = stats.decimal_paired_difference(
        {"a": Decimal("0.003"), "b": Decimal("0.004"), "c": Decimal("0.005")},
        {"a": Decimal("0.001"), "b": Decimal("0.001"), "c": Decimal("0.001")},
    )
    assert found is not None and found.mean == Decimal("0.003") and found.verdict == "higher"


def test_trials_of_a_prompt_are_averaged_before_counting():
    per_cell = {("p1", 1): 4.0, ("p1", 2): 2.0, ("p2", 1): 5.0, ("p3", 1): 1.0}
    cells = [("p1", 1), ("p1", 2), ("p2", 1), ("p3", 1), ("p4", 1)]
    means = stats.prompt_means(stats.by_prompt(per_cell, cells, float))
    assert means == {"p1": 3.0, "p2": 5.0, "p3": 1.0}
    interval = stats.mean_interval(list(means.values()))
    assert interval is not None and interval.n == 3


@given(st.lists(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False), min_size=3, max_size=40))
def test_interval_contains_the_mean_and_ignores_order(values):
    interval = stats.mean_interval(values)
    assert interval is not None
    assert interval.low <= interval.mean + 1e-6 and interval.mean <= interval.high + 1e-6
    again = stats.mean_interval(list(reversed(values)))
    assert again is not None and again.low == pytest.approx(interval.low) and again.high == pytest.approx(interval.high)


# --- calibration -----------------------------------------------------------------------------------
def test_average_ranks_share_ties():
    assert average_ranks([3.0, 1.0, 3.0, 2.0]) == [3.5, 1.0, 3.5, 2.0]


def test_spearman_extremes_and_no_variation():
    assert spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([3, 3, 3, 3, 3], [1, 2, 3, 4, 5]) is None


def test_spearman_interval_needs_enough_pairs():
    assert spearman_interval(0.8, 4) is None
    low, high = spearman_interval(0.8, 30)  # type: ignore[misc]
    assert -1 < low < 0.8 < high < 1


def test_rescale_maps_a_rubric_scale_onto_one_to_five():
    assert rescale(1, 1, 10) == 1 and rescale(10, 1, 10) == 5
    assert rescale(3, 1, 5) == 3
