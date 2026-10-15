"""Regression tests for the findings of the independent security review."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from foundry_eval.execution.budget import BudgetState
from foundry_eval.paths import ensure_data_dir
from foundry_eval.providers.anthropic_messages import AnthropicMessagesAdapter
from foundry_eval.providers.base import ProviderCall, Timeouts
from foundry_eval.providers.http import is_loopback_url, make_async_client, make_timeout
from foundry_eval.providers.openai_compat import ChatCompletionsAdapter, ResponsesAdapter
from foundry_eval.types import ApiStyle
from tests.api_support import (
    CANARY,
    SlowAdapter,
    create_endpoint,
    create_profile,
    make_client,
    set_manual,
    start_run,
    wait_for_run,
)


def redirecting_app() -> tuple[FastAPI, list[str]]:
    """Answers every call with a redirect to another host and records every request it sees."""
    app = FastAPI()
    seen: list[str] = []

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def redirect(path: str, request: Request) -> Response:
        seen.append(f"{request.url.hostname}/{path}")
        return Response(status_code=307, headers={"location": "http://collector.example/steal"})

    return app, seen


def call(api_style: ApiStyle, url: str, body: dict[str, Any]) -> ProviderCall:
    return ProviderCall(
        api_style=api_style,
        url=url,
        query={},
        auth_style="api-key",
        api_key=CANARY,
        body=body,
        timeouts=Timeouts(5, 5, 10),
    )


CLAUDE_BODY = {"model": "claude-test", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


# --------------------------------------------------------------------------- 1. redirects
@pytest.mark.parametrize("stream", [False, True])
async def test_claude_adapter_never_follows_redirects(stream):
    app, seen = redirecting_app()
    adapter = AnthropicMessagesAdapter(transport=httpx2.ASGITransport(app=app))
    result = await adapter.execute(
        call(
            ApiStyle.ANTHROPIC_MESSAGES,
            "https://res.services.ai.azure.com/anthropic",
            {**CLAUDE_BODY, "stream": stream},
        )
    )
    assert not result.ok
    assert result.error_type == "redirect" and result.http_status == 307
    assert (
        "collector.example" in (result.error_message or "") and "never sent to another address" in result.error_message
    )
    assert seen == ["res.services.ai.azure.com/anthropic/v1/messages"]  # exactly one request, never to the target


@pytest.mark.parametrize("adapter_cls", [ResponsesAdapter, ChatCompletionsAdapter])
async def test_openai_adapters_report_redirects_as_errors(adapter_cls):
    app, seen = redirecting_app()
    adapter = adapter_cls(transport=httpx2.ASGITransport(app=app))
    result = await adapter.execute(
        call(adapter.api_style, "https://res.services.ai.azure.com/openai/v1/responses", {"model": "m", "input": "x"})
    )
    assert not result.ok and result.error_type == "redirect" and result.http_status == 307
    assert len(seen) == 1


def test_real_clients_disable_redirects_and_proxies_for_loopback():
    loopback = make_async_client(timeout=make_timeout(1, 1), url="http://127.0.0.1:4000/openai/v1/responses")
    remote = make_async_client(timeout=make_timeout(1, 1), url="https://res.services.ai.azure.com/openai/v1/responses")
    assert loopback.follow_redirects is False and remote.follow_redirects is False
    assert loopback.trust_env is False  # no HTTP proxy may see a key sent to a local endpoint
    assert remote.trust_env is True  # corporate proxies still apply to Azure

    adapter = AnthropicMessagesAdapter()
    local_sdk = adapter._client(call(ApiStyle.ANTHROPIC_MESSAGES, "http://localhost:4000/anthropic", CLAUDE_BODY))
    remote_sdk = adapter._client(
        call(ApiStyle.ANTHROPIC_MESSAGES, "https://r.services.ai.azure.com/anthropic", CLAUDE_BODY)
    )
    assert local_sdk._client.follow_redirects is False and remote_sdk._client.follow_redirects is False
    assert local_sdk._client._mounts == {}  # environment proxies are not mounted for loopback


def test_loopback_detection():
    for url in ("http://127.0.0.1:1", "http://localhost", "http://[::1]:80", "http://api.localhost:3"):
        assert is_loopback_url(url), url
    for url in ("https://res.services.ai.azure.com", "http://10.0.0.1", "http://localhost.example.com"):
        assert not is_loopback_url(url), url


async def test_a_dropped_claude_stream_is_a_failed_result_not_a_crash():
    class DroppingStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","type":"message",'
            raise httpx2.ReadError("connection reset by peer")

    class DroppingTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=DroppingStream())

    adapter = AnthropicMessagesAdapter(transport=DroppingTransport())
    result = await adapter.execute(
        call(
            ApiStyle.ANTHROPIC_MESSAGES, "https://res.services.ai.azure.com/anthropic", {**CLAUDE_BODY, "stream": True}
        )
    )
    assert not result.ok and result.error_type == "network"


# --------------------------------------------------------------------------- 2. budget
def test_budget_state_commits_the_reservation_of_an_unsettled_sent_call():
    state = BudgetState(limit=Decimal("1"))
    assert state.reserve("a", Decimal("0.6"))
    state.commit_reserved("a")
    assert state.committed == Decimal("0.6") and state.reserved == {}
    assert not state.reserve("b", Decimal("0.6"))


def _budget_profile(client) -> dict:
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
    )
    set_manual(client, profile["id"], "input_uncached", "1")
    set_manual(client, profile["id"], "output", "10")
    return profile


BUDGET_REQUEST = {"prompts": [{"user": "x"}], "trials": 1, "budget": "0.0019"}  # worst case per call 0.001257


def test_a_cancelled_sent_call_still_counts_against_the_budget(tmp_path):
    slow = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=30)
    with make_client(tmp_path / "data", adapters={ApiStyle.OPENAI_CHAT: slow}) as client:
        profile = _budget_profile(client)
        request = {
            **BUDGET_REQUEST,
            "candidates": [{"profile_id": profile["id"], "settings": {"max_output_tokens": 100}}],
        }
        run_id = start_run(client, request)
        deadline = time.time() + 5
        while slow.active < 1 and time.time() < deadline:
            time.sleep(0.02)
        client.post(f"/api/runs/{run_id}/cancel")
        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["budget"]["committed"] == "0.001257"  # the worst case, not zero
        slow.delay = 0
        retry = client.post(f"/api/runs/{run_id}/retry-failed", headers={"Idempotency-Key": "idem-retry-0001"}).json()
        detail = wait_for_run(client, run_id)
    statuses = {a["id"]: a["status"] for a in detail["attempts"]}
    assert statuses[retry["attempt_ids"][0]] == "skipped_budget"
    assert slow.calls == 1


def test_sent_calls_without_usage_count_at_their_reservation_after_a_restart(tmp_path):
    data = tmp_path / "data"
    slow = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=30)
    with make_client(data, adapters={ApiStyle.OPENAI_CHAT: slow}) as client:
        profile = _budget_profile(client)
        request = {
            **BUDGET_REQUEST,
            "candidates": [{"profile_id": profile["id"], "settings": {"max_output_tokens": 100}}],
        }
        run_id = start_run(client, request)
        deadline = time.time() + 5
        while slow.active < 1 and time.time() < deadline:
            time.sleep(0.02)
    replay = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=0)
    with make_client(data, adapters={ApiStyle.OPENAI_CHAT: replay}) as client:
        [attempt] = client.get(f"/api/runs/{run_id}").json()["attempts"]
        assert attempt["status"] == "interrupted" and attempt["budget_reserved"] == "0.001257"
        retry = client.post(f"/api/runs/{run_id}/retry-failed", headers={"Idempotency-Key": "idem-retry-0002"}).json()
        detail = wait_for_run(client, run_id)
    assert {a["id"]: a["status"] for a in detail["attempts"]}[retry["attempt_ids"][0]] == "skipped_budget"
    assert replay.calls == 0


# --------------------------------------------------------------------------- 3. idempotency
def _failed_run(client) -> tuple[str, str]:
    endpoint = create_endpoint(client)
    failing = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="fail-500"
    )
    run_id = start_run(client, {"prompts": [{"user": "x"}], "candidates": [{"profile_id": failing["id"]}]})
    detail = wait_for_run(client, run_id)
    return run_id, detail["attempts"][0]["id"]


def test_concurrent_retries_with_one_key_create_one_attempt(client, mock_app):
    run_id, attempt_id = _failed_run(client)
    before = mock_app.state.mock.requests["/openai/v1/chat/completions"]

    def retry(_: int) -> httpx2.Response:
        return client.post(f"/api/attempts/{attempt_id}/retry", headers={"Idempotency-Key": "idem-same-0001"})

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(retry, range(12)))
    assert {r.status_code for r in responses} == {200}
    assert len({tuple(r.json()["attempt_ids"]) for r in responses}) == 1
    wait_for_run(client, run_id)
    attempts = client.get(f"/api/runs/{run_id}").json()["attempts"]
    assert len(attempts) == 2
    assert mock_app.state.mock.requests["/openai/v1/chat/completions"] == before + 1

    # A fresh key cannot retry the same attempt again; retry the newest attempt instead.
    again = client.post(f"/api/attempts/{attempt_id}/retry", headers={"Idempotency-Key": "idem-other-0001"})
    assert again.status_code == 409 and "already retried" in again.json()["detail"]
    reused = client.post(f"/api/runs/{run_id}/retry-failed", headers={"Idempotency-Key": "idem-same-0001"})
    assert reused.status_code == 409  # the key belongs to a different action


def test_concurrent_retry_failed_requests_never_retry_an_attempt_twice(client):
    run_id, _attempt_id = _failed_run(client)

    def retry_all(index: int) -> httpx2.Response:
        return client.post(f"/api/runs/{run_id}/retry-failed", headers={"Idempotency-Key": f"idem-many-{index:04d}"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(retry_all, range(8)))
    assert {r.status_code for r in responses} == {200}
    wait_for_run(client, run_id)
    attempts = client.get(f"/api/runs/{run_id}").json()["attempts"]
    retried = [a["retry_of"] for a in attempts if a["retry_of"]]
    # Separate actions may retry the newest failed attempt in turn, but no attempt is retried twice.
    assert len(retried) == len(set(retried)) >= 1
    assert len(attempts) == 1 + len(retried)


def test_concurrent_run_creation_with_one_key_never_errors(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
    )
    request = {"prompts": [{"user": "x"}], "candidates": [{"profile_id": profile["id"]}]}

    def create(_: int) -> httpx2.Response:
        return client.post("/api/runs", json=request, headers={"Idempotency-Key": "idem-race-0001"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(create, range(8)))
    assert {r.status_code for r in responses} <= {200, 201}
    assert len({r.json()["run_id"] for r in responses}) == 1
    assert sum(r.status_code == 201 for r in responses) == 1


# --------------------------------------------------------------------------- 4. redaction of raw responses
def test_raw_provider_bodies_that_echo_the_key_are_redacted_before_storage(tmp_path):
    echo = FastAPI()

    @echo.post("/openai/v1/chat/completions")
    async def echo_headers(request: Request) -> JSONResponse:
        return JSONResponse(
            {"error": {"message": "bad request", "debug": {"api-key": request.headers.get("api-key")}}}, status_code=400
        )

    data = tmp_path / "data"
    with make_client(data, mock_app=echo) as client:
        endpoint = create_endpoint(client)
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
        )
        run_id = start_run(client, {"prompts": [{"user": "x"}], "candidates": [{"profile_id": profile["id"]}]})
        detail = wait_for_run(client, run_id)
        raw = client.get(f"/api/attempts/{detail['attempts'][0]['id']}/raw").text
    assert CANARY not in raw and "[REDACTED]" in raw
    for file in data.rglob("*"):
        if file.is_file():
            assert CANARY.encode() not in file.read_bytes(), f"key found in {file}"


# --------------------------------------------------------------------------- 5. key characters
@pytest.mark.parametrize("key", ["abc\x00defghijk", "sk-ünicode-123456", "two words key", "short"])
def test_keys_must_be_printable_ascii(client, key):
    endpoint = client.post("/api/endpoints", json={"name": "E", "base_url": "https://e.services.ai.azure.com"}).json()
    response = client.put(f"/api/endpoints/{endpoint['id']}/key", json={"api_key": key})
    assert response.status_code in (400, 422)
    assert client.get("/api/endpoints").json()[0]["key_configured"] is False


# --------------------------------------------------------------------------- hardening
def test_unexpected_errors_still_carry_security_headers(client, monkeypatch):
    def broken(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(client.app.state.services.runs, "list_runs", broken)
    response = client.get("/api/runs")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal error. Details are in the app's log file."}
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_override_source_urls_must_be_web_links(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
    )
    response = client.put(
        f"/api/profiles/{profile['id']}/rates/output/override",
        json={"price": "1", "unit": "1M", "note": "discount", "source_url": "javascript:alert(1)"},
    )
    assert response.status_code == 422


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_data_folder_is_private_on_posix(tmp_path):
    folder = ensure_data_dir(tmp_path / "private")
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "logs").stat().st_mode & 0o777 == 0o700


def test_concurrent_judge_requests_with_one_key_create_one_batch(client):
    endpoint = create_endpoint(client)
    profile = create_profile(
        client,
        endpoint["id"],
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-4.1-judge",
        context_window=100_000,
    )
    run_id = start_run(
        client,
        {
            "prompts": [{"user": "x"}],
            "candidates": [
                {"profile_id": profile["id"], "settings": {"temperature": 0.1}},
                {"profile_id": profile["id"], "settings": {"temperature": 0.9}},
            ],
        },
    )
    wait_for_run(client, run_id)

    def judge(_: int) -> httpx2.Response:
        return client.post(
            f"/api/runs/{run_id}/evaluations",
            json={"judge_profile_id": profile["id"]},
            headers={"Idempotency-Key": "idem-judge-race"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(judge, range(8)))
    assert {r.status_code for r in responses} <= {200, 201}
    assert sum(r.status_code == 201 for r in responses) == 1
    assert len({tuple(r.json()["evaluation_ids"]) for r in responses}) == 1
    wait_for_run(client, run_id)
    assert len(client.get(f"/api/runs/{run_id}/evaluations").json()) == 1
