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
