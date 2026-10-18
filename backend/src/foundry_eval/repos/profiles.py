"""Model profile persistence (including preset rate defaults on creation)."""

from __future__ import annotations

import sqlite3

from ..db.database import Database
from ..domain.capabilities import Capabilities
from ..domain.presets import PRESETS
from ..domain.profiles import ModelProfile, ProfileFields
from ..pricing.ratecard import categories_for
from ..timeutil import iso_now, new_id
from .endpoints import NotFoundError

_COLUMNS = (
    "endpoint_id",
    "name",
    "api_style",
    "route",
    "custom_path",
    "api_version",
    "deployment",
    "publisher",
    "model_name",
    "model_version",
    "context_window",
    "max_output_tokens",
    "preset",
    "capabilities_json",
    "deployment_type",
    "processing_tier",
    "pricing_region",
    "meter_terms",
    "long_context_threshold",
    "streaming",
    "include_usage",
    "timeout_connect_s",
    "timeout_read_s",
    "timeout_total_s",
    "notes",
)


def _values(fields: ProfileFields) -> tuple[object, ...]:
    data = fields.model_dump()
    return (
        data["endpoint_id"],
        data["name"].strip(),
        fields.api_style.value,
        data["route"],
        data["custom_path"],
        data["api_version"],
        data["deployment"].strip(),
        data["publisher"],
        data["model_name"],
        data["model_version"],
        data["context_window"],
        data["max_output_tokens"],
        data["preset"],
        fields.capabilities.model_dump_json(),
        data["deployment_type"],
        data["processing_tier"],
        data["pricing_region"] or None,
        data["meter_terms"],
        data["long_context_threshold"],
        int(data["streaming"]),
        int(data["include_usage"]),
        float(data["timeout_connect_s"]),
        float(data["timeout_read_s"]),
        float(data["timeout_total_s"]),
        data["notes"],
    )


def _row(row: sqlite3.Row) -> ModelProfile:
    return ModelProfile(
        id=row["id"],
        endpoint_id=row["endpoint_id"],
        name=row["name"],
        api_style=row["api_style"],
        route=row["route"],
        custom_path=row["custom_path"],
        api_version=row["api_version"],
        deployment=row["deployment"],
        publisher=row["publisher"],
        model_name=row["model_name"],
        model_version=row["model_version"],
        context_window=row["context_window"],
        max_output_tokens=row["max_output_tokens"],
        preset=row["preset"],
        capabilities=Capabilities.model_validate_json(row["capabilities_json"]),
        deployment_type=row["deployment_type"],
        processing_tier=row["processing_tier"],
        pricing_region=row["pricing_region"],
        meter_terms=row["meter_terms"],
        long_context_threshold=row["long_context_threshold"],
        streaming=bool(row["streaming"]),
        include_usage=bool(row["include_usage"]),
        timeout_connect_s=row["timeout_connect_s"],
        timeout_read_s=row["timeout_read_s"],
        timeout_total_s=row["timeout_total_s"],
        notes=row["notes"],
        archived=bool(row["archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def validate_profile_fields(fields: ProfileFields) -> None:
    preset = PRESETS.get(fields.preset)
    if preset is None:
        raise ValueError(f"Unknown preset {fields.preset!r}.")
    if fields.api_style not in preset.api_styles:
        raise ValueError(f"Preset {preset.label!r} does not support the {fields.api_style.value} API.")
    if fields.api_style.is_cli and fields.route != "cli":
        raise ValueError("Local CLI profiles must use the 'cli' route.")
    if not fields.api_style.is_cli and fields.route == "cli":
        raise ValueError("HTTP API profiles cannot use the 'cli' route.")
    if fields.api_style.value == "anthropic_messages" and fields.route not in ("anthropic", "custom"):
        raise ValueError("Claude profiles use the 'anthropic' route (or a custom path).")
    if fields.api_style.is_openai and fields.route == "anthropic":
        raise ValueError("OpenAI-compatible profiles cannot use the 'anthropic' route.")
    if fields.api_style.value == "openai_responses" and fields.route == "foundry_models":
        raise ValueError("The Foundry Models inference route supports Chat Completions only.")
    if fields.route == "custom" and not fields.custom_path:
        raise ValueError("A custom route needs a custom path.")


class ProfileRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, fields: ProfileFields) -> ModelProfile:
        validate_profile_fields(fields)
        now = iso_now()
        profile_id = new_id()
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._db.transaction() as conn:
            conn.execute(
                f"INSERT INTO model_profiles (id, {', '.join(_COLUMNS)}, archived, created_at, updated_at)"
                f" VALUES (?, {placeholders}, 0, ?, ?)",
                (profile_id, *_values(fields), now, now),
            )
            preset = PRESETS[fields.preset]
            for category in categories_for(fields.api_style):
                source = preset.rate_defaults.get(category, "manual")
                conn.execute(
                    "INSERT INTO profile_rates (id, profile_id, category, tier_above_input, layer, source, currency,"
                    " declared_by, note, updated_at) VALUES (?, ?, ?, 0, 'base', ?, 'USD', ?, ?, ?)",
                    (
                        new_id(),
                        profile_id,
                        category.value,
                        source,
                        "preset" if source == "not_applicable" else None,
                        f"preset {preset.id}" if source == "not_applicable" else None,
                        now,
                    ),
                )
        return self.get(profile_id)

    def get(self, profile_id: str) -> ModelProfile:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM model_profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"profile {profile_id} not found")
        return _row(row)

    def list(self, *, include_archived: bool = False) -> list[ModelProfile]:
        query = "SELECT * FROM model_profiles"
        if not include_archived:
            query += " WHERE archived = 0"
        with self._db.connect() as conn:
            rows = conn.execute(query + " ORDER BY name COLLATE NOCASE").fetchall()
        return [_row(r) for r in rows]

    def update(self, profile_id: str, fields: ProfileFields) -> ModelProfile:
        validate_profile_fields(fields)
        current = self.get(profile_id)
        assignments = ", ".join(f"{c} = ?" for c in _COLUMNS)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE model_profiles SET {assignments}, updated_at = ? WHERE id = ?",
                (*_values(fields), iso_now(), profile_id),
            )
            old, new = current.long_context_threshold, fields.long_context_threshold
            if old and new and old != new:
                conn.execute(
                    "UPDATE profile_rates SET tier_above_input = ? WHERE profile_id = ? AND tier_above_input = ?",
                    (new, profile_id, old),
                )
            elif old and not new:
                conn.execute(
                    "DELETE FROM profile_rates WHERE profile_id = ? AND tier_above_input = ?", (profile_id, old)
                )
        return self.get(profile_id)

    def set_archived(self, profile_id: str, archived: bool) -> ModelProfile:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE model_profiles SET archived = ?, updated_at = ? WHERE id = ?",
                (int(archived), iso_now(), profile_id),
            )
        return self.get(profile_id)
