from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from foundry_eval.db.database import Database
from foundry_eval.pricing.catalog import PriceCatalog
from foundry_eval.pricing.matcher import classify, match_meters, search_catalog
from foundry_eval.pricing.ratecard import Category
from foundry_eval.pricing.retail import (
    DEFAULT_PRODUCT_FILTERS,
    RetailPricesClient,
    RetailPricesError,
    parse_item,
    validate_region,
)
from foundry_eval.types import ApiStyle

FIXTURE = Path(__file__).parent.parent / "fixtures" / "retail" / "catalog_eastus2.json"
R = ApiStyle.OPENAI_RESPONSES


def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(), parse_float=Decimal)


@pytest.fixture
def catalog(tmp_path) -> PriceCatalog:
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    cat = PriceCatalog(db)
    items = [parse_item(raw) for raw in fixture_payload()["Items"]]
    fetch = cat.start_fetch("eastus2", "USD", list(DEFAULT_PRODUCT_FILTERS))
    cat.replace_region(fetch, "eastus2", "USD", items)
    return cat


def match(catalog: PriceCatalog, terms: str, *, deployment: str = "global", tier: str = "standard"):
    return match_meters(
        catalog.items("eastus2", "USD"),
        api_style=R,
        meter_terms=terms,
        deployment_type=deployment,
        processing_tier=tier,
    )


# --- Retail Prices client -------------------------------------------------------------------------
async def test_client_pages_parses_decimals_and_merges_filters():
    payload = fixture_payload()
    items = payload["Items"]
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        if "page=2" in str(request.url):
            body = {"Items": items[20:], "NextPageLink": None}
        elif "Foundry" in request.url.params["$filter"]:
            body = {"Items": items[:20], "NextPageLink": "https://prices.azure.com/api/retail/prices?page=2"}
        else:
            body = {"Items": items[:5], "NextPageLink": None}
        return httpx2.Response(200, content=json.dumps(body, default=str).encode())

    client = RetailPricesClient(transport=httpx2.MockTransport(handler))
    fetched, warnings = await client.fetch("eastus2", "USD")
    assert warnings == []
    assert len(fetched) == len(
        {(i["meterId"], i["skuId"], str(i["tierMinimumUnits"]), i["effectiveStartDate"]) for i in items}
    )
    assert len(calls) == 3
    first = calls[0]
    assert "currencyCode=%27USD%27" in first and "armRegionName+eq+%27eastus2%27" in first
    mm = next(i for i in fetched if i.meter_name == "MM3.5 Inp glbl")
    assert mm.retail_price == Decimal("0.0015")
    assert isinstance(mm.retail_price, Decimal)


async def test_client_fails_when_any_filter_fails():
    # A partial result would replace the cached catalog and drop meters, so one failed filter fails the fetch.
    def handler(request: httpx2.Request) -> httpx2.Response:
        if "contains" in request.url.params["$filter"]:
            return httpx2.Response(400, content=b'{"Error": "bad filter"}')
        return httpx2.Response(200, content=b'{"Items": [], "NextPageLink": null}')

    with pytest.raises(RetailPricesError, match="HTTP 400"):
        await RetailPricesClient(transport=httpx2.MockTransport(handler)).fetch("eastus2", "USD")


async def test_client_fails_when_every_filter_fails():
    client = RetailPricesClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(503)))
    with pytest.raises(RetailPricesError):
        await client.fetch("eastus2", "USD")


async def test_client_refuses_next_link_to_other_host():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'{"Items": [], "NextPageLink": "https://evil.example/next"}')

    with pytest.raises(RetailPricesError, match="another host"):
        await RetailPricesClient(transport=httpx2.MockTransport(handler)).fetch(
            "eastus2", "USD", ["serviceName eq 'X'"]
        )


@pytest.mark.parametrize("region", ["East US 2", "eastus2'; drop", "", "x"])
def test_region_validation_blocks_filter_injection(region):
    with pytest.raises(RetailPricesError):
        validate_region(region)


def test_parse_item_requires_meter_and_price():
    with pytest.raises(RetailPricesError):
        parse_item({"meterName": "x"})


# --- Catalog -----------------------------------------------------------------------------------
def test_catalog_keeps_latest_effective_price_not_in_future(catalog):
    outputs = [i for i in catalog.items("eastus2", "USD") if i.meter_name == "gpt-4.1 Outp glbl"]
    assert len(outputs) == 1
    assert outputs[0].retail_price == Decimal("8.0")
    assert catalog.last_fetch("eastus2", "USD").status == "succeeded"
    assert catalog.last_fetch("westus", "USD") is None


def test_catalog_refresh_replaces_region(catalog):
    fetch = catalog.start_fetch("eastus2", "USD", [])
    catalog.replace_region(fetch, "eastus2", "USD", [])
    assert catalog.items("eastus2", "USD") == []


# --- Matcher -----------------------------------------------------------------------------------
def test_realtime_meters_match_text_only(catalog):
    result = match(catalog, "gpt-realtime-2")
    assert result.get(Category.INPUT_UNCACHED).chosen.info.item.retail_price == Decimal("4.0")
    assert result.get(Category.CACHE_READ).chosen.info.item.retail_price == Decimal("0.4")
    assert result.get(Category.OUTPUT).chosen.info.item.retail_price == Decimal("24.0")
    assert result.get(Category.CACHE_WRITE).status == "not_found"
    audio = [c for c in result.get(Category.INPUT_UNCACHED).candidates if "Audio" in c.info.item.meter_name]
    assert audio and not audio[0].eligible


def test_gpt56_terra_short_and_long_context_without_vendor_prefix(catalog):
    result = match(catalog, "gpt-5.6-terra")
    base = {
        c: result.get(c, "base")
        for c in (Category.INPUT_UNCACHED, Category.CACHE_READ, Category.CACHE_WRITE, Category.OUTPUT)
    }
    assert all(m.status == "matched" for m in base.values())
    assert base[Category.CACHE_WRITE].chosen.info.item.retail_price == Decimal("3.125")
    assert base[Category.OUTPUT].chosen.info.item.meter_name == "5.6 terra ShortCo Opt Std Gl"
    long = result.get(Category.INPUT_UNCACHED, "long")
    assert long.status == "matched" and long.chosen.info.item.retail_price == Decimal("5.0")
    assert result.notes


def test_priority_tier_selects_pp_meters(catalog):
    result = match(catalog, "5.6 terra", tier="priority")
    assert result.get(Category.OUTPUT).chosen.info.item.meter_name == "5.6 terra ShortCo Opt PP Gl"


def test_data_zone_selects_dz_meter_only(catalog):
    result = match(catalog, "5.6 terra", deployment="data_zone")
    assert result.get(Category.INPUT_UNCACHED).chosen.info.item.retail_price == Decimal("2.75")
    assert result.get(Category.OUTPUT).status == "not_found"


def test_versioned_meters_need_version_in_terms(catalog):
    without = match(catalog, "gpt-4o")
    assert without.get(Category.INPUT_UNCACHED).status == "not_found"
    versioned = match(catalog, "gpt-4o 1120")
    assert versioned.get(Category.INPUT_UNCACHED).status == "matched"
    assert versioned.get(Category.CACHE_READ).status == "not_found"
    mini = match(catalog, "gpt-4o-mini 0718")
    assert mini.get(Category.OUTPUT).chosen.info.item.retail_price == Decimal("0.6")


def test_regional_vs_global_and_unit_conflict(catalog):
    glbl = match(catalog, "gpt-4.1")
    assert glbl.get(Category.INPUT_UNCACHED).chosen.info.item.retail_price == Decimal("2.0")
    cached = glbl.get(Category.CACHE_READ)
    assert cached.status == "not_found"
    reasons = " ".join(r for c in cached.candidates for r in c.reasons)
    assert "conflicts with unitOfMeasure" in reasons
    assert "deployment type not stated" in reasons
    regional = match(catalog, "gpt-4.1", deployment="regional")
    assert regional.get(Category.OUTPUT).chosen.info.item.retail_price == Decimal("8.8")


def test_volume_tiers_and_duplicates_are_ambiguous(catalog):
    tiered = match(catalog, "tiered-model")
    assert tiered.get(Category.INPUT_UNCACHED).status == "not_found"
    assert "volume tiers" in " ".join(tiered.get(Category.INPUT_UNCACHED).candidates[0].reasons)
    dup = match(catalog, "dup-model")
    assert dup.get(Category.INPUT_UNCACHED).status == "ambiguous"


def test_partner_models(catalog):
    fw = match(catalog, "FW DeepSeek V4.1 Flash", deployment="data_zone")
    assert fw.get(Category.CACHE_READ).chosen.info.item.retail_price == Decimal("0.008")
    mistral = match(catalog, "MM3.5")
    assert mistral.get(Category.OUTPUT).chosen.info.unit_tokens == 1000


@pytest.mark.parametrize("terms", ["gpt-5.6", "terra", "4o", "deepseek", "gpt", "", "5.6 terra luna", "realtime"])
def test_no_false_positive_matches(catalog, terms):
    result = match(catalog, terms)
    assert all(m.status != "matched" for m in result.matches)


def test_hourly_meter_is_excluded(catalog):
    item = next(i for i in catalog.items("eastus2", "USD") if i.meter_name == "Live 1 glbl")
    info = classify(item, R)
    assert any("unit" in p for p in info.problems)


def test_search_catalog_returns_residuals(catalog):
    found = search_catalog(catalog.items("eastus2", "USD"), "terra", R)
    assert found and all("terra" in f.residual_text for f in found)
    assert {f.residual_text for f in found} == {"5.6 terra"}


def _item(meter_name: str, unit: str = "1M"):
    from foundry_eval.pricing.catalog import CatalogItem

    return CatalogItem(
        meter_id=meter_name,
        sku_id="s",
        region="eastus2",
        currency="USD",
        tier_min_units=Decimal(0),
        effective_start="",
        service_name="Foundry Models",
        product_name="Azure OpenAI",
        sku_name=meter_name,
        meter_name=meter_name,
        unit_of_measure=unit,
        retail_price=Decimal("1"),
        retrieved_at="t",
    )


def test_context_length_variants_are_identity_not_units():
    items = [_item("gpt-4-32k Inp glbl"), _item("gpt-4-32k Outp glbl"), _item("phi-3-mini-128k-instruct Inp glbl")]
    assert all(
        m.status != "matched"
        for m in match_meters(
            items, api_style=R, meter_terms="gpt-4", deployment_type="global", processing_tier="standard"
        ).matches
    )
    phi = match_meters(
        items, api_style=R, meter_terms="phi-3-mini-instruct", deployment_type="global", processing_tier="standard"
    )
    assert all(m.status != "matched" for m in phi.matches)
    exact = match_meters(
        items, api_style=R, meter_terms="gpt-4-32k", deployment_type="global", processing_tier="standard"
    )
    assert exact.get(Category.INPUT_UNCACHED).status == "matched"
    assert classify(_item("x Inp glbl 1M Tokens"), R).residual == ("x",)


def test_bare_vendor_prefix_never_matches_empty_residual():
    items = [_item("Text Inp glbl"), _item("cached Inp glbl"), _item("Outp glbl")]
    result = match_meters(items, api_style=R, meter_terms="gpt", deployment_type="global", processing_tier="standard")
    assert all(m.status != "matched" for m in result.matches)


async def test_client_skips_bad_items_and_other_regions():
    good = {
        "meterId": "m1",
        "meterName": "a Inp glbl",
        "retailPrice": 1,
        "armRegionName": "eastus2",
        "currencyCode": "USD",
        "unitOfMeasure": "1M",
    }
    bad = {
        "meterId": "m2",
        "meterName": "b Inp glbl",
        "retailPrice": True,
        "armRegionName": "eastus2",
        "currencyCode": "USD",
    }
    elsewhere = {**good, "meterId": "m3", "armRegionName": "westeurope", "currencyCode": "EUR"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=json.dumps({"Items": [good, bad, elsewhere], "NextPageLink": None}).encode()
        )

    items, warnings = await RetailPricesClient(transport=httpx2.MockTransport(handler)).fetch(
        "eastus2", "USD", ["serviceName eq 'X'"]
    )
    assert [i.meter_id for i in items] == ["m1"]
    assert warnings and "invalid number" in warnings[0]
