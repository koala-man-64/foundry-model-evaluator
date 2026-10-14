"""Resolve a profile's current rate card from its rate configuration and the cached catalog."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.profiles import Endpoint, ModelProfile
from ..repos.rates import RateRow
from .catalog import CatalogItem, PriceCatalog
from .matcher import MatchResult, classify, match_meters
from .ratecard import Category, CategoryRate, Rate, RateCard, RateSource, RateTier, categories_for
from .units import UnitError, parse_token_unit, unit_label


@dataclass(frozen=True)
class PricingContext:
    region: str
    currency: str


def pricing_context(profile: ModelProfile, endpoint: Endpoint, currency: str) -> PricingContext:
    return PricingContext(region=(profile.pricing_region or endpoint.region or "").strip(), currency=currency)


def _auto_meter_problem(item: CatalogItem, row: RateRow, profile: ModelProfile, long_tier: bool) -> str | None:
    """Defence in depth for automatically matched meters (user-pinned meters are trusted)."""
    info = classify(item, profile.api_style)
    if info.category is not row.category:
        return f"the matched meter is not a {row.category.value} meter"
    if info.deployment_type != profile.deployment_type:
        return "the matched meter is for a different deployment type; match prices again"
    if info.processing_tier != profile.processing_tier:
        return "the matched meter is for a different processing tier; match prices again"
    if long_tier != (info.context == "long"):
        return "the matched meter is for a different context-length tier; match prices again"
    return None


def _azure_rate(row: RateRow, items: list[CatalogItem], context: PricingContext, profile: ModelProfile) -> CategoryRate:
    pinned = row.source == "azure_pinned"
    source = RateSource.AZURE_PINNED if pinned else RateSource.AZURE_AUTO
    if not row.meter_id:
        return CategoryRate.unknown("No unambiguous Azure meter matched; pin a meter or enter a manual rate.")
    if not context.region:
        return CategoryRate.unknown("Set the endpoint region to look up Azure prices.")
    matches = [i for i in items if i.meter_id == row.meter_id and (not row.sku_id or i.sku_id == row.sku_id)]
    if not matches:
        return CategoryRate.unknown(
            f"Meter {row.meter_id} is not in the cached {context.region} catalog; refresh prices."
        )
    if len(matches) > 1:
        return CategoryRate.unknown("The selected meter has several volume tiers; enter a manual rate instead.")
    item = matches[0]
    if not pinned:
        problem = _auto_meter_problem(item, row, profile, long_tier=row.tier_above_input > 0)
        if problem:
            return CategoryRate.unknown(problem)
    try:
        tokens = parse_token_unit(item.unit_of_measure)
    except UnitError as exc:
        return CategoryRate.unknown(str(exc))
    return CategoryRate.priced(
        Rate(
            price=item.retail_price,
            unit_tokens=tokens,
            unit_label=item.unit_of_measure,
            currency=item.currency,
            source=source,
            source_url="https://prices.azure.com/api/retail/prices",
            meter_id=item.meter_id,
            sku_id=item.sku_id,
            meter_name=item.meter_name,
            product_name=item.product_name,
            region=item.region,
            effective_date=item.effective_start or None,
            retrieved_at=item.retrieved_at,
        )
    )


def _base_rate(
    row: RateRow | None, items: list[CatalogItem], context: PricingContext, profile: ModelProfile
) -> CategoryRate:
    if row is None:
        return CategoryRate.unknown("No rate configured.")
    if row.source in ("azure_auto", "azure_pinned"):
        return _azure_rate(row, items, context, profile)
    if row.source == "not_applicable":
        # Undeclared origin is treated as a preset default (the cautious choice).
        return CategoryRate.not_applicable(row.note or "not applicable", declared_by=row.declared_by or "preset")
    if row.source == "manual":
        if row.price is None or not row.unit_tokens:
            return CategoryRate.unknown("Enter a manual rate with its source URL.")
        return CategoryRate.priced(
            Rate(
                price=row.price,
                unit_tokens=row.unit_tokens,
                unit_label=row.unit_label or unit_label(row.unit_tokens),
                currency=row.currency,
                source=RateSource.MANUAL,
                source_url=row.source_url,
                effective_date=row.effective_date,
                retrieved_at=row.updated_at,
                note=row.note,
            )
        )
    return CategoryRate.unknown(f"Unknown rate source {row.source!r}.")


def _apply_override(base: CategoryRate, override: RateRow) -> CategoryRate:
    if override.price is None or not override.unit_tokens:
        return base
    base_rate = base.rate if base.rate is not None else None
    return CategoryRate.priced(
        Rate(
            price=override.price,
            unit_tokens=override.unit_tokens,
            unit_label=override.unit_label or unit_label(override.unit_tokens),
            currency=override.currency,
            source=RateSource.CUSTOM_OVERRIDE,
            source_url=override.source_url,
            retrieved_at=override.updated_at,
            note=override.note,
            base_price=base_rate.price if base_rate else None,
            base_unit_tokens=base_rate.unit_tokens if base_rate else None,
            base_source=base_rate.source if base_rate else None,
        )
    )


def resolve_rate_card(
    profile: ModelProfile,
    endpoint: Endpoint,
    rows: list[RateRow],
    catalog: PriceCatalog,
    currency: str,
) -> RateCard:
    context = pricing_context(profile, endpoint, currency)
    items = catalog.items(context.region, currency) if context.region else []
    explanations: list[str] = []
    thresholds = [0]
    if profile.long_context_threshold:
        thresholds.append(profile.long_context_threshold)
    tiers: list[RateTier] = []
    for threshold in thresholds:
        categories: dict[Category, CategoryRate] = {}
        for category in categories_for(profile.api_style):
            matching = [r for r in rows if r.category is category and r.tier_above_input == threshold]
            base = next((r for r in matching if r.layer == "base"), None)
            override = next((r for r in matching if r.layer == "override"), None)
            if base is None and threshold != 0:
                explanations.append(
                    f"No long-context rate for {category.value}; "
                    f"requests above {threshold} input tokens are incomplete."
                )
            resolved = _base_rate(base, items, context, profile)
            if resolved.rate is not None and resolved.rate.currency != currency:
                resolved = CategoryRate.unknown(f"Rate currency {resolved.rate.currency} differs from {currency}.")
            if override is not None:
                if override.currency != currency:
                    resolved = CategoryRate.unknown(f"Override currency {override.currency} differs from {currency}.")
                else:
                    resolved = _apply_override(resolved, override)
            categories[category] = resolved
        tiers.append(RateTier(above_input_tokens=threshold, categories=categories))
    return RateCard(
        api_style=profile.api_style,
        currency=currency,
        processing_tier=profile.processing_tier,
        tiers=tiers,
        explanations=explanations,
    )


def run_match(profile: ModelProfile, endpoint: Endpoint, catalog: PriceCatalog, currency: str) -> MatchResult:
    context = pricing_context(profile, endpoint, currency)
    items = catalog.items(context.region, currency) if context.region else []
    return match_meters(
        items,
        api_style=profile.api_style,
        meter_terms=profile.meter_terms,
        deployment_type=profile.deployment_type,
        processing_tier=profile.processing_tier,
    )
