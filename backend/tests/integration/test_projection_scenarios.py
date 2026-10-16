"""Projection scenarios through the API: grouped exclusion reasons and the assumed cache-write TTL."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from foundry_eval.types import ApiStyle
from tests.api_support import create_endpoint, create_profile, set_manual, start_run, wait_for_run

CLAUDE_RATES = {
    "input_uncached": "3",
    "cache_read": "0.3",
    "cache_write_5m": "3.75",
    "cache_write_1h": "6",
    "output": "15",
}


def project(client: TestClient, run_id: str, **body) -> dict:
    response = client.post("/api/projections", json={"run_ids": [run_id], **body})
    assert response.status_code == 200, response.text
    return response.json()


def test_exclusions_with_the_same_cause_are_counted_once(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="deepseek-x"
    )
    set_manual(client, profile["id"], "output", "10")  # no input rate
    prompts = [{"user": "Hi"}, {"user": "Tell me about the sea, please."}, {"user": "Why " * 40}]
    run_id = start_run(client, {"prompts": prompts, "candidates": [{"profile_id": profile["id"]}]})
    wait_for_run(client, run_id)
    [item] = project(client, run_id)["candidates"]
    assert item["status"] == "unavailable" and item["attempts_excluded"] == 3
    assert item["exclusion_reasons"] == ["3 excluded: no rate for uncached input"]


def test_a_cache_write_share_without_a_known_ttl_uses_the_assumed_ttl(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="claude-adaptive", api_style=ApiStyle.ANTHROPIC_MESSAGES, deployment="claude-x"
    )
    for category, price in CLAUDE_RATES.items():
        set_manual(client, profile["id"], category, price)
    request = {
        "prompts": [{"user": "hello there"}],
        "candidates": [{"profile_id": profile["id"], "settings": {"max_output_tokens": 200}}],
    }
    run_id = start_run(client, request)
    wait_for_run(client, run_id)
    five = project(client, run_id, what_if={"cache_write_share": "0.3"})["candidates"][0]
    assert five["status"] == "complete" and five["attempts_excluded"] == 0, five
    assert "cache writes priced at the 5-minute cache rate (assumed TTL)" in five["assumptions"]
    hour = project(client, run_id, what_if={"cache_write_share": "0.3", "cache_write_ttl": "1h"})["candidates"][0]
    assert "cache writes priced at the 1-hour cache rate (assumed TTL)" in hour["assumptions"]
    assert Decimal(hour["cost_per_request"]) > Decimal(five["cost_per_request"])


def test_prices_in_another_currency_are_excluded(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="deepseek-y"
    )
    set_manual(client, profile["id"], "input_uncached", "1")
    set_manual(client, profile["id"], "output", "10")
    run_id = start_run(
        client, {"prompts": [{"user": "Hi"}], "candidates": [{"profile_id": profile["id"]}], "trials": 2}
    )
    wait_for_run(client, run_id)
    assert client.put("/api/settings", json={"currency": "EUR"}).status_code == 200
    result = project(client, run_id)
    assert result["currency"] == "EUR"
    [item] = result["candidates"]
    assert item["status"] == "unavailable" and item["exclusion_reasons"] == ["2 excluded: prices in USD"]


def test_no_priced_request_gives_no_cost_spread(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="deepseek-none"
    )
    set_manual(client, profile["id"], "output", "10")  # no input rate: nothing can be priced completely
    run_id = start_run(client, {"prompts": [{"user": "Hi"}], "candidates": [{"profile_id": profile["id"]}]})
    wait_for_run(client, run_id)
    [item] = project(client, run_id)["candidates"]
    assert item["status"] == "unavailable" and item["cost_spread"] is None  # unknown, never zero


PROMPTS = [{"user": "Hi"}, {"user": "Name a colour."}, {"user": "Why " * 30}, {"user": "Explain tides " * 12}]


def spread_runs(client: TestClient) -> tuple[str, str, list[Decimal]]:
    """Two runs of one candidate: the first without an input rate (excluded), the second priced."""
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="deepseek-spread"
    )
    set_manual(client, profile["id"], "output", "10")
    set_manual(client, profile["id"], "cache_read", "0.5")  # repeated prompts are partly cache reads
    unpriced = start_run(
        client, {"prompts": [{"user": "Hello"}], "candidates": [{"profile_id": profile["id"]}], "trials": 2}
    )
    wait_for_run(client, unpriced)
    set_manual(client, profile["id"], "input_uncached", "1")
    priced = start_run(
        client,
        {"prompts": [*PROMPTS, {"user": "One more " * 50}], "candidates": [{"profile_id": profile["id"]}], "trials": 2},
        key="idem-key-0002",
    )
    detail = wait_for_run(client, priced)
    costs = [Decimal(attempt["cost"]["total"]) for attempt in detail["attempts"]]
    assert len(costs) == 10 and len(set(costs)) > 3, costs
    return unpriced, priced, costs


def test_cost_spread_uses_nearest_rank_over_complete_estimates_only(client):
    unpriced, priced, costs = spread_runs(client)
    response = client.post("/api/projections", json={"run_ids": [unpriced, priced], "monthly_requests": 1000})
    assert response.status_code == 200, response.text
    [item] = response.json()["candidates"]
    assert item["attempts_used"] == 10 and item["attempts_excluded"] == 2
    spread = item["cost_spread"]
    ordered = sorted(costs)
    # Nearest rank over 10 requests: p50 is the 5th, p90 the 9th, p95 and the maximum the 10th.
    assert [Decimal(spread[key]) for key in ("p50", "p90", "p95", "max")] == [
        ordered[4],
        ordered[8],
        ordered[9],
        ordered[9],
    ]
    assert Decimal(spread["month_if_every_request_p50"]) == ordered[4] * 1000
    assert Decimal(spread["month_if_every_request_p95"]) == ordered[9] * 1000
    assert spread["basis"] == "nearest-rank over 10 requests with complete estimates (2 excluded)"
    assert spread["sample_warning"] == "Only 10 requests priced: percentiles from fewer than 30 are rough."
    assert all(isinstance(spread[key], str) for key in ("p50", "p90", "p95", "max"))  # money as decimal strings


def test_batch_what_if_scales_every_request_cost_and_is_labelled(client):
    _unpriced, priced, _costs = spread_runs(client)
    plain = project(client, priced)
    batched = project(client, priced, what_if={"batch_share": "0.4", "batch_rate_ratio": "0.5"})
    before, after = plain["candidates"][0], batched["candidates"][0]
    factor = Decimal("0.8")  # 60% at the full cost, 40% at half of it
    for key in ("cost_per_request", "cost_per_1k_requests", "cost_per_month"):
        assert Decimal(after[key]) == Decimal(before[key]) * factor, key
    for key in ("p50", "p90", "p95", "max", "month_if_every_request_p50", "month_if_every_request_p95"):
        assert Decimal(after["cost_spread"][key]) == Decimal(before["cost_spread"][key]) * factor, key
    label = "batch: 40% of requests at x0.5 the cost (entered ratio, not a published batch price)"
    assert label in batched["assumptions"] and label not in plain["assumptions"]
    assert any(note.startswith("Batch is blended into every request's cost") for note in batched["notes"])
    assert not any(note.startswith("Batch") for note in plain["notes"])
    assert batched["what_if"]["batch_share"] == "0.4" and batched["what_if"]["batch_rate_ratio"] == "0.5"
    # The whole share at a ratio of 1 changes nothing.
    same = project(client, priced, what_if={"batch_share": "1", "batch_rate_ratio": "1"})["candidates"][0]
    assert same["cost_per_request"] == before["cost_per_request"]


def test_batch_what_if_is_validated(client):
    for what_if in (
        {"batch_share": "0.5"},
        {"batch_rate_ratio": "0.5"},
        {"batch_share": "0.5", "batch_rate_ratio": "0"},
        {"batch_share": "0.5", "batch_rate_ratio": "1.5"},
        {"batch_share": "1.2", "batch_rate_ratio": "0.5"},
        {"batch_share": 0.5, "batch_rate_ratio": "0.5"},
    ):
        response = client.post("/api/projections", json={"run_ids": ["x"], "what_if": what_if})
        assert response.status_code == 422, what_if
