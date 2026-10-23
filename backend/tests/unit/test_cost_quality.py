"""Cost and quality coordinates must describe the same shared prompt/trial cells."""

from decimal import Decimal

import pytest

from foundry_eval.analysis.comparisons import _CellCost, _CellValues, _cost_quality_points


@pytest.mark.parametrize("kind", ["manual", "judge", "checks"])
def test_chart_uses_joint_cells_across_candidates(kind):
    shared, cost_only, score_only = ("shared", 0), ("cost-only", 0), ("score-only", 0)
    values = {
        "a": _CellValues(cost={shared: _CellCost(Decimal("1"), False), cost_only: _CellCost(Decimal("99"), True)}),
        "b": _CellValues(cost={shared: _CellCost(Decimal("2"), False), cost_only: _CellCost(Decimal("88"), False)}),
        # A candidate with no quality evidence must not remove the other candidates' shared cell.
        "unscored": _CellValues(cost={cost_only: _CellCost(Decimal("3"), False)}),
        "disjoint": _CellValues(cost={cost_only: _CellCost(Decimal("4"), False)}),
    }
    setattr(values["a"], kind, {shared: 0.9, score_only: 0.1})
    setattr(values["b"], kind, {shared: 0.8, cost_only: 0.2, score_only: 0.3})
    setattr(values["disjoint"], kind, {score_only: 0.7})

    result = _cost_quality_points(values, [shared, cost_only, score_only])

    a, b = getattr(result["a"], kind), getattr(result["b"], kind)
    assert a is not None and b is not None
    assert (a.avg_cost, a.avg_score, a.cells) == (Decimal("1"), 0.9, 1)
    assert (b.avg_cost, b.avg_score, b.cells) == (Decimal("2"), 0.8, 1)
    assert not a.custom_rates  # the custom rate only occurred outside the paired sample
    assert getattr(result["unscored"], kind) is None
    assert getattr(result["disjoint"], kind) is None


def test_equal_metric_counts_do_not_imply_overlapping_cells():
    first, second = ("first", 0), ("second", 0)
    value = _CellValues(cost={first: _CellCost(Decimal("1"), False)}, judge={second: 5.0})

    point = _cost_quality_points({"a": value}, [first, second])["a"].judge

    assert point is None


def test_chart_validates_judge_bases_on_its_own_shared_cells():
    first, second = ("first", 0), ("second", 0)
    value = _CellValues(
        cost={first: _CellCost(Decimal("1"), False), second: _CellCost(Decimal("2"), False)},
        judge={first: 4.0, second: 5.0},
        judge_bases={first: {"basis-a"}, second: {"basis-b"}},
    )
    # The table's judge metric only uses first, because the unpriced candidate wasn't judged on second.
    unpriced = _CellValues(judge={first: 3.0}, judge_bases={first: {"basis-a"}})

    point = _cost_quality_points({"a": value, "unpriced": unpriced}, [first, second])["a"].judge

    assert point is None
