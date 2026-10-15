from __future__ import annotations

from decimal import Decimal

from foundry_eval.pricing.calculator import (
    CostContext,
    CostEstimate,
    LineStatus,
    estimate_cost,
    sum_estimates,
)
from foundry_eval.pricing.ratecard import Category, CategoryRate, RateCard, RateTier
from foundry_eval.types import ApiStyle
from foundry_eval.usage.normalizers import normalize_usage

from .rates import NA, UNKNOWN, claude_card, openai_card, per_million

R, C, A = ApiStyle.OPENAI_RESPONSES, ApiStyle.OPENAI_CHAT, ApiStyle.ANTHROPIC_MESSAGES


def line(estimate: CostEstimate, category: Category):
    return next(item for item in estimate.lines if item.category is category)


def test_openai_cached_and_reasoning_are_not_double_charged(usage_fixture):
    facts = normalize_usage(R, usage_fixture("responses_cached_reasoning"))
    est = estimate_cost(facts, openai_card())
    # uncached 1160 x 1.25/M + cached 3840 x 0.125/M + output 1200 x 10/M (reasoning is inside output)
    assert est.status == "complete"
    assert est.total == Decimal("0.01393")
    assert line(est, Category.INPUT_UNCACHED).tokens == 1160
    assert line(est, Category.CACHE_READ).tokens == 3840
    assert line(est, Category.OUTPUT).tokens == 1200
    write = line(est, Category.CACHE_WRITE)
    assert write.status is LineStatus.FOLDED
    assert est.lower == est.upper == est.total


def test_gpt56_cache_write_priced(usage_fixture):
    card = openai_card(
        input_price="2.50", cached=per_million("0.25"), write=per_million("3.125"), output=per_million("15")
    )
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_cache_write")), card)
    assert est.status == "complete"
    # 500 x 2.5 + 1000 x 0.25 + 2500 x 3.125 + 500 x 15, all per million
    assert est.total == Decimal("0.0168125")


def test_unreported_cache_write_with_priced_meter_gives_bounds(usage_fixture):
    card = openai_card(
        input_price="2.50", cached=per_million("0.25"), write=per_million("3.125"), output=per_million("15")
    )
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_cache_write_missing")), card)
    assert est.status == "incomplete"
    assert est.total is None
    # pool of 3000 tokens priced between 2.5 and 3.125 per million, plus cached and output
    assert est.lower == Decimal("0.01525")
    assert est.upper == Decimal("0.017125")
    assert est.pools[0].tokens == 3000
    assert line(est, Category.INPUT_UNCACHED).status is LineStatus.UNASSIGNED
    assert any("not reported" in reason for reason in est.reasons)


def test_pool_with_equal_rates_is_complete():
    card = openai_card(input_price="2", write=per_million("2"))
    facts = normalize_usage(R, {"input_tokens": 100, "output_tokens": 0, "input_tokens_details": {"cached_tokens": 0}})
    est = estimate_cost(facts, card)
    assert est.status == "complete"
    assert est.total == Decimal("0.0002")


def test_pool_with_unknown_rate_has_no_upper_bound():
    card = openai_card(write=UNKNOWN)
    facts = normalize_usage(R, {"input_tokens": 100, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 0}})
    est = estimate_cost(facts, card)
    assert est.status == "incomplete"
    assert est.upper is None
    assert est.lower == Decimal("0.0001")  # output only; pool lower bound is 0 with an unknown rate


def test_missing_cached_field_with_priced_cache_read_is_incomplete(usage_fixture):
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_no_details")), openai_card())
    assert est.status == "incomplete"
    # 800 tokens split between input (1.25) and cache read (0.125); output 50 x 10
    assert est.lower == Decimal("0.0006")
    assert est.upper == Decimal("0.0015")


def test_not_applicable_cache_read_folds_into_input(usage_fixture):
    card = openai_card(cached=CategoryRate.not_applicable("user: no caching discount"))
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_cached_reasoning")), card)
    assert est.status == "complete"
    assert line(est, Category.INPUT_UNCACHED).tokens == 5000
    assert line(est, Category.CACHE_READ).status is LineStatus.FOLDED
    assert est.total == Decimal("0.01825")


def test_zero_tokens_with_unknown_rate_is_complete(usage_fixture):
    card = openai_card(cached=UNKNOWN)
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_basic")), card)
    assert est.status == "complete"
    assert line(est, Category.CACHE_READ).status is LineStatus.ZERO
    assert est.total == Decimal("0.0045")


def test_unknown_rate_with_tokens_is_incomplete_not_zero(usage_fixture):
    card = openai_card(output=UNKNOWN)
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_basic")), card)
    assert est.status == "incomplete"
    assert est.total is None
    assert est.lower == Decimal("0.0015")
    assert est.upper is None
    assert line(est, Category.OUTPUT).status is LineStatus.UNKNOWN_RATE
    assert any("no rate for output" in reason for reason in est.reasons)


def test_usage_missing_is_incomplete_with_open_upper_bound():
    est = estimate_cost(None, openai_card())
    assert est.status == "incomplete"
    assert est.lower == 0
    assert est.upper is None


def test_not_sent_costs_exactly_zero():
    est = CostEstimate.not_sent()
    assert est.status == "not_sent"
    assert est.total == 0 and est.is_complete


def test_output_not_reported_is_incomplete():
    facts = normalize_usage(C, {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}})
    est = estimate_cost(facts, openai_card(api_style=C))
    assert est.status == "incomplete"
    assert line(est, Category.OUTPUT).status is LineStatus.UNKNOWN_TOKENS
    assert est.upper is None


def test_total_input_not_reported_is_incomplete():
    facts = normalize_usage(C, {"completion_tokens": 10})
    est = estimate_cost(facts, openai_card(api_style=C))
    assert est.status == "incomplete"
    assert line(est, Category.INPUT_UNCACHED).status is LineStatus.UNKNOWN_TOKENS


def test_inconsistent_usage_is_incomplete(usage_fixture):
    card = openai_card(write=per_million("1.5625"))
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_inconsistent")), card)
    assert est.status == "incomplete"
    assert est.upper is None
    assert est.lower == 0  # inconsistent counts make both bounds untrustworthy
    assert any("inconsistent usage" in reason for reason in est.reasons)


def test_negative_remainder_when_cache_field_is_folded_into_nothing():
    # Reads alone exceed the total: the remainder would be negative.
    facts = normalize_usage(R, {"input_tokens": 10, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 20}})
    est = estimate_cost(facts, openai_card())
    assert est.status == "incomplete"


def test_unrecognized_and_modality_fields_make_estimate_incomplete(usage_fixture):
    card = openai_card(api_style=C)
    audio = estimate_cost(normalize_usage(C, usage_fixture("chat_audio")), card)
    assert audio.status == "incomplete" and audio.upper is None
    unknown = estimate_cost(normalize_usage(C, usage_fixture("chat_unknown_field")), card)
    assert unknown.status == "incomplete"
    assert any("mystery_tokens" in reason for reason in unknown.reasons)


def test_informational_fields_do_not_affect_cost(usage_fixture):
    est = estimate_cost(normalize_usage(C, usage_fixture("chat_text_tokens")), openai_card(api_style=C))
    assert est.status == "complete"


def test_service_tier_mismatch_is_incomplete():
    facts = normalize_usage(
        R,
        {"input_tokens": 10, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}},
        service_tier="priority",
    )
    est = estimate_cost(facts, openai_card())
    assert est.status == "incomplete"
    assert est.lower == 0 and est.upper is None
    default = normalize_usage(
        R,
        {"input_tokens": 10, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}},
        service_tier="default",
    )
    assert estimate_cost(default, openai_card()).status == "complete"


def test_context_tiers_select_by_total_input():
    long_tier = RateTier(
        above_input_tokens=272_000,
        categories={
            Category.INPUT_UNCACHED: per_million("5"),
            Category.CACHE_READ: per_million("0.5"),
            Category.CACHE_WRITE: NA,
            Category.OUTPUT: per_million("22.5"),
        },
    )
    base = openai_card(input_price="2.5", cached=per_million("0.25"), output=per_million("15")).tiers[0]
    card = RateCard(api_style=R, tiers=[long_tier, base])

    def run(total: int) -> CostEstimate:
        usage = {"input_tokens": total, "output_tokens": 0, "input_tokens_details": {"cached_tokens": 0}}
        return estimate_cost(normalize_usage(R, usage), card)

    at_threshold = run(272_000)
    assert at_threshold.tier_above_input == 0
    assert at_threshold.total == Decimal("0.68")
    above = run(272_001)
    assert above.tier_above_input == 272_000
    assert above.total == Decimal("1.360005")
    below = run(271_999)
    assert below.tier_above_input == 0


def test_unknown_total_with_tiers_reports_bounds_across_tiers():
    long_tier = RateTier(
        above_input_tokens=100_000,
        categories={
            Category.INPUT_UNCACHED: per_million("8"),
            Category.CACHE_READ: per_million("0.8"),
            Category.CACHE_WRITE_5M: per_million("10"),
            Category.CACHE_WRITE_1H: per_million("16"),
            Category.OUTPUT: per_million("40"),
        },
    )
    card = RateCard(api_style=A, tiers=[*claude_card().tiers, long_tier])
    facts = normalize_usage(A, {"input_tokens": 1000, "output_tokens": 100})  # cache fields missing
    est = estimate_cost(facts, card)
    assert est.status == "incomplete"
    assert est.tier_above_input is None
    assert any("tier unknown" in reason for reason in est.reasons)


def test_claude_split_writes(usage_fixture):
    est = estimate_cost(normalize_usage(A, usage_fixture("claude_cache_write_split")), claude_card())
    assert est.status == "complete"
    # 2048x4 + 1800x0.2 + 148x5 + 100x8 + 503x20 per million
    assert est.total == Decimal("0.020152")


def test_claude_cache_read_example(usage_fixture):
    est = estimate_cost(normalize_usage(A, usage_fixture("claude_cache_read")), claude_card())
    assert est.status == "complete"
    assert est.total == Decimal("0.03026")  # 50x4 + 100000x0.2 + 503x20 per million


def test_claude_write_ttl_inferred_from_request(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_cache_write_no_split"))
    est = estimate_cost(facts, claude_card(), CostContext(requested_cache_ttl="1h"))
    assert est.status == "complete"
    assert line(est, Category.CACHE_WRITE_1H).tokens == 2000
    assert line(est, Category.CACHE_WRITE_5M).tokens == 0
    assert est.assumptions and "inferred" in est.assumptions[0]
    assert est.total == Decimal("0.01812")  # 30x4 + 2000x8 + 100x20 per million


def test_claude_write_ttl_unknown_gives_bounds(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_cache_write_no_split"))
    est = estimate_cost(facts, claude_card())
    assert est.status == "incomplete"
    assert est.lower == Decimal("0.01212")
    assert est.upper == Decimal("0.01812")


def test_claude_write_ttl_unknown_with_equal_rates_is_complete(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_cache_write_no_split"))
    est = estimate_cost(facts, claude_card(write_5m="6", write_1h="6"))
    assert est.status == "complete"
    assert est.total == Decimal("0.01412")


def _claude_card_with_writes(write_entry):
    return RateCard(
        api_style=A,
        tiers=[
            RateTier(
                categories={
                    Category.INPUT_UNCACHED: per_million("4"),
                    Category.CACHE_READ: per_million("0.2"),
                    Category.CACHE_WRITE_5M: write_entry,
                    Category.CACHE_WRITE_1H: write_entry,
                    Category.OUTPUT: per_million("20"),
                }
            )
        ],
    )


def test_claude_unsplit_writes_fold_when_user_declares_not_applicable(usage_fixture):
    from .rates import NA_USER

    est = estimate_cost(
        normalize_usage(A, usage_fixture("claude_cache_write_no_split")), _claude_card_with_writes(NA_USER)
    )
    assert est.status == "complete"
    assert line(est, Category.INPUT_UNCACHED).tokens == 2030


def test_claude_unsplit_writes_with_preset_not_applicable_are_incomplete(usage_fixture):
    est = estimate_cost(normalize_usage(A, usage_fixture("claude_cache_write_no_split")), _claude_card_with_writes(NA))
    assert est.status == "incomplete"
    # 30 uncached x 4 + 100 output x 20 per million; the 2000 written tokens have no trusted rate
    assert est.lower == Decimal("0.00212")
    assert est.upper is None
    assert any("profile preset" in reason for reason in est.reasons)


def test_claude_one_ttl_not_applicable_pools_with_input_rate(usage_fixture):
    from .rates import NA_USER

    card = RateCard(
        api_style=A,
        tiers=[
            RateTier(
                categories={
                    Category.INPUT_UNCACHED: per_million("4"),
                    Category.CACHE_READ: per_million("0.2"),
                    Category.CACHE_WRITE_5M: NA_USER,
                    Category.CACHE_WRITE_1H: per_million("8"),
                    Category.OUTPUT: per_million("20"),
                }
            )
        ],
    )
    est = estimate_cost(normalize_usage(A, usage_fixture("claude_cache_write_no_split")), card)
    assert est.status == "incomplete"
    assert est.pools[0].categories == [Category.INPUT_UNCACHED, Category.CACHE_WRITE_1H]


def test_claude_folded_cache_read(usage_fixture):
    card = claude_card(cache_read=CategoryRate.not_applicable("user"))
    est = estimate_cost(normalize_usage(A, usage_fixture("claude_cache_read")), card)
    assert est.status == "complete"
    assert line(est, Category.INPUT_UNCACHED).tokens == 100050


def test_claude_folded_category_with_unknown_tokens():
    card = claude_card(cache_read=CategoryRate.not_applicable("user"))
    facts = normalize_usage(A, {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0})
    est = estimate_cost(facts, card)
    assert est.status == "incomplete"
    assert line(est, Category.INPUT_UNCACHED).status is LineStatus.UNKNOWN_TOKENS


def test_claude_missing_write_field_is_incomplete(usage_fixture):
    est = estimate_cost(normalize_usage(A, usage_fixture("claude_missing_cache_fields")), claude_card())
    assert est.status == "incomplete"


def test_claude_thinking_not_double_charged(usage_fixture):
    est = estimate_cost(normalize_usage(A, usage_fixture("claude_thinking")), claude_card())
    assert est.status == "complete"
    assert est.total == Decimal("0.0516")  # 900x4 + 2400x20 per million; thinking is inside output


def test_custom_override_is_flagged_on_line():
    from foundry_eval.pricing.ratecard import RateSource

    card = openai_card(output=per_million("9", source=RateSource.CUSTOM_OVERRIDE))
    facts = normalize_usage(R, {"input_tokens": 0, "output_tokens": 1000, "input_tokens_details": {"cached_tokens": 0}})
    est = estimate_cost(facts, card)
    assert line(est, Category.OUTPUT).custom_rate is True
    assert est.total == Decimal("0.009")


def test_per_thousand_units():
    from .rates import per_thousand

    card = openai_card(output=per_thousand("0.0075"))
    facts = normalize_usage(R, {"input_tokens": 0, "output_tokens": 2000, "input_tokens_details": {"cached_tokens": 0}})
    assert estimate_cost(facts, card).total == Decimal("0.015")


def test_sum_estimates_complete_and_incomplete():
    a = CostEstimate(
        status="complete", currency="USD", total=Decimal("0.1"), lower=Decimal("0.1"), upper=Decimal("0.1")
    )
    b = CostEstimate.not_sent()
    total = sum_estimates([a, b])
    assert total.status == "complete" and total.total == Decimal("0.1")
    c = CostEstimate.usage_missing("USD", "x")
    partial = sum_estimates([a, c])
    assert partial.status == "incomplete"
    assert partial.lower == Decimal("0.1") and partial.upper is None


def test_sum_estimates_rejects_mixed_currency():
    import pytest

    eur = CostEstimate(status="complete", currency="EUR", total=Decimal(1), lower=Decimal(1), upper=Decimal(1))
    with pytest.raises(ValueError, match="cannot add"):
        sum_estimates([eur])


def test_money_serializes_as_plain_decimal_text():
    est = CostEstimate(
        status="complete", currency="USD", total=Decimal("1E-7"), lower=Decimal("1E-7"), upper=Decimal("1E-7")
    )
    dumped = est.model_dump(mode="json")
    assert dumped["total"] == "0.0000001"
    assert isinstance(dumped["lower"], str)


def test_claude_uncached_not_reported():
    facts = normalize_usage(A, {"output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    est = estimate_cost(facts, claude_card())
    assert line(est, Category.INPUT_UNCACHED).status is LineStatus.UNKNOWN_TOKENS


def test_assumptions_are_deduplicated_across_unknown_tiers():
    long_tier = RateTier(
        above_input_tokens=100_000,
        categories={
            c: per_million("9")
            for c in (
                Category.INPUT_UNCACHED,
                Category.CACHE_READ,
                Category.CACHE_WRITE_5M,
                Category.CACHE_WRITE_1H,
                Category.OUTPUT,
            )
        },
    )
    card = RateCard(api_style=A, tiers=[*claude_card().tiers, long_tier])
    facts = normalize_usage(A, {"input_tokens": 10, "cache_creation_input_tokens": 20, "output_tokens": 1})
    est = estimate_cost(facts, card, CostContext(requested_cache_ttl="5m"))
    assert len(est.assumptions) == 1
    assert est.status == "incomplete"


def test_preset_not_applicable_with_reported_tokens_is_flagged(usage_fixture):
    # A GPT-5.6 response reports cache writes, but the profile still uses the pre-5.6 preset.
    est = estimate_cost(normalize_usage(R, usage_fixture("responses_cache_write")), openai_card())
    assert est.status == "incomplete"
    assert any("marked not applicable only by the profile preset" in reason for reason in est.reasons)
    assert est.upper is None


def test_user_declared_not_applicable_is_trusted(usage_fixture):
    from .rates import NA_USER

    est = estimate_cost(normalize_usage(R, usage_fixture("responses_cache_write")), openai_card(write=NA_USER))
    assert est.status == "complete"
    # cache writes are billed as uncached input: (4000 - 1000) x 1.25 + 1000 x 0.125 + 500 x 10
    assert est.total == Decimal("0.00887500")


def test_preset_not_applicable_cache_read_does_not_inflate_lower_bound():
    from foundry_eval.pricing.ratecard import CategoryRate

    card = openai_card(
        api_style=C,
        input_price="0.375",
        cached=CategoryRate.not_applicable("preset generic-chat", declared_by="preset"),
        output=per_million("1.5"),
    )
    facts = normalize_usage(
        C, {"prompt_tokens": 2000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1000}}
    )
    est = estimate_cost(facts, card)
    assert est.status == "incomplete"
    # Only the 1000 uncached tokens and the output are certain: the cached tokens may have a lower rate.
    assert est.lower == Decimal("0.000525")
    assert line(est, Category.CACHE_READ).status is LineStatus.UNKNOWN_RATE


def test_partial_ttl_breakdown_blocks_request_ttl_inference():
    facts = normalize_usage(
        A,
        {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 100,
            "output_tokens": 1,
            "cache_creation": {"ephemeral_1h_input_tokens": 0},
        },
    )
    assert facts.cache_write_split_partial
    est = estimate_cost(facts, claude_card(), CostContext(requested_cache_ttl="1h"))
    assert est.status == "incomplete"
    assert est.assumptions == []


def test_rate_in_other_currency_is_unknown():
    from foundry_eval.pricing.ratecard import CategoryRate, Rate, RateSource

    eur = CategoryRate.priced(
        Rate(
            price=Decimal("9"),
            unit_tokens=1_000_000,
            unit_label="1M",
            currency="EUR",
            source=RateSource.CUSTOM_OVERRIDE,
        )
    )
    est = estimate_cost(
        normalize_usage(R, {"input_tokens": 0, "output_tokens": 500, "input_tokens_details": {"cached_tokens": 0}}),
        openai_card(output=eur),
    )
    assert est.status == "incomplete"
    assert any("EUR" in reason for reason in est.reasons)


def test_unrecognized_service_tier_is_incomplete():
    facts = normalize_usage(
        R,
        {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}},
        service_tier=" on_demand ",
    )
    est = estimate_cost(facts, openai_card())
    assert est.status == "incomplete" and est.lower == 0
    padded = normalize_usage(
        R,
        {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}},
        service_tier=" priority",
    )
    assert estimate_cost(padded, openai_card()).status == "incomplete"


def test_negative_rates_and_cards_without_base_tier_are_rejected():
    import pytest
    from pydantic import ValidationError

    from foundry_eval.pricing.ratecard import Rate, RateSource

    with pytest.raises(ValidationError):
        Rate(price=Decimal("-1"), unit_tokens=1000, unit_label="1K", source=RateSource.MANUAL)
    with pytest.raises(ValidationError):
        RateCard(api_style=R, tiers=[RateTier(above_input_tokens=1000)])
    with pytest.raises(ValidationError):
        RateCard(api_style=R, tiers=[RateTier(), RateTier()])
