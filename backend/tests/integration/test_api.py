from __future__ import annotations

import time

import httpx2
import pytest

from foundry_eval.config import AppConfig
from foundry_eval.instance_lock import InstanceLockError
from foundry_eval.types import ApiStyle
from tests.api_support import (
    CANARY,
    ORIGIN,
    SlowAdapter,
    create_endpoint,
    create_profile,
    make_client,
    set_manual,
    start_run,
    wait_for_run,
)


# ---------------------------------------------------------------------------------------------
def test_end_to_end_compare_with_failure_isolation(client, mock_app, tmp_path):
    endpoint = create_endpoint(client)
    gpt = create_profile(
        client,
        endpoint["id"],
        preset="openai-reasoning-cache-write",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-5.6-terra",
        meter_terms="5.6 terra",
    )
    claude = create_profile(
        client,
        endpoint["id"],
        preset="claude-adaptive",
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        deployment="claude-opus-test",
    )
    broken = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="fail-500"
    )
    refresh = client.post("/api/pricing/refresh", json={})
    assert refresh.status_code == 200 and refresh.json()[0]["status"] == "succeeded"
    for category, price in [
        ("input_uncached", "4"),
        ("cache_read", "0.2"),
        ("cache_write_5m", "5"),
        ("cache_write_1h", "8"),
        ("output", "20"),
    ]:
        set_manual(client, claude["id"], category, price)
    rates = client.get(f"/api/profiles/{gpt['id']}/rates").json()
    assert rates["card"]["tiers"][0]["categories"]["output"]["rate"]["price"] == "15"

    request = {
        "prompts": [{"system": "Be brief.", "user": "Compare prompt caching on two providers."}],
        "candidates": [
            {"profile_id": gpt["id"], "settings": {"max_output_tokens": 2000, "effort": "low"}},
            {"profile_id": gpt["id"], "settings": {"max_output_tokens": 2000, "effort": "high"}},
            {
                "profile_id": claude["id"],
                "settings": {"max_output_tokens": 4000, "thinking_mode": "adaptive", "effort": "medium"},
            },
            {"profile_id": broken["id"], "settings": {"max_output_tokens": 100}},
        ],
    }
    preview = client.post("/api/runs/preview", json=request).json()
    assert preview["ok"] and preview["calls"] == 4
    assert [c["label"] for c in preview["candidates"]][:2] == [
        "gpt-5.6-terra (effort=low)",
        "gpt-5.6-terra (effort=high)",
    ]

    run_id = start_run(client, request)
    again = client.post("/api/runs", json=request, headers={"Idempotency-Key": "idem-key-0001"})
    assert again.status_code == 200 and again.json() == {"run_id": run_id, "created": False}

    detail = wait_for_run(client, run_id)
    statuses = {a["candidate_id"]: a["status"] for a in detail["attempts"]}
    labels = {c["id"]: c["label"] for c in detail["candidates"]}
    by_label = {labels[k]: v for k, v in statuses.items()}
    assert by_label["fail-500"] == "failed"
    assert [by_label[k] for k in ("gpt-5.6-terra (effort=low)", "gpt-5.6-terra (effort=high)", "claude-opus-test")] == [
        "succeeded"
    ] * 3
    assert detail["run"]["status"] == "completed_with_errors"
    gpt_attempt = next(a for a in detail["attempts"] if labels[a["candidate_id"]].endswith("(effort=high)"))
    assert gpt_attempt["usage"]["reasoning"] == 1024
    assert gpt_attempt["cost"]["status"] == "complete"
    assert isinstance(gpt_attempt["cost"]["total"], str)
    assert gpt_attempt["request_body"]["reasoning"] == {"effort": "high"}
    failed = next(a for a in detail["attempts"] if a["status"] == "failed")
    assert failed["cost"]["status"] == "incomplete" and failed["http_status"] == 500
    assert detail["generation_cost"]["status"] == "incomplete"
    assert detail["judging_cost"]["status"] == "empty"
    assert mock_app.state.mock.requests["/openai/v1/chat/completions"] == 1  # no automatic retry

    # explicit retry creates a new attempt record; idempotent per key
    retry = client.post(f"/api/attempts/{failed['id']}/retry", headers={"Idempotency-Key": "retry-key-001"})
    assert retry.status_code == 200
    repeat = client.post(f"/api/attempts/{failed['id']}/retry", headers={"Idempotency-Key": "retry-key-001"})
    assert repeat.json() == retry.json()
    detail = wait_for_run(client, run_id)
    retried = [a for a in detail["attempts"] if a["retry_of"] == failed["id"]]
    assert len(retried) == 1 and retried[0]["status"] == "failed"
    assert mock_app.state.mock.requests["/openai/v1/chat/completions"] == 2

    # manual score
    scored = client.put(f"/api/attempts/{gpt_attempt['id']}/manual-score", json={"score": 4, "notes": "solid"})
    assert scored.status_code == 200
    assert client.get(f"/api/runs/{run_id}").json()["manual_scores"][gpt_attempt["id"]]["score"] == 4
    assert client.put(f"/api/attempts/{gpt_attempt['id']}/manual-score", json={"score": 7}).status_code == 422

    raw = client.get(f"/api/attempts/{gpt_attempt['id']}/raw").json()
    assert raw["raw_usage"]["output_tokens_details"]["reasoning_tokens"] == 1024

    history = client.get("/api/runs").json()
    assert history[0]["run"]["id"] == run_id and history[0]["counts"]["succeeded"] == 3

    # The key was sent to the provider and never returned by the API.
    assert mock_app.state.mock.key_hashes
    for path in (
        "/api/endpoints",
        f"/api/runs/{run_id}",
        "/api/runs",
        f"/api/attempts/{gpt_attempt['id']}/raw",
        "/api/settings",
    ):
        assert CANARY not in client.get(path).text


def test_price_snapshot_preserves_history(client):
    endpoint = create_endpoint(client)
    claude = create_profile(
        client,
        endpoint["id"],
        preset="claude-adaptive",
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        deployment="claude-haiku-test",
    )
    for category in ("input_uncached", "cache_read", "cache_write_5m", "cache_write_1h", "output"):
        set_manual(client, claude["id"], category, "1")
    request = {
        "prompts": [{"user": "hi"}],
        "candidates": [{"profile_id": claude["id"], "settings": {"max_output_tokens": 50}}],
    }
    run_id = start_run(client, request)
    before = wait_for_run(client, run_id)
    total_before = before["attempts"][0]["cost"]["total"]
    set_manual(client, claude["id"], "output", "100")
    after = client.get(f"/api/runs/{run_id}").json()
    assert after["attempts"][0]["cost"]["total"] == total_before
    snapshot = client.get(f"/api/price-snapshots/{after['candidates'][0]['price_snapshot_id']}").json()
    assert snapshot["card"]["tiers"][0]["categories"]["output"]["rate"]["price"] == "1"


def test_run_validation_errors(client):
    endpoint = create_endpoint(client)
    gpt = create_profile(
        client, endpoint["id"], preset="openai-reasoning", api_style=ApiStyle.OPENAI_RESPONSES, deployment="gpt-5"
    )
    bad = {"prompts": [{"user": "x"}], "candidates": [{"profile_id": gpt["id"], "settings": {"temperature": 0.3}}]}
    preview = client.post("/api/runs/preview", json=bad).json()
    assert not preview["ok"] and "not supported" in preview["errors"][0]
    response = client.post("/api/runs", json=bad, headers={"Idempotency-Key": "k-invalid-01"})
    assert response.status_code == 422 and response.json()["preview"]["errors"]
    assert client.post("/api/runs", json=bad).status_code == 400  # Idempotency-Key required
    budgeted = {"prompts": [{"user": "x"}], "candidates": [{"profile_id": gpt["id"], "settings": {}}], "budget": "0.01"}
    errors = client.post("/api/runs/preview", json=budgeted).json()["errors"]
    assert any("bounded cost" in e for e in errors)
    assert client.post("/api/runs/preview", json={**budgeted, "budget": 0.01}).status_code == 422  # floats rejected


def test_key_management_never_returns_keys(client):
    endpoint = create_endpoint(client)
    listed = client.get("/api/endpoints").json()
    assert listed[0]["key_configured"] is True and "api_key" not in listed[0]
    moved = client.patch(
        f"/api/endpoints/{endpoint['id']}",
        json={"name": "Moved", "base_url": "https://other.services.ai.azure.com", "region": "eastus2"},
    ).json()
    assert moved["key_configured"] is False  # a key is never sent to a different host
    client.put(f"/api/endpoints/{endpoint['id']}/key", json={"api_key": CANARY})
    assert client.delete(f"/api/endpoints/{endpoint['id']}/key").json() == {"key_configured": False}
    assert client.put(f"/api/endpoints/{endpoint['id']}/key", json={"api_key": "has space"}).status_code == 400


# --- engine semantics ---------------------------------------------------------------------------
def test_concurrency_is_bounded_to_three(tmp_path):
    slow = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=0.2)
    with make_client(tmp_path / "data", adapters={ApiStyle.OPENAI_CHAT: slow}) as client:
        endpoint = create_endpoint(client)
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
        )
        run_id = start_run(
            client, {"prompts": [{"user": "x"}], "candidates": [{"profile_id": profile["id"]}], "trials": 7}
        )
        detail = wait_for_run(client, run_id)
    assert slow.calls == 7
    assert slow.max_active == 3
    assert all(a["status"] == "succeeded" for a in detail["attempts"])


def test_cancel_marks_sent_and_queued_attempts_differently(tmp_path):
    slow = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=30)
    with make_client(tmp_path / "data", adapters={ApiStyle.OPENAI_CHAT: slow}) as client:
        endpoint = create_endpoint(client)
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
        )
        run_id = start_run(
            client, {"prompts": [{"user": "x"}], "candidates": [{"profile_id": profile["id"]}], "trials": 5}
        )
        deadline = time.time() + 5
        while slow.active < 3 and time.time() < deadline:
            time.sleep(0.02)
        progress = client.post(f"/api/runs/{run_id}/cancel").json()
        assert progress["status"] == "cancelled"
        detail = client.get(f"/api/runs/{run_id}").json()
    statuses = [a["status"] for a in detail["attempts"]]
    assert statuses.count("cancelled") == 5
    sent = [a for a in detail["attempts"] if a["request_sent_at"]]
    unsent = [a for a in detail["attempts"] if not a["request_sent_at"]]
    assert len(sent) == 3 and len(unsent) == 2
    assert all(a["cost"]["status"] == "incomplete" for a in sent)  # may still be billed
    assert all(a["cost"]["status"] == "not_sent" and a["cost"]["total"] == "0" for a in unsent)


def test_restart_marks_unfinished_work_interrupted_without_replay(tmp_path, mock_app):
    data = tmp_path / "data"
    slow = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=30)
    with make_client(data, adapters={ApiStyle.OPENAI_CHAT: slow}) as client:
        endpoint = create_endpoint(client)
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
        )
        run_id = start_run(
            client, {"prompts": [{"user": "x"}], "candidates": [{"profile_id": profile["id"]}], "trials": 4}
        )
        deadline = time.time() + 5
        while slow.active < 3 and time.time() < deadline:
            time.sleep(0.02)
    # App stopped mid-run. A new process must not replay anything.
    replay = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=0)
    with make_client(data, adapters={ApiStyle.OPENAI_CHAT: replay}) as client:
        detail = client.get(f"/api/runs/{run_id}").json()
        time.sleep(0.2)
    assert detail["run"]["status"] == "interrupted"
    assert {a["status"] for a in detail["attempts"]} == {"interrupted"}
    assert replay.calls == 0


def test_second_instance_is_refused(tmp_path):
    data = tmp_path / "data"
    with make_client(data):  # noqa: SIM117 - the inner client must start while the first is running
        with pytest.raises(InstanceLockError), make_client(data):
            pass


def test_budget_skips_calls_that_could_exceed_it(tmp_path):
    slow = SlowAdapter(ApiStyle.OPENAI_CHAT, delay=0)
    with make_client(tmp_path / "data", adapters={ApiStyle.OPENAI_CHAT: slow}) as client:
        endpoint = create_endpoint(client)
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
        )
        set_manual(client, profile["id"], "input_uncached", "1")
        set_manual(client, profile["id"], "output", "10")
        # worst case per call: (1 byte + 256 framing) x $1/M + 100 x $10/M = 0.001257
        request = {
            "prompts": [{"user": "x"}],
            "candidates": [{"profile_id": profile["id"], "settings": {"max_output_tokens": 100}}],
            "trials": 3,
            "budget": "0.0026",
        }
        preview = client.post("/api/runs/preview", json=request).json()
        assert preview["ok"] and preview["worst_case_total"] == "0.003771"
        run_id = start_run(client, request)
        detail = wait_for_run(client, run_id)
    statuses = sorted(a["status"] for a in detail["attempts"])
    assert statuses == ["skipped_budget", "succeeded", "succeeded"]
    assert slow.calls == 2
    assert detail["budget"]["limit"] == "0.0026"


def test_missing_key_fails_before_sending(tmp_path, mock_app):
    with make_client(tmp_path / "data", mock_app=mock_app) as client:
        endpoint = client.post(
            "/api/endpoints", json={"name": "NoKey", "base_url": "https://x.services.ai.azure.com", "region": "eastus2"}
        ).json()
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="m"
        )
        preview = client.post(
            "/api/runs/preview", json={"prompts": [{"user": "x"}], "candidates": [{"profile_id": profile["id"]}]}
        ).json()
        assert not preview["ok"] and "no API key" in preview["errors"][0]


# --- HTTP guard ---------------------------------------------------------------------------------
def test_guard_rejects_foreign_hosts_and_origins(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 421
    assert client.get("/api/health", headers={"Host": "127.0.0.1:9999"}).status_code == 421
    assert client.get("/api/health", headers={"Origin": "http://localhost:3000"}).status_code == 403
    assert client.get("/api/health", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/health", headers={"Sec-Fetch-Site": "none"}).status_code == 200
    body = {"name": "x", "base_url": "https://x.services.ai.azure.com"}
    assert client.post("/api/endpoints", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/endpoints", json=body, headers={"Origin": ORIGIN, "X-FME-Client": ""}).status_code == 403
    assert client.post("/api/endpoints", json=body, headers={"Sec-Fetch-Site": "none"}).status_code == 403
    assert client.post("/api/endpoints", content="name=x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.options("/api/endpoints").status_code == 405
    localhost_ok = client.get("/api/health", headers={"Host": "localhost:8765", "Origin": "http://localhost:8765"})
    assert localhost_ok.status_code == 200


def test_guard_rejects_missing_origin_on_post(tmp_path):
    with make_client(tmp_path / "data") as client:
        bare = httpx2.Client(transport=client._transport, base_url=ORIGIN)  # type: ignore[attr-defined]
        response = bare.post(
            "/api/endpoints",
            json={"name": "x", "base_url": "https://x.services.ai.azure.com"},
            headers={"X-FME-Client": "1"},
        )
        assert response.status_code == 403


def test_security_headers_and_no_cors(client):
    response = client.get("/api/health")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    rejected = client.get("/api/health", headers={"Host": "evil.example"})
    assert "content-security-policy" in rejected.headers


def test_non_loopback_bind_is_rejected(tmp_path):
    from foundry_eval.config import ConfigError

    with pytest.raises(ConfigError):
        AppConfig(data_dir=tmp_path, host="0.0.0.0")  # noqa: S104 - verifying it is refused


def test_canary_key_never_reaches_disk(tmp_path, mock_app):
    data = tmp_path / "data"
    with make_client(data, mock_app=mock_app) as client:
        endpoint = create_endpoint(client)
        profile = create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="rate-limited"
        )
        run_id = start_run(client, {"prompts": [{"user": "hello"}], "candidates": [{"profile_id": profile["id"]}]})
        wait_for_run(client, run_id)
        client.post(f"/api/endpoints/{endpoint['id']}/verify")
    for file in data.rglob("*"):
        if file.is_file():
            assert CANARY.encode() not in file.read_bytes(), f"key found in {file}"
