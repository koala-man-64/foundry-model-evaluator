"""Editable per-profile rate configuration (base sources and labelled custom overrides)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..db.database import Database
from ..money import Money, to_decimal, to_text
from ..pricing.ratecard import Category
from ..timeutil import iso_now, new_id

BaseSource = Literal["azure_auto", "azure_pinned", "manual", "not_applicable"]


class RateRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    profile_id: str
    category: Category
    tier_above_input: int
    layer: Literal["base", "override"]
    source: str
    price: Money | None = None
    unit_label: str | None = None
    unit_tokens: int | None = None
    currency: str = "USD"
    meter_id: str | None = None
    sku_id: str | None = None
    source_url: str | None = None
    effective_date: str | None = None
    note: str | None = None
    declared_by: Literal["preset", "user"] | None = None
    updated_at: str


def _row(row: sqlite3.Row) -> RateRow:
    return RateRow(
        id=row["id"],
        profile_id=row["profile_id"],
        category=Category(row["category"]),
        tier_above_input=row["tier_above_input"],
        layer=row["layer"],
        source=row["source"],
        price=to_decimal(row["price"]) if row["price"] is not None else None,
        unit_label=row["unit_label"],
        unit_tokens=row["unit_tokens"],
        currency=row["currency"],
        meter_id=row["meter_id"],
        sku_id=row["sku_id"],
        source_url=row["source_url"],
        effective_date=row["effective_date"],
        note=row["note"],
        declared_by=row["declared_by"],
        updated_at=row["updated_at"],
    )


class RateRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def list(self, profile_id: str) -> list[RateRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM profile_rates WHERE profile_id = ? ORDER BY tier_above_input, category, layer",
                (profile_id,),
            ).fetchall()
        return [_row(r) for r in rows]

    def _upsert(
        self,
        conn: sqlite3.Connection,
        profile_id: str,
        category: Category,
        tier: int,
        layer: str,
        *,
        source: str,
        price: Decimal | None = None,
        unit_label: str | None = None,
        unit_tokens: int | None = None,
        currency: str = "USD",
        meter_id: str | None = None,
        sku_id: str | None = None,
        source_url: str | None = None,
        effective_date: str | None = None,
        note: str | None = None,
        declared_by: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO profile_rates (id, profile_id, category, tier_above_input, layer, source, price, unit_label,"
            " unit_tokens, currency, meter_id, sku_id, source_url, effective_date, note, declared_by, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(profile_id, category, tier_above_input, layer) DO UPDATE SET"
            " source = excluded.source, price = excluded.price, unit_label = excluded.unit_label,"
            " unit_tokens = excluded.unit_tokens, currency = excluded.currency, meter_id = excluded.meter_id,"
            " sku_id = excluded.sku_id, source_url = excluded.source_url, effective_date = excluded.effective_date,"
            " note = excluded.note, declared_by = excluded.declared_by, updated_at = excluded.updated_at",
            (
                new_id(),
                profile_id,
                category.value,
                tier,
                layer,
                source,
                to_text(price) if price is not None else None,
                unit_label,
                unit_tokens,
                currency,
                meter_id,
                sku_id,
                source_url,
                effective_date,
                note,
                declared_by,
                iso_now(),
            ),
        )

    def set_azure(
        self,
        profile_id: str,
        category: Category,
        tier: int,
        *,
        pinned: bool,
        meter_id: str | None,
        sku_id: str | None,
        currency: str,
    ) -> None:
        with self._db.transaction() as conn:
            self._upsert(
                conn,
                profile_id,
                category,
                tier,
                "base",
                source="azure_pinned" if pinned else "azure_auto",
                meter_id=meter_id,
                sku_id=sku_id,
                currency=currency,
            )

    def set_manual(
        self,
        profile_id: str,
        category: Category,
        tier: int,
        *,
        price: Decimal,
        unit_tokens: int,
        unit_label: str,
        currency: str,
        source_url: str,
        effective_date: str | None,
        note: str | None,
    ) -> None:
        with self._db.transaction() as conn:
            self._upsert(
                conn,
                profile_id,
                category,
                tier,
                "base",
                source="manual",
                price=price,
                unit_label=unit_label,
                unit_tokens=unit_tokens,
                currency=currency,
                source_url=source_url,
                effective_date=effective_date,
                note=note,
            )

    def set_not_applicable(self, profile_id: str, category: Category, tier: int, *, note: str | None) -> None:
        with self._db.transaction() as conn:
            self._upsert(
                conn,
                profile_id,
                category,
                tier,
                "base",
                source="not_applicable",
                note=note or "declared by the user",
                declared_by="user",
            )

    def set_override(
        self,
        profile_id: str,
        category: Category,
        tier: int,
        *,
        price: Decimal,
        unit_tokens: int,
        unit_label: str,
        currency: str,
        note: str,
        source_url: str | None,
    ) -> None:
        with self._db.transaction() as conn:
            self._upsert(
                conn,
                profile_id,
                category,
                tier,
                "override",
                source="custom_override",
                price=price,
                unit_label=unit_label,
                unit_tokens=unit_tokens,
                currency=currency,
                note=note,
                source_url=source_url,
            )

    def clear(self, profile_id: str, category: Category, tier: int, layer: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM profile_rates"
                " WHERE profile_id = ? AND category = ? AND tier_above_input = ? AND layer = ?",
                (profile_id, category.value, tier, layer),
            )
