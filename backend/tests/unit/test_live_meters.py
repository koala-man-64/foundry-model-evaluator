"""The matcher against real Azure Retail Prices meter names (captured live for eastus2 on 2026-10-16).

The hand-made fixture in catalog_eastus2.json spelled cached input "cached Inp"; live meters mostly
write "Cd Inp" / "cd Inp", so cache reads were never matched until this fixture caught it. These tests
pin the behaviour on real names, including the variants that must never match a plain profile
(batch, flex, fine-tuned, the "L" long-context variant of 4.3).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from foundry_eval.db.database import Database
from foundry_eval.pricing.catalog import PriceCatalog
from foundry_eval.pricing.matcher import match_meters
from foundry_eval.pricing.ratecard import Category
from foundry_eval.pricing.retail import DEFAULT_PRODUCT_FILTERS, parse_item
from foundry_eval.types import ApiStyle

LIVE = Path(__file__).parent.parent / "fixtures" / "retail" / "live_eastus2_2026-10-16.json"
TEXT_CATEGORIES = (Category.INPUT_UNCACHED, Category.CACHE_READ, Category.OUTPUT)


@pytest.fixture
def live(tmp_path) -> PriceCatalog:
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    catalog = PriceCatalog(db)
    payload = json.loads(LIVE.read_text(encoding="utf-8"), parse_float=Decimal)
    fetch = catalog.start_fetch("eastus2", "USD", list(DEFAULT_PRODUCT_FILTERS))
    catalog.replace_region(fetch, "eastus2", "USD", [parse_item(raw) for raw in payload["Items"]])
    return catalog


def chosen(catalog: PriceCatalog, terms: str, category: Category, context: str = "base", **kwargs) -> str | None:
    result = match_meters(
        catalog.items("eastus2", "USD"),
        api_style=ApiStyle.OPENAI_RESPONSES,
        meter_terms=terms,
        deployment_type=kwargs.get("deployment", "global"),
        processing_tier=kwargs.get("tier", "standard"),
    )
    match = result.get(category, context)
    if match is None or match.status != "matched" or match.chosen is None:
        return None
    return match.chosen.info.item.meter_name


@pytest.mark.parametrize(
    ("terms", "deployment", "tier", "context", "expected"),
    [
        (
            "5.6 terra",
            "global",
            "standard",
            "base",
            {
                Category.INPUT_UNCACHED: "5.6 terra ShortCo Inp Std Gl 1M Tokens",
                Category.CACHE_READ: "5.6 terra ShortCo Cd Inp Std Gl 1M Tokens",
                Category.CACHE_WRITE: "5.6 terra ShortCo Cd Wr Std Gl 1M Tokens",
                Category.OUTPUT: "5.6 terra ShortCo Opt Std Gl 1M Tokens",
            },
        ),
        (
            "5.6 terra",
            "global",
            "standard",
            "long",
            {
                Category.CACHE_READ: "5.6 terra LongCo Cd Inp Std Gl 1M Tokens",
                Category.OUTPUT: "5.6 terra LongCo Opt Std Gl 1M Tokens",
            },
        ),
        (
            "5.6 terra",
            "data_zone",
            "priority",
            "base",
            {
                Category.INPUT_UNCACHED: "5.6 terra ShortCo Inp PP DZ 1M Tokens",
                Category.CACHE_READ: "5.6 terra ShortCo Cd Inp PP DZ 1M Tokens",
            },
        ),
        (
            "5.4 mini",
            "global",
            "standard",
            "base",
            {
                Category.INPUT_UNCACHED: "5.4 mini Inp Gl 1M Tokens",
                Category.CACHE_READ: "5.4 mini cd Inp Gl 1M Tokens",
                Category.OUTPUT: "5.4 mini Opt Gl 1M Tokens",
            },
        ),
        (
            "5.4 mini",
            "global",
            "priority",
            "base",
            {Category.CACHE_READ: "5.4 mini pp cd Inp Gl 1M Tokens"},
        ),
        (
            "4.1 mini",
            "global",
            "priority",
            "base",
            {
                Category.INPUT_UNCACHED: "4.1 mini pp Inp Gl Tokens",
                Category.CACHE_READ: "4.1 mini pp cd Inp Gl Tokens",
                Category.OUTPUT: "4.1 mini pp Opt Gl Tokens",
            },
        ),
        (
            "4.3",
            "global",
            "standard",
            "base",
            {
                Category.INPUT_UNCACHED: "4.3 Inp Glbl Tokens",
                Category.CACHE_READ: "4.3 Cached Inp Glbl Tokens",
                Category.OUTPUT: "4.3 Outp Glbl Tokens",
            },
        ),
    ],
)
def test_live_meters_match_every_text_category(live, terms, deployment, tier, context, expected):
    for category, meter in expected.items():
        assert chosen(live, terms, category, context, deployment=deployment, tier=tier) == meter, category


@pytest.mark.parametrize(
    ("terms", "deployment", "tier"),
    [
        ("4.1 mini", "global", "standard"),  # only priority and fine-tuned meters exist: no guess
        ("54 mini", "global", "standard"),  # the flex meters' own stem still says Flex
        ("56terra", "global", "standard"),  # flex long/short ("Fl", "LoCo") are not standard meters
    ],
)
def test_variants_without_a_plain_meter_stay_unmatched(live, terms, deployment, tier):
    for category in TEXT_CATEGORIES:
        assert chosen(live, terms, category, deployment=deployment, tier=tier) is None, category


def test_batch_flex_and_fine_tuned_meters_never_stand_in_for_standard_ones(live):
    names = {
        chosen(live, terms, category, deployment=deployment, tier=tier)
        for terms in ("5.4 mini", "4.1 mini", "5.6 terra", "4.3")
        for deployment in ("global", "data_zone")
        for tier in ("standard", "priority")
        for category in TEXT_CATEGORIES
    }
    names.discard(None)
    assert names, "expected some matches"
    for name in names:
        lowered = f" {name.lower()} "
        assert not any(word in lowered for word in (" batch ", " flex ", " fl ", " ft ", " l tokens"))
