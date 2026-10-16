"""Gates for headless suite runs: parsing, run gates, baseline verdicts, exit precedence, and the client."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx2
import pytest

from foundry_eval import cli
from foundry_eval.app_client import ApiError, AppClient, RequestOutcomeUnknownError
from foundry_eval.gates import (
    EXIT_CODES,
    Gates,
    GateSpecError,
    assess_metric,
    baseline_gate,
    judging_failures,
    overall_outcome,
    parse_fail_if,
    parse_metrics,
    parse_tolerances,
    run_gates,
)

COMPARISON: dict[str, Any] = {"cells_matched": 6, "judge_bases": 1, "interval_min_prompts": 3, "candidates": []}


def test_fail_if_parses_each_gate_once():
    assert parse_fail_if([]) == Gates()
    gates = parse_fail_if(["cost-upper-above=0.25", "failures-above=2", "incomplete-cost"])
    assert gates == Gates(failures_above=2, cost_upper_above=Decimal("0.25"), incomplete_cost=True)
    for values, message in (
        (["latency-above=3"], "unknown gate"),
        (["failures-above=1", "failures-above=2"], "more than once"),
        (["failures-above="], "needs a value"),
        (["failures-above=1.5"], "whole number"),
        (["failures-above=-1"], "cannot be negative"),
        (["cost-upper-above=lots"], "decimal amount"),
        (["cost-upper-above=-1"], "cannot be negative"),
        (["incomplete-cost=yes"], "takes no value"),
    ):
        with pytest.raises(GateSpecError, match=message):
            parse_fail_if(values)


def test_tolerances_and_metrics():
    assert parse_tolerances(["judge=0.5", "cost=0.001"]) == {"judge": Decimal("0.5"), "cost": Decimal("0.001")}
    with pytest.raises(GateSpecError, match="unknown tolerance"):
        parse_tolerances(["latency=10"])
    with pytest.raises(GateSpecError, match="more than once"):
        parse_tolerances(["judge=1", "judge=2"])
    assert parse_metrics(None) == ("judge", "checks", "cost")
    assert parse_metrics("cost, checks,cost") == ("cost", "checks")
    for value in ("", "latency", "judge,latency"):
        with pytest.raises(GateSpecError):
            parse_metrics(value)


def detail(attempts: list[dict], **cost: Any) -> dict[str, Any]:
    combined = {"status": "complete", "currency": "USD", "upper": "0.1", "pending": 0, "incomplete": 0, **cost}
    return {"attempts": attempts, "combined_cost": combined}


def attempt(
    identifier: str, status: str = "succeeded", retry_of: str | None = None, prompt: str = "p1", trial: int = 1
) -> dict[str, Any]:
    return {"id": identifier, "status": status, "retry_of": retry_of, "prompt_id": prompt, "trial_index": trial}


def test_failures_count_a_retried_attempt_through_its_retry():
    attempts = [attempt("a", "failed"), attempt("b", "succeeded", retry_of="a"), attempt("c", "skipped_budget")]
    [failures] = run_gates(Gates(), detail(attempts))
    assert failures["value"] == 1 and failures["outcome"] == "attempts_failed"
    assert failures["detail"] == "1 of 2 attempts did not succeed (1 skipped_budget)"
    [allowed] = run_gates(Gates(failures_above=1), detail(attempts))
    assert allowed["outcome"] == "passed"


def test_cost_gates_never_take_an_unknown_cost_as_zero():
    gates = Gates(cost_upper_above=Decimal("0.05"), incomplete_cost=True)
    _, limit, complete = run_gates(gates, detail([], upper="0.1"))
    assert limit["outcome"] == "gate_failed" and complete["outcome"] == "passed"
    _, limit, _ = run_gates(gates, detail([], upper="0.05"))
    assert limit["outcome"] == "passed"  # at the limit is not above it
    _, limit, complete = run_gates(gates, detail([], status="pending", upper=None, pending=2))
    assert limit["outcome"] == complete["outcome"] == "cost_incomplete"
    assert "2 pending" in limit["detail"] and "never taken as zero" in limit["detail"]
    _, limit, complete = run_gates(gates, detail([], status="incomplete", upper="0.01", incomplete=1))
    assert limit["outcome"] == "passed" and complete["outcome"] == "cost_incomplete"


def difference(verdict: str, mean: Any, low: Any = None, high: Any = None, prompts: int = 6) -> dict[str, Any]:
    return {"verdict": verdict, "mean": mean, "low": low, "high": high, "prompts": prompts}


def assess(metric: str, diff: dict | None, tolerance: str = "0", **extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "applies": True,
        "candidate": {"judge_scored": 6, "checks_cells": 6, "cost_cells": 6},
        "baseline": {"judge_scored": 6, "checks_cells": 6, "cost_cells": 6},
        "comparison": COMPARISON,
        **extra,
    }
    return assess_metric(metric, diff, tolerance=Decimal(tolerance), **options)


def test_a_regression_needs_the_whole_interval_beyond_the_tolerance():
    worse = difference("lower", -0.6, low=-0.9, high=-0.3)
    assert assess("judge", worse)["status"] == "regressed"
    assert assess("judge", worse, "0.25")["status"] == "regressed"
    within = assess("judge", worse, "0.3")  # the end nearest zero (-0.3) is not beyond -0.3
    assert within["status"] == "ok" and "within the tolerance of 0.3" in within["reason"]
    pricier = difference("higher", "0.002", low="0.001", high="0.003")
    assert assess("cost", pricier)["status"] == "regressed"
    assert assess("cost", pricier, "0.001")["status"] == "ok"
    # Every paired difference the same: no interval, so the difference itself decides.
    constant = difference("lower", -1.0)
    assert assess("checks", constant, "0.5")["status"] == "regressed"
    assert assess("checks", constant, "1")["status"] == "ok"


def test_better_or_unclear_differences_pass():
    for metric, verdict in (("judge", "higher"), ("checks", "cannot_tell"), ("cost", "lower")):
        result = assess(metric, difference(verdict, 0.1, low=-0.1, high=0.3))
        assert result["status"] == "ok" and result["verdict"] == verdict


def test_missing_evidence_is_inconclusive_and_absent_metrics_do_not_apply():
    few = assess("judge", difference("insufficient_data", -1.0, prompts=2))
    assert few["status"] == "inconclusive" and "2 paired prompts" in few["reason"] and "at least 3" in few["reason"]
    assert assess("checks", None, applies=False)["status"] == "not_applicable"
    one_sided = assess("judge", None, candidate={"judge_scored": 0})
    assert one_sided["status"] == "inconclusive" and "candidate has no judge score" in one_sided["reason"]
    baseline_only = assess("checks", None, baseline={"checks_cells": 0})
    assert "baseline has no checks pass rate" in baseline_only["reason"]
    unpriced = assess("cost", None, candidate={"cost_cells": 0, "cost_excluded": 4})
    assert "4 incomplete cost estimates" in unpriced["reason"]
    unmatched = assess("cost", None, comparison={**COMPARISON, "cells_matched": 0})
    assert "no prompt and trial succeeded" in unmatched["reason"]
    mixed = assess("judge", difference("higher", 1.0, 0.5, 1.5), comparison={**COMPARISON, "judge_bases": 2})
    assert mixed["status"] == "inconclusive" and "different rubric versions" in mixed["reason"]


def test_baseline_gate_needs_a_candidate_besides_the_baseline():
    comparison = {**COMPARISON, "candidates": [{"candidate_key": "base", "label": "Base"}]}
    options: dict[str, Any] = {
        "baseline_run_id": "run-1",
        "baseline_key": "base",
        "metrics": ("cost",),
        "tolerances": {},
        "applies": {"cost": True},
    }
    empty = baseline_gate(comparison, candidate_keys=[], **options)
    assert empty["outcome"] == "gate_failed" and "no candidate other than the baseline" in empty["detail"]
    comparison["candidates"].append(
        {"candidate_key": "new", "label": "New", "cost_cells": 6, "vs_baseline": {"cost": difference("lower", "-1")}}
    )
    passed = baseline_gate(comparison, candidate_keys=["new"], **options)
    assert passed["outcome"] == "passed" and passed["baseline_candidate"] == {"candidate_key": "base", "label": "Base"}


def test_the_most_basic_problem_decides_the_exit_code():
    def results(*found: str) -> list[dict[str, str]]:
        return [{"outcome": outcome} for outcome in found]

    assert overall_outcome(results("passed", "passed")) == "passed"
    assert overall_outcome(results("gate_failed", "cost_incomplete")) == "cost_incomplete"
    assert overall_outcome(results("gate_failed", "cost_incomplete", "attempts_failed")) == "attempts_failed"
    assert EXIT_CODES == {"passed": 0, "gate_failed": 1, "attempts_failed": 3, "cost_incomplete": 4}


def test_the_client_sends_the_spa_headers_and_never_retries():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/slow":
            raise httpx2.ReadTimeout("timed out", request=request)
        return httpx2.Response(422, json={"detail": [{"msg": "budget: too large"}]})

    origin = "http://127.0.0.1:8765"
    with httpx2.Client(base_url=origin, transport=httpx2.MockTransport(handler)) as http:
        client = AppClient(origin, http=http)
        with pytest.raises(RequestOutcomeUnknownError):
            client.post("/slow", {"budget": "1"}, idempotency_key="cli-suite-key")
        with pytest.raises(ApiError) as error:
            client.get("/api/x", [("run_id", "a"), ("run_id", "b")])
    assert len(seen) == 2  # one request each: nothing was retried
    paid = seen[0].headers
    assert paid["origin"] == origin and paid["x-fme-client"] == "1" and paid["idempotency-key"] == "cli-suite-key"
    assert seen[1].url.query == b"run_id=a&run_id=b" and "idempotency-key" not in seen[1].headers
    assert error.value.status == 422 and error.value.detail == "budget: too large"


def test_the_run_key_names_one_suite_and_candidate_choice():
    key = cli.suite_run_key("suite-1", ["b", "a"], "build-7")
    assert key == cli.suite_run_key("suite-1", ["a", "b"], "build-7")  # the order of --candidate does not matter
    assert key.startswith("cli-suite-") and 8 <= len(key) <= 128
    others = {
        cli.suite_run_key("suite-2", ["a", "b"], "build-7"),
        cli.suite_run_key("suite-1", ["a"], "build-7"),
        cli.suite_run_key("suite-1", None, "build-7"),
        cli.suite_run_key("suite-1", ["a", "b"], "build-8"),
    }
    assert key not in others and len(others) == 4


def test_failed_judging_counts_unless_a_re_judging_succeeded():
    evaluations = [
        {"prompt_id": "p1", "trial_index": 1, "status": "malformed"},
        {"prompt_id": "p1", "trial_index": 1, "status": "succeeded"},
        {"prompt_id": "p2", "trial_index": 1, "status": "failed"},
        {"prompt_id": "p2", "trial_index": 2, "status": "succeeded"},
    ]
    assert judging_failures(evaluations) == (3, 1)
    [failures] = run_gates(Gates(), detail([attempt("a")]), evaluations)
    assert failures["value"] == 1 and failures["outcome"] == "attempts_failed"
    assert "1 of 3 judged prompts and trials have no judge result" in failures["detail"]
    # Automatic judging that did not start fails even when failures are allowed.
    stopped = {**detail([attempt("a")]), "run": {"judge_error": "Automatic judging did not start: no key"}}
    [failures] = run_gates(Gates(failures_above=10), stopped)
    assert failures["outcome"] == "attempts_failed" and "did not start" in failures["detail"]


def test_a_judged_run_expects_a_judge_result_for_every_answered_prompt_and_trial():
    attempts = [attempt("a", prompt="p1"), attempt("b", prompt="p2"), attempt("c", "failed", prompt="p3")]
    evaluations = [{"prompt_id": "p1", "trial_index": 1, "status": "succeeded"}]
    # p2 was answered but has no evaluation at all; p3 has no answer, so it counts once, as a failed answer.
    assert judging_failures(evaluations, {("p1", 1), ("p2", 1)}) == (2, 1)
    judged = {**detail(attempts), "run": {"judge_config": {"judge_profile_id": "j"}}}
    [failures] = run_gates(Gates(failures_above=1), judged, evaluations)
    assert failures["value"] == 2 and failures["outcome"] == "attempts_failed"
    assert "1 of 2 judged prompts and trials have no judge result" in failures["detail"]
    # Without automatic judging, only the evaluations that exist are counted.
    [failures] = run_gates(Gates(failures_above=1), detail(attempts), evaluations)
    assert failures["value"] == 1 and failures["outcome"] == "passed"


def test_a_pending_cost_is_not_final_even_with_an_upper_bound():
    gates = Gates(cost_upper_above=Decimal(1))
    _, limit = run_gates(gates, detail([], status="pending", upper="0.01", pending=1))
    assert limit["outcome"] == "cost_incomplete"


def test_the_checks_tolerance_is_a_share():
    assert parse_tolerances(["checks=1"]) == {"checks": Decimal(1)}
    with pytest.raises(GateSpecError, match="from 0 to 1"):
        parse_tolerances(["checks=1.5"])


def test_the_same_bad_difference_on_too_few_prompts_is_inconclusive():
    worse = assess("checks", difference("cannot_tell", -1.0, prompts=4))
    assert worse["status"] == "inconclusive" and "all 4 paired prompts" in worse["reason"]
    assert "at least 6" in worse["reason"]
    assert assess("checks", difference("cannot_tell", -1.0, prompts=4), "1")["status"] == "ok"
    assert assess("checks", difference("cannot_tell", 1.0, prompts=4))["status"] == "ok"
    assert assess("cost", difference("cannot_tell", "0", prompts=4))["status"] == "ok"


def test_a_baseline_gate_without_a_comparison_fails():
    comparison = {
        **COMPARISON,
        "candidates": [
            {"candidate_key": "base", "label": "Base"},
            {"candidate_key": "new", "label": "New", "vs_baseline": {}},
        ],
    }
    options: dict[str, Any] = {
        "baseline_run_id": "run-1",
        "baseline_key": "base",
        "candidate_keys": ["new"],
        "metrics": ("judge", "checks"),
        "tolerances": {},
        "applies": {"judge": False, "checks": False},
    }
    nothing = baseline_gate(comparison, **options)
    assert nothing["outcome"] == "gate_failed" and "none of the chosen metrics applies" in nothing["detail"]
    failed = baseline_gate({"candidates": []}, error="No attempts found.", **options)
    assert failed["outcome"] == "gate_failed" and failed["detail"] == "the comparison failed: No attempts found."
    pooled = baseline_gate(comparison, remeasured=True, **options)
    assert pooled["outcome"] == "gate_failed" and "pooled into the baseline" in pooled["detail"]
    assert pooled["candidates"] == []


class StuckRun:
    """A client whose run is queued while nothing runs it."""

    def get(self, path: str, params: Any = None) -> dict[str, Any]:
        return {"status": "queued", "active": False, "total": 2, "counts": {"queued": 2}}


def test_a_queued_run_that_nothing_runs_is_reported_at_once():
    with pytest.raises(cli.RunSuiteError, match="not running it"):
        cli._wait_for_run(StuckRun(), "run-1", timeout=3600, poll=0.01)  # type: ignore[arg-type]
