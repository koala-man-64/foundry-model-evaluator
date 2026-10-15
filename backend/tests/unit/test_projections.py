from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from foundry_eval.analysis.projections import ScenarioUsage, WhatIf, _split, _stable_reason, scenario_usage
from foundry_eval.pricing.calculator import CostContext, estimate_cost
from foundry_eval.pricing.ratecard import Category, RateTier
from foundry_eval.types import ApiStyle
from foundry_eval.usage.facts import UsageFacts
from tests.unit.rates import claude_card, openai_card, per_million


def openai_facts(total: int = 1000, read: int | None = 200, write: int | None = 0, output: int = 100) -> UsageFacts:
    return UsageFacts(
        api_style=ApiStyle.OPENAI_RESPONSES,
        input_total=total,
        input_uncached=None if read is None or write is None else total - read - write,
        input_uncached_derived=True,
        cache_read=read,
        cache_write=write,
        output_total=output,
        reasoning=40,
    )


def test_identity_scenario_keeps_measured_usage():
    facts = openai_facts()
    scenario, reason, assumptions = scenario_usage(facts, WhatIf())
    assert reason is None and scenario is not None and assumptions == []
    assert (scenario.input_total, scenario.input_uncached, scenario.cache_read, scenario.cache_write) == (
        1000,
        800,
        200,
        0,
    )
    assert (scenario.output_total, scenario.reasoning) == (100, 40)


def test_multipliers_scale_each_part_and_round_half_up():
    scenario = scenario_usage(
        openai_facts(total=1001, read=201, write=0, output=101), WhatIf(input_multiplier="1.5", output_multiplier="0.5")
    ).usage
    assert scenario is not None
    # 1001 * 1.5 = 1501.5 -> 1502, split as 800 : 201 : 0 with the largest remainder; the parts add up.
    assert (scenario.input_uncached, scenario.cache_read, scenario.input_total) == (1200, 302, 1502)
    assert scenario.output_total == 51  # 50.5 rounds half up


def test_cache_shares_replace_the_measured_split():
    scenario = scenario_usage(
        openai_facts(read=None, write=None), WhatIf(cache_read_share="0.3", cache_write_share="0.1")
    ).usage
    assert scenario is not None
    assert (scenario.cache_read, scenario.cache_write, scenario.input_uncached) == (300, 100, 600)


def test_shares_that_round_over_the_total_are_clamped():
    scenario = scenario_usage(openai_facts(total=3), WhatIf(cache_read_share="0.5", cache_write_share="0.5")).usage
    assert scenario is not None
    assert (scenario.cache_read, scenario.cache_write, scenario.input_uncached) == (2, 1, 0)


def test_shares_are_ignored_for_categories_that_are_not_billed_separately():
    scenario = scenario_usage(openai_facts(write=0), WhatIf(cache_write_share="0.5"), write_applies=False).usage
    assert scenario is not None and scenario.cache_write == 0


def test_unknown_counts_without_a_share_stay_unknown():
    scenario = scenario_usage(openai_facts(write=None), WhatIf()).usage
    assert scenario is not None and scenario.cache_write is None and scenario.input_uncached is None


def test_missing_totals_cannot_be_projected():
    facts = UsageFacts(api_style=ApiStyle.OPENAI_CHAT, output_total=10)
    assert scenario_usage(facts, WhatIf()) == ScenarioUsage(None, "total input tokens not reported", [])
    facts = UsageFacts(api_style=ApiStyle.OPENAI_CHAT, input_total=10)
    assert scenario_usage(facts, WhatIf()) == ScenarioUsage(None, "output tokens not reported", [])


def test_claude_ttl_mix_is_kept_when_write_volume_changes():
    facts = UsageFacts(
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        input_total=1000,
        input_uncached=600,
        cache_read=100,
        cache_write=300,
        cache_write_5m=100,
        cache_write_1h=200,
        output_total=50,
    )
    scenario, _, assumptions = scenario_usage(facts, WhatIf(cache_write_share="0.6"))
    assert scenario is not None
    assert (scenario.cache_write, scenario.cache_write_5m, scenario.cache_write_1h) == (600, 200, 400)
    # The other 40% keeps the measured mix of uncached input and cache reads (600 : 100).
    assert (scenario.input_total, scenario.input_uncached, scenario.cache_read) == (1000, 343, 57)
    assert assumptions == ["the rest of the input keeps its measured mix of uncached input and cache reads"]


def test_what_if_validation():
    with pytest.raises(ValidationError, match="cannot exceed 1"):
        WhatIf(cache_read_share="0.7", cache_write_share="0.4")
    with pytest.raises(ValidationError, match="greater than 0"):
        WhatIf(input_multiplier="0")
    with pytest.raises(ValidationError, match="between 0 and 1"):
        WhatIf(cache_read_share="1.5")
    with pytest.raises(ValidationError):
        WhatIf(input_multiplier=1.5)  # floats are rejected: send decimal strings


def test_labels_describe_every_assumption():
    labels = WhatIf(input_multiplier="2", output_multiplier="0.5", cache_read_share="0.25").labels()
    assert labels == ["input length x2", "output length x0.5", "cache reads 25% of input"]


def test_scenario_input_can_cross_into_the_long_context_tier():
    long_tier = RateTier(
        above_input_tokens=272_000,
        categories={
            Category.INPUT_UNCACHED: per_million("5"),
            Category.CACHE_READ: per_million("0.5"),
            Category.CACHE_WRITE: per_million("6.25"),
            Category.OUTPUT: per_million("22.5"),
        },
    )
    base = RateTier(
        categories={
            Category.INPUT_UNCACHED: per_million("2.5"),
            Category.CACHE_READ: per_million("0.25"),
            Category.CACHE_WRITE: per_million("3.125"),
            Category.OUTPUT: per_million("15"),
        }
    )
    card = openai_card(tiers=[base, long_tier])
    facts = openai_facts(total=200_000, read=0, write=0, output=1000)
    small = scenario_usage(facts, WhatIf()).usage
    large = scenario_usage(facts, WhatIf(input_multiplier="2")).usage
    assert small is not None and large is not None
    small_cost = estimate_cost(small, card)
    large_cost = estimate_cost(large, card)
    assert small_cost.tier_above_input == 0 and large_cost.tier_above_input == 272_000
    assert small_cost.total == Decimal("0.5") + Decimal("0.015")
    assert large_cost.total == Decimal("2") + Decimal("0.0225")


def claude_facts(**values) -> UsageFacts:
    return UsageFacts(api_style=ApiStyle.ANTHROPIC_MESSAGES, output_total=50, **values)


def long_context_card():
    def tier(above: int, factor: int) -> RateTier:
        return RateTier(
            above_input_tokens=above,
            categories={
                Category.INPUT_UNCACHED: per_million(str(Decimal("2.5") * factor)),
                Category.CACHE_READ: per_million(str(Decimal("0.25") * factor)),
                Category.CACHE_WRITE: per_million(str(Decimal("3.125") * factor)),
                Category.OUTPUT: per_million("15"),
            },
        )

    return openai_card(tiers=[tier(0, 1), tier(272_000, 2)])


def test_the_scaled_total_is_rounded_once_so_rounding_cannot_cross_a_tier():
    # 181,333 * 1.5 = 271,999.5 -> 272,000, which is not above the 272,000 threshold. Rounding each
    # part on its own gave 271,997 + 2 + 2 = 272,001 and the long-context price.
    facts = openai_facts(total=181_333, read=1, write=1, output=10)
    scenario = scenario_usage(facts, WhatIf(input_multiplier="1.5")).usage
    assert scenario is not None and scenario.input_total == 272_000
    assert scenario.input_uncached + scenario.cache_read + scenario.cache_write == 272_000
    assert estimate_cost(scenario, long_context_card()).tier_above_input == 0


def test_a_share_keeps_the_measured_mix_of_the_other_categories():
    # 4,000 input with 3,000 measured cache writes; cache reads set to half the input.
    facts = openai_facts(total=4000, read=0, write=3000, output=10)
    scenario, _, assumptions = scenario_usage(facts, WhatIf(cache_read_share="0.5"))
    assert scenario is not None
    assert (scenario.cache_read, scenario.cache_write, scenario.input_uncached) == (2000, 1500, 500)
    assert assumptions == ["the rest of the input keeps its measured mix of uncached input and cache writes"]


def test_a_zero_share_sets_its_category_to_zero():
    scenario, _, assumptions = scenario_usage(openai_facts(read=200, write=0), WhatIf(cache_read_share="0"))
    assert scenario is not None
    assert (scenario.cache_read, scenario.cache_write, scenario.input_uncached) == (0, 0, 1000)
    assert assumptions == []


def test_known_counts_that_do_not_fit_the_shares_are_reduced_and_labelled():
    # Cache writes not reported (so uncached input is unknown); cache reads measured at 60% of input.
    facts = openai_facts(total=1000, read=600, write=None, output=10)
    scenario, _, assumptions = scenario_usage(facts, WhatIf(cache_write_share="0.6"))
    assert scenario is not None
    assert (scenario.cache_read, scenario.cache_write, scenario.input_uncached) == (400, 600, 0)
    assert assumptions == ["cache reads reduced to fit the cache shares"]


def test_a_partial_ttl_breakdown_is_not_trusted_in_a_scenario():
    facts = claude_facts(
        input_total=4000,
        input_uncached=1000,
        cache_read=0,
        cache_write=3000,
        cache_write_split_partial=True,
    )
    scenario = scenario_usage(facts, WhatIf(), requested_ttl="5m").usage
    assert scenario is not None and scenario.cache_write_split_partial
    assert scenario.cache_write_5m is None and scenario.cache_write_1h is None
    estimate = estimate_cost(scenario, claude_card(), CostContext(requested_cache_ttl="5m"))
    assert estimate.status == "incomplete"  # the TTL is not inferred from the request either


def test_scenario_cache_writes_use_an_assumed_ttl_only_when_nothing_tells_it():
    facts = claude_facts(
        input_total=9000, input_uncached=9000, cache_read=0, cache_write=0, cache_write_5m=0, cache_write_1h=0
    )
    scenario, _, assumptions = scenario_usage(facts, WhatIf(cache_write_share="0.3"))
    assert scenario is not None
    assert (scenario.cache_write, scenario.cache_write_5m, scenario.cache_write_1h) == (2700, 2700, 0)
    assert assumptions == ["cache writes priced at the 5-minute cache rate (assumed TTL)"]
    assert estimate_cost(scenario, claude_card()).status == "complete"

    scenario, _, assumptions = scenario_usage(facts, WhatIf(cache_write_share="0.3", cache_write_ttl="1h"))
    assert scenario is not None and (scenario.cache_write_5m, scenario.cache_write_1h) == (0, 2700)
    assert assumptions == ["cache writes priced at the 1-hour cache rate (assumed TTL)"]

    # The request's cache TTL is better evidence: the calculator infers it, as for measured usage.
    scenario, _, assumptions = scenario_usage(facts, WhatIf(cache_write_share="0.3"), requested_ttl="1h")
    assert scenario is not None and scenario.cache_write_5m is None and assumptions == []
    estimate = estimate_cost(scenario, claude_card(), CostContext(requested_cache_ttl="1h"))
    assert estimate.status == "complete" and any("inferred from the request (1h)" in a for a in estimate.assumptions)


def test_largest_remainder_split_adds_up():
    assert _split(10, {"a": Decimal(1) / 3, "b": Decimal(1) / 3, "c": Decimal(1) / 3}) == {"a": 4, "b": 3, "c": 3}
    assert _split(0, {"a": Decimal("0.5"), "b": Decimal("0.5")}) == {"a": 0, "b": 0}
    assert _split(7, {"a": Decimal(1), "b": Decimal(0)}) == {"a": 7, "b": 0}


def test_exclusion_reasons_are_grouped_without_token_counts():
    assert _stable_reason("no rate for uncached input (1,000 tokens)") == "no rate for uncached input"
    assert _stable_reason("2700 tokens: cache-write TTL split not reported") == "cache-write TTL split not reported"
    assert _stable_reason("unrecognized usage field mystery_tokens = 7") == "unrecognized usage field mystery_tokens"
    assert _stable_reason("audio_tokens = 5: non-text tokens are not priced") == (
        "audio_tokens: non-text tokens are not priced"
    )


def test_split_corrects_fractions_that_sum_to_more_than_one():
    parts = _split(10, {"a": Decimal("0.6"), "b": Decimal("0.6")})
    assert sum(parts.values()) == 10 and all(value >= 0 for value in parts.values())


def test_a_share_with_no_measured_rest_gives_the_rest_to_uncached_input():
    # Every measured input token was a cache read; with reads at 50%, the rest is uncached input.
    scenario, _, assumptions = scenario_usage(
        openai_facts(total=1000, read=1000, write=0), WhatIf(cache_read_share="0.5")
    )
    assert scenario is not None
    assert (scenario.cache_read, scenario.cache_write, scenario.input_uncached) == (500, 0, 500)
    assert assumptions == []
