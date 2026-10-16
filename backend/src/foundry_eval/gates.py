"""Pass/fail gates for headless suite runs, and the regression verdict against a baseline run.

Gates read the app's own API answers (the run detail and a comparison), so they judge exactly what
the app shows. Each gate ends in one outcome. The command exits with the most basic problem found:

* ``attempts_failed`` (3): more paid calls did not succeed than ``failures-above`` allows (none by
  default): answers, and judge evaluations of prompts and trials that have no judge result. A run
  whose automatic judging did not start fails here too. The measurement itself is incomplete, so this
  comes first.
* ``cost_incomplete`` (4): a cost gate could not be decided because an estimate is incomplete or
  still pending. An unknown cost is never taken as zero.
* ``gate_failed`` (1): a cost limit was exceeded, or the comparison with the baseline found a
  regression or could not reach a verdict on a metric that has data.
* ``passed`` (0).

Errors that stop the command before a verdict (bad arguments, the app not running, a refused
preview) exit with 2.

**Baseline verdicts.** The comparison gives each metric a paired difference to the baseline
candidate, with a verdict that is "higher" or "lower" only when the whole 95% interval is on one side
of zero. The candidate regressed when the verdict points the bad way (a lower judge score or checks
pass rate, a higher cost per answer) and the end of the interval nearest zero lies beyond the
tolerance, so with a zero tolerance the verdict alone decides. Without an interval (every paired
difference the same) the difference itself is used. "Cannot tell" passes: there is data, and no
regression shows. A metric with data but no verdict (too few paired prompts, values on one side
only, judges that are not comparable, or the same bad difference on too few prompts for the sign
test) is inconclusive and fails the gate, because missing evidence is not a pass. A metric neither
run has (no judge, no checks) does not apply, but a gate on which no chosen metric applies, or whose
comparison failed, fails. The baseline must stay frozen: a new run that measured the baseline
candidate again would be pooled into it, so that fails as well.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from .analysis.stats import SIGN_TEST_MIN
from .money import to_text, validate_money_input

Outcome = Literal["passed", "gate_failed", "attempts_failed", "cost_incomplete"]
EXIT_CODES: dict[Outcome, int] = {"passed": 0, "gate_failed": 1, "attempts_failed": 3, "cost_incomplete": 4}
EXIT_ERROR = 2
_PRECEDENCE: tuple[Outcome, ...] = ("attempts_failed", "cost_incomplete", "gate_failed")

BASELINE_METRICS = ("judge", "checks", "cost")
# The verdict that means the candidate is worse than the baseline.
_WORSE = {"judge": "lower", "checks": "lower", "cost": "higher"}
_NAMES = {"judge": "judge score", "checks": "checks pass rate", "cost": "cost per answer"}
# The comparison field that counts each candidate's cells with a value for the metric.
_CELLS = {"judge": "judge_scored", "checks": "checks_cells", "cost": "cost_cells"}


class GateSpecError(ValueError):
    pass


@dataclass(frozen=True)
class Gates:
    failures_above: int = 0
    cost_upper_above: Decimal | None = None
    incomplete_cost: bool = False


def _amount(text: str, what: str) -> Decimal:
    try:
        value = validate_money_input(text.strip())
    except ValueError as exc:
        raise GateSpecError(f"{what} must be a decimal amount such as 0.25") from exc
    if value < 0:
        raise GateSpecError(f"{what} cannot be negative")
    return value


def parse_fail_if(values: Sequence[str]) -> Gates:
    """``cost-upper-above=<amount>``, ``failures-above=<n>`` and ``incomplete-cost``, each at most once."""
    found: dict[str, str] = {}
    for raw in values:
        name, has_value, value = (part.strip() for part in raw.partition("="))
        if name in found:
            raise GateSpecError(f"--fail-if {name} is given more than once")
        if name == "incomplete-cost":
            if has_value:
                raise GateSpecError("--fail-if incomplete-cost takes no value")
        elif name in ("cost-upper-above", "failures-above"):
            if not value:
                raise GateSpecError(f"--fail-if {name} needs a value, as in {name}=<value>")
        else:
            raise GateSpecError(
                f"unknown gate {raw!r}; use cost-upper-above=<amount>, failures-above=<n> or incomplete-cost"
            )
        found[name] = value
    failures = 0
    if "failures-above" in found:
        try:
            failures = int(found["failures-above"])
        except ValueError as exc:
            raise GateSpecError("failures-above must be a whole number") from exc
        if failures < 0:
            raise GateSpecError("failures-above cannot be negative")
    cost = _amount(found["cost-upper-above"], "cost-upper-above") if "cost-upper-above" in found else None
    return Gates(failures_above=failures, cost_upper_above=cost, incomplete_cost="incomplete-cost" in found)


def parse_tolerances(values: Sequence[str]) -> dict[str, Decimal]:
    """``judge=<points>``, ``checks=<share 0-1>`` and ``cost=<amount per answer>``; 0 when not given."""
    tolerances: dict[str, Decimal] = {}
    for raw in values:
        name, _, value = (part.strip() for part in raw.partition("="))
        if name not in BASELINE_METRICS:
            raise GateSpecError(f"unknown tolerance {raw!r}; use judge=, checks= or cost=")
        if name in tolerances:
            raise GateSpecError(f"--tolerance {name} is given more than once")
        tolerances[name] = _amount(value, f"the {name} tolerance")
        if name == "checks" and tolerances[name] > 1:
            raise GateSpecError("the checks tolerance is a share of answers, from 0 to 1")
    return tolerances


def parse_metrics(value: str | None) -> tuple[str, ...]:
    if value is None:
        return BASELINE_METRICS
    names = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [name for name in names if name not in BASELINE_METRICS]
    if unknown or not names:
        raise GateSpecError(f"--baseline-metrics takes a comma-separated list of {', '.join(BASELINE_METRICS)}")
    return tuple(dict.fromkeys(names))


def counted_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The attempts that count: one replaced by an explicit retry counts through that retry."""
    replaced = {a["retry_of"] for a in attempts if a.get("retry_of")}
    return [a for a in attempts if a["id"] not in replaced]


def judging_failures(evaluations: list[dict[str, Any]]) -> tuple[int, int]:
    """(prompts and trials judged, those without a successful judge evaluation).

    A later successful re-judging replaces an earlier failure, as in comparisons.
    """
    judged: dict[tuple[str, int], bool] = {}
    for evaluation in evaluations:
        cell = (evaluation["prompt_id"], evaluation["trial_index"])
        judged[cell] = judged.get(cell, False) or evaluation["status"] == "succeeded"
    return len(judged), sum(1 for succeeded in judged.values() if not succeeded)


def _unknown_cost(cost: dict[str, Any]) -> str:
    labels = (("pending", "pending"), ("incomplete", "incomplete"), ("other_currency", "in another currency"))
    parts = [f"{cost[field]} {label}" for field, label in labels if cost.get(field)]
    return f"the cost is not fully known ({', '.join(parts) or 'unknown'}); an unknown cost is never taken as zero"


def run_gates(
    gates: Gates, detail: dict[str, Any], evaluations: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """The gates on the run itself: failed paid calls, and its estimated cost (answers and judging)."""
    attempts = counted_attempts(detail["attempts"])
    failed = Counter(a["status"] for a in attempts if a["status"] != "succeeded")
    judged, unjudged = judging_failures(evaluations or [])
    judge_error = detail.get("run", {}).get("judge_error")
    count = sum(failed.values()) + unjudged
    statuses = ", ".join(f"{n} {status}" for status, n in sorted(failed.items()))
    text = f"{sum(failed.values())} of {len(attempts)} attempts did not succeed" + (f" ({statuses})" if failed else "")
    if judged:
        text += f"; {unjudged} of {judged} judged prompts and trials have no judge result"
    if judge_error:
        text += f"; {judge_error}"
    results: list[dict[str, Any]] = [
        {
            "gate": "failures-above",
            "limit": gates.failures_above,
            "value": count,
            "outcome": "attempts_failed" if count > gates.failures_above or judge_error else "passed",
            "detail": text,
        }
    ]
    cost = detail["combined_cost"]
    currency = cost["currency"]
    if gates.cost_upper_above is not None:
        limit = gates.cost_upper_above
        upper = cost.get("upper")
        if upper is None or cost["status"] == "pending" or cost.get("pending"):
            outcome: Outcome = "cost_incomplete"
            text = _unknown_cost(cost)
        elif Decimal(upper) > limit:
            outcome, text = "gate_failed", f"the cost is at most {upper} {currency}, above the limit"
        else:
            outcome, text = "passed", f"the cost is at most {upper} {currency}"
        results.append(
            {"gate": "cost-upper-above", "limit": to_text(limit), "value": upper, "outcome": outcome, "detail": text}
        )
    if gates.incomplete_cost:
        complete = cost["status"] in ("complete", "empty")
        results.append(
            {
                "gate": "incomplete-cost",
                "value": cost["status"],
                "outcome": "passed" if complete else "cost_incomplete",
                "detail": "every cost estimate is complete" if complete else _unknown_cost(cost),
            }
        )
    return results


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def assess_metric(
    metric: str,
    difference: dict[str, Any] | None,
    *,
    tolerance: Decimal,
    applies: bool,
    candidate: dict[str, Any],
    baseline: dict[str, Any] | None,
    comparison: dict[str, Any],
) -> dict[str, Any]:
    """One metric of one candidate against the baseline: regressed, ok, inconclusive or not applicable."""
    name = _NAMES[metric]
    result: dict[str, Any] = {"status": "inconclusive", "tolerance": to_text(tolerance)}
    if difference is not None:
        result.update({k: difference[k] for k in ("verdict", "mean", "low", "high", "prompts")})
    if metric == "judge" and comparison.get("judge_bases", 0) > 1:
        result["reason"] = "the judge scores come from different rubric versions, judges or judge settings"
        return result
    if difference is None:
        mine = candidate.get(_CELLS[metric], 0)
        theirs = (baseline or {}).get(_CELLS[metric], 0)
        if not applies:
            result.update(status="not_applicable", reason=f"neither run has a {name}")
        elif comparison.get("cells_matched", 0) == 0:
            result["reason"] = "no prompt and trial succeeded for both the candidate and the baseline"
        elif mine == 0 and theirs == 0:
            result["reason"] = f"no compared answer has a {name}"
        elif mine == 0:
            excluded = candidate.get("cost_excluded", 0) if metric == "cost" else 0
            suffix = f" ({excluded} incomplete cost estimates)" if excluded else ""
            result["reason"] = f"the candidate has no {name} in the compared cells{suffix}"
        elif theirs == 0:
            result["reason"] = f"the baseline has no {name} in the compared cells"
        else:
            result["reason"] = f"no prompt has a {name} for both"
        return result
    verdict = difference["verdict"]
    if verdict == "insufficient_data":
        minimum = comparison.get("interval_min_prompts", 3)
        result["reason"] = f"{difference['prompts']} paired prompts; a verdict needs at least {minimum}"
        return result
    worse = _WORSE[metric]
    # The end of the interval nearest zero, or the difference itself when every paired difference is the same.
    constant = difference["low"] is None and difference["high"] is None
    if worse == "higher":
        edge = _number(difference["mean"] if constant else difference["low"])
        beyond = edge is not None and edge > tolerance
    else:
        edge = _number(difference["mean"] if constant else difference["high"])
        beyond = edge is not None and edge < -tolerance
    if verdict == worse:
        reason = f"{name} is {worse} than the baseline"
        if tolerance:
            reason += f", {'beyond' if beyond else 'within'} the tolerance of {to_text(tolerance)}"
        result.update(status="regressed" if beyond else "ok", reason=reason)
    elif verdict == "cannot_tell" and constant and beyond:
        result["reason"] = (
            f"{name} is {worse} by the same amount on all {difference['prompts']} paired prompts; "
            f"a verdict then needs at least {SIGN_TEST_MIN} prompts"
        )
    else:
        result.update(status="ok", reason=f"{name}: {verdict.replace('_', ' ')}")
    return result


def baseline_gate(
    comparison: dict[str, Any],
    *,
    baseline_run_id: str,
    baseline_key: str,
    candidate_keys: list[str],
    metrics: Sequence[str],
    tolerances: dict[str, Decimal],
    applies: dict[str, bool],
    remeasured: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    """Each new candidate against the baseline candidate, on the chosen metrics.

    ``remeasured``: the new run ran the baseline candidate again. ``error``: the comparison failed.
    """
    by_key = {c["candidate_key"]: c for c in comparison["candidates"]}
    baseline = by_key.get(baseline_key)
    rows: list[dict[str, Any]] = []
    for key in [] if remeasured or error else candidate_keys:
        candidate = by_key.get(key, {"candidate_key": key})
        differences = candidate.get("vs_baseline") or {}
        rows.append(
            {
                "candidate_key": key,
                "label": candidate.get("label"),
                "metrics": {
                    metric: assess_metric(
                        metric,
                        differences.get(metric),
                        tolerance=tolerances.get(metric, Decimal(0)),
                        applies=applies.get(metric, True),
                        candidate=candidate,
                        baseline=baseline,
                        comparison=comparison,
                    )
                    for metric in metrics
                },
            }
        )
    statuses = [m["status"] for row in rows for m in row["metrics"].values()]
    outcome: Outcome = "gate_failed"
    if error:
        text = f"the comparison failed: {error}"
    elif remeasured:
        text = (
            "the new run also ran the baseline candidate, which would be pooled into the baseline; "
            "choose the new candidates with --candidate"
        )
    elif not rows:
        text = "the new run has no candidate other than the baseline candidate"
    elif "regressed" in statuses:
        text = "a candidate regressed against the baseline"
    elif "inconclusive" in statuses:
        text = "a metric with data has no verdict against the baseline"
    elif all(status == "not_applicable" for status in statuses):
        text = "none of the chosen metrics applies to these runs, so nothing was compared"
    else:
        outcome, text = "passed", "no regression against the baseline"
    return {
        "gate": "baseline",
        "outcome": outcome,
        "detail": text,
        "baseline_run_id": baseline_run_id,
        "baseline_candidate": {"candidate_key": baseline_key, "label": (baseline or {}).get("label")},
        "cells_matched": comparison.get("cells_matched"),
        "prompts_matched": comparison.get("prompts_matched"),
        "notes": comparison.get("notes", []),
        "candidates": rows,
    }


def overall_outcome(results: Sequence[dict[str, Any]]) -> Outcome:
    found = {result["outcome"] for result in results}
    return next((outcome for outcome in _PRECEDENCE if outcome in found), "passed")
