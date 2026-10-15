from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from foundry_eval.analysis.projections import WhatIf, scenario_usage
from foundry_eval.pricing.calculator import estimate_cost
from foundry_eval.pricing.ratecard import Category, RateTier
from foundry_eval.types import ApiStyle
from foundry_eval.usage.facts import UsageFacts
from tests.unit.rates import openai_card, per_million


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
    scenario, reason = scenario_usage(facts, WhatIf())
    assert reason is None and scenario is not None
    assert (scenario.input_total, scenario.input_uncached, scenario.cache_read, scenario.cache_write) == (
        1000,
        800,
        200,
        0,
    )
    assert (scenario.output_total, scenario.reasoning) == (100, 40)


def test_multipliers_scale_each_part_and_round_half_up():
    scenario, _ = scenario_usage(
        openai_facts(total=1001, read=201, write=0, output=101), WhatIf(input_multiplier="1.5", output_multiplier="0.5")
    )
    assert scenario is not None
    # 800 * 1.5 = 1200, 201 * 1.5 = 301.5 -> 302; the total stays the exact sum of the parts.
    assert (scenario.input_uncached, scenario.cache_read, scenario.input_total) == (1200, 302, 1502)
    assert scenario.output_total == 51  # 50.5 rounds half up


def test_cache_shares_replace_the_measured_split():
    scenario, _ = scenario_usage(
        openai_facts(read=None, write=None), WhatIf(cache_read_share="0.3", cache_write_share="0.1")
    )
    assert scenario is not None
    assert (scenario.cache_read, scenario.cache_write, scenario.input_uncached) == (300, 100, 600)


def test_shares_that_round_over_the_total_are_clamped():
    scenario, _ = scenario_usage(openai_facts(total=3), WhatIf(cache_read_share="0.5", cache_write_share="0.5"))
    assert scenario is not None
    assert scenario.cache_read + scenario.cache_write <= 3 and scenario.input_uncached >= 0


def test_shares_are_ignored_for_categories_that_are_not_billed_separately():
    scenario, _ = scenario_usage(openai_facts(write=0), WhatIf(cache_write_share="0.5"), write_applies=False)
    assert scenario is not None and scenario.cache_write == 0


def test_unknown_counts_without_a_share_stay_unknown():
    scenario, _ = scenario_usage(openai_facts(write=None), WhatIf())
    assert scenario is not None and scenario.cache_write is None and scenario.input_uncached is None


def test_missing_totals_cannot_be_projected():
    facts = UsageFacts(api_style=ApiStyle.OPENAI_CHAT, output_total=10)
    assert scenario_usage(facts, WhatIf()) == (None, "total input tokens not reported")
    facts = UsageFacts(api_style=ApiStyle.OPENAI_CHAT, input_total=10)
    assert scenario_usage(facts, WhatIf()) == (None, "output tokens not reported")


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
    scenario, _ = scenario_usage(facts, WhatIf(cache_write_share="0.6"))
    assert scenario is not None
    assert (scenario.cache_write, scenario.cache_write_5m, scenario.cache_write_1h) == (600, 200, 400)
    assert scenario.input_total == 1000 and scenario.input_uncached == 300


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
    small, _ = scenario_usage(facts, WhatIf())
    large, _ = scenario_usage(facts, WhatIf(input_multiplier="2"))
    assert small is not None and large is not None
    small_cost = estimate_cost(small, card)
    large_cost = estimate_cost(large, card)
    assert small_cost.tier_above_input == 0 and large_cost.tier_above_input == 272_000
    assert small_cost.total == Decimal("0.5") + Decimal("0.015")
    assert large_cost.total == Decimal("2") + Decimal("0.0225")
