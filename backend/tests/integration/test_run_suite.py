"""Headless ``run-suite``: the command line against a running test app wired to the mock Foundry server.

The command reaches the app through the test client with the web interface's default headers removed,
so every request it makes carries the ``Origin`` and ``X-FME-Client`` headers it sends itself.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from foundry_eval import cli
from foundry_eval.app_client import AppClient
from foundry_eval.devtools.mock_foundry import create_mock_app
from foundry_eval.domain.profiles import ProfileFields
from foundry_eval.types import ApiStyle
from tests.api_support import create_endpoint, create_profile, make_client, set_manual

# Six prompts of different lengths: enough for a sign-test verdict, and costs that differ per prompt.
PROMPTS = [
    "Name a colour.",
    "What is the capital of France?",
    "Explain in one sentence why the sky looks blue during the day.",
    "List three prime numbers above ten and say which one is the largest.",
    "Summarise the plot of a heist movie in two short sentences, without naming the movie.",
    "Describe how a bicycle gear works to someone who has never ridden a bicycle, in plain words.",
]
# The mock answers in one style per deployment: deepseek-base and deepseek-v2 answer step by step,
# deepseek-new answers in detail, so this check passes for the first two and fails for the last.
STEP_BY_STEP = {"kind": "contains", "value": "step-by-step"}


@contextmanager
def without_spa_headers(client: TestClient) -> Iterator[None]:
    saved = {name: client.headers.pop(name) for name in ("Origin", "X-FME-Client")}
    try:
        yield
    finally:
        client.headers.update(saved)


def chat_profile(client: TestClient, endpoint_id: str, deployment: str, rates: tuple[str, str] = ("1", "10")) -> dict:
    profile = create_profile(
        client, endpoint_id, preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment=deployment
    )
    set_manual(client, profile["id"], "input_uncached", rates[0])
    set_manual(client, profile["id"], "output", rates[1])
    return profile


def create_suite(client: TestClient, profiles: list[dict], name: str = "Regression", **extra: Any) -> dict:
    payload = {
        "name": name,
        "prompts": [{"title": f"Q{i + 1}", "user": text, "checks": [STEP_BY_STEP]} for i, text in enumerate(PROMPTS)],
        "candidates": [{"profile_id": p["id"], "settings": {"max_output_tokens": 64}} for p in profiles],
        **extra,
    }
    response = client.post("/api/suites", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def chat_calls(mock) -> int:
    return sum(count for path, count in mock.state.mock.requests.items() if path.endswith("/chat/completions"))


@pytest.fixture
def mock():
    return create_mock_app()


@pytest.fixture
def app(tmp_path, monkeypatch, mock) -> Iterator[TestClient]:
    """The running app (a test client); the command reaches it through the same client."""
    monkeypatch.setenv("FME_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("FME_PORT", raising=False)
    with make_client(tmp_path / "data", mock_app=mock) as client:
        monkeypatch.setattr(cli, "_app_client", lambda config: AppClient(config.origin, http=client))
        yield client


def run_cli(client: TestClient, capsys, *argv: str) -> tuple[int, dict, str]:
    with without_spa_headers(client):
        code = cli.main(["run-suite", *argv, "--poll-interval", "0.02", "--timeout", "30"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def outcomes(report: dict) -> dict[str, str]:
    return {gate["gate"]: gate["outcome"] for gate in report["gates"]}


def test_run_suite_passes_and_a_repeated_run_key_replays_without_new_calls(app, mock, capsys):
    endpoint = create_endpoint(app)
    profiles = [chat_profile(app, endpoint["id"], name) for name in ("deepseek-base", "deepseek-v2")]
    suite = create_suite(app, profiles)
    with without_spa_headers(app):  # what the command must add itself
        assert app.post(f"/api/suites/{suite['id']}/preview", json={}).status_code == 403

    args = ["--suite", "Regression", "--budget", "1", "--run-key", "build-1"]
    gates = ["--fail-if", "cost-upper-above=1", "--fail-if", "incomplete-cost"]
    code, report, err = run_cli(app, capsys, *args, *gates)
    assert code == 0, (report, err)
    assert report["outcome"] == "passed" and report["exit_code"] == 0
    assert outcomes(report) == {"failures-above": "passed", "cost-upper-above": "passed", "incomplete-cost": "passed"}
    run = report["run"]
    assert run["created"] is True and run["status"] == "completed"
    assert run["url"] == f"http://127.0.0.1:8765/runs/{run['id']}"
    assert run["attempts"] == {"total": 12, "by_status": {"succeeded": 12}}
    assert report["preview"]["calls"] == 12 and report["preview"]["budget"] == "1"
    assert report["cost"]["status"] == "complete" and report["cost"]["budget"] == "1"
    assert Decimal(report["cost"]["upper"]) == Decimal(report["cost"]["total"]) <= 1
    assert chat_calls(mock) == 12
    assert "12/12 attempts finished" in err and "Result: passed" in err
    detail = app.get(f"/api/runs/{run['id']}").json()
    assert detail["run"]["idempotency_key"] == cli.suite_run_key(suite["id"], None, "build-1")
    assert detail["run"]["title"] == "Regression (build-1)" and detail["run"]["kind"] == "suite"

    # The same run key returns the same run: nothing is sent again, and the gates are checked again.
    code, replay, err = run_cli(app, capsys, *args, *gates)
    assert code == 0 and replay["run"]["id"] == run["id"] and replay["run"]["created"] is False
    assert "used before" in err
    assert chat_calls(mock) == 12
    assert len(app.get("/api/runs").json()) == 1


def test_baseline_gate_fails_on_a_regression_and_tolerances_relax_it(app, mock, capsys):
    endpoint = create_endpoint(app)
    base = chat_profile(app, endpoint["id"], "deepseek-base")
    pricier = chat_profile(app, endpoint["id"], "deepseek-new", rates=("2", "20"))
    same = chat_profile(app, endpoint["id"], "deepseek-v2")
    suite = create_suite(app, [base, pricier, same])

    code, baseline, _ = run_cli(
        app, capsys, "--suite", suite["id"], "--budget", "1", "--run-key", "base-1", "--candidate", "deepseek-base"
    )
    assert code == 0, baseline
    assert [c["label"] for c in baseline["run"]["candidates"]] == ["deepseek-base"]
    assert chat_calls(mock) == 6
    baseline_id = baseline["run"]["id"]

    # Running the baseline candidate again would pool it into the frozen baseline: refused before spending.
    full = ["--suite", "Regression", "--budget", "1", "--run-key", "full-1", "--baseline-run", baseline_id]
    code, refused, _ = run_cli(app, capsys, *full)
    assert code == 2 and "would be the baseline candidate again" in refused["error"] and chat_calls(mock) == 6
    # So is a suite that shares no prompt with the baseline run.
    create_suite(app, [pricier], name="Other prompts", prompts=[{"user": "Something else entirely"}])
    other = ["--suite", "Other prompts", "--budget", "1", "--run-key", "other-1", "--baseline-run", baseline_id]
    code, refused, _ = run_cli(app, capsys, *other)
    assert code == 2 and "None of the suite's prompts" in refused["error"] and chat_calls(mock) == 6

    args = ["--suite", "Regression", "--budget", "1", "--run-key", "new-1", "--baseline-run", baseline_id]
    args += ["--candidate", "deepseek-new", "--candidate", "deepseek-v2"]
    code, report, err = run_cli(app, capsys, *args)
    assert chat_calls(mock) == 18  # only the two chosen candidates ran
    assert code == 1 and report["outcome"] == "gate_failed", (report, err)
    gate = report["gates"][-1]
    assert gate["gate"] == "baseline" and gate["baseline_run_id"] == baseline_id
    assert gate["baseline_candidate"]["label"] == "deepseek-base"
    rows = {row["label"]: row["metrics"] for row in gate["candidates"]}
    assert rows["deepseek-new"]["cost"]["status"] == "regressed"
    assert rows["deepseek-new"]["cost"]["verdict"] == "higher"
    assert rows["deepseek-new"]["checks"]["status"] == "regressed"
    assert rows["deepseek-new"]["checks"]["verdict"] == "lower" and rows["deepseek-new"]["checks"]["prompts"] == 6
    assert rows["deepseek-new"]["judge"]["status"] == "not_applicable"
    assert {m["status"] for m in rows["deepseek-v2"].values()} == {"ok", "not_applicable"}
    assert "deepseek-new cost: regressed" in err

    # The same run key with tolerances: no new calls, and the regressions are within them.
    code, relaxed, err = run_cli(app, capsys, *args, "--tolerance", "cost=1", "--tolerance", "checks=1")
    assert code == 0, (relaxed, err)
    assert relaxed["run"]["id"] == report["run"]["id"] and chat_calls(mock) == 18
    metrics = {row["label"]: row["metrics"] for row in relaxed["gates"][-1]["candidates"]}["deepseek-new"]
    assert metrics["cost"]["status"] == "ok" and "within the tolerance" in metrics["cost"]["reason"]
    assert metrics["cost"]["tolerance"] == "1"

    # Only the judge is chosen, and neither run was judged: nothing was compared, which is not a pass.
    code, judged, _ = run_cli(app, capsys, *args, "--baseline-metrics", "judge")
    assert code == 1 and "none of the chosen metrics applies" in judged["gates"][-1]["detail"]
    assert set(judged["gates"][-1]["candidates"][0]["metrics"]) == {"judge"}


def test_a_profile_pointed_at_a_new_deployment_is_compared_with_its_frozen_baseline(app, mock, capsys):
    endpoint = create_endpoint(app)
    profile = chat_profile(app, endpoint["id"], "deepseek-base")
    create_suite(app, [profile])
    code, baseline, _ = run_cli(app, capsys, "--suite", "Regression", "--budget", "1", "--run-key", "old-deployment")
    assert code == 0, baseline

    # Same profile and settings, new deployment: a different candidate, so not the baseline run again.
    fields = {name: value for name, value in profile.items() if name in ProfileFields.model_fields}
    response = app.put(f"/api/profiles/{profile['id']}", json={**fields, "deployment": "deepseek-new"})
    assert response.status_code == 200, response.text
    args = ["--suite", "Regression", "--budget", "1", "--run-key", "new-deployment"]
    code, report, err = run_cli(app, capsys, *args, "--baseline-run", baseline["run"]["id"])
    assert chat_calls(mock) == 12, err
    [row] = report["gates"][-1]["candidates"]
    assert row["candidate_key"] != report["gates"][-1]["baseline_candidate"]["candidate_key"]
    # The new deployment answers in another style, so its checks regress against the old one.
    assert code == 1 and row["metrics"]["checks"]["status"] == "regressed", report


def judge_profile(client: TestClient, endpoint_id: str, deployment: str) -> dict:
    profile = create_profile(
        client,
        endpoint_id,
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment=deployment,
        model_name="gpt-4.1",
        context_window=100_000,
    )
    for category, price in (("input_uncached", "2"), ("cache_read", "0.5"), ("output", "8")):
        set_manual(client, profile["id"], category, price)
    return profile


def test_judged_runs_compare_judge_scores_and_failed_judging_counts(app, mock, capsys):
    endpoint = create_endpoint(app)
    base = chat_profile(app, endpoint["id"], "deepseek-base")
    same = chat_profile(app, endpoint["id"], "deepseek-v2")
    judging = {"enabled": True, "settings": {"max_output_tokens": 2000}}
    good = judge_profile(app, endpoint["id"], "gpt-4.1-judge")
    create_suite(app, [base, same], judge={**judging, "judge_profile_id": good["id"]})
    first = ["--suite", "Regression", "--budget", "1", "--run-key", "judged-base", "--candidate", "deepseek-base"]
    code, baseline, err = run_cli(app, capsys, *first)
    assert code == 0, (baseline, err)
    assert baseline["run"]["judging"]["by_status"] == {"succeeded": 6}
    assert Decimal(baseline["cost"]["judging"]["total"]) > 0

    second = ["--suite", "Regression", "--budget", "1", "--run-key", "judged-new", "--candidate", "deepseek-v2"]
    code, report, err = run_cli(app, capsys, *second, "--baseline-run", baseline["run"]["id"])
    assert code == 0, (report, err)
    judge = report["gates"][-1]["candidates"][0]["metrics"]["judge"]
    assert judge["status"] == "ok" and judge["prompts"] == 6, judge

    # A judge whose output cannot be read leaves every prompt without a judge result.
    broken = judge_profile(app, endpoint["id"], "gpt-4.1-judge-malformed")
    create_suite(app, [base], name="Broken judge", judge={**judging, "judge_profile_id": broken["id"]})
    code, report, _ = run_cli(app, capsys, "--suite", "Broken judge", "--budget", "1", "--run-key", "broken-1")
    assert code == 3 and report["outcome"] == "attempts_failed"
    [failures] = report["gates"]
    assert failures["value"] == 6 and "6 of 6 judged prompts and trials have no judge result" in failures["detail"]


def test_failed_attempts_exit_3_unless_the_gate_allows_them(app, mock, capsys):
    endpoint = create_endpoint(app)
    profiles = [chat_profile(app, endpoint["id"], name) for name in ("deepseek-base", "fail-500")]
    create_suite(app, profiles)
    args = ["--suite", "Regression", "--budget", "1", "--run-key", "flaky-1"]

    code, report, _ = run_cli(app, capsys, *args)
    assert code == 3 and report["outcome"] == "attempts_failed"
    [failures] = report["gates"]
    assert failures["value"] == 6 and failures["limit"] == 0 and "6 failed" in failures["detail"]
    assert report["run"]["status"] == "completed_with_errors"

    code, allowed, _ = run_cli(app, capsys, *args, "--fail-if", "failures-above=6")
    assert code == 0 and allowed["run"]["id"] == report["run"]["id"] and chat_calls(mock) == 12


def test_an_incomplete_cost_is_its_own_outcome_and_never_zero(app, mock, capsys):
    endpoint = create_endpoint(app)
    create_suite(app, [chat_profile(app, endpoint["id"], "deepseek-no-usage")])
    args = ["--suite", "Regression", "--budget", "1", "--run-key", "no-usage-1"]

    code, report, _ = run_cli(app, capsys, *args, "--fail-if", "incomplete-cost")
    assert code == 4 and report["outcome"] == "cost_incomplete"
    cost = report["cost"]
    assert cost["status"] == "incomplete" and cost["total"] is None and cost["incomplete"] == 6
    assert Decimal(cost["upper"]) == Decimal(cost["budget_committed"]) > 0  # bounded by the reservations

    # The upper bound is known, so a cost limit can still be decided; the incomplete cost outranks it.
    code, limited, _ = run_cli(app, capsys, *args, "--fail-if", "cost-upper-above=1")
    assert code == 0 and outcomes(limited)["cost-upper-above"] == "passed"
    code, both, _ = run_cli(
        app, capsys, *args, "--fail-if", "cost-upper-above=0.000001", "--fail-if", "incomplete-cost"
    )
    assert code == 4 and outcomes(both) == {
        "failures-above": "passed",
        "cost-upper-above": "gate_failed",
        "incomplete-cost": "cost_incomplete",
    }
    assert chat_calls(mock) == 6


def test_refusals_spend_nothing(app, mock, capsys):
    endpoint = create_endpoint(app)
    profile = chat_profile(app, endpoint["id"], "deepseek-base")
    suite = create_suite(app, [profile])
    base = ["--suite", "Regression", "--budget", "1", "--run-key", "refused-1"]
    cases = [
        (["--suite", "Regression", "--budget", "0", "--run-key", "k"], "--budget must be greater than zero"),
        (["--suite", "Regression", "--budget", "lots", "--run-key", "k"], "--budget must be a decimal amount"),
        (["--suite", "Nope", "--budget", "1", "--run-key", "k"], "No suite has the id or name 'Nope'"),
        ([*base, "--candidate", "other"], "is not a candidate of this suite"),
        ([*base, "--tolerance", "cost=0.1"], "need --baseline-run"),
        ([*base, "--fail-if", "latency-above=3"], "unknown gate"),
        ([*base, "--baseline-run", "no-such-run"], "The baseline run 'no-such-run' does not exist"),
    ]
    for argv, message in cases:
        code, report, err = run_cli(app, capsys, *argv)
        assert code == 2 and report["outcome"] == "error" and message in report["error"], (argv, report)
        assert message in err

    assert app.delete(f"/api/endpoints/{endpoint['id']}/key").status_code == 200
    code, report, _ = run_cli(app, capsys, *base)
    assert code == 2 and "The app refused the run" in report["error"] and "no API key" in report["error"]
    assert report["preview"]["errors"] and report["suite"]["id"] == suite["id"]

    assert app.post(f"/api/suites/{suite['id']}/archive", json={"archived": True}).status_code == 200
    code, report, _ = run_cli(app, capsys, *base)
    assert code == 2 and "archived" in report["error"]
    assert chat_calls(mock) == 0 and app.get("/api/runs").json() == []


def test_the_budget_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run-suite", "--suite", "x", "--run-key", "k"])
    assert exc.value.code == 2 and "--budget" in capsys.readouterr().err


def test_a_stopped_app_is_reported_without_a_retry(tmp_path, monkeypatch, capsys):
    with socket.socket() as sock:  # a free port that nothing listens on once this socket is closed
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("FME_DATA_DIR", str(tmp_path / "data"))
    code = cli.main(["run-suite", "--suite", "x", "--budget", "1", "--run-key", "k", "--port", str(port)])
    captured = capsys.readouterr()
    assert code == 2 and json.loads(captured.out)["outcome"] == "error"
    assert f"The app is not running at http://127.0.0.1:{port}" in captured.err


def test_suite_runs_can_choose_candidates_and_keep_their_labels(client: TestClient, mock_app):
    endpoint = create_endpoint(client)
    profile = chat_profile(client, endpoint["id"], "deepseek-base")
    payload = {
        "name": "Settings",
        "prompts": [{"user": "Hello"}],
        "candidates": [
            {"profile_id": profile["id"], "settings": {"max_output_tokens": 64, "temperature": 0.2}},
            {"profile_id": profile["id"], "settings": {"max_output_tokens": 64, "temperature": 0.8}},
        ],
    }
    suite = client.post("/api/suites", json=payload).json()
    first, second = (c["id"] for c in suite["candidates"])
    path = f"/api/suites/{suite['id']}"

    preview = client.post(f"{path}/preview", json={"candidate_ids": [second]}).json()
    assert preview["ok"] and preview["calls"] == 1
    [chosen] = preview["candidates"]
    assert chosen["label"] == "deepseek-base (temperature=0.8)" and len(chosen["candidate_key"]) == 16
    for body, message in (
        ({"candidate_ids": ["nope"]}, "Not candidates of this suite: nope"),
        ({"candidate_ids": [first, first]}, "only once"),
        ({"candidate_ids": []}, "at least 1"),
    ):
        response = client.post(f"{path}/runs", json=body, headers={"Idempotency-Key": "idem-choose-bad"})
        assert response.status_code == 422 and message in response.text, response.text
    assert client.get("/api/runs").json() == []

    response = client.post(f"{path}/runs", json={"candidate_ids": [second]}, headers={"Idempotency-Key": "idem-pick"})
    assert response.status_code == 201, response.text
    detail = client.get(f"/api/runs/{response.json()['run_id']}").json()
    assert [c["label"] for c in detail["candidates"]] == ["deepseek-base (temperature=0.8)"]
    assert detail["candidates"][0]["candidate_key"] == chosen["candidate_key"]  # the preview's key is the run's
    assert len(detail["attempts"]) == 1
