"""Decimal helpers. Money and rates are never represented as binary floats."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

ZERO = Decimal(0)
MILLION = Decimal(1_000_000)
PRECISION = 40


def to_decimal(value: object) -> Decimal:
    """Parse a decimal from str/int/Decimal. Floats are rejected to avoid binary rounding."""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise TypeError("booleans are not decimals")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a decimal number: {value!r}") from exc
    else:
        raise TypeError(f"expected a decimal string, got {type(value).__name__}")
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    return result


def to_text(value: Decimal) -> str:
    """Plain (non-exponent) decimal text without trailing zeros: Decimal('1E-7') -> '0.0000001'."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def mul(*values: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        result = Decimal(1)
        for value in values:
            result *= value
        return +result


def div(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return numerator / denominator


def add(*values: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        total = ZERO
        for value in values:
            total += value
        return +total


def _validate_money(value: object) -> Decimal:
    # Raise ValueError (not TypeError) so pydantic reports a validation error (HTTP 422).
    if isinstance(value, float):
        raise ValueError("money values must be sent as decimal strings, not floats")
    try:
        return to_decimal(value)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


# Pydantic type for money/rates: accepts strings/ints/Decimals, serializes to plain decimal text.
Money = Annotated[
    Decimal,
    BeforeValidator(_validate_money),
    PlainSerializer(to_text, return_type=str, when_used="always"),
]

# Values a person enters (prices, budgets, what-if factors) are bounded, so that a value such as
# 1E-50000000 cannot expand into megabytes of digits when it is stored, computed with or shown.
INPUT_MAX_DIGITS = 30
INPUT_MAX_PLACES = 20
INPUT_MAX_MAGNITUDE = 15  # values below 10^15


def validate_money_input(value: object) -> Decimal:
    result = _validate_money(value)
    # Checked on the digits and exponent alone: arithmetic on such values can overflow.
    _sign, digits, exponent = result.as_tuple()
    assert isinstance(exponent, int)  # finite, checked above
    if len(digits) > INPUT_MAX_DIGITS:
        raise ValueError(f"use at most {INPUT_MAX_DIGITS} digits")
    if exponent < -INPUT_MAX_PLACES:
        raise ValueError(f"use at most {INPUT_MAX_PLACES} decimal places")
    if exponent + len(digits) > INPUT_MAX_MAGNITUDE:
        raise ValueError(f"the value is too large (the limit is {10**INPUT_MAX_MAGNITUDE:,})")
    return result


MoneyInput = Annotated[
    Decimal,
    BeforeValidator(validate_money_input),
    PlainSerializer(to_text, return_type=str, when_used="always"),
]
