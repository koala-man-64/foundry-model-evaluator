"""Rate cards: per-category token rates, optionally tiered by request input size."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..money import MILLION, Money, div, mul
from ..types import ApiStyle


class Category(StrEnum):
    INPUT_UNCACHED = "input_uncached"
    CACHE_READ = "cache_read"
    CACHE_WRITE = "cache_write"  # OpenAI-style single cache-write category (no TTL)
    CACHE_WRITE_5M = "cache_write_5m"  # Claude 5-minute cache writes
    CACHE_WRITE_1H = "cache_write_1h"  # Claude 1-hour cache writes
    OUTPUT = "output"


OPENAI_CATEGORIES: tuple[Category, ...] = (
    Category.INPUT_UNCACHED,
    Category.CACHE_READ,
    Category.CACHE_WRITE,
    Category.OUTPUT,
)
CLAUDE_CATEGORIES: tuple[Category, ...] = (
    Category.INPUT_UNCACHED,
    Category.CACHE_READ,
    Category.CACHE_WRITE_5M,
    Category.CACHE_WRITE_1H,
    Category.OUTPUT,
)
CATEGORY_LABELS: dict[Category, str] = {
    Category.INPUT_UNCACHED: "Uncached input",
    Category.CACHE_READ: "Cache read",
    Category.CACHE_WRITE: "Cache write",
    Category.CACHE_WRITE_5M: "Cache write (5 min)",
    Category.CACHE_WRITE_1H: "Cache write (1 hour)",
    Category.OUTPUT: "Output",
}


def categories_for(api_style: ApiStyle) -> tuple[Category, ...]:
    return OPENAI_CATEGORIES if api_style.is_openai else CLAUDE_CATEGORIES


class RateSource(StrEnum):
    AZURE_AUTO = "azure_auto"
    AZURE_PINNED = "azure_pinned"
    MANUAL = "manual"
    CUSTOM_OVERRIDE = "custom_override"


class RateStatus(StrEnum):
    PRICED = "priced"
    NOT_APPLICABLE = "not_applicable"  # no separate price: these tokens are billed as uncached input
    UNKNOWN = "unknown"


class Rate(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Money  # price per unit
    unit_tokens: int = Field(gt=0)
    unit_label: str
    currency: str = "USD"
    source: RateSource
    source_url: str | None = None
    meter_id: str | None = None
    sku_id: str | None = None
    meter_name: str | None = None
    product_name: str | None = None
    region: str | None = None
    effective_date: str | None = None
    retrieved_at: str | None = None
    note: str | None = None
    # When ``source`` is a custom override, the underlying base rate (for display/audit).
    base_price: Money | None = None
    base_unit_tokens: int | None = None
    base_source: RateSource | None = None

    @field_validator("price")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("rates cannot be negative")
        return value

    @property
    def per_million(self) -> Decimal:
        return div(mul(self.price, MILLION), Decimal(self.unit_tokens))

    def cost(self, tokens: int) -> Decimal:
        return div(mul(Decimal(tokens), self.price), Decimal(self.unit_tokens))


class CategoryRate(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: RateStatus
    rate: Rate | None = None
    reason: str | None = None  # why unknown, or why not applicable
    # Who declared the category not applicable: a preset default or the user explicitly.
    declared_by: Literal["preset", "user"] | None = None

    @classmethod
    def unknown(cls, reason: str = "no rate configured") -> CategoryRate:
        return cls(status=RateStatus.UNKNOWN, reason=reason)

    @classmethod
    def not_applicable(cls, reason: str, declared_by: Literal["preset", "user"] = "user") -> CategoryRate:
        return cls(status=RateStatus.NOT_APPLICABLE, reason=reason, declared_by=declared_by)

    @classmethod
    def priced(cls, rate: Rate) -> CategoryRate:
        return cls(status=RateStatus.PRICED, rate=rate)


class RateTier(BaseModel):
    model_config = ConfigDict(frozen=True)

    # This tier applies to requests whose total input tokens exceed this threshold (0 = base tier).
    above_input_tokens: int = Field(default=0, ge=0)
    categories: dict[Category, CategoryRate] = Field(default_factory=dict)

    def get(self, category: Category) -> CategoryRate:
        return self.categories.get(category) or CategoryRate.unknown()


class RateCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_style: ApiStyle
    currency: str = "USD"
    processing_tier: str = "standard"
    tiers: list[RateTier]
    explanations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_base_tier(self) -> RateCard:
        if not any(t.above_input_tokens == 0 for t in self.tiers):
            raise ValueError("a rate card needs a base tier (above_input_tokens = 0)")
        if len({t.above_input_tokens for t in self.tiers}) != len(self.tiers):
            raise ValueError("rate card tiers must have distinct thresholds")
        return self

    def sorted_tiers(self) -> list[RateTier]:
        return sorted(self.tiers, key=lambda t: t.above_input_tokens)

    def tier_for(self, total_input: int) -> RateTier:
        chosen = self.sorted_tiers()[0]
        for tier in self.sorted_tiers():
            if tier.above_input_tokens == 0 or total_input > tier.above_input_tokens:
                chosen = tier
        return chosen
