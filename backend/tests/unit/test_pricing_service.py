from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from foundry_eval.db.database import Database
from foundry_eval.domain.presets import preset_capabilities
from foundry_eval.domain.profiles import ProfileFields
from foundry_eval.pricing.ratecard import Category, RateSource, RateStatus
from foundry_eval.pricing.retail import RetailPricesClient, parse_item
from foundry_eval.pricing.service import PricingService
from foundry_eval.pricing.snapshots import create_snapshot, load_snapshot
from foundry_eval.repos.endpoints import ConflictError, EndpointRepository
from foundry_eval.repos.profiles import ProfileRepository
from foundry_eval.types import ApiStyle

FIXTURE = Path(__file__).parent.parent / "fixtures" / "retail" / "catalog_eastus2.json"


def retail_transport(price_scale: str = "1") -> httpx2.MockTransport:
    payload = json.loads(FIXTURE.read_text())
    scale = Decimal(price_scale)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if "Foundry" not in request.url.params["$filter"]:
            return httpx2.Response(200, content=b'{"Items": [], "NextPageLink": null}')
        items = []
        for raw in payload["Items"]:
            raw = dict(raw)
            raw["retailPrice"] = str(Decimal(str(raw["retailPrice"])) * scale)
            items.append(raw)
        body = (
            json.dumps({"Items": items, "NextPageLink": None})
            .replace('"retailPrice": "', '"retailPrice": ')
            .replace('", "unitPrice"', ', "unitPrice"')
        )
        return httpx2.Response(200, content=body.encode())

    return httpx2.MockTransport(handler)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    return database


def make_profile(db: Database, *, preset: str, api_style: ApiStyle, terms: str = "", route: str = "openai_v1", **extra):
    endpoints = EndpointRepository(db)
    endpoint = endpoints.create(
        name="Res", base_url="https://res.services.ai.azure.com", region="eastus2", auth_style="api-key"
    )
    fields = ProfileFields(
        endpoint_id=endpoint.id,
        name=f"{preset}-{terms}",
        api_style=api_style,
        route=route,
        deployment="dep",
        preset=preset,
        capabilities=preset_capabilities(preset, api_style),
        meter_terms=terms,
        **extra,
    )
    return ProfileRepository(db).create(fields), endpoint


async def test_refresh_match_and_resolve_azure_prices(db):
    profile, _ = make_profile(
        db, preset="openai-reasoning-cache-write", api_style=ApiStyle.OPENAI_RESPONSES, terms="gpt-5.6-terra"
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    results = await service.refresh()
    assert results[0].status == "succeeded" and results[0].item_count > 40
    card = service.resolve(profile)
    base = card.tiers[0]
    assert base.get(Category.INPUT_UNCACHED).rate.price == Decimal("2.5")
    assert base.get(Category.CACHE_WRITE).rate.price == Decimal("3.125")
    assert base.get(Category.OUTPUT).rate.source is RateSource.AZURE_AUTO
    assert base.get(Category.OUTPUT).rate.retrieved_at
    assert len(card.tiers) == 1  # long-context meters exist but no threshold is set


async def test_long_context_tier_requires_threshold(db):
    profile, _ = make_profile(
        db,
        preset="openai-reasoning-cache-write",
        api_style=ApiStyle.OPENAI_RESPONSES,
        terms="5.6 terra",
        long_context_threshold=272_000,
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    card = service.resolve(profile)
    assert [t.above_input_tokens for t in card.tiers] == [0, 272_000]
    assert card.tiers[1].get(Category.INPUT_UNCACHED).rate.price == Decimal("5.0")


async def test_preset_not_applicable_is_not_overwritten_by_matching(db):
    profile, _ = make_profile(db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="5.6 terra")
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    write = service.resolve(profile).tiers[0].get(Category.CACHE_WRITE)
    assert write.status is RateStatus.NOT_APPLICABLE
    assert write.declared_by == "preset"


async def test_unmatched_categories_are_unknown_with_reason(db):
    profile, _ = make_profile(db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="gpt-4o")
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    entry = service.resolve(profile).tiers[0].get(Category.INPUT_UNCACHED)
    assert entry.status is RateStatus.UNKNOWN
    assert "pin a meter" in (entry.reason or "")


async def test_pinning_and_overrides(db):
    profile, _ = make_profile(db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="gpt-4o")
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    item = next(i for i in service.catalog.items("eastus2", "USD") if i.meter_name == "gpt-4o-1120-Inp-glbl")
    service.pin(profile.id, Category.INPUT_UNCACHED, 0, item.meter_id, item.sku_id)
    entry = service.resolve(profile).tiers[0].get(Category.INPUT_UNCACHED)
    assert entry.rate.source is RateSource.AZURE_PINNED and entry.rate.price == Decimal("2.5")
    with pytest.raises(ValueError, match="not in the cached catalog"):
        service.pin(profile.id, Category.OUTPUT, 0, "missing-meter", None)
    service.rates.set_override(
        profile.id,
        Category.INPUT_UNCACHED,
        0,
        price=Decimal("2"),
        unit_tokens=1_000_000,
        unit_label="1M",
        currency="USD",
        note="negotiated what-if",
        source_url=None,
    )
    overridden = service.resolve(profile).tiers[0].get(Category.INPUT_UNCACHED)
    assert overridden.rate.source is RateSource.CUSTOM_OVERRIDE
    assert overridden.rate.base_price == Decimal("2.5")
    # Matching again never replaces a pinned choice.
    service.match_and_apply(profile.id)
    service.rates.clear(profile.id, Category.INPUT_UNCACHED, 0, "override")
    assert service.resolve(profile).tiers[0].get(Category.INPUT_UNCACHED).rate.source is RateSource.AZURE_PINNED


async def test_pinned_single_nonzero_volume_tier_resolves_unknown(db):
    profile, _ = make_profile(db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="tiered-model")
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()

    payload = json.loads(FIXTURE.read_text(), parse_float=Decimal)
    raw = next(
        item
        for item in payload["Items"]
        if item["meterName"] == "tiered-model Inp glbl" and item["tierMinimumUnits"] == 1_000_000
    )
    item = parse_item(raw)
    fetch_id = service.catalog.start_fetch("eastus2", "USD", [])
    service.catalog.replace_region(fetch_id, "eastus2", "USD", [item])
    service.pin(profile.id, Category.INPUT_UNCACHED, 0, item.meter_id, item.sku_id)

    entry = service.resolve(profile).tiers[0].get(Category.INPUT_UNCACHED)
    assert entry.status is RateStatus.UNKNOWN
    assert "volume tier" in (entry.reason or "")


def test_manual_rates_for_claude(db):
    profile, _ = make_profile(db, preset="claude-adaptive", api_style=ApiStyle.ANTHROPIC_MESSAGES, route="anthropic")
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    assert service.resolve(profile).tiers[0].get(Category.OUTPUT).status is RateStatus.UNKNOWN
    service.rates.set_manual(
        profile.id,
        Category.OUTPUT,
        0,
        price=Decimal("20"),
        unit_tokens=1_000_000,
        unit_label="1M",
        currency="USD",
        source_url="https://www.anthropic.com/pricing",
        effective_date="2026-09-01",
        note=None,
    )
    rate = service.resolve(profile).tiers[0].get(Category.OUTPUT).rate
    assert rate.source is RateSource.MANUAL and rate.source_url.startswith("https://")
    assert rate.per_million == Decimal("20")


async def test_snapshots_preserve_history_across_refresh(db):
    profile, endpoint = make_profile(
        db, preset="openai-reasoning-cache-write", api_style=ApiStyle.OPENAI_RESPONSES, terms="5.6 terra"
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    card = service.resolve(profile)
    with db.transaction() as conn:
        snapshot_id = create_snapshot(conn, profile, endpoint, card)
    service.retail = RetailPricesClient(transport=retail_transport("2"))
    await service.refresh()
    assert service.resolve(profile).tiers[0].get(Category.OUTPUT).rate.price == Decimal("30.0")
    with db.connect() as conn:
        frozen, meta = load_snapshot(conn, snapshot_id)
    assert frozen.tiers[0].get(Category.OUTPUT).rate.price == Decimal("15.0")
    assert frozen.tiers[0].get(Category.CACHE_WRITE).rate.meter_name == "5.6 terra ShortCo Cd Wr Std Gl"
    assert frozen.tiers[0].get(Category.OUTPUT).rate.effective_date == "2026-07-01T00:00:00Z"
    assert meta["pricing_identity"] == profile.pricing_identity(endpoint, "USD")
    assert frozen == card


async def test_failed_refresh_is_recorded(db):
    make_profile(db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="x")
    service = PricingService(db, RetailPricesClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(500))))
    results = await service.refresh()
    assert results[0].status == "failed"
    assert service.catalog.last_fetch("eastus2", "USD").status == "failed"


def test_endpoint_host_change_rotates_credential_ref(db):
    endpoints = EndpointRepository(db)
    endpoint = endpoints.create(
        name="A", base_url="https://a.services.ai.azure.com/", region="eastus2", auth_style="api-key"
    )
    same, changed = endpoints.update(
        endpoint.id, name="A2", base_url="https://a.services.ai.azure.com", region="eastus2", auth_style="api-key"
    )
    assert not changed and same.credential_ref == endpoint.credential_ref
    moved, changed = endpoints.update(
        endpoint.id, name="A2", base_url="https://b.services.ai.azure.com", region="eastus2", auth_style="api-key"
    )
    assert changed and moved.credential_ref != endpoint.credential_ref and moved.key_set_at is None


def test_endpoint_url_validation_and_delete_conflict(db):
    endpoints = EndpointRepository(db)
    with pytest.raises(ValueError):
        endpoints.create(name="bad", base_url="http://example.com", region="", auth_style="api-key")
    with pytest.raises(ValueError):
        endpoints.create(name="bad", base_url="https://user:pw@example.com", region="", auth_style="api-key")
    local = endpoints.create(name="mock", base_url="http://127.0.0.1:9999", region="", auth_style="bearer")
    assert local.base_url == "http://127.0.0.1:9999"
    _, endpoint = make_profile(db, preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT)
    with pytest.raises(ConflictError):
        endpoints.delete(endpoint.id)
    assert endpoints.delete(local.id).id == local.id


def test_profile_validation(db):
    endpoint = EndpointRepository(db).create(
        name="R", base_url="https://r.services.ai.azure.com", region="eastus2", auth_style="api-key"
    )
    repo = ProfileRepository(db)

    def fields(**kw):
        base = dict(
            endpoint_id=endpoint.id,
            name="p",
            api_style=ApiStyle.OPENAI_RESPONSES,
            route="openai_v1",
            deployment="d",
            preset="openai-reasoning",
            capabilities=preset_capabilities("openai-reasoning", ApiStyle.OPENAI_RESPONSES),
        )
        base.update(kw)
        return ProfileFields(**base)

    with pytest.raises(ValueError, match="Unknown preset"):
        repo.create(fields(preset="nope"))
    with pytest.raises(ValueError, match="does not support"):
        repo.create(fields(preset="claude-adaptive"))
    with pytest.raises(ValueError, match="Chat Completions only"):
        repo.create(fields(route="foundry_models"))
    with pytest.raises(ValueError, match="custom path"):
        repo.create(fields(route="custom"))
    with pytest.raises(ValueError, match="Custom path must start"):
        fields(route="custom", custom_path="https://evil")
    created = repo.create(fields())
    updated = repo.update(created.id, fields(name="renamed", long_context_threshold=1000))
    assert updated.name == "renamed"
    assert repo.set_archived(created.id, True).archived
    assert repo.list() == []
    assert len(repo.list(include_archived=True)) == 1


async def test_stale_auto_meters_are_cleared_when_terms_change(db):
    profile, _ = make_profile(
        db,
        preset="openai-reasoning-cache-write",
        api_style=ApiStyle.OPENAI_RESPONSES,
        terms="5.6 terra",
        long_context_threshold=272_000,
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    assert len(service.resolve(profile).tiers) == 2
    fields = ProfileFields(
        **{k: v for k, v in profile.model_dump().items() if k in ProfileFields.model_fields}
        | {"meter_terms": "5.6 luna"}
    )
    updated = ProfileRepository(db).update(profile.id, fields)
    service.match_and_apply(profile.id)
    card = service.resolve(updated)
    assert card.tiers[0].get(Category.INPUT_UNCACHED).rate.price == Decimal("1.0")
    long = card.tiers[1].get(Category.INPUT_UNCACHED)
    assert long.status is RateStatus.UNKNOWN  # luna has no LongCo meters: no stale terra price


async def test_auto_meter_with_changed_deployment_is_rejected_at_resolve(db):
    profile, _ = make_profile(
        db, preset="openai-reasoning-cache-write", api_style=ApiStyle.OPENAI_RESPONSES, terms="5.6 terra"
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    fields = ProfileFields(
        **{k: v for k, v in profile.model_dump().items() if k in ProfileFields.model_fields}
        | {"deployment_type": "data_zone"}
    )
    updated = ProfileRepository(db).update(profile.id, fields)  # no re-match yet
    entry = service.resolve(updated).tiers[0].get(Category.OUTPUT)
    assert entry.status is RateStatus.UNKNOWN
    assert "deployment type" in (entry.reason or "")


async def test_snapshots_are_sealed_against_inserts(db):
    import sqlite3

    profile, endpoint = make_profile(
        db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="5.6 terra"
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    with db.transaction() as conn:
        snapshot_id = create_snapshot(conn, profile, endpoint, service.resolve(profile))
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "INSERT INTO price_snapshot_rates (snapshot_id, category, tier_above_input, status, currency)"
            " VALUES (?, 'output', 272000, 'priced', 'USD')",
            (snapshot_id,),
        )


async def test_a_partly_failed_refresh_keeps_the_cached_catalog_and_matches(db):
    profile, _ = make_profile(
        db, preset="openai-reasoning-cache-write", api_style=ApiStyle.OPENAI_RESPONSES, terms="gpt-5.6-terra"
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    cached = len(service.catalog.items("eastus2", "USD"))
    good = retail_transport()

    def one_filter_fails(request: httpx2.Request) -> httpx2.Response:
        if "Foundry" in request.url.params["$filter"]:
            return httpx2.Response(503)
        return good.handler(request)  # type: ignore[attr-defined]

    service.retail = RetailPricesClient(transport=httpx2.MockTransport(one_filter_fails))
    results = await service.refresh()
    assert results[0].status == "failed" and f"The {cached} cached prices were kept unchanged" in (
        results[0].error or ""
    )
    assert len(service.catalog.items("eastus2", "USD")) == cached
    output = service.resolve(profile).tiers[0].get(Category.OUTPUT)
    assert output.status is RateStatus.PRICED and output.rate.source is RateSource.AZURE_AUTO


async def test_refresh_failure_closes_fetch_record(db):
    make_profile(db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="x")

    class Boom(RetailPricesClient):
        async def fetch(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("unexpected")

    service = PricingService(db, Boom())
    results = await service.refresh()
    assert results[0].status == "failed"
    assert service.catalog.last_fetch("eastus2", "USD").status == "failed"


async def test_an_empty_answer_does_not_wipe_a_cached_region(db):
    profile, _ = make_profile(
        db, preset="openai-reasoning-cache-write", api_style=ApiStyle.OPENAI_RESPONSES, terms="gpt-5.6-terra"
    )
    service = PricingService(db, RetailPricesClient(transport=retail_transport()))
    await service.refresh()
    cached = len(service.catalog.items("eastus2", "USD"))
    in_use = service.catalog.last_fetch("eastus2", "USD")
    empty = httpx2.MockTransport(lambda r: httpx2.Response(200, content=b'{"Items": [], "NextPageLink": null}'))
    service.retail = RetailPricesClient(transport=empty)
    results = await service.refresh()
    assert results[0].status == "failed" and "returned no prices" in (results[0].error or "")
    assert len(service.catalog.items("eastus2", "USD")) == cached
    assert service.resolve(profile).tiers[0].get(Category.OUTPUT).status is RateStatus.PRICED
    # The last successful fetch still describes the prices in use.
    assert service.catalog.last_fetch("eastus2", "USD", status="succeeded") == in_use
    assert service.catalog.last_fetch("eastus2", "USD").status == "failed"


async def test_a_first_failed_refresh_does_not_claim_a_cache(db):
    make_profile(db, preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, terms="x")
    service = PricingService(db, RetailPricesClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(503))))
    [result] = await service.refresh()
    assert result.status == "failed" and "kept" not in (result.error or "")
