"""Immutable price snapshots frozen when a run (or judge evaluation) is created."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from ..domain.profiles import Endpoint, ModelProfile
from ..money import to_decimal, to_text
from ..timeutil import iso_now, new_id
from ..types import ApiStyle
from .ratecard import Category, CategoryRate, Rate, RateCard, RateSource, RateStatus, RateTier
from .resolver import pricing_context


def create_snapshot(
    conn: sqlite3.Connection,
    profile: ModelProfile,
    endpoint: Endpoint,
    card: RateCard,
) -> str:
    snapshot_id = new_id()
    context = pricing_context(profile, endpoint, card.currency)
    header_context = {
        "api_style": card.api_style.value,
        "region": context.region,
        "deployment_type": profile.deployment_type,
        "processing_tier": card.processing_tier,
        "profile_name": profile.name,
        "explanations": card.explanations,
        "tiers": [t.above_input_tokens for t in card.tiers],
    }
    for tier in card.tiers:
        for category, entry in tier.categories.items():
            rate = entry.rate
            conn.execute(
                "INSERT INTO price_snapshot_rates (snapshot_id, category, tier_above_input, status, source, price,"
                " unit_label, unit_tokens, price_per_million, currency, base_price, base_unit_tokens, base_source,"
                " meter_id, sku_id, meter_name, product_name, region, source_url, effective_date, retrieved_at, note,"
                " declared_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    category.value,
                    tier.above_input_tokens,
                    entry.status.value,
                    rate.source.value if rate else None,
                    to_text(rate.price) if rate else None,
                    rate.unit_label if rate else None,
                    rate.unit_tokens if rate else None,
                    to_text(rate.per_million) if rate else None,
                    rate.currency if rate else card.currency,
                    to_text(rate.base_price) if rate and rate.base_price is not None else None,
                    rate.base_unit_tokens if rate else None,
                    rate.base_source.value if rate and rate.base_source else None,
                    rate.meter_id if rate else None,
                    rate.sku_id if rate else None,
                    rate.meter_name if rate else None,
                    rate.product_name if rate else None,
                    rate.region if rate else None,
                    rate.source_url if rate else None,
                    rate.effective_date if rate else None,
                    rate.retrieved_at if rate else None,
                    (rate.note if rate else entry.reason),
                    entry.declared_by,
                ),
            )
    conn.execute(
        "INSERT INTO price_snapshots (id, profile_id, created_at, currency, processing_tier, pricing_identity,"
        " context_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            profile.id,
            iso_now(),
            card.currency,
            card.processing_tier,
            profile.pricing_identity(endpoint, card.currency),
            json.dumps(header_context, sort_keys=True),
        ),
    )
    return snapshot_id


def load_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> tuple[RateCard, dict[str, object]]:
    header = conn.execute("SELECT * FROM price_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if header is None:
        raise LookupError(f"price snapshot {snapshot_id} not found")
    context = json.loads(header["context_json"])
    rows = conn.execute(
        "SELECT * FROM price_snapshot_rates WHERE snapshot_id = ? ORDER BY tier_above_input", (snapshot_id,)
    ).fetchall()
    tiers: dict[int, dict[Category, CategoryRate]] = {}
    for row in rows:
        status = RateStatus(row["status"])
        category = Category(row["category"])
        if status is RateStatus.PRICED:
            entry = CategoryRate.priced(
                Rate(
                    price=to_decimal(row["price"]),
                    unit_tokens=row["unit_tokens"],
                    unit_label=row["unit_label"] or "",
                    currency=row["currency"],
                    source=RateSource(row["source"]),
                    source_url=row["source_url"],
                    meter_id=row["meter_id"],
                    sku_id=row["sku_id"],
                    meter_name=row["meter_name"],
                    product_name=row["product_name"],
                    region=row["region"],
                    effective_date=row["effective_date"],
                    retrieved_at=row["retrieved_at"],
                    note=row["note"],
                    base_price=to_decimal(row["base_price"]) if row["base_price"] is not None else None,
                    base_unit_tokens=row["base_unit_tokens"],
                    base_source=RateSource(row["base_source"]) if row["base_source"] else None,
                )
            )
        elif status is RateStatus.NOT_APPLICABLE:
            declared: Literal["preset", "user"] = "user" if row["declared_by"] == "user" else "preset"
            entry = CategoryRate.not_applicable(row["note"] or "not applicable", declared_by=declared)
        else:
            entry = CategoryRate.unknown(row["note"] or "no rate configured")
        tiers.setdefault(row["tier_above_input"], {})[category] = entry
    card = RateCard(
        api_style=ApiStyle(context["api_style"]),
        currency=header["currency"],
        processing_tier=header["processing_tier"],
        tiers=[RateTier(above_input_tokens=t, categories=c) for t, c in sorted(tiers.items())]
        or [RateTier(above_input_tokens=0)],
        explanations=list(context.get("explanations", [])),
    )
    meta = {
        "id": header["id"],
        "created_at": header["created_at"],
        "pricing_identity": header["pricing_identity"],
        **context,
    }
    return card, meta
