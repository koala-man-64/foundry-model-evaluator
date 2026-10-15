"""Rate-card builders shared by pricing tests."""

from __future__ import annotations

from decimal import Decimal

from foundry_eval.pricing.ratecard import Category, CategoryRate, Rate, RateCard, RateSource, RateTier
from foundry_eval.types import ApiStyle


def per_million(price: str, source: RateSource = RateSource.MANUAL) -> CategoryRate:
    return CategoryRate.priced(
        Rate(
            price=Decimal(price),
            unit_tokens=1_000_000,
            unit_label="1M",
            source=source,
            source_url="https://example.test/p",
        )
    )


def per_thousand(price: str) -> CategoryRate:
    return CategoryRate.priced(
        Rate(price=Decimal(price), unit_tokens=1_000, unit_label="1K", source=RateSource.AZURE_AUTO)
    )


NA = CategoryRate.not_applicable("preset: no cache-write charge", declared_by="preset")
NA_USER = CategoryRate.not_applicable("user: no separate charge", declared_by="user")
UNKNOWN = CategoryRate.unknown()


def openai_card(
    *,
    input_price: str = "1.25",
    cached: CategoryRate | None = None,
    write: CategoryRate | None = None,
    output: CategoryRate | None = None,
    tiers: list[RateTier] | None = None,
    processing_tier: str = "standard",
    api_style: ApiStyle = ApiStyle.OPENAI_RESPONSES,
) -> RateCard:
    base = RateTier(
        categories={
            Category.INPUT_UNCACHED: per_million(input_price),
            Category.CACHE_READ: cached if cached is not None else per_million("0.125"),
            Category.CACHE_WRITE: write if write is not None else NA,
            Category.OUTPUT: output if output is not None else per_million("10"),
        }
    )
    return RateCard(api_style=api_style, tiers=tiers or [base], processing_tier=processing_tier)


def claude_card(*, write_5m: str = "5", write_1h: str = "8", cache_read: CategoryRate | None = None) -> RateCard:
    return RateCard(
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        tiers=[
            RateTier(
                categories={
                    Category.INPUT_UNCACHED: per_million("4"),
                    Category.CACHE_READ: cache_read if cache_read is not None else per_million("0.20"),
                    Category.CACHE_WRITE_5M: per_million(write_5m),
                    Category.CACHE_WRITE_1H: per_million(write_1h),
                    Category.OUTPUT: per_million("20"),
                }
            )
        ],
    )
