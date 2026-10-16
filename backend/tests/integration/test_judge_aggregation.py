"""Judge evidence aggregation and position-swap passes, end to end against the mock Foundry judge.

The mock judge is purely positional: whatever is shown first (label A) scores 4.25 on the default
rubric, the second 4.0, and the first is preferred. A swap pass therefore always flips its preference,
and an answer judged in both positions averages 4.125 with a spread of 0.25.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from foundry_eval.domain.profiles import ProfileFields
from foundry_eval.judging.service import SwapRequest
from foundry_eval.providers.base import ProviderCall, ProviderResult
from foundry_eval.repos.endpoints import ConflictError
from foundry_eval.types import ApiStyle, FinishReason
from tests.api_support import create_endpoint, create_profile, set_manual, start_run, wait_for_run

FIRST, SECOND = 4.25, 4.0  # the mock judge's weighted overall for positions A and B


def terra(client: TestClient, endpoint_id: str) -> dict:
    return create_profile(
        client,
        endpoint_id,
        preset="openai-reasoning-cache-write",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-5.6-terra",
        name="Terra",
        model_name="gpt-5.6-terra",
    )


def judge_profile(client: TestClient, endpoint_id: str, deployment: str = "gpt-4.1-judge") -> dict:
    return create_profile(
        client,
        endpoint_id,
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment=deployment,
        name="Judge",
        model_name="gpt-4.1",
        context_window=100_000,
    )


def effort_run(client: TestClient, profile_id: str, prompts: list[str], key: str = "idem-agg-run-1") -> str:
    """Two candidates of one profile whose answers differ by their effort (so their texts can be told apart)."""
    run_id = start_run(
        client,
        {
            "prompts": [{"user": p} for p in prompts],
            "candidates": [
                {"profile_id": profile_id, "settings": {"effort": "low"}},
                {"profile_id": profile_id, "settings": {"effort": "high"}},
            ],
        },
        key=key,
    )
    wait_for_run(client, run_id)
    return run_id


def wait_judging(client: TestClient, run_id: str) -> list[dict]:
    deadline = time.time() + 20
    while time.time() < deadline:
        found = client.get(f"/api/runs/{run_id}/evaluations").json()
        if found and all(e["status"] not in ("queued", "running") for e in found):
            return found
        time.sleep(0.05)
    raise AssertionError("judging did not finish")


def judge(client: TestClient, run_id: str, payload: dict, key: str) -> list[str]:
    response = client.post(f"/api/runs/{run_id}/evaluations", json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    wait_judging(client, run_id)
    return response.json()["evaluation_ids"]


def swap(client: TestClient, run_id: str, payload: dict, key: str) -> list[str]:
    response = client.post(f"/api/runs/{run_id}/evaluations/swap", json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    wait_judging(client, run_id)
    return response.json()["evaluation_ids"]


def evaluation(client: TestClient, evaluation_id: str) -> dict:
    return client.get(f"/api/evaluations/{evaluation_id}").json()


def rubric(client: TestClient, name: str) -> str:
    created = client.post(
        "/api/rubrics", json={"name": name, "definition": {"criteria": [{"key": "accuracy", "name": "Accuracy"}]}}
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


# --------------------------------------------------------------------------- swap pass
def test_a_swap_pass_reverses_the_order_and_is_averaged_with_its_source(client, mock_app):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["Why is the sky blue?"])
    [source_id] = judge(client, run_id, {"judge_profile_id": judge_id, "settings": {"temperature": 0.2}}, "idem-src-1")
    source = evaluation(client, source_id)

    preview = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": source_id}).json()
    assert preview["ok"], preview["errors"]
    assert preview["calls"] == 1 and [g["swap_of"] for g in preview["groups"]] == [source_id]
    assert preview["rubric_version_id"] == source["rubric_version_id"]

    calls = mock_app.state.mock.requests["/openai/v1/responses"]
    [swap_id] = swap(client, run_id, {"evaluation_id": source_id}, "idem-swap-1")
    assert mock_app.state.mock.requests["/openai/v1/responses"] == calls + 1
    swapped = evaluation(client, swap_id)
    assert swapped["status"] == "succeeded" and swapped["swap_of"] == source_id
    # Exactly the reversed presentation order, labelled by position; same judge, rubric and settings.
    assert [o["attempt_id"] for o in swapped["order"]] == [o["attempt_id"] for o in reversed(source["order"])]
    assert [o["label"] for o in swapped["order"]] == ["A", "B"]
    assert swapped["rubric_version_id"] == source["rubric_version_id"]
    assert swapped["judge_settings"]["temperature"] == 0.2
    texts = {a["id"]: a["output_text"] for a in client.get(f"/api/runs/{run_id}").json()["attempts"]}
    body = client.get(f"/api/evaluations/{swap_id}/raw").json()["request_body"]
    sent = str(body["input"])
    shown_first = sent.index("Response A (begin")
    assert sent.index(texts[source["order"][-1]["attempt_id"]]) > shown_first
    assert sent.index(texts[source["order"][-1]["attempt_id"]]) < sent.index("Response B (begin")

    # The same key returns the same evaluations without another paid call.
    again = client.post(
        f"/api/runs/{run_id}/evaluations/swap",
        json={"evaluation_id": source_id},
        headers={"Idempotency-Key": "idem-swap-1"},
    )
    assert again.status_code == 200 and again.json() == {"evaluation_ids": [swap_id], "created": False}
    assert mock_app.state.mock.requests["/openai/v1/responses"] == calls + 1

    # Both evaluations count: each answer was shown first once and second once.
    detail = client.get(f"/api/runs/{run_id}").json()
    for attempt_id, aggregate in detail["judge_aggregates"].items():
        assert aggregate["mean_weighted_overall"] == pytest.approx((FIRST + SECOND) / 2)
        assert aggregate["n_evaluations"] == 2 and aggregate["spread"] == pytest.approx(FIRST - SECOND)
        assert aggregate["win_share"] == pytest.approx(0.5)
        assert aggregate["evaluation_ids"] == [source_id, swap_id] and aggregate["left_out"] == 0
        # The run view keeps showing the latest single evaluation.
        assert detail["judge_scores"][attempt_id]["evaluation_id"] == swap_id
    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    assert comparison["judge_bases"] == 1
    for item in comparison["candidates"]:
        assert item["avg_judge_score"] == pytest.approx((FIRST + SECOND) / 2)
        assert item["judge_evaluations"] == 2 and item["judge_comparisons"] == 2
        assert item["judge_spread"] == pytest.approx(FIRST - SECOND) and item["judge_spread_cells"] == 1
        assert item["win_rate"] == pytest.approx(0.5)
    [cell] = comparison["cells"]
    assert cell["judge_evaluations"] == 2 and cell["judge_left_out"] == 0
    assert cell["judge_spread"] == pytest.approx(FIRST - SECOND)

    # Position bias per judge basis: the positional mock judge flips every time.
    [group] = client.get("/api/analysis/calibration", params={"run_id": run_id}).json()["groups"]
    assert (group["swap_pairs"], group["swap_flips"], group["swap_consistent"]) == (1, 1, 0)
    assert group["flip_rate"] == 1.0 and group["consistency_rate"] == 0.0


def test_a_swap_pass_is_refused_when_it_would_not_pair(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [source_id] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-src-2")
    [swap_id] = swap(client, run_id, {"evaluation_id": source_id}, "idem-swap-2")

    def preview(payload: dict) -> dict:
        return client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json=payload).json()

    assert any("already has a swap pass" in e for e in preview({"evaluation_id": source_id})["errors"])
    assert any("itself a swap pass" in e for e in preview({"evaluation_id": swap_id})["errors"])
    assert preview({"evaluation_id": "no-such-evaluation"})["errors"] == [
        "The evaluation to swap does not exist in this run."
    ]
    refused = client.post(
        f"/api/runs/{run_id}/evaluations/swap",
        json={"evaluation_id": source_id},
        headers={"Idempotency-Key": "idem-swap-3"},
    )
    assert refused.status_code == 422 and "already has a swap pass" in refused.json()["detail"]
    # A key already used for ordinary judging cannot start a swap pass, and the reverse.
    reused = client.post(
        f"/api/runs/{run_id}/evaluations/swap",
        json={"evaluation_id": source_id},
        headers={"Idempotency-Key": "idem-src-2"},
    )
    assert reused.status_code == 409
    reused = client.post(
        f"/api/runs/{run_id}/evaluations",
        json={"judge_profile_id": judge_id},
        headers={"Idempotency-Key": "idem-swap-2"},
    )
    assert reused.status_code == 409

    # The judge must still be the same: a changed deployment would not be like for like.
    other_id = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-src-3")[0]
    profile = client.get(f"/api/profiles/{judge_id}").json()
    fields = {k: v for k, v in profile.items() if k in ProfileFields.model_fields}
    updated = client.put(f"/api/profiles/{judge_id}", json={**fields, "deployment": "gpt-4.1-judge-2"})
    assert updated.status_code == 200, updated.text
    assert any("judge profile changed" in e for e in preview({"evaluation_id": other_id})["errors"])


def test_only_a_succeeded_comparison_of_answers_can_be_swapped(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    malformed = judge_profile(client, endpoint["id"], deployment="judge-malformed")["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [bad] = judge(client, run_id, {"judge_profile_id": malformed}, "idem-src-bad")
    assert evaluation(client, bad)["status"] == "malformed"
    preview = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": bad}).json()
    assert preview["errors"] == ["Only a succeeded evaluation can be swapped."] and preview["calls"] == 0

    single = start_run(
        client, {"prompts": [{"user": "Alone"}], "candidates": [{"profile_id": candidate["id"]}]}, key="idem-single"
    )
    wait_for_run(client, single)
    [alone] = judge(client, single, {"judge_profile_id": judge_profile(client, endpoint["id"])["id"]}, "idem-src-alone")
    preview = client.post(f"/api/runs/{single}/evaluations/swap/preview", json={"evaluation_id": alone}).json()
    assert any("fewer than two answers" in e for e in preview["errors"]), preview["errors"]
    # An evaluation of another run is not found in this one.
    preview = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": alone}).json()
    assert preview["errors"] == ["The evaluation to swap does not exist in this run."]


def test_a_judge_profile_edited_after_the_preview_stops_the_swap(client, mock_app):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [source_id] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-src-toctou")
    services = client.app.state.services
    previewed = services.judge.preview_swap

    def preview_then_edit(run: str, request: SwapRequest):
        result = previewed(run, request)
        # Another tab edits the judge between this request's preview and its insert.
        profile = services.profiles.get(judge_id)
        fields = ProfileFields.model_validate(
            {**profile.model_dump(include=set(ProfileFields.model_fields)), "deployment": "gpt-4.1-judge-3"}
        )
        services.profiles.update(judge_id, fields)
        return result

    services.judge.preview_swap = preview_then_edit
    calls = mock_app.state.mock.requests["/openai/v1/responses"]
    response = client.post(
        f"/api/runs/{run_id}/evaluations/swap",
        json={"evaluation_id": source_id},
        headers={"Idempotency-Key": "idem-toctou"},
    )
    assert response.status_code == 422 and "changed since the preview" in response.json()["detail"]
    assert len(client.get(f"/api/runs/{run_id}/evaluations").json()) == 1
    assert mock_app.state.mock.requests["/openai/v1/responses"] == calls


def test_concurrent_swap_requests_with_different_keys_create_one_swap(client):
    """The already-swapped check is repeated under the write lock, after each request's preview."""
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [source_id] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-src-race")
    service = client.app.state.services.judge
    # Both requests planned (and previewed) before either was written.
    plans = [service._swap_plan(run_id, SwapRequest(evaluation_id=source_id)) for _ in range(2)]
    setups = [service._resolve(plan.request, []) for plan in plans if plan.request is not None]
    ids, created = service._insert(run_id, plans[0].request, setups[0], plans[0].groups, "idem-race-1", "manual")
    assert created and len(ids) == 1
    with pytest.raises(ConflictError):
        service._insert(run_id, plans[1].request, setups[1], plans[1].groups, "idem-race-2", "manual")
    assert [e["swap_of"] for e in client.get(f"/api/runs/{run_id}/evaluations").json()] == [None, source_id]


def test_a_run_wide_swap_pass_takes_the_latest_unswapped_evaluation_per_prompt(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One", "Two"])
    first, second = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-src-4")
    swap(client, run_id, {"evaluation_id": first}, "idem-swap-4")
    # Another rubric is another basis: a run-wide pass on the default rubric never touches it.
    judge(client, run_id, {"judge_profile_id": judge_id, "rubric_id": rubric(client, "Other")}, "idem-src-5")
    # Prompt two judged again on the default basis: one swap per prompt and trial, of the most recent.
    prompt_one, prompt_two = (p["id"] for p in client.get(f"/api/runs/{run_id}").json()["prompts"])
    [newer] = judge(client, run_id, {"judge_profile_id": judge_id, "prompt_id": prompt_two}, "idem-src-5b")
    # Prompt one already has a swap: a newer evaluation there does not make the run-wide pass pay again.
    judge(client, run_id, {"judge_profile_id": judge_id, "prompt_id": prompt_one}, "idem-src-5c")

    payload = {"evaluation_id": second, "scope": "run"}
    preview = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json=payload).json()
    assert preview["ok"], preview["errors"]
    assert [g["swap_of"] for g in preview["groups"]] == [newer]
    [warning] = [w for w in preview["warnings"] if "left out of this swap pass" in w]
    assert "1 because it already has a swap pass" in warning
    assert "1 because its prompt and trial already have a swap pass on this basis" in warning
    assert "1 because a more recent evaluation of the same prompt and trial is swapped instead" in warning
    [swap_id] = swap(client, run_id, payload, "idem-swap-5")
    assert evaluation(client, swap_id)["swap_of"] == newer
    # Every prompt and trial now has a swap: another run-wide pass has nothing to pay for.
    again = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json=payload).json()
    assert not again["ok"] and again["calls"] == 0
    assert "There is nothing to swap in this scope." in again["errors"]

    groups = client.get("/api/analysis/calibration", params={"run_id": run_id}).json()["groups"]
    by_rubric = {g["rubric"]: g for g in groups}
    assert by_rubric["Default rubric v1"]["swap_pairs"] == 2 and by_rubric["Default rubric v1"]["swap_flips"] == 2
    assert by_rubric["Other v1"]["swap_pairs"] == 0 and by_rubric["Other v1"]["flip_rate"] is None


def test_a_swap_pass_goes_through_the_run_budget_and_is_never_retried(client, mock_app):
    endpoint = create_endpoint(client)
    claude = create_profile(
        client,
        endpoint["id"],
        preset="claude-adaptive",
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        deployment="claude-judge",
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
            "candidates": [
                {"profile_id": claude["id"], "settings": {"max_output_tokens": 50}},
                {"profile_id": claude["id"], "settings": {"max_output_tokens": 60}},
            ],
            "budget": "0.5",
        },
    )
    wait_for_run(client, run_id)
    [source_id] = judge(
        client, run_id, {"judge_profile_id": claude["id"], "settings": {"max_output_tokens": 1000}}, "idem-src-6"
    )
    assert evaluation(client, source_id)["status"] == "succeeded"

    # Dearer output since: the swap's worst case no longer fits the remaining budget.
    set_manual(client, claude["id"], "output", "1000")
    preview = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": source_id}).json()
    assert preview["ok"], preview["errors"]
    remaining = Decimal("0.5") - Decimal(client.get(f"/api/runs/{run_id}").json()["budget"]["committed"])
    assert Decimal(preview["worst_case_total"]) > remaining
    assert any("above the run's remaining budget" in w for w in preview["warnings"]), preview["warnings"]
    before = mock_app.state.mock.requests["/anthropic/v1/messages"]
    [swap_id] = swap(client, run_id, {"evaluation_id": source_id}, "idem-swap-6")
    skipped = evaluation(client, swap_id)
    assert skipped["status"] == "skipped_budget" and skipped["cost"]["status"] == "not_sent"
    assert mock_app.state.mock.requests["/anthropic/v1/messages"] == before
    # Nothing retries it; only a new explicit request (a new key) could swap the source again.
    time.sleep(0.2)
    assert len(client.get(f"/api/runs/{run_id}/evaluations").json()) == 2
    again = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": source_id}).json()
    assert again["ok"] and again["calls"] == 1


def test_a_swap_pass_needs_a_bounded_cost_on_a_budgeted_run(client):
    endpoint = create_endpoint(client)
    claude = create_profile(
        client,
        endpoint["id"],
        preset="claude-adaptive",
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        deployment="claude-judge",
    )
    for category, price in (
        ("input_uncached", "3"),
        ("cache_read", "0.3"),
        ("cache_write_5m", "3.75"),
        ("output", "15"),
    ):
        set_manual(client, claude["id"], category, price)
    set_manual(client, claude["id"], "cache_write_1h", "6")
    run_id = start_run(
        client,
        {
            "prompts": [{"user": "hi"}],
            "candidates": [
                {"profile_id": claude["id"], "settings": {"max_output_tokens": 50}},
                {"profile_id": claude["id"], "settings": {"max_output_tokens": 60}},
            ],
            "budget": "0.5",
        },
    )
    wait_for_run(client, run_id)
    [source_id] = judge(
        client, run_id, {"judge_profile_id": claude["id"], "settings": {"max_output_tokens": 1000}}, "idem-src-7"
    )
    # The output rate becomes unknown: the swap's cost can no longer be bounded, so it is refused.
    cleared = client.put(f"/api/profiles/{claude['id']}/rates/output", json={"mode": "azure_auto"})
    assert cleared.status_code == 200, cleared.text
    preview = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": source_id}).json()
    assert not preview["ok"] and any("This run has a budget" in e for e in preview["errors"]), preview["errors"]


# --------------------------------------------------------------------------- aggregation
def test_each_prompt_and_trial_uses_the_basis_with_the_most_evaluations_then_the_latest(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    other_rubric = rubric(client, "One criterion")
    run_id = effort_run(client, candidate["id"], ["Hello"])
    [a1] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-basis-a1")
    [b1] = judge(client, run_id, {"judge_profile_id": judge_id, "rubric_id": other_rubric}, "idem-basis-b1")

    # One each: the most recent basis is used and the other evaluation is left out, with a note.
    detail = client.get(f"/api/runs/{run_id}").json()
    assert {tuple(a["evaluation_ids"]) for a in detail["judge_aggregates"].values()} == {(b1,)}
    assert {a["left_out"] for a in detail["judge_aggregates"].values()} == {1}
    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    assert comparison["judge_bases"] == 1 and comparison["cells"][0]["judge_left_out"] == 1
    assert any("1 judge evaluation used another rubric version" in n for n in comparison["notes"]), comparison["notes"]

    # Two beat one, although the other is more recent.
    [a2] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-basis-a2")
    by_id = {e["id"]: e for e in client.get(f"/api/runs/{run_id}/evaluations").json()}
    expected: dict[str, float] = {}
    for evaluation_id in (a1, a2):
        for c in by_id[evaluation_id]["candidates"]:
            expected[c["attempt_id"]] = expected.get(c["attempt_id"], 0.0) + c["weighted_overall"] / 2
    detail = client.get(f"/api/runs/{run_id}").json()
    for attempt_id, aggregate in detail["judge_aggregates"].items():
        assert aggregate["evaluation_ids"] == [a1, a2] and aggregate["left_out"] == 1
        assert aggregate["mean_weighted_overall"] == pytest.approx(expected[attempt_id])
    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    keys = {c["id"]: c["candidate_key"] for c in detail["candidates"]}
    by_key = {c["candidate_key"]: c for c in comparison["candidates"]}
    for attempt in detail["attempts"]:
        item = by_key[keys[attempt["candidate_id"]]]
        assert item["avg_judge_score"] == pytest.approx(expected[attempt["id"]])
        assert item["judge_evaluations"] == 2
    assert comparison["judge_bases"] == 1 and comparison["cells"][0]["judge_evaluations"] == 2


def test_calibration_counts_an_answer_judged_twice_once(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [source_id] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-cal-src")
    swap(client, run_id, {"evaluation_id": source_id}, "idem-cal-swap")
    for value, attempt in zip((5, 2), client.get(f"/api/runs/{run_id}").json()["attempts"], strict=True):
        response = client.put(f"/api/attempts/{attempt['id']}/manual-score", json={"score": value, "notes": ""})
        assert response.status_code == 200, response.text
    [group] = client.get("/api/analysis/calibration", params={"run_id": run_id}).json()["groups"]
    assert group["pairs"] == 2  # two answers, not four (answer, evaluation) pairs
    # The two evaluations preferred different answers, so the judge has no pick to compare with yours.
    assert group["top_pick_total"] == 0


def test_evaluations_of_answers_retried_since_are_left_out(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [source_id] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-stale-src")
    attempt_id = client.get(f"/api/runs/{run_id}").json()["attempts"][0]["id"]
    retried = client.post(f"/api/attempts/{attempt_id}/retry", json={}, headers={"Idempotency-Key": "idem-stale-retry"})
    assert retried.status_code == 200, retried.text
    wait_for_run(client, run_id)

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["judge_aggregates"] == {}
    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    assert all(c["avg_judge_score"] is None and c["win_rate"] is None for c in comparison["candidates"])
    assert comparison["cells"][0]["judge_left_out"] == 1
    assert any("retried since" in n for n in comparison["notes"]), comparison["notes"]
    preview = client.post(f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": source_id}).json()
    assert any("retried since" in e for e in preview["errors"]), preview["errors"]


def test_swap_pairs_of_answers_retried_since_are_left_out(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [source_id] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-pair-src")
    swap(client, run_id, {"evaluation_id": source_id}, "idem-pair-swap")
    [group] = client.get("/api/analysis/calibration", params={"run_id": run_id}).json()["groups"]
    assert group["swap_pairs"] == 1
    attempt_id = client.get(f"/api/runs/{run_id}").json()["attempts"][0]["id"]
    retried = client.post(f"/api/attempts/{attempt_id}/retry", json={}, headers={"Idempotency-Key": "idem-pair-retry"})
    assert retried.status_code == 200, retried.text
    wait_for_run(client, run_id)
    groups = client.get("/api/analysis/calibration", params={"run_id": run_id}).json()["groups"]
    assert all(g["swap_pairs"] == 0 for g in groups)


def test_a_run_wide_pass_swaps_a_prompt_whose_only_swap_is_of_retried_answers(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = effort_run(client, candidate["id"], ["One"])
    [old] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-redo-src")
    swap(client, run_id, {"evaluation_id": old}, "idem-redo-swap")
    attempt_id = client.get(f"/api/runs/{run_id}").json()["attempts"][0]["id"]
    retried = client.post(f"/api/attempts/{attempt_id}/retry", json={}, headers={"Idempotency-Key": "idem-redo-retry"})
    assert retried.status_code == 200, retried.text
    wait_for_run(client, run_id)
    [new] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-redo-src-2")
    preview = client.post(
        f"/api/runs/{run_id}/evaluations/swap/preview", json={"evaluation_id": new, "scope": "run"}
    ).json()
    assert preview["ok"], preview["errors"]
    assert [g["swap_of"] for g in preview["groups"]] == [new]
    assert any("1 because an answer it judged was retried since" in w for w in preview["warnings"]), preview["warnings"]


class FailsOnce:
    """Chat Completions stand-in whose first call for one deployment fails; every other call succeeds."""

    api_style = ApiStyle.OPENAI_CHAT

    def __init__(self, deployment: str) -> None:
        self.deployment = deployment
        self.failed = False

    async def execute(self, call: ProviderCall) -> ProviderResult:
        model = call.body["model"]
        if model == self.deployment and not self.failed:
            self.failed = True
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
        return ProviderResult(
            ok=True,
            request_body=call.body,
            streamed=False,
            latency_ms=5,
            raw_usage={"prompt_tokens": 10, "completion_tokens": 5},
            output_text=f"answer from {model}",
            finish_reason=FinishReason.COMPLETED,
            http_status=200,
        )


def test_an_evaluation_from_before_a_retry_added_an_answer_is_not_mixed_in(client):
    """The review's blocker: {a, b} judged while c had failed, then {a, b, c} after c was retried."""
    client.app.state.services.engine.adapters[ApiStyle.OPENAI_CHAT] = FailsOnce("deepseek-c")
    endpoint = create_endpoint(client)
    chats = [
        create_profile(
            client, endpoint["id"], preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment=f"deepseek-{x}"
        )
        for x in "abc"
    ]
    judge_id = judge_profile(client, endpoint["id"])["id"]
    run_id = start_run(
        client, {"prompts": [{"user": "Hi"}], "candidates": [{"profile_id": c["id"]} for c in chats]}, key="idem-grow"
    )
    detail = wait_for_run(client, run_id)
    [failed] = [a for a in detail["attempts"] if a["status"] != "succeeded"]
    [before] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-grow-j1")
    swap(client, run_id, {"evaluation_id": before}, "idem-grow-swap")  # two evaluations of {a, b}
    retried = client.post(f"/api/attempts/{failed['id']}/retry", json={}, headers={"Idempotency-Key": "idem-grow-r"})
    assert retried.status_code == 200, retried.text
    wait_for_run(client, run_id)
    [after] = judge(client, run_id, {"judge_profile_id": judge_id}, "idem-grow-j2")  # one evaluation of {a, b, c}
    assert len(evaluation(client, after)["order"]) == 3

    detail = client.get(f"/api/runs/{run_id}").json()
    assert {tuple(a["evaluation_ids"]) for a in detail["judge_aggregates"].values()} == {(after,)}
    assert {a["left_out"] for a in detail["judge_aggregates"].values()} == {2}
    comparison = client.get("/api/comparisons", params={"run_id": run_id}).json()
    # One matched cell, one evaluation of all three answers: the preference shares sum to exactly 1.
    assert sum(c["judge_wins"] for c in comparison["candidates"]) == pytest.approx(1.0)
    assert all(c["judge_comparisons"] == 1 and c["judge_evaluations"] == 1 for c in comparison["candidates"])
    assert comparison["cells"][0]["judge_left_out"] == 2
    assert any("judged fewer answers" in n for n in comparison["notes"]), comparison["notes"]


def test_withheld_judge_cells_report_their_evaluations_as_left_out(client):
    endpoint = create_endpoint(client)
    candidate = terra(client, endpoint["id"])
    judge_id = judge_profile(client, endpoint["id"])["id"]
    first = effort_run(client, candidate["id"], ["Same prompt"], key="idem-withheld-1")
    judge(client, first, {"judge_profile_id": judge_id}, "idem-withheld-j1")
    second = effort_run(client, candidate["id"], ["Same prompt"], key="idem-withheld-2")
    judge(
        client, second, {"judge_profile_id": judge_id, "rubric_id": rubric(client, "Other basis")}, "idem-withheld-j2"
    )
    comparison = client.get("/api/comparisons", params={"run_id": [first, second]}).json()
    assert comparison["judge_bases"] == 2
    [cell] = comparison["cells"]
    assert cell["judge_evaluations"] == 0 and cell["judge_left_out"] == 2 and cell["judge_spread"] is None
