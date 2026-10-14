"""Azure Retail Prices ``unitOfMeasure`` parsing (tokens per priced unit)."""

from __future__ import annotations

import re
from decimal import Decimal

_UNIT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KkMm])\s*(?:tokens?)?\s*$", re.IGNORECASE)
_NAME_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KkMm])\s+tokens?\b", re.IGNORECASE)
_MULTIPLIER = {"k": 1_000, "m": 1_000_000}


class UnitError(ValueError):
    """The unit is not a supported per-token unit."""


def parse_token_unit(text: str) -> int:
    """Return the number of tokens per priced unit.

    Accepts ``1K``, ``1M``, ``1K Tokens``, ``1M Tokens`` (and numeric multiples such as ``10K``).
    Anything else (``1 Hour``, ``1``, ``1/Day``…) is rejected rather than guessed.
    """
    match = _UNIT_RE.match(text or "")
    if not match:
        raise UnitError(f"unsupported unit of measure {text!r}")
    count = Decimal(match.group(1)) * _MULTIPLIER[match.group(2).lower()]
    if count != count.to_integral_value() or count <= 0:
        raise UnitError(f"unsupported unit of measure {text!r}")
    return int(count)


def unit_in_name(name: str) -> int | None:
    """Tokens per unit if a meter/SKU name embeds a unit such as ``1M Tokens``; else ``None``."""
    match = _NAME_UNIT_RE.search(name or "")
    if not match:
        return None
    count = Decimal(match.group(1)) * _MULTIPLIER[match.group(2).lower()]
    return int(count) if count == count.to_integral_value() else None


def unit_label(tokens: int) -> str:
    if tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}M tokens"
    if tokens % 1_000 == 0:
        return f"{tokens // 1_000}K tokens"
    return f"{tokens} tokens"
