"""Run totals: statuses, bounds and estimates in another currency."""

from __future__ import annotations

from decimal import Decimal

import pytest

from foundry_eval.execution.totals import combine_totals, total_costs
from foundry_eval.pricing.calculator import CostEstimate

D = Decimal


def complete(amount: str, currency: str = "USD") -> CostEstimate:
    return CostEstimate(status="complete", currency=currency, total=D(amount), lower=D(amount), upper=D(amount))


def test_statuses_and_bounds():
    assert total_costs([]).status == "empty" and total_costs([]).total == 0
    done = total_costs([complete("0.1"), complete("0.2"), CostEstimate.not_sent()])
    assert (done.status, done.total, done.upper, done.counted) == ("complete", D("0.3"), D("0.3"), 3)
    pending = total_costs([complete("0.1"), None])
    assert (pending.status, pending.total, pending.upper, pending.pending) == ("pending", None, None, 1)
    missing = total_costs([complete("0.1"), CostEstimate.usage_missing("USD", "no usage")])
    assert (missing.status, missing.total, missing.lower, missing.upper) == ("incomplete", None, D("0.1"), None)


def test_estimates_in_another_currency_make_the_total_incomplete():
    total = total_costs([complete("0.1"), complete("0.5", "EUR")], "USD")
    assert total.status == "incomplete" and total.total is None
    assert total.lower == D("0.1") and total.upper is None
    assert total.counted == 1 and total.other_currency == 1
    only_other = total_costs([complete("0.5", "EUR")], "USD")
    assert only_other.status == "incomplete" and only_other.total is None and only_other.other_currency == 1


def test_combined_totals_keep_the_counts_and_refuse_mixed_currencies():
    generation = total_costs([complete("0.1")], "USD")
    judging = total_costs([complete("0.2", "EUR")], "USD")
    combined = combine_totals(generation, judging)
    assert combined.status == "incomplete" and combined.other_currency == 1 and combined.lower == D("0.1")
    with pytest.raises(ValueError):
        combine_totals(generation, total_costs([complete("0.2", "EUR")], "EUR"))


def test_a_sent_call_without_usage_is_bounded_by_its_reservation():
    bounded = CostEstimate.usage_missing("USD", "request was sent but no usage was received", D("0.0374"))
    assert (bounded.status, bounded.lower, bounded.upper) == ("incomplete", D(0), D("0.0374"))
    assert bounded.assumptions and "reserved" in bounded.assumptions[0]
    unbounded = CostEstimate.usage_missing("USD", "request was sent but no usage was received")
    assert unbounded.upper is None and unbounded.assumptions == []
    assert total_costs([bounded, complete("0.1")]).upper == D("0.1374")
