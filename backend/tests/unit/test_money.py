from __future__ import annotations

from decimal import Decimal

import pytest

from foundry_eval.money import add, div, mul, to_decimal, to_text, validate_money_input


def test_to_decimal_accepts_strings_ints_decimals():
    assert to_decimal("0.0025") == Decimal("0.0025")
    assert to_decimal(3) == Decimal(3)
    assert to_decimal(Decimal("1.5")) == Decimal("1.5")


@pytest.mark.parametrize("bad", [0.1, True, None, [1]])
def test_to_decimal_rejects_floats_and_others(bad):
    with pytest.raises(TypeError):
        to_decimal(bad)


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity"])
def test_to_decimal_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        to_decimal(bad)


def test_to_text_is_plain_notation():
    assert to_text(Decimal("1E-7")) == "0.0000001"
    assert to_text(Decimal("2.50000")) == "2.5"
    assert to_text(Decimal("100")) == "100"
    assert to_text(Decimal("-0.0")) == "0"
    assert to_text(Decimal("0E-10")) == "0"


def test_arithmetic_is_exact():
    assert mul(Decimal("1160"), Decimal("1.25")) == Decimal("1450.00")
    assert div(Decimal("1"), Decimal("3")).as_tuple().exponent < -30
    assert add(Decimal("0.1"), Decimal("0.2")) == Decimal("0.3")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("1E-50000000", "decimal places"),
        ("1E-999999999999", "decimal places"),
        ("1E+30000000", "too large"),
        ("0E+30000000", "too large"),
        ("1000000000000000", "too large"),
        ("0.000000000000000000001", "decimal places"),
        ("1." + "1" * 30, "digits"),
    ],
)
def test_entered_amounts_are_bounded(value, message):
    with pytest.raises(ValueError, match=message):
        validate_money_input(value)


@pytest.mark.parametrize("value", ["0", "0.05", "2.75", "0.0000025", "100", "999999999999999", "1.5E-7", 3])
def test_ordinary_amounts_are_accepted(value):
    assert validate_money_input(value) == Decimal(str(value))
