from __future__ import annotations

import json
import re
import sqlite3
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from foundry_eval.types import ApiStyle
from tests.api_support import (
    SlowAdapter,
    create_endpoint,
    create_profile,
    make_client,
    set_manual,
    start_run,
    wait_for_run,
)

PROMPT = "Explain why the sky looks blue in two sentences."


def terra(client: TestClient, endpoint_id: str, **extra) -> dict:
    return create_profile(
        client,
        endpoint_id,
        preset="openai-reasoning-cache-write",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment=extra.pop("deployment", "gpt-5.6-terra"),
        name=extra.pop("name", "Terra profile"),
        model_name="gpt-5.6-terra",
        **extra,
    )


def judge_profile(client: TestClient, endpoint_id: str, deployment: str = "gpt-4.1-judge", **extra) -> dict:
    return create_profile(
        client,
        endpoint_id,
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment=deployment,
        name=extra.pop("name", "Judge profile"),
        model_name=extra.pop("model_name", "gpt-4.1"),
        context_window=extra.pop("context_window", 100_000),
        **extra,
    )


def two_effort_run(client: TestClient, profile_id: str, key: str = "idem-run-0001", **extra) -> str:
    return start_run(
        client,
        {
            "title": "Efforts",
            "prompts": [{"system": "Answer briefly.", "user": PROMPT}],
            "candidates": [
                {"profile_id": profile_id, "settings": {"effort": "low"}},
                {"profile_id": profile_id, "settings": {"effort": "high"}},
            ],
            **extra,
        },
        key=key,
    )


def run_judge(client: TestClient, run_id: str, payload: dict, key: str = "idem-judge-0001") -> list[str]:
    response = client.post(f"/api/runs/{run_id}/evaluations", json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()["evaluation_ids"]


def evaluations(client: TestClient, run_id: str) -> list[dict]:
    response = client.get(f"/api/runs/{run_id}/evaluations")
    assert response.status_code == 200, response.text
    return response.json()


def outside_responses(text: str) -> str:
    """Judge request text with the response blocks removed (responses are model-written content)."""
    return re.sub(r"=== Response [A-Z]+ \(begin (\w+)\) ===.*?=== Response [A-Z]+ \(end \1\) ===", "", text, flags=re.S)


# --------------------------------------------------------------------------- explicit judging
def test_explicit_judge_scores_anonymized_responses(client, mock_app):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge = judge_profile(client, endpoint["id"])
    run_id = two_effort_run(client, candidate["id"])
    wait_for_run(client, run_id)
    payload = {"judge_profile_id": judge["id"], "settings": {"max_output_tokens": 2000}}

    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json=payload).json()
    assert preview["ok"], preview["errors"]
    assert preview["structured_output"] is True
    assert preview["participant_reasons"] == []
    assert [g["responses"] for g in preview["groups"]] == [2]
    assert preview["rubric_version_id"] == "default-v1"

    ids = run_judge(client, run_id, payload)
    detail = wait_for_run(client, run_id)
    calls = mock_app.state.mock.requests["/openai/v1/responses"]
    [evaluation] = evaluations(client, run_id)
    assert evaluation["id"] == ids[0]
    assert evaluation["status"] == "succeeded", evaluation
    assert evaluation["trigger"] == "manual"
    assert evaluation["judge_is_participant"] is False
    assert {c["label"] for c in evaluation["candidates"]} == {"A", "B"}
    assert all(len(c["criteria"]) == 4 for c in evaluation["candidates"])
    attempt_ids = {a["id"] for a in detail["attempts"]}
    assert {o["attempt_id"] for o in evaluation["order"]} == attempt_ids
    assert set(evaluation["candidate_labels"].values()) == {"Terra profile (effort=high)", "Terra profile (effort=low)"}
    if evaluation["preferred"] != "tie":
        by_label = {o["label"]: o["candidate_id"] for o in evaluation["order"]}
        assert evaluation["preferred_candidate_id"] == by_label[evaluation["preferred"]]
    weights = [c["weighted_overall"] for c in evaluation["candidates"]]
    for c, weighted in zip(evaluation["candidates"], weights, strict=True):
        assert weighted == pytest.approx(sum(x["score"] for x in c["criteria"]) / 4, abs=0.005)

    # The judge saw the task, the rubric and the answers, never identities, costs or timing.
    raw = client.get(f"/api/evaluations/{ids[0]}/raw").json()
    body = raw["request_body"]
    assert body["model"] == "gpt-4.1-judge"
    text = body["instructions"] + json.dumps(body["input"], ensure_ascii=False)
    assert PROMPT in text and "Answer briefly." in text
    visible = outside_responses(text)
    for secret in ("gpt-5.6-terra", "Terra profile", "effort=", "Mock", "cost", "latency", "$", candidate["id"]):
        assert secret not in visible, secret
    for attempt in detail["attempts"]:
        assert attempt["id"] not in text
        assert attempt["output_text"] in text
    assert body["text"]["format"]["type"] == "json_schema"

    # Judging cost is reported separately, and combined = generation + judging.
    detail = client.get(f"/api/runs/{run_id}").json()
    generation, judging, combined = detail["generation_cost"], detail["judging_cost"], detail["combined_cost"]
    assert judging["counted"] == 1 and generation["counted"] == 2
    assert Decimal(combined["lower"]) == Decimal(generation["lower"]) + Decimal(judging["lower"])
    assert set(detail["judge_scores"]) == attempt_ids
    assert detail["evaluation_counts"] == {"succeeded": 1}

    # Same idempotency key: the same evaluations, no new paid call.
    again = client.post(f"/api/runs/{run_id}/evaluations", json=payload, headers={"Idempotency-Key": "idem-judge-0001"})
    assert again.status_code == 200 and again.json()["evaluation_ids"] == ids
    assert mock_app.state.mock.requests["/openai/v1/responses"] == calls


def test_re_judging_creates_a_new_evaluation_and_keeps_the_old_one(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge = judge_profile(client, endpoint["id"])
    run_id = two_effort_run(client, candidate["id"])
    wait_for_run(client, run_id)
    payload = {"judge_profile_id": judge["id"]}
    first = run_judge(client, run_id, payload, key="idem-judge-a001")
    wait_for_run(client, run_id)
    second = run_judge(client, run_id, payload, key="idem-judge-a002")
    wait_for_run(client, run_id)
    assert first != second
    assert [e["id"] for e in evaluations(client, run_id)] == first + second


def test_malformed_judge_output_is_kept_for_inspection(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge = judge_profile(client, endpoint["id"], deployment="judge-malformed")
    run_id = two_effort_run(client, candidate["id"])
    wait_for_run(client, run_id)
    run_judge(client, run_id, {"judge_profile_id": judge["id"]})
    wait_for_run(client, run_id)
    [evaluation] = evaluations(client, run_id)
    assert evaluation["status"] == "malformed"
    assert "cannot provide JSON" in evaluation["output_text"]
    assert any("not valid JSON" in e for e in evaluation["parse_errors"])
    assert evaluation["candidates"] == [] and evaluation["preferred"] is None
    # The call was made (and may be billed); its rates are not configured here, so the estimate is incomplete.
    assert evaluation["request_sent_at"] is not None and evaluation["cost"]["status"] == "incomplete"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["judge_scores"] == {}
    assert {a["status"] for a in detail["attempts"]} == {"succeeded"}  # attempts untouched


def test_oversized_judge_input_is_rejected_without_any_call(client, mock_app):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge = judge_profile(client, endpoint["id"], context_window=300)
    run_id = two_effort_run(client, candidate["id"])
    wait_for_run(client, run_id)
    payload = {"judge_profile_id": judge["id"], "settings": {"max_output_tokens": 100}}
    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json=payload).json()
    assert preview["ok"] and preview["calls"] == 0
    assert preview["groups"][0]["oversized"] is True
    assert preview["groups"][0]["input_limit_tokens"] == 200
    before = mock_app.state.mock.requests["/openai/v1/responses"]
    run_judge(client, run_id, payload)
    wait_for_run(client, run_id)
    [evaluation] = evaluations(client, run_id)
    assert evaluation["status"] == "rejected_oversized"
    assert "Nothing was sent and nothing was truncated" in evaluation["error_message"]
    assert evaluation["request_sent_at"] is None
    assert evaluation["cost"]["status"] == "not_sent"
    assert mock_app.state.mock.requests["/openai/v1/responses"] == before


def test_a_judge_that_took_part_is_flagged(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    run_id = two_effort_run(client, candidate["id"])
    wait_for_run(client, run_id)
    payload = {"judge_profile_id": candidate["id"]}
    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json=payload).json()
    assert preview["ok"]
    assert any("also took part" in w for w in preview["warnings"])
    run_judge(client, run_id, payload)
    wait_for_run(client, run_id)
    [evaluation] = evaluations(client, run_id)
    assert evaluation["judge_is_participant"] is True
    assert any("same model profile" in r for r in evaluation["participant_reasons"])


def test_judge_needs_succeeded_responses_and_a_valid_setup(client):
    endpoint = create_endpoint(client)
    failing = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="fail-500"
    )
    judge = judge_profile(client, endpoint["id"])
    run_id = start_run(client, {"prompts": [{"user": "x"}], "candidates": [{"profile_id": failing["id"]}]})
    wait_for_run(client, run_id)
    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json={"judge_profile_id": judge["id"]}).json()
    assert not preview["ok"]
    assert "There are no succeeded responses to judge in this scope." in preview["errors"]
    response = client.post(
        f"/api/runs/{run_id}/evaluations",
        json={"judge_profile_id": "missing"},
        headers={"Idempotency-Key": "idem-x-0001"},
    )
    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]
    unsupported = client.post(
        f"/api/runs/{run_id}/evaluations/preview",
        json={"judge_profile_id": judge["id"], "settings": {"thinking_mode": "adaptive"}},
    ).json()
    assert any("Judge settings" in e for e in unsupported["errors"]) or not unsupported["ok"]


def test_cancelling_a_run_cancels_judge_calls(tmp_path):
    data = tmp_path / "data"
    slow = SlowAdapter(ApiStyle.OPENAI_RESPONSES, delay=30)
    with make_client(data) as client:
        endpoint = create_endpoint(client)
        candidate = terra(client, endpoint["id"])
        judge = judge_profile(client, endpoint["id"])
        run_id = two_effort_run(client, candidate["id"])
        wait_for_run(client, run_id)
        client.app.state.services.engine.adapters[ApiStyle.OPENAI_RESPONSES] = slow
        run_judge(client, run_id, {"judge_profile_id": judge["id"]})
        deadline = time.time() + 5
        while slow.active < 1 and time.time() < deadline:
            time.sleep(0.02)
        client.post(f"/api/runs/{run_id}/cancel")
        [evaluation] = evaluations(client, run_id)
    assert evaluation["status"] == "cancelled"
    assert evaluation["request_sent_at"] is not None
    assert evaluation["cost"]["status"] == "incomplete"  # sent: the provider may still bill it


def test_restart_marks_unfinished_evaluations_interrupted_without_replay(tmp_path):
    data = tmp_path / "data"
    with make_client(data) as client:
        endpoint = create_endpoint(client)
        candidate = terra(client, endpoint["id"])
        judge = judge_profile(client, endpoint["id"])
        run_id = two_effort_run(client, candidate["id"])
        wait_for_run(client, run_id)
        slow = SlowAdapter(ApiStyle.OPENAI_RESPONSES, delay=30)
        client.app.state.services.engine.adapters[ApiStyle.OPENAI_RESPONSES] = slow
        run_judge(client, run_id, {"judge_profile_id": judge["id"]})
        deadline = time.time() + 5
        while slow.active < 1 and time.time() < deadline:
            time.sleep(0.02)
    # Simulate a crash that left an evaluation queued, too.
    with sqlite3.connect(data / "foundry-eval.sqlite3") as conn:
        conn.execute("UPDATE evaluations SET status = 'queued', finished_at = NULL, request_sent_at = NULL")
    replay = SlowAdapter(ApiStyle.OPENAI_RESPONSES, delay=0)
    with make_client(data, adapters={ApiStyle.OPENAI_RESPONSES: replay}) as client:
        [evaluation] = evaluations(client, run_id)
        time.sleep(0.2)
    assert evaluation["status"] == "interrupted"
    assert evaluation["cost"]["status"] == "not_sent"
    assert replay.calls == 0


def test_judge_calls_respect_the_run_budget(client, mock_app):
    endpoint = create_endpoint(client)
    claude = create_profile(
        client,
        endpoint["id"],
        preset="claude-adaptive",
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        deployment="claude-test",
    )
    for category, price in (
        ("input_uncached", "3"),
        ("cache_read", "0.3"),
        ("cache_write_5m", "3.75"),
        ("cache_write_1h", "6"),
        ("output", "15"),
    ):
        set_manual(client, claude["id"], category, price)
    run_id = start_run(
        client,
        {
            "prompts": [{"user": "hi"}],
            "candidates": [{"profile_id": claude["id"], "settings": {"max_output_tokens": 50}}],
            "budget": "0.01",
        },
    )
    detail = wait_for_run(client, run_id)
    assert detail["attempts"][0]["status"] == "succeeded"
    before = mock_app.state.mock.requests["/anthropic/v1/messages"]
    payload = {"judge_profile_id": claude["id"], "settings": {"max_output_tokens": 4000}}
    preview = client.post(f"/api/runs/{run_id}/evaluations/preview", json=payload).json()
    assert preview["ok"] and Decimal(preview["worst_case_total"]) > Decimal("0.01")
    run_judge(client, run_id, payload)
    wait_for_run(client, run_id)
    [evaluation] = evaluations(client, run_id)
    assert evaluation["status"] == "skipped_budget"
    assert evaluation["cost"]["status"] == "not_sent"
    assert mock_app.state.mock.requests["/anthropic/v1/messages"] == before


# --------------------------------------------------------------------------- rubrics
def test_rubrics_are_validated_and_versions_are_immutable(client):
    bad = {
        "name": "Bad",
        "definition": {"criteria": [{"key": "a", "name": "A"}, {"key": "a", "name": "Again"}]},
    }
    response = client.post("/api/rubrics", json=bad)
    assert response.status_code == 422 and "unique" in response.text
    response = client.post(
        "/api/rubrics", json={"name": "Weighted", "definition": {"criteria": [{"key": "a", "name": "A", "weight": 0}]}}
    )
    assert response.status_code == 422 and "greater than zero" in response.text

    created = client.post(
        "/api/rubrics",
        json={
            "name": "Code review",
            "definition": {"scale_min": 0, "scale_max": 10, "criteria": [{"key": "bugs", "name": "Bugs"}]},
        },
    ).json()
    assert created["latest"]["version"] == 1
    version = client.post(
        f"/api/rubrics/{created['id']}/versions",
        json={"criteria": [{"key": "bugs", "name": "Bugs", "weight": 2}, {"key": "style", "name": "Style"}]},
    ).json()
    assert version["version"] == 2
    versions = client.get(f"/api/rubrics/{created['id']}/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    rubrics = client.get("/api/rubrics").json()
    assert rubrics[0]["id"] == "default" and rubrics[0]["is_default"]

    db = client.app.state.services.db
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE rubric_versions SET scale_max = 3 WHERE id = ?", (version["id"],))
    assert client.patch("/api/rubrics/default", json={"archived": True}).status_code == 409


# --------------------------------------------------------------------------- suites
def suite_payload(profile_id: str, judge_id: str | None = None, **extra) -> dict:
    payload = {
        "name": extra.pop("name", "Sky suite"),
        "trials": extra.pop("trials", 2),
        "prompts": [{"title": "Sky", "user": PROMPT}, {"title": "Sea", "user": "Why is the sea salty?"}],
        "candidates": [
            {"profile_id": profile_id, "settings": {"effort": "low"}},
            {"profile_id": profile_id, "settings": {"effort": "high"}},
        ],
        **extra,
    }
    if judge_id:
        payload["judge"] = {"enabled": True, "judge_profile_id": judge_id, "settings": {"max_output_tokens": 3000}}
    return payload


def test_suite_crud_keeps_prompt_ids_and_validates(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    response = client.post("/api/suites", json=suite_payload(candidate["id"]))
    assert response.status_code == 201, response.text
    suite = response.json()
    first, second = suite["prompts"]
    update = suite_payload(candidate["id"])
    update["prompts"] = [
        {"id": second["id"], "title": "Sea (edited)", "user": second["user"]},
        {"title": "New", "user": "What is a rainbow?"},
    ]
    updated = client.put(f"/api/suites/{suite['id']}", json=update).json()
    assert updated["prompts"][0]["id"] == second["id"] and updated["prompts"][0]["title"] == "Sea (edited)"
    assert first["id"] not in {p["id"] for p in updated["prompts"]}
    assert len(updated["prompts"]) == 2

    bad = suite_payload("no-such-profile")
    assert client.post("/api/suites", json=bad).status_code == 422
    unsupported = suite_payload(candidate["id"])
    unsupported["candidates"][0]["settings"] = {"temperature": 0.5}
    response = client.post("/api/suites", json=unsupported)
    assert (response.status_code == 422 and "Temperature" in response.text) or "temperature" in response.text

    client.post(f"/api/suites/{suite['id']}/archive", json={"archived": True})
    response = client.post(f"/api/suites/{suite['id']}/runs", json={}, headers={"Idempotency-Key": "idem-suite-0001"})
    assert response.status_code == 409
    assert client.get("/api/suites").json() == []
    assert len(client.get("/api/suites?include_archived=true").json()) == 1
    assert client.delete(f"/api/suites/{suite['id']}").status_code == 204


def test_suite_run_with_automatic_judging_and_comparison(client, mock_app):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge = judge_profile(client, endpoint["id"])
    suite = client.post("/api/suites", json=suite_payload(candidate["id"], judge["id"])).json()
    assert suite["judge"]["enabled"] is True
    preview = client.post(f"/api/suites/{suite['id']}/preview", json={}).json()
    assert preview["ok"], preview["errors"]
    assert preview["calls"] == 8  # 2 prompts x 2 candidates x 2 trials
    response = client.post(f"/api/suites/{suite['id']}/runs", json={}, headers={"Idempotency-Key": "idem-suite-0002"})
    assert response.status_code == 201, response.text
    run_id = response.json()["run_id"]
    detail = wait_for_run(client, run_id, timeout=40)
    assert detail["run"]["kind"] == "suite" and detail["run"]["suite_id"] == suite["id"]
    assert detail["run"]["judge_config"]["rubric_version_id"] == "default-v1"
    assert detail["run"]["status"] == "completed" and detail["run"]["judge_error"] is None
    assert len(detail["attempts"]) == 8
    judged = evaluations(client, run_id)
    assert len(judged) == 4  # one per (prompt, trial)
    assert {e["trigger"] for e in judged} == {"auto"} and {e["status"] for e in judged} == {"succeeded"}
    assert detail["judging_cost"]["counted"] == 4

    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    assert comparison["cells_total"] == 4 and comparison["cells_matched"] == 4
    assert len(comparison["candidates"]) == 2
    for item in comparison["candidates"]:
        assert item["attempts"] == 4 and item["failures"] == 0 and item["covered_cells"] == 4
        assert item["avg_judge_score"] is not None and item["judge_scored"] == 4
        assert item["judge_comparisons"] == 4 and 0 <= item["win_rate"] <= 1
        assert item["avg_latency_ms"] is not None and item["avg_input_tokens"] is not None
    assert sum(c["judge_wins"] for c in comparison["candidates"]) == pytest.approx(4)

    runs = client.get("/api/runs", params={"suite_id": suite["id"]}).json()
    assert [r["run"]["id"] for r in runs] == [run_id]


def test_comparison_coverage_across_runs_with_a_failing_candidate(client):
    endpoint = create_endpoint(client)
    good = terra(client, endpoint["id"])
    flaky = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="fail-500"
    )
    first = start_run(
        client,
        {"prompts": [{"user": "a"}, {"user": "b"}], "candidates": [{"profile_id": good["id"]}]},
        key="idem-cmp-0001",
    )
    second = start_run(
        client,
        {
            "prompts": [{"user": "a"}, {"user": "b"}],
            "candidates": [{"profile_id": good["id"]}, {"profile_id": flaky["id"]}],
        },
        key="idem-cmp-0002",
    )
    wait_for_run(client, first)
    wait_for_run(client, second)
    comparison = client.get("/api/comparisons", params={"run_id": [first, second]}).json()
    assert comparison["cells_total"] == 2
    assert comparison["cells_matched"] == 0  # the failing candidate has no usable cell
    by_label = {c["label"]: c for c in comparison["candidates"]}
    assert by_label["fail-500"]["failures"] == 2 and by_label["fail-500"]["covered_cells"] == 0
    assert by_label["Terra profile"]["runs"] == 2 and by_label["Terra profile"]["attempts"] == 4
    assert by_label["Terra profile"]["avg_cost"] is None  # no matched cells
    assert all(cell["missing"] == ["fail-500"] for cell in comparison["cells"])
    only_good = client.get(
        "/api/comparisons",
        params={"run_id": [first, second], "candidate_key": by_label["Terra profile"]["candidate_key"]},
    ).json()
    assert only_good["cells_matched"] == 2


def test_automatic_judging_failure_is_recorded_on_the_run(client):
    endpoint = create_endpoint(client)
    failing = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="fail-500"
    )
    judge = judge_profile(client, endpoint["id"])
    payload = suite_payload(failing["id"], judge["id"], trials=1)
    payload["candidates"] = [{"profile_id": failing["id"]}]
    suite = client.post("/api/suites", json=payload).json()
    run_id = client.post(
        f"/api/suites/{suite['id']}/runs", json={}, headers={"Idempotency-Key": "idem-suite-0003"}
    ).json()["run_id"]
    detail = wait_for_run(client, run_id)
    assert detail["run"]["status"] == "completed_with_errors"
    assert "Automatic judging did not start" in detail["run"]["judge_error"]
    assert "no succeeded responses" in detail["run"]["judge_error"]
    assert evaluations(client, run_id) == []


def test_automatic_judging_needs_a_usable_judge_before_the_run_starts(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    keyless = client.post(
        "/api/endpoints",
        json={"name": "No key", "base_url": "https://other.services.ai.azure.com", "region": "eastus2"},
    ).json()
    judge = judge_profile(client, keyless["id"])
    suite = client.post("/api/suites", json=suite_payload(candidate["id"], judge["id"])).json()
    preview = client.post(f"/api/suites/{suite['id']}/preview", json={}).json()
    assert not preview["ok"]
    assert any(e.startswith("Automatic judging: No API key") for e in preview["errors"])
    response = client.post(f"/api/suites/{suite['id']}/runs", json={}, headers={"Idempotency-Key": "idem-suite-0004"})
    assert response.status_code == 422


# --------------------------------------------------------------------------- projections
def test_projections_reprice_each_attempt_and_switch_price_basis(client):
    endpoint = create_endpoint(client)
    claude = create_profile(
        client,
        endpoint["id"],
        preset="claude-adaptive",
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        deployment="claude-test",
    )
    rates = {
        "input_uncached": "3",
        "cache_read": "0.3",
        "cache_write_5m": "3.75",
        "cache_write_1h": "6",
        "output": "15",
    }
    for category, price in rates.items():
        set_manual(client, claude["id"], category, price)
    run_id = start_run(
        client,
        {
            "prompts": [{"user": "hello there"}],
            "candidates": [{"profile_id": claude["id"], "settings": {"max_output_tokens": 200}}],
            "trials": 3,
        },
    )
    detail = wait_for_run(client, run_id)
    totals = [Decimal(a["cost"]["total"]) for a in detail["attempts"]]
    expected = sum(totals) / len(totals)

    def project(**body) -> dict:
        response = client.post("/api/projections", json={"run_ids": [run_id], "monthly_requests": 50000, **body})
        assert response.status_code == 200, response.text
        return response.json()

    original = project()
    [item] = original["candidates"]
    assert item["attempts_used"] == 3 and item["attempts_excluded"] == 0 and item["status"] == "complete"
    assert Decimal(item["cost_per_request"]) == expected
    assert Decimal(item["cost_per_1k_requests"]) == expected * 1000
    assert Decimal(item["cost_per_month"]) == expected * 50000
    assert "prices: the price snapshot each run was created with" in original["assumptions"]

    # Output twice as long: only the output part doubles.
    output_costs = [
        sum((Decimal(line["cost"]) for line in a["cost"]["lines"] if line["category"] == "output"), Decimal(0))
        for a in detail["attempts"]
    ]
    doubled = project(what_if={"output_multiplier": "2"})["candidates"][0]
    assert Decimal(doubled["cost_per_request"]) == (sum(totals) + sum(output_costs)) / 3
    assert "output length x2" in project(what_if={"output_multiplier": "2"})["assumptions"]

    # New rates: the original basis keeps the run's snapshot, the current basis uses today's rates.
    set_manual(client, claude["id"], "output", "150")
    assert project()["candidates"][0]["cost_per_request"] == item["cost_per_request"]
    current = project(price_basis="current")["candidates"][0]
    assert Decimal(current["cost_per_request"]) > expected
    assert current["warnings"] == []


def test_projection_exclusions_and_inapplicable_shares(client):
    endpoint = create_endpoint(client)
    standard = create_profile(
        client, endpoint["id"], preset="openai-standard", api_style=ApiStyle.OPENAI_RESPONSES, deployment="gpt-4.1"
    )
    for category, price in (("input_uncached", "2"), ("cache_read", "0.5"), ("output", "8")):
        set_manual(client, standard["id"], category, price)
    no_usage = create_profile(
        client,
        endpoint["id"],
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="no-usage",
        streaming=False,  # a complete JSON response without a usage object: succeeded, usage unknown
    )
    failing = create_profile(
        client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment="fail-500"
    )
    run_id = start_run(
        client,
        {
            "prompts": [{"user": "q"}],
            "candidates": [
                {"profile_id": standard["id"]},
                {"profile_id": no_usage["id"]},
                {"profile_id": failing["id"]},
            ],
        },
    )
    wait_for_run(client, run_id)
    result = client.post(
        "/api/projections",
        json={"run_ids": [run_id], "what_if": {"cache_write_share": "0.2", "cache_read_share": "0.5"}},
    ).json()
    by_label = {c["label"]: c for c in result["candidates"]}
    used = by_label["gpt-4.1"]
    assert used["status"] == "complete" and used["cache_write_applicable"] is False and used["cache_read_applicable"]
    assert any("cache write share was not applied" in w for w in used["warnings"])
    skipped = by_label["no-usage"]
    assert skipped["status"] == "unavailable" and skipped["attempts_excluded"] == 1
    assert skipped["exclusion_reasons"] == ["1 excluded: usage not reported"]
    assert by_label["fail-500"]["exclusion_reasons"] == ["1 excluded: did not succeed"]
    response = client.post(
        "/api/projections",
        json={"run_ids": [run_id], "what_if": {"cache_read_share": "0.8", "cache_write_share": "0.3"}},
    )
    assert response.status_code == 422
