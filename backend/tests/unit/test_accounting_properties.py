"""Property tests for accounting invariants (disjoint categories, no double charging, bounds)."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from foundry_eval.pricing.calculator import LineStatus, estimate_cost
from foundry_eval.pricing.ratecard import Category
from foundry_eval.types import ApiStyle
from foundry_eval.usage.normalizers import normalize_usage

from .rates import claude_card, openai_card, per_million

R, A = ApiStyle.OPENAI_RESPONSES, ApiStyle.ANTHROPIC_MESSAGES
token = st.integers(min_value=0, max_value=2_000_000)
prices = st.decimals(min_value=Decimal("0.001"), max_value=Decimal("100"), places=4)


@st.composite
def openai_usage(draw):
    total = draw(token)
    cached = draw(st.integers(0, total))
    write = draw(st.integers(0, total - cached))
    output = draw(token)
    reasoning = draw(st.integers(0, output))
    return {
        "input_tokens": total,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": write},
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": reasoning},
    }


def _card(pin: Decimal, pread: Decimal, pwrite: Decimal, pout: Decimal):
    return openai_card(
        input_price=str(pin),
        cached=per_million(str(pread)),
        write=per_million(str(pwrite)),
        output=per_million(str(pout)),
    )


@settings(max_examples=300, deadline=None)
@given(openai_usage(), prices, prices, prices, prices)
def test_openai_categories_are_disjoint_and_sum_to_total(usage, pin, pread, pwrite, pout):
    facts = normalize_usage(R, usage)
    est = estimate_cost(facts, _card(pin, pread, pwrite, pout))
    assert est.status == "complete"
    input_lines = [ln for ln in est.lines if ln.category is not Category.OUTPUT]
    assert sum(ln.tokens or 0 for ln in input_lines) == usage["input_tokens"]
    output_line = next(ln for ln in est.lines if ln.category is Category.OUTPUT)
    assert output_line.tokens == usage["output_tokens"]
    assert est.total == sum((ln.cost or Decimal(0)) for ln in est.lines)
    expected = (
        Decimal(
            usage["input_tokens"]
            - usage["input_tokens_details"]["cached_tokens"]
            - usage["input_tokens_details"]["cache_write_tokens"]
        )
        * pin
        + Decimal(usage["input_tokens_details"]["cached_tokens"]) * pread
        + Decimal(usage["input_tokens_details"]["cache_write_tokens"]) * pwrite
        + Decimal(usage["output_tokens"]) * pout
    ) / Decimal(1_000_000)
    assert est.total == expected


@settings(max_examples=200, deadline=None)
@given(openai_usage(), st.integers(0, 2_000_000))
def test_reasoning_never_changes_cost(usage, other_reasoning):
    card = _card(Decimal("1.25"), Decimal("0.125"), Decimal("1.5"), Decimal("10"))
    base = estimate_cost(normalize_usage(R, usage), card)
    usage2 = {
        **usage,
        "output_tokens_details": {"reasoning_tokens": min(other_reasoning, usage["output_tokens"])},
    }
    assert estimate_cost(normalize_usage(R, usage2), card).total == base.total


@settings(max_examples=300, deadline=None)
@given(openai_usage(), prices, prices, prices, prices)
def test_bounds_contain_true_cost_when_cache_write_not_reported(usage, pin, pread, pwrite, pout):
    card = _card(pin, pread, pwrite, pout)
    truth = estimate_cost(normalize_usage(R, usage), card).total
    hidden = {
        **usage,
        "input_tokens_details": {"cached_tokens": usage["input_tokens_details"]["cached_tokens"]},
    }
    est = estimate_cost(normalize_usage(R, hidden), card)
    assert est.upper is not None and truth is not None
    assert est.lower <= truth <= est.upper
    if est.status == "complete":
        assert est.total == truth


@st.composite
def claude_usage(draw):
    w5, w1 = draw(token), draw(token)
    return {
        "input_tokens": draw(token),
        "cache_read_input_tokens": draw(token),
        "cache_creation_input_tokens": w5 + w1,
        "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
        "output_tokens": draw(token),
    }


@settings(max_examples=300, deadline=None)
@given(claude_usage())
def test_claude_total_input_is_three_way_sum(usage):
    facts = normalize_usage(A, usage)
    est = estimate_cost(facts, claude_card())
    assert est.status == "complete"
    assert facts is not None
    assert (
        facts.input_total
        == usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
    )
    input_tokens = sum(ln.tokens or 0 for ln in est.lines if ln.category is not Category.OUTPUT)
    assert input_tokens == facts.input_total


@settings(max_examples=200, deadline=None)
@given(claude_usage(), st.sampled_from([None, "5m", "1h"]))
def test_claude_unsplit_writes_bounds_contain_truth(usage, ttl):
    from foundry_eval.pricing.calculator import CostContext

    card = claude_card()
    truth = estimate_cost(normalize_usage(A, usage), card).total
    hidden = {k: v for k, v in usage.items() if k != "cache_creation"}
    est = estimate_cost(normalize_usage(A, hidden), card)
    assert est.upper is not None and truth is not None
    assert est.lower <= truth <= est.upper
    if ttl is not None:
        inferred = estimate_cost(normalize_usage(A, hidden), card, CostContext(requested_cache_ttl=ttl))
        assert inferred.status == "complete"


@settings(max_examples=200, deadline=None)
@given(openai_usage(), st.integers(1, 10_000))
def test_more_output_never_costs_less(usage, extra):
    card = _card(Decimal("1"), Decimal("0.1"), Decimal("1.25"), Decimal("8"))
    more = {**usage, "output_tokens": usage["output_tokens"] + extra}
    low = estimate_cost(normalize_usage(R, usage), card)
    high = estimate_cost(normalize_usage(R, more), card)
    assert high.lower >= low.lower


def test_no_line_is_ever_priced_with_unknown_tokens():
    est = estimate_cost(normalize_usage(R, {"output_tokens": 5}), openai_card())
    for ln in est.lines:
        if ln.tokens is None:
            assert ln.status in (LineStatus.UNKNOWN_TOKENS, LineStatus.UNASSIGNED, LineStatus.FOLDED)
            assert ln.cost is None
