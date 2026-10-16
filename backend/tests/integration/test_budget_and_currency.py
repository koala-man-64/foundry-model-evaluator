"""Budget accounting and currency handling: the judge preview bound, restarts, reservations, currencies."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from decimal import Decimal

from fastapi.testclient import TestClient

from foundry_eval.execution.budget import input_token_upper_bound
from foundry_eval.types import ApiStyle
from tests.api_support import create_endpoint, create_profile, make_client, set_manual, start_run, wait_for_run

CLAUDE_RATES = {
    "input_uncached": "3",
    "cache_read": "0.3",
    "cache_write_5m": "3.75",
    "cache_write_1h": "6",
    "output": "15",
}


def claude(client: TestClient, endpoint_id: str, deployment: str) -> dict:
    profile = create_profile(
        client, endpoint_id, preset="claude-adaptive", api_style=ApiStyle.ANTHROPIC_MESSAGES, deployment=deployment
    )
    for category, price in CLAUDE_RATES.items():
        set_manual(client, profile["id"], category, price)
    return profile


def chat(client: TestClient, endpoint_id: str, deployment: str) -> dict:
    profile = create_profile(
        client, endpoint_id, preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment=deployment
    )
    set_manual(client, profile["id"], "input_uncached", "1")
    set_manual(client, profile["id"], "output", "10")
    return profile


def budgeted_claude_run(client: TestClient, profile_id: str, budget: str, key: str = "idem-run-0001") -> str:
    request = {
        "prompts": [{"user": "hi"}],
        "candidates": [
            {"profile_id": profile_id, "settings": {"max_output_tokens": 50}},
            {"profile_id": profile_id, "settings": {"max_output_tokens": 60}},
        ],
        "budget": budget,
    }
    run_id = start_run(client, request, key=key)
    wait_for_run(client, run_id)
    return run_id


def wait_evaluations(client: TestClient, run_id: str) -> list[dict]:
    deadline = time.time() + 20
    while time.time() < deadline:
        found = client.get(f"/api/runs/{run_id}/evaluations").json()
        if found and all(e["status"] not in ("queued", "running") for e in found):
            return found
        time.sleep(0.05)
    raise AssertionError("judging did not finish")


def test_judge_preview_shows_the_worst_case_the_engine_reserves(client):
    endpoint = create_endpoint(client)
    candidate = claude(client, endpoint["id"], "claude-cand")
    judge = claude(client, endpoint["id"], "claude-judge")
    run_id = budgeted_claude_run(client, candidate["id"], "0.5")
    payload = {"judge_profile_id": judge["id"], "settings": {"max_output_tokens": 1000}}
    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json=payload).json()
    assert preview["ok"] and preview["calls"] == 1, preview
    assert not any("remaining budget" in w for w in preview["warnings"])
    response = client.post(f"/api/runs/{run_id}/evaluations", json=payload, headers={"Idempotency-Key": "idem-j-01"})
    assert response.status_code == 201, response.text
    [evaluation] = wait_evaluations(client, run_id)
    assert evaluation["status"] == "succeeded"
    # The body (structured-output schema included) bounds the input, as in the engine's reservation.
    assert Decimal(evaluation["budget_reserved"]) == Decimal(preview["worst_case_total"])


def test_generation_preview_shows_the_worst_case_the_engine_reserves(client):
    endpoint = create_endpoint(client)
    profile = claude(client, endpoint["id"], "claude-cand")
    request = {
        "prompts": [
            {"user": "hi"},
            {"system": "Be brief.", "user": 'A longer prompt with "quotes",\nnewlines and tabs\tthat JSON escapes.'},
        ],
        "candidates": [{"profile_id": profile["id"], "settings": {"max_output_tokens": 50}}],
        "budget": "0.5",
    }
    preview = client.post("/api/runs/preview", json=request).json()
    assert preview["ok"], preview
    worst = Decimal(preview["candidates"][0]["worst_case_per_call"])
    run_id = start_run(client, request, key="idem-gen-preview-01")
    detail = wait_for_run(client, run_id)
    # Each call reserves against its stored request body; the preview shows the largest of them.
    reserved = sorted(Decimal(a["budget_reserved"]) for a in detail["attempts"])
    assert reserved[-1] == worst and reserved[0] < worst
    # The body bound covers what the bare prompt text misses (settings, framing keys, JSON escapes).
    longer = request["prompts"][1]
    text_only = input_token_upper_bound(longer["system"], longer["user"])
    per_million = Decimal(1_000_000)
    text_only_worst = text_only * Decimal(CLAUDE_RATES["cache_write_1h"]) / per_million + 50 * Decimal(15) / per_million
    assert worst > text_only_worst


def test_judge_preview_warns_when_the_worst_case_exceeds_the_remaining_budget(client):
    endpoint = create_endpoint(client)
    candidate = claude(client, endpoint["id"], "claude-cand")
    judge = claude(client, endpoint["id"], "claude-judge")
    run_id = budgeted_claude_run(client, candidate["id"], "0.036")
    committed = Decimal(client.get(f"/api/runs/{run_id}").json()["budget"]["committed"])
    payload = {"judge_profile_id": judge["id"], "settings": {"max_output_tokens": 1000}}
    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json=payload).json()
    assert Decimal(preview["worst_case_total"]) > Decimal("0.036") - committed
    assert any("above the run's remaining budget" in w for w in preview["warnings"]), preview["warnings"]
    response = client.post(f"/api/runs/{run_id}/evaluations", json=payload, headers={"Idempotency-Key": "idem-j-02"})
    assert response.status_code == 201, response.text
    [evaluation] = wait_evaluations(client, run_id)
    assert evaluation["status"] == "skipped_budget" and evaluation["cost"]["status"] == "not_sent"


def test_budget_spending_is_shown_after_a_restart(tmp_path):
    data = tmp_path / "data"
    with make_client(data) as client:
        endpoint = create_endpoint(client)
        candidate = claude(client, endpoint["id"], "claude-cand")
        run_id = budgeted_claude_run(client, candidate["id"], "0.5")
        before = client.get(f"/api/runs/{run_id}").json()
    with make_client(data) as client:
        after = client.get(f"/api/runs/{run_id}").json()
    assert after["budget"]["committed"] == before["budget"]["committed"]
    assert Decimal(after["budget"]["committed"]) == Decimal(after["generation_cost"]["total"]) > 0


def test_a_sent_call_without_usage_is_bounded_by_its_reservation(client):
    endpoint = create_endpoint(client)
    profile = chat(client, endpoint["id"], "deepseek-no-usage")
    request = {
        "prompts": [{"user": "Hello"}],
        "candidates": [{"profile_id": profile["id"], "settings": {"max_output_tokens": 100}}],
        "budget": "0.01",
    }
    run_id = start_run(client, request)
    detail = wait_for_run(client, run_id)
    [attempt] = detail["attempts"]
    assert attempt["status"] == "succeeded" and attempt["usage"] is None
    cost = attempt["cost"]
    assert cost["status"] == "incomplete" and cost["lower"] == "0"
    assert attempt["budget_reserved"] is not None and cost["upper"] == attempt["budget_reserved"]
    assert any("reserved" in assumption for assumption in cost["assumptions"])
    assert detail["generation_cost"]["upper"] == attempt["budget_reserved"]
    assert detail["budget"]["committed"] == attempt["budget_reserved"]


def test_interrupted_work_is_priced_in_the_currency_of_its_snapshot(tmp_path):
    data = tmp_path / "data"
    with make_client(data) as client:
        endpoint = create_endpoint(client)
        profile = chat(client, endpoint["id"], "deepseek-a")
        run_id = start_run(client, {"prompts": [{"user": "Hello"}], "candidates": [{"profile_id": profile["id"]}]})
        wait_for_run(client, run_id)
        assert client.put("/api/settings", json={"currency": "EUR"}).status_code == 200
    # Simulate a crash in the middle of the call.
    with closing(sqlite3.connect(data / "foundry-eval.sqlite3")) as conn, conn:
        conn.execute("UPDATE attempts SET status = 'running', finished_at = NULL")
        conn.execute("UPDATE runs SET status = 'running', finished_at = NULL")
    with make_client(data) as client:
        detail = client.get(f"/api/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        [attempt] = detail.json()["attempts"]
        assert attempt["status"] == "interrupted"
        assert attempt["cost"]["currency"] == "USD" and attempt["cost"]["status"] == "incomplete"
        totals = detail.json()["generation_cost"]
        assert totals["currency"] == "USD" and totals["status"] == "incomplete" and totals["other_currency"] == 0


def test_judging_a_budgeted_run_needs_rates_in_the_run_currency(client):
    endpoint = create_endpoint(client)
    candidate = claude(client, endpoint["id"], "claude-cand")
    run_id = budgeted_claude_run(client, candidate["id"], "0.5")
    assert client.put("/api/settings", json={"currency": "EUR"}).status_code == 200
    judge = claude(client, endpoint["id"], "claude-judge")  # rates entered in EUR
    payload = {"judge_profile_id": judge["id"], "settings": {"max_output_tokens": 1000}}
    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json=payload).json()
    assert not preview["ok"]
    assert "This run's budget is in USD, but the judge's rates are in EUR." in preview["errors"]
