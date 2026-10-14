"""Application settings stored in SQLite (no secrets)."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..db.database import Database
from ..pricing.retail import DEFAULT_PRODUCT_FILTERS


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(default=3, ge=1, le=10)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    product_filters: list[str] = Field(default_factory=lambda: list(DEFAULT_PRODUCT_FILTERS))
    # Upper bound used for the judge input size check when a judge profile has no context window.
    judge_input_limit_tokens: int = Field(default=100_000, ge=1_000)

    @field_validator("product_filters")
    @classmethod
    def _filters(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v.strip()]
        if not cleaned:
            raise ValueError("At least one Retail Prices product filter is required.")
        for clause in cleaned:
            if len(clause) > 300 or any(ch in clause for ch in "\r\n;"):
                raise ValueError("Product filters must be single OData clauses.")
        return cleaned


class SettingsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> AppSettings:
        with self._db.connect() as conn:
            row = conn.execute("SELECT value_json FROM app_settings WHERE key = 'app'").fetchone()
        if row is None:
            return AppSettings()
        return AppSettings.model_validate(json.loads(row["value_json"]))

    def save(self, settings: AppSettings) -> AppSettings:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value_json) VALUES ('app', ?)"
                " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (settings.model_dump_json(),),
            )
        return settings
