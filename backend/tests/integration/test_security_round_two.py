"""Regression tests for the findings of the second security review."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from foundry_eval import cli
from foundry_eval.providers.base import ProviderCall, ProviderResult
from foundry_eval.types import ApiStyle, FinishReason
from tests.api_support import SlowAdapter, create_endpoint, create_profile, set_manual, start_run, wait_for_run

ADVERSARIAL = {
    "padded fence": "```json" + " " * 20_000 + "x",
    "huge number": '{"responses": [], "preferred": ' + "9" * 5000 + "}",
    "deep nesting": "[" * 100_000 + "]" * 100_000,
}


class ScriptedJudge:
    """A judge that answers with the given text."""

    api_style = ApiStyle.OPENAI_RESPONSES

    def __init__(self, text: str) -> None:
        self.text = text

    async def execute(self, call: ProviderCall) -> ProviderResult:
        return ProviderResult(
            ok=True,
            request_body=call.body,
            streamed=False,
            latency_ms=5,
            raw_usage={"input_tokens": 500, "output_tokens": 50},
            output_text=self.text,
            finish_reason=FinishReason.COMPLETED,
            http_status=200,
        )


def judged_run(client: TestClient) -> tuple[str, str]:
    endpoint = create_endpoint(client)
    candidates = []
    for name in ("deepseek-a", "deepseek-b"):
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment=name
        )
        set_manual(client, profile["id"], "input_uncached", "1")
        set_manual(client, profile["id"], "output", "10")
        candidates.append({"profile_id": profile["id"]})
    judge = create_profile(
        client,
        endpoint["id"],
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-4.1-judge",
        model_name="gpt-4.1",
        context_window=100_000,
    )
    run_id = start_run(client, {"prompts": [{"user": "Hello"}], "candidates": candidates})
    wait_for_run(client, run_id)
    return run_id, judge["id"]


def finished_evaluations(client: TestClient, run_id: str, count: int) -> list[dict]:
    deadline = time.time() + 20
    while time.time() < deadline:
        found = client.get(f"/api/runs/{run_id}/evaluations").json()
        if len(found) == count and all(e["status"] not in ("queued", "running") for e in found):
            return found
        time.sleep(0.05)
    raise AssertionError("judging did not finish")


def test_adversarial_judge_output_is_kept_as_malformed_without_stalling(client):
    run_id, judge_id = judged_run(client)
    adapters = client.app.state.services.engine.adapters
    started = time.perf_counter()
    for index, text in enumerate(ADVERSARIAL.values()):
        adapters[ApiStyle.OPENAI_RESPONSES] = ScriptedJudge(text)
        response = client.post(
            f"/api/runs/{run_id}/evaluations",
            json={"judge_profile_id": judge_id},
            headers={"Idempotency-Key": f"idem-adversarial-{index}"},
        )
        assert response.status_code == 201, response.text
        finished_evaluations(client, run_id, index + 1)
    assert time.perf_counter() - started < 10
    evaluations = client.get(f"/api/runs/{run_id}/evaluations").json()
    for evaluation, text in zip(evaluations, ADVERSARIAL.values(), strict=True):
        assert evaluation["status"] == "malformed", evaluation["error_message"]
        assert evaluation["parse_errors"] and evaluation["output_text"] == text  # kept for inspection
        assert evaluation["usage"]["input_total"] == 500  # usage is recorded, not lost to an internal error


@pytest.mark.parametrize("value", ["1E-50000000", "1E-999999999999", "1E+30000000", "0.000000000000000000001"])
def test_entered_amounts_are_bounded(client, value):
    response = client.post("/api/projections", json={"run_ids": ["x"], "what_if": {"input_multiplier": value}})
    assert response.status_code == 422
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="d"
    )
    request = {"prompts": [{"user": "Hello"}], "candidates": [{"profile_id": profile["id"]}], "budget": value}
    response = client.post("/api/runs", json=request, headers={"Idempotency-Key": "idem-bounded-0001"})
    assert response.status_code == 422 and "budget" in response.text
    rate = {"mode": "manual", "price": value, "unit": "1M", "source_url": "https://example.com/pricing"}
    assert client.put(f"/api/profiles/{profile['id']}/rates/output", json=rate).status_code == 422


def test_ordinary_amounts_are_still_accepted(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="d"
    )
    rate = {"mode": "manual", "price": "0.0000025", "unit": "1K", "source_url": "https://example.com/pricing"}
    assert client.put(f"/api/profiles/{profile['id']}/rates/output", json=rate).status_code == 200
    response = client.post("/api/projections", json={"run_ids": ["x"], "what_if": {"input_multiplier": "1.25"}})
    assert response.status_code == 404  # validated, then the run is not found


def test_the_cli_rejects_unbounded_budgets(capsys):
    assert cli.main(["smoke", "--profile", "x", "--budget", "1E-50000000"]) == 2
    assert "--budget must be a decimal amount" in capsys.readouterr().err


def test_label_order_is_revealed_only_after_judging(client):
    run_id, judge_id = judged_run(client)
    slow = SlowAdapter(ApiStyle.OPENAI_RESPONSES, delay=1.0)
    client.app.state.services.engine.adapters[ApiStyle.OPENAI_RESPONSES] = slow
    response = client.post(
        f"/api/runs/{run_id}/evaluations", json={"judge_profile_id": judge_id}, headers={"Idempotency-Key": "idem-o-01"}
    )
    assert response.status_code == 201, response.text
    deadline = time.time() + 5
    while slow.active < 1 and time.time() < deadline:
        time.sleep(0.02)
    [running] = client.get(f"/api/runs/{run_id}/evaluations").json()
    assert running["status"] == "running"
    assert running["order"] == [] and running["candidate_labels"] == {}
    single = client.get(f"/api/evaluations/{running['id']}").json()
    assert single["order"] == [] and single["candidate_labels"] == {}
    [done] = finished_evaluations(client, run_id, 1)
    assert [entry["label"] for entry in done["order"]] == ["A", "B"] and len(done["candidate_labels"]) == 2
