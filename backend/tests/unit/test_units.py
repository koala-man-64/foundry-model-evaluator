from __future__ import annotations

import pytest

from foundry_eval.pricing.units import UnitError, parse_token_unit, unit_in_name, unit_label


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("1K", 1_000),
        ("1M", 1_000_000),
        ("1K Tokens", 1_000),
        ("1M Tokens", 1_000_000),
        ("10K", 10_000),
        ("1m tokens", 1_000_000),
        ("0.5M", 500_000),
    ],
)
def test_supported_units(text, tokens):
    assert parse_token_unit(text) == tokens


@pytest.mark.parametrize("text", ["1 Hour", "1", "1/Day", "", "Hours", "1K Images", "0.0001K", "0K"])
def test_unsupported_units_are_rejected(text):
    with pytest.raises(UnitError):
        parse_token_unit(text)


def test_unit_in_name():
    assert unit_in_name("gpt-realtime-2 Text Inp glbl 1M Tokens") == 1_000_000
    assert unit_in_name("MM3.5 Inp glbl 1K Tokens") == 1_000
    assert unit_in_name("5.6 terra ShortCo Inp Std Gl") is None
    assert unit_in_name("x 0.0005K Tokens") is None


def test_unit_label():
    assert unit_label(1_000_000) == "1M tokens"
    assert unit_label(1_000) == "1K tokens"
    assert unit_label(250) == "250 tokens"
