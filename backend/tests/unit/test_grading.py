"""Deterministic answer checks: each kind, refusals at definition time, and the subset JSON schema."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from foundry_eval.grading.checks import Check, run_check, run_checks, schema_error

CHECK = TypeAdapter(Check)


def check(**data):
    return CHECK.validate_python(data)


@pytest.mark.parametrize(
    ("definition", "answer", "passed"),
    [
        ({"kind": "contains", "value": "Paris"}, "The capital is paris.", True),
        ({"kind": "contains", "value": "Paris", "case_sensitive": True}, "the capital is paris.", False),
        ({"kind": "not_contains", "value": "as an AI"}, "Paris.", True),
        ({"kind": "not_contains", "value": "as an AI"}, "As an AI, I think Paris.", False),
        ({"kind": "exact", "value": "Paris"}, "  paris \n", True),
        ({"kind": "exact", "value": "New  York"}, "new york", True),
        ({"kind": "exact", "value": "Paris"}, "Paris, France", False),
        ({"kind": "regex", "pattern": r"^\d{4}-\d{2}-\d{2}$"}, "2026-10-16", True),
        ({"kind": "regex", "pattern": r"\bERROR\b", "case_sensitive": True}, "error", False),
        ({"kind": "number", "value": "42"}, "Steps: 6 x 7 = 42", True),
        ({"kind": "number", "value": "42"}, "It is 42, not 41.", False),  # the last number counts
        ({"kind": "number", "value": "42", "which": "any"}, "It is 42, not 41.", True),
        ({"kind": "number", "value": "1234.5", "tolerance": "0.1"}, "Total: 1,234.56", True),
        ({"kind": "number", "value": "3"}, "three", False),
    ],
)
def test_check_kinds(definition, answer, passed):
    assert run_check(check(**definition), answer).passed is passed


def test_number_detail_says_what_was_found():
    result = run_check(check(kind="number", value="10"), "about 12")
    assert not result.passed and result.detail == "found 12 (expected 10 ± 0)"


SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "age": {"type": "integer", "minimum": 0},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "kind": {"enum": ["a", "b"]},
    },
    "required": ["name", "age"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ('{"name": "Ada", "age": 36}', None),
        ('```json\n{"name": "Ada", "age": 36, "tags": ["x"]}\n```', None),
        ('{"name": "Ada"}', "missing 'age'"),
        ('{"name": "Ada", "age": 36.5}', "$.age should be integer"),
        ('{"name": "Ada", "age": true}', "$.age should be integer"),
        ('{"name": "Ada", "age": -1}', "$.age is below 0"),
        ('{"name": "Ada", "age": 1, "extra": 1}', "unexpected 'extra'"),
        ('{"name": "Ada", "age": 1, "tags": ["a", "b", "c"]}', "more than 2 items"),
        ('{"name": "Ada", "age": 1, "kind": "c"}', "should be one of"),
        ("Sure! Here is the JSON: {}", "not valid JSON"),
    ],
)
def test_json_schema_check(answer, error):
    result = run_check(check(kind="json_schema", schema=SCHEMA), answer)
    assert result.passed is (error is None)
    if error:
        assert error in result.detail


def test_json_schema_check_round_trips_with_its_alias():
    parsed = check(kind="json_schema", schema={"type": "object"})
    assert CHECK.dump_python(parsed, mode="json") == {"kind": "json_schema", "schema": {"type": "object"}}


@pytest.mark.parametrize(
    "definition",
    [
        {"kind": "regex", "pattern": "("},
        {"kind": "regex", "pattern": r"(a+)+$"},
        {"kind": "regex", "pattern": r"(\w*)*"},
        {"kind": "number", "value": 0.1},
        {"kind": "number", "value": "1", "tolerance": "-1"},
        {"kind": "json_schema", "schema": {"type": "object", "patternProperties": {}}},
        {"kind": "json_schema", "schema": {"type": "decimal"}},
        {"kind": "json_schema", "schema": {"additionalProperties": {"type": "string"}}},
        {"kind": "contains", "value": ""},
        {"kind": "contains", "value": "x", "unknown": 1},
        {"kind": "telepathy"},
    ],
)
def test_checks_that_cannot_run_as_written_are_refused(definition):
    with pytest.raises(ValidationError):
        check(**definition)


def test_an_answer_passes_only_when_every_check_passes():
    checks = [check(kind="contains", value="Paris"), check(kind="not_contains", value="London")]
    outcome = run_checks(checks, "Paris, not London")
    assert not outcome.passed and [r.passed for r in outcome.results] == [True, False]
    assert run_checks(checks, "Paris").passed


def test_schema_numbers_compare_exactly():
    assert schema_error(Decimal("0.1"), {"type": "number", "maximum": 0.1}) is None
    assert schema_error(Decimal("0.30000000000000004"), {"maximum": 0.3}) is not None
