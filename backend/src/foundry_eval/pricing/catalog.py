"""SQLite cache of Azure Retail Prices items. Network access happens only on explicit refresh."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ..db.database import Database
from ..money import to_decimal, to_text
from ..timeutil import iso_now, new_id
from .retail import RetailItem, raw_json


@dataclass(frozen=True)
class CatalogItem:
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
    retrieved_at: str

    @property
    def key(self) -> str:
        return f"{self.meter_id}|{self.sku_id}"


@dataclass(frozen=True)
class FetchInfo:
    id: str
    region: str
    currency: str
    started_at: str
    finished_at: str | None
    item_count: int | None
    status: str
    error: str | None


def _row_to_item(row: sqlite3.Row) -> CatalogItem:
    return CatalogItem(
        meter_id=row["meter_id"],
        sku_id=row["sku_id"],
        region=row["region"],
        currency=row["currency"],
        tier_min_units=to_decimal(row["tier_min_units"]),
        effective_start=row["effective_start"],
        service_name=row["service_name"],
        product_name=row["product_name"],
        sku_name=row["sku_name"],
        meter_name=row["meter_name"],
        unit_of_measure=row["unit_of_measure"],
        retail_price=to_decimal(row["retail_price"]),
        retrieved_at=row["retrieved_at"],
    )


def _not_in_future(effective_start: str, now: datetime) -> bool:
    if not effective_start:
        return True
    try:
        parsed = datetime.fromisoformat(effective_start.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= now


def current_items(items: list[CatalogItem], now: datetime | None = None) -> list[CatalogItem]:
    """Latest effective price per (meter, sku, volume tier) that is not in the future."""
    now = now or datetime.now(UTC)
    chosen: dict[tuple[str, str, Decimal], CatalogItem] = {}
    for item in items:
        if not _not_in_future(item.effective_start, now):
            continue
        key = (item.meter_id, item.sku_id, item.tier_min_units)
        existing = chosen.get(key)
        if existing is None or item.effective_start > existing.effective_start:
            chosen[key] = item
    return list(chosen.values())


class PriceCatalog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start_fetch(self, region: str, currency: str, filters: list[str]) -> str:
        fetch_id = new_id()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO price_catalog_fetches (id, region, currency, filters_json, started_at, status)"
                " VALUES (?, ?, ?, ?, ?, 'running')",
                (fetch_id, region, currency, json.dumps(filters), iso_now()),
            )
        return fetch_id

    def fail_fetch(self, fetch_id: str, error: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE price_catalog_fetches SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (iso_now(), error, fetch_id),
            )

    def replace_region(self, fetch_id: str, region: str, currency: str, items: list[RetailItem]) -> int:
        retrieved_at = iso_now()
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM price_catalog WHERE region = ? AND currency = ?", (region, currency))
            conn.executemany(
                "INSERT OR REPLACE INTO price_catalog (meter_id, sku_id, region, currency, tier_min_units,"
                " effective_start, service_name, product_name, sku_name, meter_name, unit_of_measure,"
                " retail_price, unit_price, price_type, is_primary_region, raw_json, retrieved_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item.meter_id,
                        item.sku_id,
                        region,
                        currency,
                        to_text(item.tier_min_units),
                        item.effective_start,
                        item.service_name,
                        item.product_name,
                        item.sku_name,
                        item.meter_name,
                        item.unit_of_measure,
                        to_text(item.retail_price),
                        to_text(item.unit_price),
                        item.price_type,
                        None if item.is_primary_region is None else int(item.is_primary_region),
                        raw_json(item),
                        retrieved_at,
                    )
                    for item in items
                ],
            )
            conn.execute(
                "UPDATE price_catalog_fetches SET status = 'succeeded', finished_at = ?, item_count = ? WHERE id = ?",
                (retrieved_at, len(items), fetch_id),
            )
        return len(items)

    def items(self, region: str, currency: str) -> list[CatalogItem]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM price_catalog WHERE region = ? AND currency = ?", (region, currency)
            ).fetchall()
        return current_items([_row_to_item(r) for r in rows])

    def meter(self, meter_id: str, sku_id: str | None, region: str, currency: str) -> list[CatalogItem]:
        """Current items for one meter (several entries mean volume tiers)."""
        return [
            item
            for item in self.items(region, currency)
            if item.meter_id == meter_id and (not sku_id or item.sku_id == sku_id)
        ]

    def last_fetch(self, region: str, currency: str, status: str | None = None) -> FetchInfo | None:
        """The latest fetch for a region, or the latest one with the given status."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM price_catalog_fetches WHERE region = ? AND currency = ? AND (? IS NULL OR status = ?)"
                " ORDER BY started_at DESC LIMIT 1",
                (region, currency, status, status),
            ).fetchone()
        if row is None:
            return None
        return FetchInfo(
            id=row["id"],
            region=row["region"],
            currency=row["currency"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            item_count=row["item_count"],
            status=row["status"],
            error=row["error"],
        )
