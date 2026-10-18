"""Pricing orchestration: refresh the Azure catalog, match meters, resolve and snapshot rate cards."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.database import Database
from ..domain.profiles import Endpoint, ModelProfile
from ..repos.endpoints import EndpointRepository
from ..repos.profiles import ProfileRepository
from ..repos.rates import RateRepository
from ..repos.settings import SettingsRepository
from .catalog import PriceCatalog
from .matcher import MatchResult
from .ratecard import Category, RateCard
from .resolver import pricing_context, resolve_rate_card, run_match
from .retail import RetailPricesClient, RetailPricesError


@dataclass
class RegionRefresh:
    region: str
    status: str
    item_count: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class PricingService:
    def __init__(self, db: Database, retail_client: RetailPricesClient) -> None:
        self.db = db
        self.catalog = PriceCatalog(db)
        self.rates = RateRepository(db)
        self.profiles = ProfileRepository(db)
        self.endpoints = EndpointRepository(db)
        self.settings = SettingsRepository(db)
        self.retail = retail_client

    def _currency(self) -> str:
        return self.settings.get().currency

    def regions_in_use(self) -> set[str]:
        regions: set[str] = set()
        for profile in self.profiles.list():
            endpoint = self.endpoints.get(profile.endpoint_id)
            region = pricing_context(profile, endpoint, self._currency()).region
            if region and region.lower() != "local":
                regions.add(region)
        return regions

    async def refresh(self, regions: set[str] | None = None) -> list[RegionRefresh]:
        """Fetch current Azure prices for the given regions (default: every region in use)."""
        settings = self.settings.get()
        results: list[RegionRefresh] = []
        for region in sorted(regions if regions is not None else self.regions_in_use()):
            cached = len(self.catalog.items(region, settings.currency))
            kept = f" The {cached} cached prices were kept unchanged." if cached else ""
            fetch_id = self.catalog.start_fetch(region, settings.currency, settings.product_filters)
            try:
                items, warnings = await self.retail.fetch(region, settings.currency, settings.product_filters)
                if not items and cached:
                    # An empty answer for a region that had prices is an outage or a bad filter, not news.
                    raise RetailPricesError("The Retail Prices API returned no prices for this region.")
            except Exception as exc:
                message = str(exc) if isinstance(exc, RetailPricesError) else f"{type(exc).__name__}: {exc}"
                self.catalog.fail_fetch(fetch_id, message + kept)
                results.append(RegionRefresh(region=region, status="failed", error=message + kept))
                continue
            count = self.catalog.replace_region(fetch_id, region, settings.currency, items)
            results.append(RegionRefresh(region=region, status="succeeded", item_count=count, warnings=warnings))
        # Re-run automatic matching so azure_auto categories follow the refreshed catalog.
        for profile in self.profiles.list():
            self.match_and_apply(profile.id)
        return results

    def match_and_apply(self, profile_id: str) -> MatchResult:
        """Match meters and update only categories whose source is ``azure_auto``.

        Every ``azure_auto`` row is re-evaluated: a category without an unambiguous match loses its
        meter (becoming unknown) instead of keeping a stale price from an earlier match.
        """
        profile = self.profiles.get(profile_id)
        endpoint = self.endpoints.get(profile.endpoint_id)
        currency = self._currency()
        result = run_match(profile, endpoint, self.catalog, currency)
        long_tier = int(profile.long_context_threshold or 0)
        chosen: dict[tuple[Category, int], tuple[str, str] | None] = {}
        for match in result.matches:
            if match.context == "long" and not long_tier:
                continue
            tier = 0 if match.context == "base" else long_tier
            item = match.chosen.info.item if match.chosen else None
            chosen[(match.category, tier)] = (item.meter_id, item.sku_id) if item else None
        rows = self.rates.list(profile_id)
        tiers = [0, long_tier] if long_tier else [0]
        for row in rows:
            if row.layer != "base" or row.source != "azure_auto" or row.tier_above_input not in tiers:
                continue
            target = chosen.get((row.category, row.tier_above_input))
            if target is None and row.meter_id is None:
                continue
            self.rates.set_azure(
                profile_id,
                row.category,
                row.tier_above_input,
                pinned=False,
                meter_id=target[0] if target else None,
                sku_id=target[1] if target else None,
                currency=currency,
            )
        # Long-context tier: create rows for newly matched long-context categories.
        if long_tier:
            existing = {(r.category, r.tier_above_input) for r in rows if r.layer == "base"}
            base_auto = {
                r.category for r in rows if r.layer == "base" and r.tier_above_input == 0 and r.source == "azure_auto"
            }
            for (category, tier), target in chosen.items():
                if tier == long_tier and (category, tier) not in existing and category in base_auto and target:
                    self.rates.set_azure(
                        profile_id,
                        category,
                        tier,
                        pinned=False,
                        meter_id=target[0],
                        sku_id=target[1],
                        currency=currency,
                    )
        return result

    def pin(self, profile_id: str, category: Category, tier: int, meter_id: str, sku_id: str | None) -> None:
        profile = self.profiles.get(profile_id)
        endpoint = self.endpoints.get(profile.endpoint_id)
        context = pricing_context(profile, endpoint, self._currency())
        if not self.catalog.meter(meter_id, sku_id, context.region, context.currency):
            raise ValueError("That meter is not in the cached catalog for this profile's region; refresh prices.")
        self.rates.set_azure(
            profile_id, category, tier, pinned=True, meter_id=meter_id, sku_id=sku_id, currency=context.currency
        )

    def resolve(self, profile: ModelProfile, endpoint: Endpoint | None = None) -> RateCard:
        endpoint = endpoint or self.endpoints.get(profile.endpoint_id)
        return resolve_rate_card(profile, endpoint, self.rates.list(profile.id), self.catalog, self._currency())
