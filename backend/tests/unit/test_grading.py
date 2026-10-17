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
        ({"kind": "contains", "value": "café"}, "Le café est ouvert", True),  # composed vs decomposed
        ({"kind": "number", "value": "42"}, "Steps: 6 x 7 = 42", True),
        ({"kind": "number", "value": "42"}, "It is 42, not 41.", False),  # the last number counts
        ({"kind": "number", "value": "42", "which": "any"}, "It is 42, not 41.", True),
        ({"kind": "number", "value": "1234.5", "tolerance": "0.1"}, "Total: 1,234.56", True),
        ({"kind": "number", "value": "3"}, "three", False),
        ({"kind": "number", "value": "20"}, "The range is 10-20", True),  # not -20
        ({"kind": "number", "value": "5"}, "The answer is .5", False),  # .5 is not 5
        ({"kind": "number", "value": "0.5"}, "The answer is .5", True),
        ({"kind": "number", "value": "-3"}, "It is " + chr(0x2212) + "3", True),  # Unicode minus
        ({"kind": "number", "value": "7"}, "7 then 1e1000000", True),  # implausible numbers are skipped
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
        ('{"name": "Ada", "age": NaN}', "not valid JSON"),
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
        {"kind": "regex", "pattern": "x" * 501},
        {"kind": "number", "value": "1e999999"},
        {"kind": "json_schema", "schema": {"minItems": "a"}},
        {"kind": "json_schema", "schema": {"minimum": "5"}},
        {"kind": "json_schema", "schema": {"maximum": True}},
        {"kind": "json_schema", "schema": {"enum": 5}},
        {"kind": "json_schema", "schema": {"enum": []}},
        {"kind": "json_schema", "schema": {"properties": []}},
        {"kind": "json_schema", "schema": {"type": [["a"]]}},
        {"kind": "json_schema", "schema": {"type": {"a": 1}}},
        {"kind": "json_schema", "schema": {"enum": list(range(201))}},
        {"kind": "json_schema", "schema": {"properties": {f"k{i}": {} for i in range(201)}}},
        {"kind": "json_schema", "schema": {"const": float("nan")}},
        {"kind": "json_schema", "schema": {"enum": [1, float("inf")]}},
        {
            "kind": "json_schema",
            "schema": {
                "items": {"items": {}},
                "properties": {f"p{i}": {"properties": {f"q{j}": {} for j in range(3)}} for i in range(150)},
            },
        },
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


def test_const_and_enum_keep_json_types_apart():
    assert schema_error(True, {"const": 1}) is not None
    assert schema_error(False, {"enum": [0, 1]}) is not None
    assert schema_error(1, {"enum": [True]}) is not None
    assert schema_error([1, {"a": Decimal("0.5")}], {"const": [1, {"a": 0.5}]}) is None


def test_a_check_that_cannot_be_evaluated_is_unknown_not_a_failure_or_an_error():
    huge = '{"n": ' + "9" * 5000 + "}"  # valid JSON, but past Python's integer-digits limit
    result = run_check(check(kind="json_schema", schema={"type": "object"}), huge)
    assert result.passed is None and "could not be evaluated" in result.detail
    outcome = run_checks([check(kind="contains", value="9"), check(kind="json_schema", schema={})], huge)
    assert outcome.passed is None
    failing = run_checks([check(kind="contains", value="x"), check(kind="json_schema", schema={})], huge)
    assert failing.passed is False  # a known failure wins over an unknown


def test_number_checks_are_exact_beyond_28_digits():
    result = run_check(check(kind="number", value="0", tolerance="1e40"), "10000000000000000000000000000000000000001")
    assert result.passed is False  # 10**40 + 1 is outside a tolerance of 10**40


def test_thousands_groups_must_be_complete():
    from foundry_eval.grading.checks import numbers_in

    assert numbers_in("total 1,234 items") == [Decimal(1234)]
    assert numbers_in("1,2345") == [Decimal(1), Decimal(2345)]


def test_large_schemas_stay_fast_on_large_answers():
    import json
    import time

    values = [f"value-{i}" for i in range(200)]
    schema = {"type": "array", "items": {"enum": values}}
    answer = json.dumps([values[i % 200] for i in range(20_000)])
    start = time.perf_counter()
    assert run_check(check(kind="json_schema", schema=schema), answer).passed is True
    assert time.perf_counter() - start < 1.0


def test_schema_numbers_compare_exactly():
    assert schema_error(Decimal("0.1"), {"type": "number", "maximum": 0.1}) is None
    assert schema_error(Decimal("0.30000000000000004"), {"maximum": 0.3}) is not None


# --- pattern checks ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("definition", "answer", "passed"),
    [
        ({"kind": "regex", "pattern": r"^\d{4}-\d{2}-\d{2}$"}, "2026-10-16", True),
        ({"kind": "regex", "pattern": r"\bparis\b"}, "It is Paris.", True),
        ({"kind": "regex", "pattern": r"\bparis\b", "case_sensitive": True}, "It is Paris.", False),
        ({"kind": "regex", "pattern": r"(?<=total: )\d+"}, "total: 42", True),  # lookbehind is supported
    ],
)
def test_pattern_checks(definition, answer, passed):
    assert run_check(check(**definition), answer).passed is passed


def test_a_slow_pattern_is_unknown_within_the_time_limit_and_then_skipped(monkeypatch):
    import time

    from foundry_eval.grading import checks as module

    monkeypatch.setattr(module, "_slow_patterns", {})
    slow = check(kind="regex", pattern=r"(a|aa)+$")  # catastrophic backtracking on a long run of a's
    answer = "a" * 60 + "!"
    for _ in range(module.MAX_REGEX_TIMEOUTS):
        start = time.perf_counter()
        result = run_check(slow, answer)
        assert result.passed is None and "took longer than" in result.detail
        assert time.perf_counter() - start < module.REGEX_TIMEOUT + 0.5
    start = time.perf_counter()
    skipped = run_check(slow, answer)
    assert skipped.passed is None and "is skipped" in skipped.detail
    assert time.perf_counter() - start < 0.05  # no more time spent on a known-slow pattern
    # Other patterns are unaffected.
    assert run_check(check(kind="regex", pattern="a+!"), answer).passed is True
