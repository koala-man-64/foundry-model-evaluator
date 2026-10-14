"""Rate sources: Azure Retail Prices matching and pinning, manual rates, custom overrides."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...money import Money
from ...pricing.catalog import FetchInfo
from ...pricing.matcher import Candidate, MeterInfo, search_catalog
from ...pricing.ratecard import Category, RateCard, categories_for
from ...pricing.resolver import pricing_context
from ...pricing.units import parse_token_unit, unit_label
from ...repos.rates import RateRow
from ..deps import ServicesDep

router = APIRouter(prefix="/api", tags=["pricing"])


class FetchOut(BaseModel):
    region: str
    currency: str
    started_at: str
    finished_at: str | None
    item_count: int | None
    status: str
    error: str | None


def _fetch_out(info: FetchInfo | None) -> FetchOut | None:
    if info is None:
        return None
    return FetchOut(
        region=info.region,
        currency=info.currency,
        started_at=info.started_at,
        finished_at=info.finished_at,
        item_count=info.item_count,
        status=info.status,
        error=info.error,
    )


class RatesOut(BaseModel):
    profile_id: str
    region: str
    currency: str
    card: RateCard
    rows: list[RateRow]
    catalog: FetchOut | None
    categories: list[Category]


class MeterOut(BaseModel):
    meter_id: str
    sku_id: str
    meter_name: str
    product_name: str
    unit_of_measure: str
    retail_price: Money
    effective_start: str
    retrieved_at: str
    residual: str
    category: Category | None
    deployment_type: str | None
    processing_tier: str
    context: str | None
    unit_tokens: int | None
    problems: list[str]


class MeterCandidateOut(BaseModel):
    meter: MeterOut
    eligible: bool
    reasons: list[str]


class CategoryMatchOut(BaseModel):
    category: Category
    context: str
    status: str
    message: str
    chosen: MeterOut | None
    candidates: list[MeterCandidateOut]


class MatchOut(BaseModel):
    matches: list[CategoryMatchOut]
    notes: list[str]
    rates: RatesOut


class RateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: int = Field(default=0, ge=0)
    mode: Literal["azure_auto", "azure_pinned", "manual", "not_applicable"]
    meter_id: str | None = None
    sku_id: str | None = None
    price: Money | None = None
    unit: str | None = None
    source_url: str | None = Field(default=None, max_length=1000)
    effective_date: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("source_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(("https://", "http://")):
            raise ValueError("The source URL must start with https:// or http://")
        return value


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: int = Field(default=0, ge=0)
    price: Money
    unit: str
    note: str = Field(min_length=1, max_length=500)
    source_url: str | None = Field(default=None, max_length=1000)


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regions: list[str] | None = None


class RefreshOut(BaseModel):
    region: str
    status: str
    item_count: int
    warnings: list[str]
    error: str | None


class PricingStatus(BaseModel):
    currency: str
    regions: list[str]
    fetches: list[FetchOut]


def _meter(info: MeterInfo) -> MeterOut:
    item = info.item
    return MeterOut(
        meter_id=item.meter_id,
        sku_id=item.sku_id,
        meter_name=item.meter_name,
        product_name=item.product_name,
        unit_of_measure=item.unit_of_measure,
        retail_price=item.retail_price,
        effective_start=item.effective_start,
        retrieved_at=item.retrieved_at,
        residual=info.residual_text,
        category=info.category,
        deployment_type=info.deployment_type,
        processing_tier=info.processing_tier,
        context=info.context,
        unit_tokens=info.unit_tokens,
        problems=info.problems,
    )


def _candidate(c: Candidate) -> MeterCandidateOut:
    return MeterCandidateOut(meter=_meter(c.info), eligible=c.eligible, reasons=c.reasons)


def _rates(services: ServicesDep, profile_id: str) -> RatesOut:
    profile = services.profiles.get(profile_id)
    endpoint = services.endpoints.get(profile.endpoint_id)
    currency = services.settings.get().currency
    context = pricing_context(profile, endpoint, currency)
    return RatesOut(
        profile_id=profile.id,
        region=context.region,
        currency=currency,
        card=services.pricing.resolve(profile, endpoint),
        rows=services.rates.list(profile.id),
        catalog=_fetch_out(services.pricing.catalog.last_fetch(context.region, currency)) if context.region else None,
        categories=list(categories_for(profile.api_style)),
    )


def _unit_tokens(unit: str | None) -> int:
    if not unit:
        raise ValueError("Choose the unit the price is quoted in (per 1K or per 1M tokens).")
    return parse_token_unit(unit)


@router.get("/profiles/{profile_id}/rates", response_model=RatesOut)
def get_rates(profile_id: str, services: ServicesDep) -> RatesOut:
    return _rates(services, profile_id)


@router.post("/profiles/{profile_id}/rates/match", response_model=MatchOut)
def match_rates(profile_id: str, services: ServicesDep) -> MatchOut:
    result = services.pricing.match_and_apply(profile_id)
    return MatchOut(
        matches=[
            CategoryMatchOut(
                category=m.category,
                context=m.context,
                status=m.status,
                message=m.message,
                chosen=_meter(m.chosen.info) if m.chosen else None,
                candidates=[_candidate(c) for c in m.candidates[:25]],
            )
            for m in result.matches
        ],
        notes=result.notes,
        rates=_rates(services, profile_id),
    )


@router.put("/profiles/{profile_id}/rates/{category}", response_model=RatesOut)
def set_rate(profile_id: str, category: Category, payload: RateIn, services: ServicesDep) -> RatesOut:
    profile = services.profiles.get(profile_id)
    if category not in categories_for(profile.api_style):
        raise ValueError(f"{category.value} does not apply to {profile.api_style.value} profiles.")
    if payload.tier and payload.tier != profile.long_context_threshold:
        raise ValueError("Long-context rates must use the profile's long-context threshold as the tier.")
    currency = services.settings.get().currency
    if payload.mode == "azure_pinned":
        if not payload.meter_id:
            raise ValueError("Choose a meter to pin.")
        services.pricing.pin(profile_id, category, payload.tier, payload.meter_id, payload.sku_id)
    elif payload.mode == "azure_auto":
        services.rates.set_azure(
            profile_id, category, payload.tier, pinned=False, meter_id=None, sku_id=None, currency=currency
        )
        services.pricing.match_and_apply(profile_id)
    elif payload.mode == "manual":
        if payload.price is None or payload.price < Decimal(0):
            raise ValueError("Enter a price of zero or more.")
        if not payload.source_url:
            raise ValueError("Manual rates need the URL of their source.")
        tokens = _unit_tokens(payload.unit)
        services.rates.set_manual(
            profile_id,
            category,
            payload.tier,
            price=payload.price,
            unit_tokens=tokens,
            unit_label=unit_label(tokens),
            currency=currency,
            source_url=payload.source_url,
            effective_date=payload.effective_date or None,
            note=payload.note or None,
        )
    else:
        services.rates.set_not_applicable(profile_id, category, payload.tier, note=payload.note)
    return _rates(services, profile_id)


@router.put("/profiles/{profile_id}/rates/{category}/override", response_model=RatesOut)
def set_override(profile_id: str, category: Category, payload: OverrideIn, services: ServicesDep) -> RatesOut:
    profile = services.profiles.get(profile_id)
    if category not in categories_for(profile.api_style):
        raise ValueError(f"{category.value} does not apply to {profile.api_style.value} profiles.")
    if payload.price < Decimal(0):
        raise ValueError("Enter a price of zero or more.")
    tokens = _unit_tokens(payload.unit)
    services.rates.set_override(
        profile_id,
        category,
        payload.tier,
        price=payload.price,
        unit_tokens=tokens,
        unit_label=unit_label(tokens),
        currency=services.settings.get().currency,
        note=payload.note,
        source_url=payload.source_url or None,
    )
    return _rates(services, profile_id)


@router.delete("/profiles/{profile_id}/rates/{category}/override", response_model=RatesOut)
def clear_override(profile_id: str, category: Category, services: ServicesDep, tier: int = 0) -> RatesOut:
    services.rates.clear(profile_id, category, tier, "override")
    return _rates(services, profile_id)


@router.get("/pricing/catalog", response_model=list[MeterOut])
def search_meters(
    services: ServicesDep,
    profile_id: str,
    q: str = Query(default="", max_length=200),
) -> list[MeterOut]:
    profile = services.profiles.get(profile_id)
    endpoint = services.endpoints.get(profile.endpoint_id)
    currency = services.settings.get().currency
    context = pricing_context(profile, endpoint, currency)
    items = services.pricing.catalog.items(context.region, currency) if context.region else []
    return [_meter(info) for info in search_catalog(items, q, profile.api_style)]


@router.post("/pricing/refresh", response_model=list[RefreshOut])
async def refresh_prices(payload: RefreshIn, services: ServicesDep) -> list[RefreshOut]:
    regions = set(payload.regions) if payload.regions else None
    results = await services.pricing.refresh(regions)
    return [
        RefreshOut(region=r.region, status=r.status, item_count=r.item_count, warnings=r.warnings, error=r.error)
        for r in results
    ]


@router.get("/pricing/status", response_model=PricingStatus)
def pricing_status(services: ServicesDep) -> PricingStatus:
    currency = services.settings.get().currency
    regions = sorted(services.pricing.regions_in_use())
    fetches = [f for f in (_fetch_out(services.pricing.catalog.last_fetch(r, currency)) for r in regions) if f]
    return PricingStatus(currency=currency, regions=regions, fetches=fetches)
