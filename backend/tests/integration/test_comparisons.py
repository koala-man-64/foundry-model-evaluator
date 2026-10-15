"""Comparisons: common cells per metric, cells counted once, currency, ties and unfinished attempts.

A scripted Chat Completions adapter returns fixed usage per (run tag, deployment, prompt), so every
cost can be computed by hand. Rates: good = 1/M input, 10/M output; flaky = 2/M input, 20/M output.
"""

from __future__ import annotations

import random
import time
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from foundry_eval.providers.base import ProviderCall, ProviderResult
from foundry_eval.types import ApiStyle, FinishReason
from tests.api_support import SlowAdapter, create_endpoint, create_profile, set_manual, start_run, wait_for_run

M = Decimal(1_000_000)
FAIL = None
BAD = "bad"  # succeeded, with an unrecognized usage field, so the cost estimate is incomplete

# (run, deployment, prompt) -> (input, output) | FAIL | (input, output, BAD)
TABLE: dict[tuple[str, str, str], Any] = {
    ("r1", "good", "alpha"): (100, 10),
    ("r1", "good", "beta"): (200, 30),
    ("r1", "flaky", "alpha"): (100, 30),
    ("r1", "flaky", "beta"): FAIL,
    ("r2", "good", "alpha"): (100, 50),
    ("r2", "good", "beta"): (200, 40, BAD),
    ("r2", "flaky", "alpha"): FAIL,
    ("r2", "flaky", "beta"): FAIL,
    ("r3", "good", "alpha"): (100, 70),
    ("r3", "good", "beta"): (200, 60),
    ("r3", "flaky", "alpha"): (100, 10),
    ("r3", "flaky", "beta"): (200, 20, BAD),
}


class Scripted:
    api_style = ApiStyle.OPENAI_CHAT

    def __init__(self) -> None:
        self.tag = "r1"

    async def execute(self, call: ProviderCall) -> ProviderResult:
        model = call.body["model"]
        user = call.body["messages"][-1]["content"]
        spec = TABLE[(self.tag, model, user)]
        if spec is FAIL:
            return ProviderResult(
                ok=False,
                request_body=call.body,
                streamed=False,
                latency_ms=5,
                http_status=500,
                error_type="http",
                error_message="scripted failure",
                finish_reason=FinishReason.ERROR,
            )
        usage: dict[str, int] = {"prompt_tokens": spec[0], "completion_tokens": spec[1]}
        if len(spec) == 3:
            usage["mystery_tokens"] = 7
        return ProviderResult(
            ok=True,
            request_body=call.body,
            streamed=False,
            latency_ms=spec[1],
            raw_usage=usage,
            output_text=f"answer to {user}",
            finish_reason=FinishReason.COMPLETED,
            http_status=200,
        )


class ScriptedRng(random.Random):
    """shuffle() reverses the responses (given in candidate order) when the next flag is True."""

    def __init__(self, flags: list[bool]) -> None:
        super().__init__(0)
        self.flags = list(flags)

    def shuffle(self, x: Any) -> None:  # type: ignore[override]
        if self.flags.pop(0):
            x.reverse()


def wait_judging(client: TestClient, run_id: str, timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        evaluations = client.get(f"/api/runs/{run_id}/evaluations").json()
        if evaluations and all(e["status"] not in ("queued", "running") for e in evaluations):
            return evaluations
        time.sleep(0.05)
    raise AssertionError("judging did not finish")


def judge_profile(client: TestClient, endpoint_id: str) -> dict:
    return create_profile(
        client,
        endpoint_id,
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-4.1-judge",
        model_name="gpt-4.1",
        context_window=100_000,
        name="Judge",
    )


def chat_profile(client: TestClient, endpoint_id: str, deployment: str, rates: tuple[str, str] = ("1", "10")) -> dict:
    profile = create_profile(
        client, endpoint_id, preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment=deployment
    )
    set_manual(client, profile["id"], "input_uncached", rates[0])
    set_manual(client, profile["id"], "output", rates[1])
    return profile


def judge(client: TestClient, run_id: str, payload: dict, key: str) -> list[dict]:
    response = client.post(f"/api/runs/{run_id}/evaluations", json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return wait_judging(client, run_id)


def test_averages_use_common_cells_and_count_each_cell_once(client):
    scripted = Scripted()
    services = client.app.state.services
    services.engine.adapters[ApiStyle.OPENAI_CHAT] = scripted
    endpoint = create_endpoint(client)
    good = chat_profile(client, endpoint["id"], "good", ("1", "10"))
    flaky = chat_profile(client, endpoint["id"], "flaky", ("2", "20"))
    judge_id = judge_profile(client, endpoint["id"])["id"]
    request = {
        "prompts": [{"user": "alpha"}, {"user": "beta"}],
        "candidates": [{"profile_id": good["id"]}, {"profile_id": flaky["id"]}],
    }
    # The mock judge prefers label A. One flag per evaluation, in prompt order: r1 and r3 put good
    # first on alpha (good wins); r3 reverses beta (flaky wins). Single-answer groups are not wins.
    flags = {"r1": [False, False], "r2": [False, False], "r3": [False, True]}
    run_ids = []
    for tag in ("r1", "r2", "r3"):
        scripted.tag = tag
        run_id = start_run(client, request, key=f"idem-{tag}-0001")
        wait_for_run(client, run_id)
        services.judge.rng = ScriptedRng(flags[tag])
        judge(client, run_id, {"judge_profile_id": judge_id}, key=f"idem-judge-{tag}")
        run_ids.append(run_id)

    comparison = client.get("/api/comparisons", params={"run_id": run_ids}).json()
    assert comparison["cells_total"] == 2 and comparison["cells_matched"] == 2
    by = {c["label"]: c for c in comparison["candidates"]}

    def good_cost(i: int, o: int) -> Decimal:
        return (Decimal(i) * 1 + Decimal(o) * 10) / M

    def flaky_cost(i: int, o: int) -> Decimal:
        return (Decimal(i) * 2 + Decimal(o) * 20) / M

    # Flaky has no complete cost on beta, so both cost averages use alpha only.
    good_alpha = (good_cost(100, 10) + good_cost(100, 50) + good_cost(100, 70)) / 3
    flaky_alpha = (flaky_cost(100, 30) + flaky_cost(100, 10)) / 2
    assert Decimal(by["good"]["avg_cost"]) == pytest.approx(good_alpha, abs=Decimal("1e-30"))
    assert Decimal(by["flaky"]["avg_cost"]) == flaky_alpha
    assert by["good"]["cost_cells"] == by["flaky"]["cost_cells"] == 1
    assert by["good"]["cost_excluded"] == 1 and by["flaky"]["cost_excluded"] == 1
    assert by["good"]["cost_other_currency"] == 0

    # Tokens are known in both cells for both: each cell's mean counts once.
    assert by["good"]["token_cells"] == by["flaky"]["token_cells"] == 2
    assert by["good"]["avg_output_tokens"] == pytest.approx(((10 + 50 + 70) / 3 + (30 + 40 + 60) / 3) / 2)
    assert by["flaky"]["avg_output_tokens"] == pytest.approx(((30 + 10) / 2 + 20) / 2)
    assert by["good"]["latency_cells"] == 2

    # Judge scores (label A 4.25, B 4.0, a single answer 4.25), averaged per cell, then across cells.
    assert by["good"]["avg_judge_score"] == pytest.approx((4.25 + (4.25 + 4.25 + 4.0) / 3) / 2)
    assert by["flaky"]["avg_judge_score"] == pytest.approx((4.0 + 4.25) / 2)
    assert by["good"]["judge_scored"] == by["flaky"]["judge_scored"] == 2

    # Win rate per cell: good wins alpha twice (one cell), flaky wins beta once.
    for label in ("good", "flaky"):
        assert by[label]["win_rate"] == pytest.approx(0.5)
        assert by[label]["win_cells"] == 2 and by[label]["judge_comparisons"] == 3
    assert by["good"]["judge_wins"] == pytest.approx(1.0)

    assert by["flaky"]["failures"] == 3 and by["good"]["failures"] == 0
    assert by["flaky"]["pending"] == 0
    assert by["flaky"]["attempts"] == 6 and by["flaky"]["succeeded"] == 3


def test_costs_in_another_currency_are_excluded_and_run_totals_keep_their_currency(client):
    endpoint = create_endpoint(client)
    first = chat_profile(client, endpoint["id"], "deepseek-a")
    second = chat_profile(client, endpoint["id"], "deepseek-b")
    request = {
        "prompts": [{"user": "Hello"}],
        "candidates": [{"profile_id": first["id"]}, {"profile_id": second["id"]}],
    }
    run_id = start_run(client, request)
    wait_for_run(client, run_id)
    usd = client.get("/api/comparisons", params={"run_id": run_id}).json()
    assert all(c["avg_cost"] is not None for c in usd["candidates"])

    response = client.put("/api/settings", json={"currency": "EUR"})
    assert response.status_code == 200, response.text
    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    assert comparison["currency"] == "EUR"
    for candidate in comparison["candidates"]:
        assert candidate["avg_cost"] is None and candidate["cost_cells"] == 0
        assert candidate["cost_other_currency"] == 1 and candidate["cost_excluded"] == 0
    assert any("2 cost estimates in another currency were excluded" in note for note in comparison["notes"])

    # A run keeps the currency it was priced in, so its totals stay readable after the switch.
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["generation_cost"]["currency"] == "USD"
    assert detail.json()["generation_cost"]["status"] == "complete"
    listing = client.get("/api/runs")
    assert listing.status_code == 200, listing.text


def test_a_tie_gives_each_judged_answer_an_equal_share(client):
    endpoint = create_endpoint(client)
    profiles = [chat_profile(client, endpoint["id"], f"deepseek-{name}") for name in ("x", "y", "z")]
    judge_id = judge_profile(client, endpoint["id"])["id"]
    # With three criteria the mock judge scores every answer 4.0, so each evaluation is a tie.
    rubric = client.post(
        "/api/rubrics",
        json={
            "name": "Three criteria",
            "definition": {"criteria": [{"key": k, "name": k.title()} for k in ("accuracy", "tone", "brevity")]},
        },
    )
    assert rubric.status_code == 201, rubric.text
    request = {
        "prompts": [{"user": "One"}, {"user": "Two"}],
        "candidates": [{"profile_id": p["id"]} for p in profiles],
    }
    run_id = start_run(client, request)
    wait_for_run(client, run_id)
    evaluations = judge(
        client, run_id, {"judge_profile_id": judge_id, "rubric_id": rubric.json()["id"]}, key="idem-judge-tie"
    )
    assert [e["preferred"] for e in evaluations] == ["tie", "tie"]

    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    for candidate in comparison["candidates"]:
        assert candidate["win_rate"] == pytest.approx(1 / 3)
        assert candidate["judge_wins"] == pytest.approx(2 / 3)
    assert sum(c["judge_wins"] for c in comparison["candidates"]) == pytest.approx(2)
    # Charts colour points by publisher: the profile's, else one derived from the API style.
    assert {(c["publisher"], c["api_style"]) for c in comparison["candidates"]} == {("Other", "openai_chat")}


def test_candidates_carry_their_publisher(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client,
        endpoint["id"],
        preset="generic-chat",
        api_style=ApiStyle.OPENAI_CHAT,
        deployment="deepseek-v",
        publisher="DeepSeek",
    )
    run_id = start_run(client, {"prompts": [{"user": "Hi"}], "candidates": [{"profile_id": profile["id"]}]})
    wait_for_run(client, run_id)
    [candidate] = client.get("/api/comparisons", params={"run_id": run_id}).json()["candidates"]
    assert candidate["publisher"] == "DeepSeek"


def test_unfinished_attempts_are_pending_not_failures(client):
    services = client.app.state.services
    services.engine.adapters[ApiStyle.OPENAI_CHAT] = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=5)
    endpoint = create_endpoint(client)
    profiles = [chat_profile(client, endpoint["id"], f"deepseek-{name}") for name in ("p", "q")]
    request = {"prompts": [{"user": "Hello"}], "candidates": [{"profile_id": p["id"]} for p in profiles]}
    run_id = start_run(client, request)

    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    for candidate in comparison["candidates"]:
        assert candidate["pending"] == 1 and candidate["failures"] == 0

    assert client.post(f"/api/runs/{run_id}/cancel", json={}).status_code == 200
    wait_for_run(client, run_id)
    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    for candidate in comparison["candidates"]:
        assert candidate["pending"] == 0 and candidate["failures"] == 1
