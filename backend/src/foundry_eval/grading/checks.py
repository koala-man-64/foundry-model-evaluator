"""Free, deterministic checks on answers: a third score source, kept apart from manual and judge scores.

A prompt can carry up to ``MAX_CHECKS`` checks. An answer *passes* when every check passes. Checks cost
nothing and run locally on the stored answer text, every time results are shown, so they always agree
with the checks frozen into the run.

Nothing is silently ignored or guessed:

- A check that cannot run as written is refused when it is saved (a JSON-schema keyword this checker does
  not implement, a keyword value of the wrong type, a number given as a float).
- A check that still cannot be evaluated on a particular answer reports ``passed = None``, "could not be
  evaluated". That is unknown, not a fail and not a pass, and it never raises.

Pattern checks use the ``regex`` package, which can stop a search after a time limit. Patterns and
answers are untrusted (a pattern may come from an imported prompt set, an answer from any model), and
compiling or searching a hostile pattern can take minutes, gigabytes or the whole native stack. So this
process never compiles or searches a pattern itself; one worker process does both:

- When a check is saved, ``pattern_problems`` compiles its pattern once in the worker, on a thread with a
  sixteenth of the stack searches get. A pattern that is invalid, needs more than ``WORKER_MEMORY`` or
  takes longer than ``COMPILE_LIMIT`` is refused.
- Each search has a time limit (``REGEX_TIMEOUT``). A worker that stops answering, overflows its stack or
  reaches its memory cap (``WORKER_MEMORY``) is replaced, and only that answer reports "could not check".
- Each view shares a ``RegexBudget``. Once it is spent, or when the worker is busy with other requests for
  longer than what is left of it, the remaining pattern checks report "not checked yet". Results are cached
  per answer and pattern (a "could not check" for ``RETRY_AFTER`` seconds only), so the next view continues
  where this one stopped, and a slow answer never affects other answers.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import logging
import math
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Sequence
from decimal import Decimal, DecimalException, localcontext
from typing import Annotated, Any, Literal

import regex
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from .results import PatternResults

MAX_CHECKS = 10
MAX_PATTERN = 500
# CPU seconds to compile a new pattern in the worker; ordinary ones take under a millisecond. Windows measures
# thread CPU time in 15.6 ms steps, so the limit is several steps.
COMPILE_LIMIT = 0.1
WORKER_GRACE = 1.0  # seconds past its own limits before a worker that has not answered is replaced
WORKER_START_WAIT = 10.0
WORKER_MEMORY = 512 * 1024 * 1024  # bytes; past this the worker fails the pattern instead of growing
VET_SECONDS = 10.0  # for vetting the new patterns of one save
REGEX_TIMEOUT = 0.25  # seconds per search
REGEX_BUDGET = 2.0  # seconds of pattern checks per view
EXPORT_REGEX_BUDGET = 30.0  # an export is one explicit download, so it may take longer than a page view
RETRY_AFTER = 600.0  # seconds before a pattern that could not be checked on an answer is tried again
MAX_ACCEPTED = 50_000  # vetted patterns remembered, by digest
MAX_WAITING = 8  # requests that may wait for a turn with the worker at once
START_BACKOFF = 30.0  # seconds before starting the worker again after it failed to start
REFUSAL_RETRY = 60.0  # seconds a refusal that depends on timing is remembered (invalid ones: until restart)
MEMORY_REFUSAL_RETRY = 600.0  # and one for memory, in case the machine itself was short of memory
MAX_SCHEMA_DEPTH = 20
# Size limits keep the work per answer bounded: a schema is checked against every item of an answer.
MAX_SCHEMA_NODES = 500
MAX_ENUM = 200
MAX_PROPERTIES = 200
# Numbers whose exponent is further from zero than this are not read: they cannot be a plausible answer
# and exact arithmetic on them is expensive or overflows.
MAX_EXPONENT = 1000

log = logging.getLogger(__name__)

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
    """The pattern is found in the answer (``regex`` syntax). A pattern is refused when saved if it is invalid
    or too slow to compile, and each search has a time limit."""

    kind: Literal["regex"]
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN)
    case_sensitive: bool = False

    @property
    def flags(self) -> int:
        return 0 if self.case_sensitive else int(regex.IGNORECASE)


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
    # True when the view ran out of time for pattern checks before this one ran (``passed`` is None).
    # Results are kept, so viewing again continues.
    not_checked: bool = False


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
# The worker process. It compiles and searches patterns for this process, which never does either itself.
_WORKER_CODE = r"""
import json, sys, threading, time

import regex

VET_STACK = 1024 * 1024
SEARCH_STACK = 16 * 1024 * 1024
KEEP = 8
compiled = {}


def limit_memory(limit):
    # An allocation past the limit fails in this process (MemoryError) instead of exhausting the machine.
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class Extended(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", Basic),
                    ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            job = kernel32.CreateJobObjectW(None, None)
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = limit
            return bool(
                job
                and kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
                and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
            )
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return True
    except Exception:
        return False


def pattern(text, flags):
    key = (text, flags)
    found = compiled.pop(key, None)
    if found is None:
        found = regex.compile(text, flags, cache_pattern=False)
    compiled[key] = found
    while len(compiled) > KEEP:
        del compiled[next(iter(compiled))]
    return found


def vet(request):
    # CPU time of this thread, so a busy machine does not make a pattern look slow. A first measurement a little
    # over the limit is taken again; one far over it is not.
    limit = request["limit"]
    best = None
    for _ in range(2):
        start = time.thread_time()
        regex.compile(request["pattern"], request["flags"], cache_pattern=False)
        took = time.thread_time() - start
        best = took if best is None else min(best, took)
        if best <= limit or best > 4 * limit:
            break
    return {"seconds": best}


def search(request):
    found = pattern(request["pattern"], request["flags"]).search(request["text"], timeout=request["timeout"])
    return {"found": found is not None}


def handle(work, request, stack):
    reply = {}

    def run():
        for attempt in (1, 2):
            try:
                reply.update(work(request))
            except TimeoutError:
                reply["timeout"] = True
            except regex.error as exc:
                reply["error"] = "invalid regular expression: " + str(exc)
            except MemoryError:
                compiled.clear()  # the patterns kept for searches may be what filled the cap: try once without
                if attempt == 1:
                    continue
                reply["memory"] = True
            except Exception as exc:
                reply["error"] = "the pattern cannot be used (" + type(exc).__name__ + ")"
            return

    # A pattern is vetted with a sixteenth of the stack searches get, so one that passes is far from overflowing.
    threading.stack_size(stack)
    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return reply


print(json.dumps({"ready": True, "memory_limited": limit_memory(int(sys.argv[1]))}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    work, stack = (vet, VET_STACK) if request["op"] == "vet" else (search, SEARCH_STACK)
    print(json.dumps(handle(work, request, stack)), flush=True)
"""


class PatternCheckerUnavailable(Exception):
    """The worker process could not be started."""


class PatternCheckerBusy(PatternCheckerUnavailable):
    """No turn with the worker came in time, or too many requests are already waiting for one."""


class _Worker:
    """One worker process. Requests and replies are JSON lines; a reader thread queues the replies."""

    def __init__(self) -> None:
        try:
            # A fixed command: this interpreter and the code above. Patterns and answers go over stdin as JSON.
            self.process = subprocess.Popen(  # noqa: S603
                [sys.executable, "-I", "-c", _WORKER_CODE, str(WORKER_MEMORY)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log.warning("The pattern checker did not start: %s", exc)
            raise PatternCheckerUnavailable("the pattern checker did not start") from exc
        try:
            self._replies: queue.Queue[str | None] = queue.Queue()
            threading.Thread(target=self._read, name="pattern-checker-reader", daemon=True).start()
            ready = self._reply(WORKER_START_WAIT)
        except BaseException:
            self.stop()
            raise
        if ready is None or ready.get("ready") is not True:
            self.stop()
            raise PatternCheckerUnavailable("the pattern checker did not start")
        self.memory_limited = ready.get("memory_limited") is True

    def _read(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        for line in stdout:
            self._replies.put(line)
        self._replies.put(None)
        stdout.close()

    def _reply(self, wait: float) -> dict[str, Any] | None:
        try:
            line = self._replies.get(timeout=wait)
        except queue.Empty:
            return None
        if line is None:
            return None
        try:
            reply = json.loads(line)
        except ValueError:
            return None
        return reply if isinstance(reply, dict) else None

    def ask(self, request: dict[str, Any], wait: float) -> dict[str, Any] | None:
        stdin = self.process.stdin
        assert stdin is not None
        try:
            stdin.write(json.dumps(request) + "\n")
            stdin.flush()
        except OSError:
            return None
        return self._reply(wait)

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        self.process.kill()
        self.process.wait()
        if self.process.stdin is not None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()


class _PatternChecker:
    """The one worker process, shared by every request and replaced whenever it stops answering in time.

    Callers take turns. A caller waits at most ``turn_wait`` for its turn, and only ``MAX_WAITING`` may wait
    at once, so pattern checks never tie up the server's threads behind a slow pattern.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting_lock = threading.Lock()
        self._waiting = 0
        self._worker: _Worker | None = None
        self._failed_start = -math.inf

    def ask(self, request: dict[str, Any], wait: float, *, turn_wait: float) -> dict[str, Any] | None:
        """The worker's reply, or None when it did not answer within ``wait`` or stopped; it is then replaced.

        Raises ``PatternCheckerBusy`` when no turn comes in time, ``PatternCheckerUnavailable`` when the worker
        cannot start.
        """
        with self._waiting_lock:
            if self._waiting >= MAX_WAITING:
                raise PatternCheckerBusy("the pattern checker is busy")
            self._waiting += 1
        try:
            turn = self._lock.acquire(timeout=max(turn_wait, 0.0))
        finally:
            with self._waiting_lock:
                self._waiting -= 1
        if not turn:
            raise PatternCheckerBusy("the pattern checker is busy")
        try:
            return self._ask(request, wait)
        finally:
            self._lock.release()

    def _ask(self, request: dict[str, Any], wait: float) -> dict[str, Any] | None:
        if self._worker is not None and not self._worker.alive():
            log.warning("The pattern checker stopped (exit code %s); starting another", self._worker.process.returncode)
            self._worker = None
        if self._worker is None:
            if time.monotonic() - self._failed_start < START_BACKOFF:
                raise PatternCheckerUnavailable("the pattern checker did not start")
            try:
                self._worker = _Worker()
            except Exception as exc:
                self._failed_start = time.monotonic()
                log.warning("The pattern checker did not start: %s", exc)
                if isinstance(exc, PatternCheckerUnavailable):
                    raise
                raise PatternCheckerUnavailable("the pattern checker did not start") from exc
            if not self._worker.memory_limited:
                log.warning("The pattern checker runs without a memory cap on this system")
        try:
            reply = self._worker.ask(request, wait)
        except BaseException:
            # Its reply may still arrive and would be read as the answer to the next request: replace it.
            self._worker.stop()
            self._worker = None
            raise
        if reply is None:
            log.warning("Replacing the pattern checker: no answer to a %s within %.2f s", request.get("op"), wait)
            self._worker.stop()
            self._worker = None
        return reply

    def close(self) -> None:
        with self._lock:
            if self._worker is not None:
                self._worker.stop()
                self._worker = None


_checker = _PatternChecker()
atexit.register(_checker.close)


class _Recent[K, V]:
    """A bounded map that keeps the most recently used entries."""

    def __init__(self, size: int) -> None:
        self._items: OrderedDict[K, V] = OrderedDict()
        self._lock = threading.Lock()
        self._size = size

    def get(self, key: K) -> V | None:
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def _pattern_key(check: RegexCheck) -> bytes:
    pattern = check.pattern.encode("utf-8", "surrogatepass")
    return hashlib.blake2b(check.flags.to_bytes(4, "little") + pattern, digest_size=16).digest()


# Vetted patterns, by digest. A pattern that drops out is simply vetted again.
_accepted: _Recent[bytes, bool] = _Recent(MAX_ACCEPTED)
# Recent refusals, so saving the same bad pattern again costs nothing: (problem, expires). An invalid pattern or one
# that needs too much memory is refused for good; one refused on timing is vetted again after REFUSAL_RETRY.
_refused: _Recent[bytes, tuple[str, float | None]] = _Recent(256)
_SIMPLER = "use smaller repeat counts or less nesting"


def pattern_problems(checks_by_prompt: Sequence[Sequence[Check]]) -> list[str]:
    """What is wrong with the patterns of prompts about to be saved or run, one message per check.

    Each new pattern is compiled once in the worker. Vetting stops after ``VET_SECONDS``, so a save with many
    slow patterns stays bounded; the patterns left are refused as not checked. Raises
    ``PatternCheckerUnavailable`` (or ``PatternCheckerBusy``) when the worker cannot vet them now.
    """
    problems: list[str] = []
    deadline = time.monotonic() + VET_SECONDS
    for prompt_number, checks in enumerate(checks_by_prompt, start=1):
        for check_number, check in enumerate(checks, start=1):
            if isinstance(check, RegexCheck) and (problem := _vet(check, deadline)):
                problems.append(f"Prompt {prompt_number}, check {check_number}: {problem}")
    return problems


def _vet(check: RegexCheck, deadline: float) -> str | None:
    key = _pattern_key(check)
    if _accepted.get(key):
        return None
    refused = _refused.get(key)
    if refused is not None and (refused[1] is None or refused[1] > time.monotonic()):
        return refused[0]
    request = {"op": "vet", "pattern": check.pattern, "flags": check.flags, "limit": COMPILE_LIMIT}
    wait = 2 * COMPILE_LIMIT + WORKER_GRACE
    reply: dict[str, Any] | None = None
    for _ in range(2):
        left = deadline - time.monotonic()
        if left <= 0:
            return "not checked: there are too many new patterns to check in one save; save fewer at a time"
        asked = time.monotonic()
        reply = _checker.ask(request, wait, turn_wait=left)
        # A worker that stopped on the pattern at once gets one more try on a fresh worker; one that ran out of time
        # does not, so a pattern that is slow to compile is refused within about a second.
        if reply is not None or time.monotonic() - asked >= 0.9 * wait:
            break
    seconds = reply.get("seconds") if reply is not None else None
    retry: float | None = REFUSAL_RETRY  # how long the refusal is remembered; None: until restart
    if reply is None:
        problem = f"the pattern is too large or complex to check safely; {_SIMPLER}"
    elif reply.get("memory") is True:
        problem = f"the pattern needs more than {WORKER_MEMORY // 2**20} MB to compile; {_SIMPLER}"
        retry = MEMORY_REFUSAL_RETRY
    elif "error" in reply:
        problem, retry = str(reply["error"]), None  # invalid whatever the machine is doing
    elif not isinstance(seconds, int | float) or isinstance(seconds, bool):
        problem = "the pattern checker gave no answer for this pattern"
    elif seconds > COMPILE_LIMIT:
        limit = f"{COMPILE_LIMIT * 1000:.0f} ms"
        took = f"{seconds * 1000:.0f} ms"
        problem = f"the pattern takes too long to compile ({took}; the limit is {limit}); {_SIMPLER}"
    else:
        _accepted.put(key, True)
        return None
    _refused.put(key, (problem, None if retry is None else time.monotonic() + retry))
    return problem


class RegexBudget:
    """Seconds of pattern checks that one view may spend, shared by every answer it shows."""

    def __init__(self, seconds: float | None = None) -> None:
        self.left = REGEX_BUDGET if seconds is None else seconds
        self.reason: str | None = None  # why the view stopped early, when it was not the time


# Results by (flags, pattern, answer), kept compact: (passed, detail, expires). A result that is not a pass
# or fail expires, so it is tried again later in case the machine was only busy.
_results = PatternResults()
atexit.register(_results.clear)


def _result_key(check: RegexCheck, text: str) -> bytes:
    pattern = check.pattern.encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(digest_size=16)
    digest.update(check.flags.to_bytes(4, "little") + len(pattern).to_bytes(4, "little") + pattern)
    digest.update(text.encode("utf-8", "surrogatepass"))
    return digest.digest()


NOT_CHECKED_YET = "not checked yet: this view ran out of time for pattern checks; reload to continue"


def _not_checked_yet(check: RegexCheck, why: str | None = None) -> CheckResult:
    detail = NOT_CHECKED_YET if why is None else f"not checked yet: {why}; reload to continue"
    return CheckResult(kind=check.kind, passed=None, detail=detail, not_checked=True)


def _search(check: RegexCheck, text: str, budget: RegexBudget) -> CheckResult:
    key = _result_key(check, text)
    cached = _results.get(key)
    if cached is not None and (cached[2] is None or cached[2] > time.monotonic()):
        return CheckResult(kind=check.kind, passed=cached[0], detail=cached[1])
    if budget.left <= 0:
        return _not_checked_yet(check, budget.reason)  # not cached: a later view runs it
    request = {"op": "search", "pattern": check.pattern, "flags": check.flags, "text": text, "timeout": REGEX_TIMEOUT}
    start = time.perf_counter()
    try:
        reply = _checker.ask(request, REGEX_TIMEOUT + COMPILE_LIMIT + WORKER_GRACE, turn_wait=budget.left)
    except PatternCheckerUnavailable as exc:  # busy, or not started: this view stops; a later one continues
        budget.left, budget.reason = 0, str(exc)
        return _not_checked_yet(check, budget.reason)
    finally:
        budget.left -= time.perf_counter() - start
    found = reply.get("found") if reply is not None else None
    if isinstance(found, bool):
        detail = "pattern found" if found else "pattern not found"
        _results.put(key, (found, detail, None))
        return CheckResult(kind=check.kind, passed=found, detail=detail)
    if reply is None:
        detail = "could not check: the pattern checker stopped on this answer"
    elif reply.get("timeout") is True:
        detail = f"could not check: the pattern took longer than {REGEX_TIMEOUT} s on this answer"
    elif reply.get("memory") is True:
        detail = f"could not check: searching this answer needs more than {WORKER_MEMORY // 2**20} MB"
    else:
        detail = f"could not be evaluated on this answer: {reply.get('error', 'no result from the pattern checker')}"
    _results.put(key, (None, detail, time.monotonic() + RETRY_AFTER))
    return CheckResult(kind=check.kind, passed=None, detail=detail)


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
    except (RecursionError, ArithmeticError, ValueError, TypeError, sqlite3.Error) as exc:
        detail = f"could not be evaluated on this answer ({type(exc).__name__})"
        return CheckResult(kind=check.kind, passed=None, detail=detail)


CHECKS = TypeAdapter(list[Check])


def checks_json(checks: list[Check]) -> str:
    return CHECKS.dump_json(checks).decode()


def load_checks(raw: str | None) -> list[Check]:
    return CHECKS.validate_json(raw or "[]")


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
