"""Azure Retail Prices API client (public, unauthenticated).

https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2

from ..money import to_decimal
from ..providers.http import make_async_client, make_timeout

DEFAULT_RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
RETAIL_API_VERSION = "2023-01-01-preview"
DEFAULT_PRODUCT_FILTERS: tuple[str, ...] = (
    "serviceName eq 'Foundry Models'",
    "contains(productName, 'Azure OpenAI')",
)
LEGACY_PRODUCT_FILTER = "serviceName eq 'Cognitive Services' and contains(productName, 'OpenAI')"
_REGION_RE = re.compile(r"^[a-z0-9]{2,40}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class RetailPricesError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetailItem:
    meter_id: str
    sku_id: str
    region: str
    currency: str
    tier_min_units: Decimal
    effective_start: str
    service_name: str
    product_name: str
    sku_name: str
    meter_name: str
    unit_of_measure: str
    retail_price: Decimal
    unit_price: Decimal
    price_type: str
    is_primary_region: bool | None
    raw: dict[str, Any]


def validate_region(region: str) -> str:
    if not _REGION_RE.match(region or ""):
        raise RetailPricesError(f"Invalid Azure region name {region!r} (expected e.g. 'eastus2').")
    return region


def validate_currency(currency: str) -> str:
    if not _CURRENCY_RE.match(currency or ""):
        raise RetailPricesError(f"Invalid currency code {currency!r}.")
    return currency


def parse_item(raw: dict[str, Any]) -> RetailItem:
    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    retail = raw.get("retailPrice")
    unit = raw.get("unitPrice", retail)
    tier = raw.get("tierMinimumUnits", 0)
    if not text("meterId") or retail is None:
        raise RetailPricesError("price item without meterId or retailPrice")
    try:
        retail_price = to_decimal(retail)
        unit_price = to_decimal(unit if unit is not None else retail)
        tier_units = to_decimal(tier if tier is not None else 0)
    except (TypeError, ValueError) as exc:
        raise RetailPricesError(f"price item {text('meterId')} has an invalid number: {exc}") from exc
    if retail_price < 0 or tier_units < 0:
        raise RetailPricesError(f"price item {text('meterId')} has a negative value")
    return RetailItem(
        meter_id=text("meterId"),
        sku_id=text("skuId"),
        region=text("armRegionName"),
        currency=text("currencyCode"),
        tier_min_units=tier_units,
        effective_start=text("effectiveStartDate"),
        service_name=text("serviceName"),
        product_name=text("productName"),
        sku_name=text("skuName"),
        meter_name=text("meterName"),
        unit_of_measure=text("unitOfMeasure"),
        retail_price=retail_price,
        unit_price=unit_price,
        price_type=text("type"),
        is_primary_region=raw.get("isPrimaryMeterRegion")
        if isinstance(raw.get("isPrimaryMeterRegion"), bool)
        else None,
        raw=raw,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def raw_json(item: RetailItem) -> str:
    return json.dumps(_jsonable(item.raw), sort_keys=True)


class RetailPricesClient:
    def __init__(
        self,
        base_url: str = DEFAULT_RETAIL_PRICES_URL,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        max_pages: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._max_pages = max_pages

    def _first_url(self, region: str, currency: str, clause: str) -> str:
        query = urlencode(
            {
                "api-version": RETAIL_API_VERSION,
                "currencyCode": f"'{currency}'",
                "$filter": f"armRegionName eq '{region}' and priceType eq 'Consumption' and ({clause})",
            }
        )
        return f"{self.base_url}?{query}"

    def _same_origin(self, url: str) -> bool:
        a, b = urlsplit(url), urlsplit(self.base_url)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)

    async def fetch(
        self,
        region: str,
        currency: str,
        product_filters: tuple[str, ...] | list[str] = DEFAULT_PRODUCT_FILTERS,
    ) -> tuple[list[RetailItem], list[str]]:
        """Fetch consumption price items for one region. Returns (items, warnings)."""
        validate_region(region)
        validate_currency(currency)
        items: dict[tuple[str, str, Decimal, str], RetailItem] = {}
        warnings: list[str] = []
        succeeded = 0
        skipped = 0
        async with make_async_client(timeout=make_timeout(15, 60), transport=self._transport) as client:
            for clause in product_filters:
                url: str | None = self._first_url(region, currency, clause)
                pages = 0
                try:
                    while url:
                        pages += 1
                        if pages > self._max_pages:
                            raise RetailPricesError("Retail Prices API returned too many pages")
                        response = await client.get(url)
                        if response.status_code != 200:
                            raise RetailPricesError(f"Retail Prices API returned HTTP {response.status_code}")
                        data = json.loads(response.content, parse_float=Decimal)
                        for raw in data.get("Items") or []:
                            try:
                                item = parse_item(raw)
                            except RetailPricesError as exc:
                                skipped += 1
                                if skipped <= 5:
                                    warnings.append(f"Skipped an unusable price item: {exc}")
                                continue
                            if item.region.lower() != region or item.currency.upper() != currency:
                                skipped += 1
                                continue
                            items[(item.meter_id, item.sku_id, item.tier_min_units, item.effective_start)] = item
                        next_link = data.get("NextPageLink")
                        if next_link and not self._same_origin(next_link):
                            raise RetailPricesError("Retail Prices API returned a next-page link on another host")
                        url = next_link or None
                    succeeded += 1
                except (httpx2.HTTPError, RetailPricesError, ValueError) as exc:
                    warnings.append(f"Filter {clause!r} failed: {exc}")
        if succeeded == 0:
            raise RetailPricesError("; ".join(warnings) or "Retail Prices API query failed")
        return list(items.values()), warnings
