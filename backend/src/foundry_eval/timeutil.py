"""Small helpers for identifiers and UTC timestamps."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")
