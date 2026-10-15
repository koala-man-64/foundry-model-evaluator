"""Strict validation of judge output against the expected labels and the rubric version."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .rubric import RubricDefinition

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ParsedCandidate:
    label: str
    scores: dict[str, int]
    criterion_explanations: dict[str, str]
    judge_overall: float
    weighted_overall: float
    explanation: str


@dataclass
class ParseResult:
    errors: list[str] = field(default_factory=list)
    candidates: list[ParsedCandidate] = field(default_factory=list)
    preferred: str | None = None
    preference_explanation: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def extract_json(text: str) -> tuple[Any, str | None]:
    """Parse the output as JSON (a single surrounding Markdown code fence is tolerated)."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    if not stripped:
        return None, "the output is empty"
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError as exc:
        return None, f"the output is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})"


def _whole_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_judge_output(
    text: str, labels: list[str], rubric: RubricDefinition, *, stopped_at_limit: bool = False
) -> ParseResult:
    result = ParseResult()
    if stopped_at_limit:
        result.errors.append("the judge stopped at its output token limit, so the output may be cut off")
    data, error = extract_json(text)
    if error:
        result.errors.append(error)
        return result
    if not isinstance(data, dict):
        result.errors.append("the output must be a JSON object")
        return result
    lo, hi = rubric.scale_min, rubric.scale_max
    keys = [c.key for c in rubric.criteria]
    responses = data.get("responses")
    if not isinstance(responses, list):
        result.errors.append('"responses" must be a list')
        responses = []
    seen: set[str] = set()
    parsed: list[ParsedCandidate] = []
    for index, item in enumerate(responses):
        where = f"responses[{index}]"
        if not isinstance(item, dict):
            result.errors.append(f"{where} must be an object")
            continue
        label = item.get("label")
        if not isinstance(label, str) or label not in labels:
            result.errors.append(f"{where}: unknown label {label!r} (expected one of {', '.join(labels)})")
            continue
        if label in seen:
            result.errors.append(f"response {label} appears more than once")
            continue
        seen.add(label)
        criteria = item.get("criteria")
        scores: dict[str, int] = {}
        explanations: dict[str, str] = {}
        invalid: set[str] = set()
        if not isinstance(criteria, list):
            result.errors.append(f'response {label}: "criteria" must be a list')
            criteria = []
        for entry in criteria:
            if not isinstance(entry, dict):
                result.errors.append(f"response {label}: each criterion must be an object")
                continue
            key = entry.get("key")
            if not isinstance(key, str) or key not in keys:
                result.errors.append(f"response {label}: unknown criterion {key!r}")
                continue
            if key in scores:
                result.errors.append(f"response {label}: criterion {key} appears more than once")
                continue
            score = _whole_number(entry.get("score"))
            if score is None:
                result.errors.append(f"response {label}: the {key} score must be a whole number")
                invalid.add(key)
                continue
            if not lo <= score <= hi:
                result.errors.append(f"response {label}: the {key} score {score} is outside {lo}-{hi}")
                invalid.add(key)
                continue
            scores[key] = score
            explanations[key] = _text(entry.get("explanation"))
        missing = [k for k in keys if k not in scores and k not in invalid]
        if missing:
            result.errors.append(f"response {label}: missing criteria {', '.join(missing)}")
        overall = _number(item.get("overall"))
        if overall is None:
            result.errors.append(f'response {label}: "overall" must be a number')
        elif not lo <= overall <= hi:
            result.errors.append(f"response {label}: the overall score {overall:g} is outside {lo}-{hi}")
        if len(scores) == len(keys) and overall is not None and lo <= overall <= hi:
            parsed.append(
                ParsedCandidate(
                    label=label,
                    scores=scores,
                    criterion_explanations=explanations,
                    judge_overall=overall,
                    weighted_overall=rubric.weighted_overall(scores),
                    explanation=_text(item.get("explanation")),
                )
            )
    absent = [label for label in labels if label not in seen]
    if absent:
        result.errors.append(f"responses missing for {', '.join(absent)}")
    preferred = data.get("preferred")
    if not isinstance(preferred, str) or preferred.strip() not in [*labels, "tie"]:
        result.errors.append(f'"preferred" must be one of {", ".join(labels)} or "tie" (got {preferred!r})')
    else:
        result.preferred = preferred.strip()
    result.preference_explanation = _text(data.get("preference_explanation"))
    if not result.errors:
        order = {label: i for i, label in enumerate(labels)}
        result.candidates = sorted(parsed, key=lambda c: order[c.label])
    return result
