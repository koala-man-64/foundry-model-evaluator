"""Estimated token cost over disjoint billing categories, using Decimal arithmetic only.

The calculator never treats an unknown as zero:

* A category whose token count is known to be zero costs nothing, whatever its rate.
* A category marked ``not_applicable`` has no separate price: its tokens are billed as uncached
  input. This is the only way an unreported cache read or write can be folded into input.
* When the provider reports an aggregate but not its split (for example total input without
  cache writes), the unassigned tokens form a *pool* priced between the cheapest and the most
  expensive possible rate. The estimate is then incomplete, with bounds.
* Reasoning tokens are part of billed output and are never priced separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ..money import ZERO, Money, add
from ..usage.facts import UsageFacts
from .ratecard import (
    CATEGORY_LABELS,
    Category,
    CategoryRate,
    RateCard,
    RateSource,
    RateStatus,
    RateTier,
    categories_for,
)

CALCULATOR_VERSION = 1

_SERVED_TIERS = {
    "default": "standard",
    "standard": "standard",
    "priority": "priority",
    "flex": "flex",
    "scale": "scale",
    "batch": "batch",
}


class LineStatus(StrEnum):
    PRICED = "priced"
    ZERO = "zero"
    FOLDED = "folded"  # not applicable: tokens are billed as uncached input
    UNKNOWN_RATE = "unknown_rate"
    UNKNOWN_TOKENS = "unknown_tokens"
    UNASSIGNED = "unassigned"  # tokens are part of a pool whose split was not reported


class CostLine(BaseModel):
    category: Category
    label: str
    tokens: int | None
    status: LineStatus
    rate_per_million: Money | None = None
    cost: Money | None = None
    source: RateSource | None = None
    custom_rate: bool = False
    note: str | None = None


class TokenPool(BaseModel):
    tokens: int
    categories: list[Category]
    lower: Money
    upper: Money | None
    note: str


class CostEstimate(BaseModel):
    status: Literal["complete", "incomplete", "not_sent"]
    currency: str
    total: Money | None  # set only when complete
    lower: Money
    upper: Money | None
    lines: list[CostLine] = Field(default_factory=list)
    pools: list[TokenPool] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    tier_above_input: int | None = None
    calculator_version: int = CALCULATOR_VERSION

    @classmethod
    def not_sent(cls, currency: str = "USD") -> CostEstimate:
        """The request was never sent to the provider, so nothing was billed."""
        return cls(status="not_sent", currency=currency, total=ZERO, lower=ZERO, upper=ZERO)

    @classmethod
    def usage_missing(cls, currency: str, reason: str) -> CostEstimate:
        return cls(status="incomplete", currency=currency, total=None, lower=ZERO, upper=None, reasons=[reason])

    @property
    def is_complete(self) -> bool:
        return self.status in ("complete", "not_sent")


@dataclass(frozen=True)
class CostContext:
    # Claude: the single cache TTL configured on the request ("5m" or "1h"), if any. Used only
    # when the provider reports cache writes without the TTL split.
    requested_cache_ttl: str | None = None


@dataclass
class _Partial:
    tier: RateTier
    lines: list[CostLine] = field(default_factory=list)
    pools: list[TokenPool] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    lower: Decimal = ZERO
    upper: Decimal | None = ZERO


def _label(category: Category) -> str:
    return CATEGORY_LABELS[category]


def _global_reasons(facts: UsageFacts, card: RateCard) -> tuple[list[str], bool, bool]:
    """Reasons that apply regardless of tier. Returns (reasons, upper_unknown, lower_unreliable)."""
    reasons = [f"inconsistent usage: {item}" for item in facts.anomalies]
    # Inconsistent counts can push any bound in either direction, so neither bound is trusted.
    lower_unreliable = bool(facts.anomalies)
    reasons += [f"unrecognized usage field {k} = {v}" for k, v in facts.unrecognized_nonzero.items()]
    reasons += [f"{k} = {v}: non-text tokens are not priced" for k, v in facts.unsupported_modalities.items()]
    upper_unknown = bool(reasons)
    reported_tier = (facts.service_tier or "").strip().lower()
    if reported_tier:
        served = _SERVED_TIERS.get(reported_tier)
        if served is None:
            reasons.append(f"provider reported an unrecognized service tier '{facts.service_tier}'")
            upper_unknown = lower_unreliable = True
        elif served != card.processing_tier:
            reasons.append(
                f"provider served the request on the '{facts.service_tier}' tier but rates are for "
                f"'{card.processing_tier}'"
            )
            upper_unknown = lower_unreliable = True
    return reasons, upper_unknown, lower_unreliable


def _currency_checked(card: RateCard) -> tuple[RateCard, list[str]]:
    """Treat rates quoted in another currency as unknown (amounts are never converted)."""
    reasons: list[str] = []
    tiers: list[RateTier] = []
    changed = False
    for tier in card.tiers:
        categories = dict(tier.categories)
        for category, entry in tier.categories.items():
            if entry.rate is not None and entry.rate.currency != card.currency:
                reasons.append(f"the {_label(category).lower()} rate is in {entry.rate.currency}, not {card.currency}")
                categories[category] = CategoryRate.unknown(f"rate currency {entry.rate.currency}")
                changed = True
        tiers.append(RateTier(above_input_tokens=tier.above_input_tokens, categories=categories))
    if not changed:
        return card, reasons
    return card.model_copy(update={"tiers": tiers}), reasons


def estimate_cost(
    facts: UsageFacts | None,
    card: RateCard,
    context: CostContext | None = None,
) -> CostEstimate:
    """Estimate the token cost of one sent request."""
    context = context or CostContext()
    if facts is None:
        return CostEstimate.usage_missing(card.currency, "usage not reported by the provider")

    card, currency_reasons = _currency_checked(card)
    reasons, upper_unknown, lower_unreliable = _global_reasons(facts, card)
    reasons += currency_reasons
    tiers = card.sorted_tiers()
    if len(tiers) == 1:
        candidates = tiers
    elif facts.input_total is None:
        candidates = tiers
        reasons.append("context-length price tier unknown: total input tokens not reported")
    else:
        candidates = [card.tier_for(facts.input_total)]

    partials = [_estimate_tier(facts, card, tier, context) for tier in candidates]
    if len(partials) == 1:
        chosen = partials[0]
        lower = chosen.lower
        upper = chosen.upper
        tier_above: int | None = chosen.tier.above_input_tokens
    else:
        chosen = partials[0]
        lower = min(p.lower for p in partials)
        uppers = [p.upper for p in partials]
        upper = None if any(u is None for u in uppers) else max(u for u in uppers if u is not None)
        tier_above = None

    all_reasons = list(reasons)
    for partial in partials:
        for reason in partial.reasons:
            if reason not in all_reasons:
                all_reasons.append(reason)
    assumptions: list[str] = []
    for partial in partials:
        for item in partial.assumptions:
            if item not in assumptions:
                assumptions.append(item)

    if upper_unknown:
        upper = None
    if lower_unreliable:
        lower = ZERO
    complete = not all_reasons
    return CostEstimate(
        status="complete" if complete else "incomplete",
        currency=card.currency,
        total=lower if complete else None,
        lower=lower,
        upper=upper,
        lines=chosen.lines,
        pools=chosen.pools,
        reasons=all_reasons,
        assumptions=assumptions,
        tier_above_input=tier_above,
    )


def _estimate_tier(facts: UsageFacts, card: RateCard, tier: RateTier, context: CostContext) -> _Partial:
    partial = _Partial(tier=tier)
    categories = categories_for(card.api_style)
    cache_categories: list[Category] = [c for c in categories if c not in (Category.INPUT_UNCACHED, Category.OUTPUT)]

    tokens: dict[Category, int | None] = {Category.CACHE_READ: facts.cache_read}
    unspecified_write: int | None = None
    if card.api_style.is_openai:
        tokens[Category.CACHE_WRITE] = facts.cache_write
    else:
        unspecified_write = _assign_claude_writes(facts, tokens, context, partial)

    def reported_positive(category: Category) -> bool:
        count = tokens.get(category)
        if count:
            return True
        return category in (Category.CACHE_WRITE_5M, Category.CACHE_WRITE_1H) and bool(unspecified_write)

    def folds(category: Category) -> bool:
        entry = tier.get(category)
        if entry.status is not RateStatus.NOT_APPLICABLE:
            return False
        # A preset default must not silently bill tokens the provider reports separately at the
        # uncached input rate: they may carry their own (possibly lower) price.
        return not (entry.declared_by != "user" and reported_positive(category))

    folded: set[Category] = {c for c in cache_categories if folds(c)}
    billed_cache: list[Category] = [c for c in cache_categories if c not in folded]
    pooled: set[Category] = set()
    pool_specs: list[tuple[int, list[Category], str]] = []
    fold_extra = 0  # unsplit Claude writes whose TTL categories are both folded

    if unspecified_write is not None:
        targets: list[Category] = []
        for category in (Category.CACHE_WRITE_5M, Category.CACHE_WRITE_1H):
            target = Category.INPUT_UNCACHED if category in folded else category
            if target not in targets:
                targets.append(target)
        pooled.update({Category.CACHE_WRITE_5M, Category.CACHE_WRITE_1H})
        if targets == [Category.INPUT_UNCACHED]:
            fold_extra += unspecified_write
        else:
            pool_specs.append((unspecified_write, targets, "cache-write TTL split not reported"))

    uncached = _uncached_tokens(facts, card, tokens, folded, billed_cache, pooled, pool_specs, partial)
    if uncached is not None:
        uncached += fold_extra

    _add_line(partial, tier, Category.INPUT_UNCACHED, uncached, folded, pooled)
    for category in cache_categories:
        _add_line(partial, tier, category, tokens.get(category), folded, pooled)
    _add_line(partial, tier, Category.OUTPUT, facts.output_total, folded, pooled)

    for count, targets, note in pool_specs:
        _add_pool(partial, tier, count, targets, note)
    return partial


def _assign_claude_writes(
    facts: UsageFacts,
    tokens: dict[Category, int | None],
    context: CostContext,
    partial: _Partial,
) -> int | None:
    """Fill Claude TTL write categories; return writes whose TTL split is unknown, if any."""
    w5, w1 = Category.CACHE_WRITE_5M, Category.CACHE_WRITE_1H
    if facts.cache_write_split_reported:
        tokens[w5], tokens[w1] = facts.cache_write_5m, facts.cache_write_1h
        return None
    if facts.cache_write is None:
        tokens[w5] = tokens[w1] = None
        return None
    if facts.cache_write == 0:
        tokens[w5] = tokens[w1] = 0
        return None
    ttl = context.requested_cache_ttl
    if ttl in ("5m", "1h") and not facts.cache_write_split_partial:
        chosen, other = (w5, w1) if ttl == "5m" else (w1, w5)
        tokens[chosen], tokens[other] = facts.cache_write, 0
        partial.assumptions.append(
            f"cache-write TTL inferred from the request ({ttl}): the provider did not report the TTL split"
        )
        return None
    tokens[w5] = tokens[w1] = None
    return facts.cache_write


def _uncached_tokens(
    facts: UsageFacts,
    card: RateCard,
    tokens: dict[Category, int | None],
    folded: set[Category],
    billed_cache: list[Category],
    pooled: set[Category],
    pool_specs: list[tuple[int, list[Category], str]],
    partial: _Partial,
) -> int | None:
    if card.api_style.is_openai:
        total = facts.input_total
        if total is None:
            return None
        known = [c for c in billed_cache if tokens.get(c) is not None]
        unknown = [c for c in billed_cache if tokens.get(c) is None]
        remainder = total - sum(tokens[c] or 0 for c in known)
        if remainder < 0:
            partial.reasons.append("separately priced cache tokens exceed total input tokens")
            return None
        if unknown:
            labels = ", ".join(_label(c).lower() for c in unknown)
            pool_specs.append(
                (
                    remainder,
                    [Category.INPUT_UNCACHED, *unknown],
                    f"split between uncached input and {labels} not reported",
                )
            )
            pooled.update({Category.INPUT_UNCACHED, *unknown})
            return None
        return remainder
    # Claude: uncached input is reported directly; categories that are not applicable fold into it.
    base = facts.input_uncached
    if base is None:
        return None
    extra = 0
    for category in folded:
        if category in pooled:
            continue
        count = tokens.get(category)
        if count is None:
            return None
        extra += count
    return base + extra


def _add_line(
    partial: _Partial,
    tier: RateTier,
    category: Category,
    count: int | None,
    folded: set[Category],
    pooled: set[Category],
) -> None:
    category_rate = tier.get(category)
    rate = category_rate.rate if category_rate.status is RateStatus.PRICED else None

    def make(tokens: int | None, status: LineStatus, cost: Decimal | None = None, note: str | None = None) -> CostLine:
        return CostLine(
            category=category,
            label=_label(category),
            tokens=tokens,
            status=status,
            rate_per_million=rate.per_million if rate else None,
            cost=cost,
            source=rate.source if rate else None,
            custom_rate=bool(rate and rate.source is RateSource.CUSTOM_OVERRIDE),
            note=note,
        )

    if category in folded and category is not Category.INPUT_UNCACHED:
        partial.lines.append(make(count, LineStatus.FOLDED, note="not separately priced: billed as uncached input"))
        return
    if category in pooled:
        partial.lines.append(make(None, LineStatus.UNASSIGNED, note="split not reported"))
        return
    if count is None:
        partial.lines.append(make(None, LineStatus.UNKNOWN_TOKENS, note="not reported"))
        partial.reasons.append(f"{_label(category).lower()} tokens not reported")
        partial.upper = None
        return
    if count == 0:
        partial.lines.append(make(0, LineStatus.ZERO, cost=ZERO))
        return
    if rate is None:
        if category_rate.status is RateStatus.NOT_APPLICABLE:
            partial.lines.append(make(count, LineStatus.UNKNOWN_RATE, note="marked not applicable by the preset"))
            partial.reasons.append(
                f"{_label(category).lower()} reported ({count} tokens) but marked not applicable only by the "
                "profile preset; confirm the rate in Models & pricing"
            )
        else:
            partial.lines.append(make(count, LineStatus.UNKNOWN_RATE, note="no rate"))
            partial.reasons.append(f"no rate for {_label(category).lower()} ({count} tokens)")
        partial.upper = None
        return
    cost = rate.cost(count)
    partial.lines.append(make(count, LineStatus.PRICED, cost=cost))
    partial.lower = add(partial.lower, cost)
    if partial.upper is not None:
        partial.upper = add(partial.upper, cost)


def _add_pool(partial: _Partial, tier: RateTier, count: int, targets: list[Category], note: str) -> None:
    if count == 0:
        partial.pools.append(TokenPool(tokens=0, categories=targets, lower=ZERO, upper=ZERO, note=note))
        return
    rates = [tier.get(target) for target in targets]
    if any(r.status is not RateStatus.PRICED or r.rate is None for r in rates):
        lower: Decimal = ZERO
        upper: Decimal | None = None
        partial.reasons.append(f"{count} tokens: {note}, and at least one possible rate is unknown")
        for target, entry in zip(targets, rates, strict=True):
            if entry.status is RateStatus.NOT_APPLICABLE:
                partial.reasons.append(
                    f"{_label(target).lower()} is marked not applicable only by the profile preset; "
                    "confirm the rate in Models & pricing"
                )
    else:
        costs = [r.rate.cost(count) for r in rates if r.rate is not None]
        lower, upper = min(costs), max(costs)
        if lower != upper:
            partial.reasons.append(f"{count} tokens: {note}")
    partial.pools.append(TokenPool(tokens=count, categories=targets, lower=lower, upper=upper, note=note))
    partial.lower = add(partial.lower, lower)
    partial.upper = None if partial.upper is None or upper is None else add(partial.upper, upper)


def sum_estimates(estimates: list[CostEstimate], currency: str = "USD") -> CostEstimate:
    """Combine estimates (e.g. all attempts of a run). Complete only if every part is complete."""
    lower = ZERO
    upper: Decimal | None = ZERO
    reasons: list[str] = []
    incomplete = 0
    for item in estimates:
        if item.currency != currency:
            raise ValueError(f"cannot add {item.currency} amounts to a {currency} total")
        lower = add(lower, item.lower)
        upper = None if upper is None or item.upper is None else add(upper, item.upper)
        if not item.is_complete:
            incomplete += 1
    if incomplete:
        reasons.append(f"{incomplete} of {len(estimates)} estimates are incomplete")
    complete = incomplete == 0
    return CostEstimate(
        status="complete" if complete else "incomplete",
        currency=currency,
        total=lower if complete else None,
        lower=lower,
        upper=upper,
        reasons=reasons,
    )
