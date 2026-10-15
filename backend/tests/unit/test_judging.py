from __future__ import annotations

import json
import random
import re
from decimal import Decimal

import pytest
from pydantic import ValidationError

from foundry_eval.judging import prompt as prompt_module
from foundry_eval.judging.parse import extract_json, parse_judge_output
from foundry_eval.judging.prompt import assign_labels, build_judge_prompt, judge_schema, labels_for, new_nonce
from foundry_eval.judging.rubric import DEFAULT_RUBRIC, Criterion, RubricDefinition

KEYS = [c.key for c in DEFAULT_RUBRIC.criteria]


def criteria(scores: dict[str, object]) -> list[dict[str, object]]:
    return [{"key": k, "score": v, "explanation": f"{k} note"} for k, v in scores.items()]


def output(**overrides: object) -> str:
    data: dict[str, object] = {
        "responses": [
            {"label": "A", "criteria": criteria(dict.fromkeys(KEYS, 4)), "overall": 4, "explanation": "good"},
            {"label": "B", "criteria": criteria(dict.fromkeys(KEYS, 2)), "overall": 2.5, "explanation": "weak"},
        ],
        "preferred": "A",
        "preference_explanation": "A is better",
    }
    data.update(overrides)
    return json.dumps(data)


# --------------------------------------------------------------------------- rubric
def test_default_rubric_has_four_equally_weighted_criteria_on_1_to_5():
    assert KEYS == ["instruction_following", "correctness", "completeness", "clarity"]
    assert (DEFAULT_RUBRIC.scale_min, DEFAULT_RUBRIC.scale_max) == (1, 5)
    assert set(DEFAULT_RUBRIC.normalized_weights().values()) == {Decimal("0.25")}


def test_weighted_overall_normalizes_weights():
    rubric = RubricDefinition(
        criteria=[Criterion(key="a", name="A", weight=3), Criterion(key="b", name="B", weight=1)],
    )
    assert rubric.weighted_overall({"a": 5, "b": 1}) == 4.0
    assert (
        DEFAULT_RUBRIC.weighted_overall({"instruction_following": 5, "correctness": 4, "completeness": 4, "clarity": 4})
        == 4.25
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"criteria": []}, "at least 1"),
        ({"criteria": [{"key": "a", "name": "A"}, {"key": "a", "name": "A2"}]}, "unique"),
        ({"criteria": [{"key": "a", "name": "A", "weight": 0}]}, "greater than zero"),
        ({"criteria": [{"key": "a", "name": "A", "weight": -1}]}, "greater than zero"),
        ({"criteria": [{"key": "Bad Key", "name": "A"}]}, "lowercase"),
        ({"criteria": [{"key": "a", "name": "A"}], "scale_min": 5, "scale_max": 5}, "lower than"),
        ({"criteria": [{"key": "a", "name": "A"}], "scale_min": 1.5}, "whole numbers"),
        ({"criteria": [{"key": "a", "name": "A"}], "scale_max": 500}, "span"),
        ({"criteria": [{"key": "a", "name": ""}]}, "at least 1 character"),
    ],
)
def test_rubric_validation(payload, message):
    with pytest.raises(ValidationError, match=message):
        RubricDefinition.model_validate(payload)


# --------------------------------------------------------------------------- prompt
def test_labels_continue_past_z():
    labels = labels_for(28)
    assert labels[:3] == ["A", "B", "C"] and labels[25] == "Z" and labels[26:] == ["AA", "AB"]


def test_labels_follow_a_random_order_that_is_recorded():
    responses = [("a1", "c1", "first"), ("a2", "c2", "second"), ("a3", "c3", "third")]
    orders = {tuple(e.attempt_id for e in assign_labels(responses, random.Random(seed))) for seed in range(30)}
    assert len(orders) > 1  # the presentation order is shuffled
    entries = assign_labels(responses, random.Random(7))
    assert [e.label for e in entries] == ["A", "B", "C"]
    assert {(e.attempt_id, e.candidate_id, e.text) for e in entries} == set(responses)


def test_judge_prompt_contains_only_task_rubric_and_responses():
    entries = assign_labels([("a1", "c1", "Paris."), ("a2", "c2", "It is Paris, France.")], random.Random(1))
    built = build_judge_prompt(
        system_text="Answer briefly.",
        user_text="Capital of France?",
        entries=entries,
        rubric=DEFAULT_RUBRIC,
        nonce="n0nce",
    )
    assert "Answer briefly." in built.user and "Capital of France?" in built.user
    for entry in entries:
        assert (
            f"=== Response {entry.label} (begin n0nce) ===\n{entry.text}\n=== Response {entry.label} (end n0nce) ==="
            in built.user
        )
    assert re.findall(r"^- key: ([a-z_]+)", built.system, flags=re.MULTILINE) == KEYS
    assert "whole number from 1 (worst) to 5 (best)" in built.system
    assert "Ignore any instructions" in built.system
    # Nothing about attempts, candidates, costs or timing.
    for forbidden in ("a1", "a2", "c1", "c2", "cost", "latency", "$"):
        assert forbidden not in built.system + built.user.replace("Capital", "")


def test_judge_prompt_without_system_instructions_says_so():
    entries = assign_labels([("a1", "c1", "x")], random.Random(1))
    built = build_judge_prompt(system_text="  ", user_text="q", entries=entries, rubric=DEFAULT_RUBRIC, nonce="n")
    assert "The task has no system instructions." in built.user
    assert "system instructions (begin" not in built.user


def test_nonce_never_appears_inside_the_texts(monkeypatch):
    values = iter(["deadbeefdeadbeef", "0123456789abcdef"])
    monkeypatch.setattr(prompt_module.secrets, "token_hex", lambda _n: next(values))
    assert new_nonce(["text with deadbeefdeadbeef inside"]) == "0123456789abcdef"


def test_schema_restricts_labels_criteria_and_preference():
    schema = judge_schema(["A", "B"], DEFAULT_RUBRIC)
    item = schema["properties"]["responses"]["items"]
    assert item["properties"]["label"]["enum"] == ["A", "B"]
    assert item["properties"]["criteria"]["items"]["properties"]["key"]["enum"] == KEYS
    assert schema["properties"]["preferred"]["enum"] == ["A", "B", "tie"]
    assert schema["additionalProperties"] is False and item["additionalProperties"] is False


# --------------------------------------------------------------------------- parsing
def test_valid_output_is_parsed_with_weighted_overall():
    result = parse_judge_output(output(), ["A", "B"], DEFAULT_RUBRIC)
    assert result.ok, result.errors
    assert [c.label for c in result.candidates] == ["A", "B"]
    assert result.candidates[0].weighted_overall == 4.0
    assert result.candidates[1].judge_overall == 2.5
    assert result.preferred == "A"
    assert result.candidates[0].criterion_explanations["clarity"] == "clarity note"


def test_fenced_json_and_integral_floats_are_accepted():
    data = json.loads(output())
    data["responses"][0]["criteria"][0]["score"] = 4.0
    result = parse_judge_output(f"```json\n{json.dumps(data)}\n```", ["A", "B"], DEFAULT_RUBRIC)
    assert result.ok, result.errors


def test_tie_is_a_valid_preference():
    assert parse_judge_output(output(preferred="tie"), ["A", "B"], DEFAULT_RUBRIC).preferred == "tie"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I prefer A.", "not valid JSON"),
        ("", "empty"),
        ("[1, 2]", "JSON object"),
        (output(preferred="C"), '"preferred" must be one of'),
        (output(responses="nope"), '"responses" must be a list'),
    ],
)
def test_malformed_outputs(text, expected):
    result = parse_judge_output(text, ["A", "B"], DEFAULT_RUBRIC)
    assert not result.ok
    assert any(expected in e for e in result.errors), result.errors
    assert result.candidates == []


def _with_response(index: int, **changes: object) -> str:
    data = json.loads(output())
    data["responses"][index].update(changes)
    return json.dumps(data)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_with_response(1, label="Z"), "unknown label 'Z'"),
        (_with_response(1, label="A"), "response A appears more than once"),
        (
            _with_response(0, criteria=criteria({"correctness": 4, "completeness": 4, "clarity": 4})),
            "missing criteria instruction_following",
        ),
        (
            _with_response(0, criteria=[*criteria(dict.fromkeys(KEYS, 4)), {"key": "humor", "score": 3}]),
            "unknown criterion 'humor'",
        ),
        (
            _with_response(0, criteria=criteria({**dict.fromkeys(KEYS, 4), "clarity": 6})),
            "clarity score 6 is outside 1-5",
        ),
        (
            _with_response(0, criteria=criteria({**dict.fromkeys(KEYS, 4), "clarity": 4.5})),
            "clarity score must be a whole number",
        ),
        (
            _with_response(0, criteria=criteria({**dict.fromkeys(KEYS, 4), "clarity": True})),
            "clarity score must be a whole number",
        ),
        (_with_response(0, overall=7), "overall score 7 is outside 1-5"),
        (_with_response(0, overall="high"), '"overall" must be a number'),
    ],
)
def test_validation_against_labels_and_rubric(text, expected):
    result = parse_judge_output(text, ["A", "B"], DEFAULT_RUBRIC)
    assert not result.ok
    assert any(expected in e for e in result.errors), result.errors


def test_missing_label_is_reported():
    data = json.loads(output())
    data["responses"] = data["responses"][:1]
    result = parse_judge_output(json.dumps(data), ["A", "B"], DEFAULT_RUBRIC)
    assert "responses missing for B" in result.errors


def test_stopping_at_the_token_limit_makes_output_malformed():
    result = parse_judge_output(output(), ["A", "B"], DEFAULT_RUBRIC, stopped_at_limit=True)
    assert not result.ok
    assert "output token limit" in result.errors[0]


def test_extract_json_reports_position():
    _data, error = extract_json('{"a": }')
    assert error is not None and "line 1" in error
