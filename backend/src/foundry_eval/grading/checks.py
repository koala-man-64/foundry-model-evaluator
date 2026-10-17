"""Free, deterministic checks on answers: a third score source, kept apart from manual and judge scores.

A prompt can carry up to ``MAX_CHECKS`` checks. An answer *passes* when every check passes. Checks cost
nothing and run locally on the stored answer text, every time results are shown, so they always agree
with the checks frozen into the run.

Nothing is silently ignored or guessed:

- A check that cannot run as written is refused when it is saved (a JSON-schema keyword this checker does
  not implement, a keyword value of the wrong type, a number given as a float).
- A check that still cannot be evaluated on a particular answer reports ``passed = None``, "could not be
  evaluated". That is unknown, not a fail and not a pass, and it never raises.

Pattern checks use the ``regex`` package, not Python's ``re``: ``re`` has no time limit and holds the
interpreter lock while it searches, so one slow pattern on one answer would freeze every read of a run.
Three limits keep pattern checks bounded:

- When a check is saved, its pattern is compiled once in a separate worker process, because compiling
  holds the lock and a short pattern with nested repeats can take minutes and gigabytes. A pattern that
  does not compile within ``COMPILE_LIMIT`` is refused. Stored checks are not compiled again on load.
- Each search has a time limit (``REGEX_TIMEOUT``) and releases the lock while it runs. One that runs out
  of time reports "could not check" (unknown) for that answer only.
- Each view shares a ``RegexBudget``. Once it is spent, the remaining pattern checks report "not checked
  yet" (unknown). Results are cached per answer and pattern, so the next view continues where this one
  stopped, and a slow answer never makes the pattern unknown on other answers.
"""

from __future__ import annotations

import atexit
import functools
import hashlib
import json
import math
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from decimal import Decimal, DecimalException, localcontext
from typing import Annotated, Any, Literal

import regex
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationInfo, field_validator, model_validator

MAX_CHECKS = 10
MAX_PATTERN = 500
COMPILE_LIMIT = 0.1  # seconds to compile a new pattern, measured in the worker
COMPILE_WAIT = 1.0  # seconds to wait for the worker's answer before stopping it
WORKER_START_WAIT = 30.0
REGEX_TIMEOUT = 0.25  # seconds per search
REGEX_BUDGET = 2.0  # seconds of pattern checks per view
EXPORT_REGEX_BUDGET = 30.0  # an export is one explicit download, so it may take longer than a page view
MAX_CACHED_RESULTS = 20_000
MAX_SCHEMA_DEPTH = 20
# Size limits keep the work per answer bounded: a schema is checked against every item of an answer.
MAX_SCHEMA_NODES = 500
MAX_ENUM = 200
MAX_PROPERTIES = 200
# Numbers whose exponent is further from zero than this are not read: they cannot be a plausible answer
# and exact arithmetic on them is expensive or overflows.
MAX_EXPONENT = 1000

# A sign belongs to a number only when it does not follow a word character or a dot, so "10-20" reads
# as 10 and 20, not 10 and -20. Decimals may start with a dot (".5"). U+2212 is normalised to "-".
_NUMBER = re.compile(r"(?<![\w.])[-+]?(?:(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?")
_UNICODE_MINUS = chr(0x2212)
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
    """The pattern is found in the answer (``regex`` syntax, searched with a time limit)."""

    kind: Literal["regex"]
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN)
    case_sensitive: bool = False

    @property
    def flags(self) -> int:
        return 0 if self.case_sensitive else int(regex.IGNORECASE)

    @model_validator(mode="after")
    def _usable(self, info: ValidationInfo) -> RegexCheck:
        if (info.context or {}).get("stored"):
            _vetter.accept(self.pattern, self.flags)  # vetted when it was saved
        else:
            _vetter.vet(self.pattern, self.flags)
        return self


class NumberCheck(_Check):
    """A number in the answer (the last one, or any) is within ``tolerance`` of ``value``."""

    kind: Literal["number"]
    value: Decimal
    tolerance: Decimal = Field(default=Decimal(0), ge=0)
    which: Literal["last", "any"] = "last"

    @field_validator("value", "tolerance", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        if isinstance(value, float | bool):
            raise ValueError("give numbers as strings or integers, not floats, so they are exact")
        return value

    @field_validator("value", "tolerance")
    @classmethod
    def _plausible(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or abs(value.adjusted()) > MAX_EXPONENT:
            raise ValueError("the number must be finite and of a plausible size")
        return value


class JsonSchemaCheck(_Check):
    """The answer (optionally inside one ``` fence) is JSON that matches the schema."""

    kind: Literal["json_schema"]
    json_schema: dict[str, Any] = Field(alias="schema")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)

    @field_validator("json_schema")
    @classmethod
    def _supported(cls, value: dict[str, Any]) -> dict[str, Any]:
        nodes = [0]
        _check_schema(value, "$", 0, nodes)
        return value


Check = Annotated[
    ContainsCheck | NotContainsCheck | ExactCheck | RegexCheck | NumberCheck | JsonSchemaCheck,
    Field(discriminator="kind"),
]


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)  # pattern results are cached and shared between views

    kind: str
    # None: the check could not be evaluated on this answer (unknown, neither pass nor fail).
    passed: bool | None
    detail: str


class CheckOutcome(BaseModel):
    # False when any check failed; None when none failed but some could not be evaluated; else True.
    passed: bool | None
    results: list[CheckResult]


# --- JSON schema subset ------------------------------------------------------------------------------
_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_COUNTS = {"minItems", "maxItems", "minLength", "maxLength"}
_BOUNDS = {"minimum", "maximum"}
_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const"} | _COUNTS | _BOUNDS
_ANNOTATIONS = {"$schema", "title", "description", "examples", "default", "$comment"}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _finite_literal(value: Any, path: str) -> None:
    """const and enum values: plain JSON, with no NaN or Infinity (they would be stored as null)."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path}: numbers must be finite")
    if isinstance(value, list):
        for item in value:
            _finite_literal(item, path)
    elif isinstance(value, dict):
        for item in value.values():
            _finite_literal(item, path)


def _check_schema(schema: Any, path: str, depth: int, nodes: list[int]) -> None:
    """Refuse at save time anything the validator below could not apply exactly as written."""
    if depth > MAX_SCHEMA_DEPTH:
        raise ValueError(f"{path}: the schema is nested too deeply")
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: a schema must be an object")
    nodes[0] += 1
    if nodes[0] > MAX_SCHEMA_NODES:
        raise ValueError(f"the schema has more than {MAX_SCHEMA_NODES} sub-schemas")
    unknown = set(schema) - _KEYWORDS - _ANNOTATIONS
    if unknown:
        supported = ", ".join(sorted(_KEYWORDS))
        raise ValueError(f"{path}: unsupported keyword(s) {', '.join(sorted(unknown))}; supported: {supported}")
    if "type" in schema:
        kinds = schema["type"]
        options = kinds if isinstance(kinds, list) else [kinds]
        if not options or not all(isinstance(k, str) and k in _TYPES for k in options):
            raise ValueError(f"{path}: type must be one of {', '.join(sorted(_TYPES))}, or a list of them")
    for name in sorted(_COUNTS & set(schema)):
        if not isinstance(schema[name], int) or isinstance(schema[name], bool) or schema[name] < 0:
            raise ValueError(f"{path}: {name} must be a whole number of at least 0")
    for name in sorted(_BOUNDS & set(schema)):
        value = schema[name]
        if not _is_number(value) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError(f"{path}: {name} must be a finite number")
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not schema["enum"]:
            raise ValueError(f"{path}: enum must be a non-empty list")
        if len(schema["enum"]) > MAX_ENUM:
            raise ValueError(f"{path}: enum has more than {MAX_ENUM} values")
        _finite_literal(schema["enum"], f"{path}.enum")
    if "const" in schema:
        _finite_literal(schema["const"], f"{path}.const")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError(f"{path}: additionalProperties must be true or false here")
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(r, str) for r in schema["required"])
    ):
        raise ValueError(f"{path}: required must be a list of property names")
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise ValueError(f"{path}: properties must be an object")
        if len(schema["properties"]) > MAX_PROPERTIES:
            raise ValueError(f"{path}: properties has more than {MAX_PROPERTIES} entries")
        for name, sub in schema["properties"].items():
            _check_schema(sub, f"{path}.properties.{name}", depth + 1, nodes)
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items", depth + 1, nodes)


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
    if kind == "integer":
        return _is_number(value) and Decimal(value) == Decimal(value).to_integral_value()
    return _is_number(value)  # number


def _plain(value: Any) -> Any:
    """Schema literals compared like the parsed answer: floats as Decimal, at every level."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def canonical(value: Any) -> Any:
    """A hashable form in which equal JSON values are equal, and true is not 1 nor false 0."""
    if isinstance(value, bool):
        return ("bool", value)
    if _is_number(value):
        number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
        return ("num", number.normalize() if number else Decimal(0))
    if isinstance(value, list):
        return ("list", tuple(canonical(v) for v in value))
    if isinstance(value, dict):
        return ("dict", tuple(sorted((k, canonical(v)) for k, v in value.items())))
    return (type(value).__name__, value)


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality: true is not 1 and false is not 0 (Python's == says they are)."""
    return bool(canonical(a) == canonical(b))


class _Prepared:
    """Per-evaluation lookups for a schema node, so a large answer does not redo them for every item."""

    def __init__(self) -> None:
        self._enums: dict[int, frozenset[Any]] = {}
        self._consts: dict[int, Any] = {}
        self._properties: dict[int, frozenset[str]] = {}

    def enum(self, schema: dict[str, Any]) -> frozenset[Any]:
        key = id(schema)
        if key not in self._enums:
            self._enums[key] = frozenset(canonical(v) for v in schema["enum"])
        return self._enums[key]

    def const(self, schema: dict[str, Any]) -> Any:
        key = id(schema)
        if key not in self._consts:
            self._consts[key] = canonical(schema["const"])
        return self._consts[key]

    def properties(self, schema: dict[str, Any]) -> frozenset[str]:
        key = id(schema)
        if key not in self._properties:
            self._properties[key] = frozenset(schema.get("properties") or {})
        return self._properties[key]


def schema_error(value: Any, schema: dict[str, Any], path: str = "$", prepared: _Prepared | None = None) -> str | None:
    """The first way ``value`` fails the schema subset, or None if it matches."""
    prepared = prepared or _Prepared()
    kinds = schema.get("type")
    if kinds is not None:
        options = kinds if isinstance(kinds, list) else [kinds]
        if not any(_type_ok(value, k) for k in options):
            return f"{path} should be {' or '.join(options)}"
    if "const" in schema and canonical(value) != prepared.const(schema):
        return f"{path} should equal {schema['const']!r}"
    if "enum" in schema and canonical(value) not in prepared.enum(schema):
        return f"{path} should be one of {schema['enum']!r}"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} is shorter than {schema['minLength']}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} is longer than {schema['maxLength']}"
    if _is_number(value):
        if "minimum" in schema and Decimal(value) < _plain(schema["minimum"]):
            return f"{path} is below {schema['minimum']}"
        if "maximum" in schema and Decimal(value) > _plain(schema["maximum"]):
            return f"{path} is above {schema['maximum']}"
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} has fewer than {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} has more than {schema['maxItems']} items"
        if "items" in schema:
            for index, item in enumerate(value):
                if error := schema_error(item, schema["items"], f"{path}[{index}]", prepared):
                    return error
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                return f"{path} is missing {name!r}"
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - prepared.properties(schema))
            if extra:
                return f"{path} has unexpected {', '.join(repr(e) for e in extra)}"
        for name in value.keys() & properties.keys():
            if error := schema_error(value[name], properties[name], f"{path}.{name}", prepared):
                return error
    return None


# --- evaluation --------------------------------------------------------------------------------------
def _fold(text: str, case_sensitive: bool) -> str:
    """Unicode-normalised (so composed and decomposed accents compare equal), case-folded unless asked."""
    normal = unicodedata.normalize("NFC", text)
    return normal if case_sensitive else normal.casefold()


def numbers_in(text: str) -> list[Decimal]:
    """The numbers in a text, in order. Implausibly large or small ones are skipped, not misread."""
    found: list[Decimal] = []
    for match in _NUMBER.finditer(text.replace(_UNICODE_MINUS, "-")):
        try:
            number = Decimal(match.group().replace(",", ""))
        except DecimalException:  # pragma: no cover - the pattern only matches valid numbers
            continue
        if abs(number.adjusted()) <= MAX_EXPONENT:
            found.append(number)
    return found


class _NotJson(ValueError):
    """NaN, Infinity and -Infinity: accepted by Python's parser, but not JSON."""


def _no_constants(name: str) -> Any:
    raise _NotJson(f"{name} is not valid JSON")


def _json_answer(text: str) -> Any:
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group("body")
    return json.loads(body, parse_float=Decimal, parse_constant=_no_constants)


# --- pattern checks ----------------------------------------------------------------------------------
_WORKER_CODE = """
import json, sys, time
import regex
print("ready", flush=True)
for line in sys.stdin:
    pattern, flags = json.loads(line)
    start = time.perf_counter()
    try:
        regex.compile(pattern, flags)
    except regex.error as exc:
        answer = {"error": f"invalid regular expression: {exc}"}
    except Exception as exc:
        answer = {"error": f"the pattern cannot be compiled ({type(exc).__name__})"}
    else:
        answer = {"seconds": time.perf_counter() - start}
    print(json.dumps(answer), flush=True)
"""


class _Worker:
    """A child process that compiles patterns, so a pattern that is slow to compile cannot freeze the app."""

    def __init__(self) -> None:
        try:
            # A fixed command: this interpreter and the code above. Patterns go over stdin as JSON.
            self.process = subprocess.Popen(  # noqa: S603
                [sys.executable, "-I", "-c", _WORKER_CODE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ValueError(f"pattern checks are unavailable: the pattern worker did not start ({exc})") from exc
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, name="regex-compile-reader", daemon=True).start()
        if self.answer(WORKER_START_WAIT) != "ready":
            self.stop()
            raise ValueError("pattern checks are unavailable: the pattern worker did not start")

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line.strip())
        self._lines.put(None)

    def answer(self, wait: float) -> str | None:
        try:
            return self._lines.get(timeout=wait)
        except queue.Empty:
            return None

    def ask(self, pattern: str, flags: int) -> str | None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps([pattern, flags]) + "\n")
            self.process.stdin.flush()
        except OSError:
            return None
        return self.answer(COMPILE_WAIT)

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        self.process.kill()
        self.process.wait()


class _Vetter:
    """Refuses new patterns that are invalid or slow to compile. One pattern is vetted at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._worker: _Worker | None = None
        # Patterns vetted or loaded from storage in this process. It grows only with distinct patterns
        # that were saved or read, each at most MAX_PATTERN characters.
        self._accepted: set[tuple[str, int]] = set()

    def accept(self, pattern: str, flags: int) -> None:
        with self._lock:
            self._accepted.add((pattern, flags))

    def vet(self, pattern: str, flags: int) -> None:
        with self._lock:
            if (pattern, flags) in self._accepted:
                return
            if self._worker is None or not self._worker.alive():
                self._worker = _Worker()
            raw = self._worker.ask(pattern, flags)
            if raw is None:  # no answer in time, or the worker died: either way it cannot be used
                self._worker.stop()
                self._worker = None
                raise ValueError(f"the pattern takes too long to compile (the limit is {COMPILE_LIMIT} s)")
            answer = json.loads(raw)
            if "error" in answer:
                raise ValueError(answer["error"])
            if answer["seconds"] > COMPILE_LIMIT:
                took = f"{answer['seconds']:.2f} s"
                raise ValueError(f"the pattern takes too long to compile ({took}; the limit is {COMPILE_LIMIT} s)")
            self._accepted.add((pattern, flags))

    def close(self) -> None:
        with self._lock:
            if self._worker is not None:
                self._worker.stop()
                self._worker = None


_vetter = _Vetter()
atexit.register(_vetter.close)


class RegexBudget:
    """Seconds of pattern checks that one view may spend, shared by every answer it shows."""

    def __init__(self, seconds: float = REGEX_BUDGET) -> None:
        self.left = seconds


@functools.lru_cache(maxsize=256)
def _compiled(pattern: str, flags: int) -> regex.Pattern[str]:
    return regex.compile(pattern, flags)


class _Results:
    """Pattern results by (pattern, flags, answer), the most recently used kept."""

    def __init__(self, size: int) -> None:
        self._items: OrderedDict[bytes, CheckResult] = OrderedDict()
        self._lock = threading.Lock()
        self._size = size

    def get(self, key: bytes) -> CheckResult | None:
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found

    def put(self, key: bytes, result: CheckResult) -> None:
        with self._lock:
            self._items[key] = result
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_results = _Results(MAX_CACHED_RESULTS)


def _result_key(check: RegexCheck, text: str) -> bytes:
    pattern = check.pattern.encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(digest_size=16)
    digest.update(check.flags.to_bytes(4, "little") + len(pattern).to_bytes(4, "little") + pattern)
    digest.update(text.encode("utf-8", "surrogatepass"))
    return digest.digest()


NOT_CHECKED_YET = "not checked yet: this view ran out of time for pattern checks; reload to continue"


def _search(check: RegexCheck, text: str, budget: RegexBudget) -> CheckResult:
    key = _result_key(check, text)
    cached = _results.get(key)
    if cached is not None:
        return cached
    if budget.left <= 0:
        return CheckResult(kind=check.kind, passed=None, detail=NOT_CHECKED_YET)  # not cached: a later view runs it
    start = time.perf_counter()
    try:
        found = _compiled(check.pattern, check.flags).search(text, timeout=REGEX_TIMEOUT, concurrent=True)
    except TimeoutError:
        detail = f"could not check: the pattern took longer than {REGEX_TIMEOUT} s on this answer"
        result = CheckResult(kind=check.kind, passed=None, detail=detail)
    else:
        detail = "pattern found" if found else "pattern not found"
        result = CheckResult(kind=check.kind, passed=found is not None, detail=detail)
    finally:
        budget.left -= time.perf_counter() - start
    _results.put(key, result)
    return result


def _evaluate(check: Check, text: str, budget: RegexBudget) -> CheckResult:
    if isinstance(check, RegexCheck):
        return _search(check, text, budget)
    if isinstance(check, ContainsCheck | NotContainsCheck):
        present = _fold(check.value, check.case_sensitive) in _fold(text, check.case_sensitive)
        passed = present if isinstance(check, ContainsCheck) else not present
        verb = "contains" if present else "does not contain"
        return CheckResult(kind=check.kind, passed=passed, detail=f"answer {verb} {check.value!r}")
    if isinstance(check, ExactCheck):
        normalized = " ".join(text.split())
        passed = _fold(normalized, check.case_sensitive) == _fold(" ".join(check.value.split()), check.case_sensitive)
        return CheckResult(kind=check.kind, passed=passed, detail="matches exactly" if passed else "differs")
    if isinstance(check, NumberCheck):
        numbers = numbers_in(text)
        if not numbers:
            return CheckResult(kind=check.kind, passed=False, detail="no number in the answer")
        pool = numbers[-1:] if check.which == "last" else numbers
        with localcontext() as ctx:
            # Exact: exponents are within +-MAX_EXPONENT, so this precision never rounds the difference.
            ctx.prec = 2 * MAX_EXPONENT + 50
            hit = next((n for n in pool if abs(n - check.value) <= check.tolerance), None)
        shown = hit if hit is not None else pool[-1]
        detail = f"found {shown} (expected {check.value} ± {check.tolerance})"
        return CheckResult(kind=check.kind, passed=hit is not None, detail=detail)
    try:
        value = _json_answer(text)
    except (json.JSONDecodeError, _NotJson) as exc:
        return CheckResult(kind=check.kind, passed=False, detail=f"not valid JSON: {str(exc)[:200]}")
    # Valid JSON the parser still refuses (an integer of thousands of digits) is "could not be evaluated".
    error = schema_error(value, check.json_schema)
    return CheckResult(kind=check.kind, passed=error is None, detail=error or "matches the schema")


def run_check(check: Check, text: str, budget: RegexBudget | None = None) -> CheckResult:
    """Evaluate one check. Never raises: an answer it cannot be evaluated on gives ``passed = None``."""
    try:
        return _evaluate(check, text, budget or RegexBudget())
    except (RecursionError, ArithmeticError, ValueError, TypeError, regex.error) as exc:
        detail = f"could not be evaluated on this answer ({type(exc).__name__})"
        return CheckResult(kind=check.kind, passed=None, detail=detail)


CHECKS = TypeAdapter(list[Check])


def checks_json(checks: list[Check]) -> str:
    return CHECKS.dump_json(checks).decode()


def load_checks(raw: str | None) -> list[Check]:
    """Checks as stored with a prompt. They were vetted when saved, so patterns are not compiled again here."""
    return CHECKS.validate_json(raw or "[]", context={"stored": True})


def run_checks(checks: list[Check], text: str, *, budget: RegexBudget) -> CheckOutcome:
    """Evaluate an answer's checks. Pass one ``budget`` for every answer of a view."""
    results = [run_check(check, text, budget) for check in checks]
    if any(r.passed is False for r in results):
        passed: bool | None = False
    elif any(r.passed is None for r in results):
        passed = None
    else:
        passed = True
    return CheckOutcome(passed=passed, results=results)
