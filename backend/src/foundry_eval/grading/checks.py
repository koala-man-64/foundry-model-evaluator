"""Free, deterministic checks on answers: a third score source, kept apart from manual and judge scores.

A prompt can carry up to ``MAX_CHECKS`` checks. An answer *passes* when every check passes. Checks cost
nothing and run locally on the stored answer text, every time results are shown, so they always agree
with the checks frozen into the run.

Nothing is silently ignored: a check that cannot be evaluated as written is rejected when it is saved
(an invalid regular expression, a JSON-schema keyword this checker does not implement), never skipped
when it runs.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

MAX_CHECKS = 10
MAX_PATTERN = 500
MAX_SCHEMA_DEPTH = 20

# A quantified group that itself contains a quantifier, such as (a+)+ or (\w*)*: the classic shape of
# catastrophic backtracking. Patterns run in-process on model output, so they are refused up front.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[*+}](?:[^()\\]|\\.)*\)\s*(?:[*+]|\{\d*,?\d*\})")
_NUMBER = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?")
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(?P<body>.*)\n```\s*$", re.DOTALL)


class _Check(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContainsCheck(_Check):
    kind: Literal["contains"]
    value: str = Field(min_length=1, max_length=2000)
    case_sensitive: bool = False


class NotContainsCheck(_Check):
    kind: Literal["not_contains"]
    value: str = Field(min_length=1, max_length=2000)
    case_sensitive: bool = False


class ExactCheck(_Check):
    """The whole answer equals the value, ignoring leading, trailing and repeated whitespace."""

    kind: Literal["exact"]
    value: str = Field(min_length=1, max_length=20_000)
    case_sensitive: bool = False


class RegexCheck(_Check):
    kind: Literal["regex"]
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN)
    case_sensitive: bool = False

    @field_validator("pattern")
    @classmethod
    def _compiles_safely(cls, value: str) -> str:
        if _NESTED_QUANTIFIER.search(value):
            raise ValueError("nested quantifiers such as (a+)+ can take very long to run; simplify the pattern")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value


class NumberCheck(_Check):
    """A number in the answer (the last one, or any) is within ``tolerance`` of ``value``."""

    kind: Literal["number"]
    value: Decimal
    tolerance: Decimal = Field(default=Decimal(0), ge=0)
    which: Literal["last", "any"] = "last"

    @field_validator("value", "tolerance", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("give numbers as strings or integers, not floats, so they are exact")
        return value


class JsonSchemaCheck(_Check):
    """The answer (optionally inside one ``` fence) is JSON that matches the schema."""

    kind: Literal["json_schema"]
    json_schema: dict[str, Any] = Field(alias="schema")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)

    @field_validator("json_schema")
    @classmethod
    def _supported(cls, value: dict[str, Any]) -> dict[str, Any]:
        _check_schema(value, "$", 0)
        return value


Check = Annotated[
    ContainsCheck | NotContainsCheck | ExactCheck | RegexCheck | NumberCheck | JsonSchemaCheck,
    Field(discriminator="kind"),
]


class CheckResult(BaseModel):
    kind: str
    passed: bool
    detail: str


class CheckOutcome(BaseModel):
    passed: bool
    results: list[CheckResult]


# --- JSON schema subset ------------------------------------------------------------------------------
_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
}  # fmt: skip
_ANNOTATIONS = {"$schema", "title", "description", "examples", "default", "$comment"}


def _check_schema(schema: Any, path: str, depth: int) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise ValueError(f"{path}: the schema is nested too deeply")
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: a schema must be an object")
    unknown = set(schema) - _KEYWORDS - _ANNOTATIONS
    if unknown:
        raise ValueError(
            f"{path}: unsupported keyword(s) {', '.join(sorted(unknown))}; supported: {', '.join(sorted(_KEYWORDS))}"
        )
    kinds = schema.get("type")
    for kind in kinds if isinstance(kinds, list) else [kinds] if kinds is not None else []:
        if kind not in _TYPES:
            raise ValueError(f"{path}: unknown type {kind!r}")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError(f"{path}: additionalProperties must be true or false here")
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(r, str) for r in schema["required"])
    ):
        raise ValueError(f"{path}: required must be a list of property names")
    for name, sub in (schema.get("properties") or {}).items():
        _check_schema(sub, f"{path}.properties.{name}", depth + 1)
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items", depth + 1)


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    if isinstance(value, bool):
        return False
    if kind == "integer":
        return isinstance(value, int) or (isinstance(value, Decimal) and value == value.to_integral_value())
    return isinstance(value, (int, Decimal))  # number


def schema_error(value: Any, schema: dict[str, Any], path: str = "$") -> str | None:
    """The first way ``value`` fails the schema subset, or None if it matches."""
    kinds = schema.get("type")
    if kinds is not None:
        options = kinds if isinstance(kinds, list) else [kinds]
        if not any(_type_ok(value, k) for k in options):
            return f"{path} should be {' or '.join(options)}"
    if "const" in schema and value != _plain(schema["const"]):
        return f"{path} should equal {schema['const']!r}"
    if "enum" in schema and value not in [_plain(v) for v in schema["enum"]]:
        return f"{path} should be one of {schema['enum']!r}"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} is shorter than {schema['minLength']}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} is longer than {schema['maxLength']}"
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        if "minimum" in schema and value < _plain(schema["minimum"]):
            return f"{path} is below {schema['minimum']}"
        if "maximum" in schema and value > _plain(schema["maximum"]):
            return f"{path} is above {schema['maximum']}"
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} has fewer than {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} has more than {schema['maxItems']} items"
        if "items" in schema:
            for index, item in enumerate(value):
                if error := schema_error(item, schema["items"], f"{path}[{index}]"):
                    return error
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                return f"{path} is missing {name!r}"
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                return f"{path} has unexpected {', '.join(repr(e) for e in extra)}"
        for name, sub in properties.items():
            if name in value and (error := schema_error(value[name], sub, f"{path}.{name}")):
                return error
    return None


def _plain(value: Any) -> Any:
    """Schema numbers compared as Decimal, like the parsed answer."""
    if isinstance(value, float):
        return Decimal(str(value))
    return value


# --- evaluation --------------------------------------------------------------------------------------
def _fold(text: str, case_sensitive: bool) -> str:
    return text if case_sensitive else text.casefold()


def _numbers(text: str) -> list[Decimal]:
    found: list[Decimal] = []
    for match in _NUMBER.finditer(text):
        try:
            found.append(Decimal(match.group().replace(",", "")))
        except InvalidOperation:  # pragma: no cover - the pattern only matches valid numbers
            continue
    return found


def _json_answer(text: str) -> Any:
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group("body")
    return json.loads(body, parse_float=Decimal)


def run_check(check: Check, text: str) -> CheckResult:
    if isinstance(check, ContainsCheck | NotContainsCheck):
        present = _fold(check.value, check.case_sensitive) in _fold(text, check.case_sensitive)
        passed = present if isinstance(check, ContainsCheck) else not present
        verb = "contains" if present else "does not contain"
        return CheckResult(kind=check.kind, passed=passed, detail=f"answer {verb} {check.value!r}")
    if isinstance(check, ExactCheck):
        normalized = " ".join(text.split())
        passed = _fold(normalized, check.case_sensitive) == _fold(" ".join(check.value.split()), check.case_sensitive)
        return CheckResult(kind=check.kind, passed=passed, detail="matches exactly" if passed else "differs")
    if isinstance(check, RegexCheck):
        flags = 0 if check.case_sensitive else re.IGNORECASE
        passed = re.search(check.pattern, text, flags) is not None
        return CheckResult(kind=check.kind, passed=passed, detail="pattern found" if passed else "pattern not found")
    if isinstance(check, NumberCheck):
        numbers = _numbers(text)
        if not numbers:
            return CheckResult(kind=check.kind, passed=False, detail="no number in the answer")
        pool = numbers[-1:] if check.which == "last" else numbers
        hit = next((n for n in pool if abs(n - check.value) <= check.tolerance), None)
        shown = hit if hit is not None else pool[-1]
        detail = f"found {shown} (expected {check.value} ± {check.tolerance})"
        return CheckResult(kind=check.kind, passed=hit is not None, detail=detail)
    try:
        value = _json_answer(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        return CheckResult(kind=check.kind, passed=False, detail=f"not valid JSON: {str(exc)[:200]}")
    error = schema_error(value, check.json_schema)
    return CheckResult(kind=check.kind, passed=error is None, detail=error or "matches the schema")


CHECKS = TypeAdapter(list[Check])


def checks_json(checks: list[Check]) -> str:
    return CHECKS.dump_json(checks).decode()


def load_checks(raw: str | None) -> list[Check]:
    return CHECKS.validate_json(raw or "[]")


def run_checks(checks: list[Check], text: str) -> CheckOutcome:
    results = [run_check(check, text) for check in checks]
    return CheckOutcome(passed=all(r.passed for r in results), results=results)
