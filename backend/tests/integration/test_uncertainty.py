"""Comparison intervals across prompts, paired differences to a baseline, and judge calibration."""

from __future__ import annotations

import math
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from foundry_eval.analysis.calibration import spearman
from foundry_eval.types import ApiStyle
from tests.api_support import create_endpoint, create_profile, set_manual, start_run, wait_for_run


def chat_profile(client: TestClient, endpoint_id: str, deployment: str) -> dict:
    profile = create_profile(
        client, endpoint_id, preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment=deployment
    )
    set_manual(client, profile["id"], "input_uncached", "1")
    set_manual(client, profile["id"], "output", "10")
    return profile


def score(client: TestClient, attempt_id: str, value: int) -> None:
    response = client.put(f"/api/attempts/{attempt_id}/manual-score", json={"score": value, "notes": ""})
    assert response.status_code == 200, response.text


def attempts_by(detail: dict, profiles: dict[str, str]) -> dict[tuple[str, str, int], dict]:
    """(profile deployment, prompt text, trial) -> attempt."""
    prompts = {p["id"]: p["user_text"] for p in detail["prompts"]}
    candidates = {c["id"]: profiles[c["profile_id"]] for c in detail["candidates"]}
    return {(candidates[a["candidate_id"]], prompts[a["prompt_id"]], a["trial_index"]): a for a in detail["attempts"]}


def test_intervals_count_prompts_and_differences_have_verdicts(client):
    endpoint = create_endpoint(client)
    good = chat_profile(client, endpoint["id"], "deepseek-good")
    base = chat_profile(client, endpoint["id"], "deepseek-base")
    names = {good["id"]: "good", base["id"]: "base"}
    prompts = ["p1", "p2", "p3", "p4"]
    request = {
        "prompts": [{"user": p} for p in prompts],
        "candidates": [{"profile_id": good["id"]}, {"profile_id": base["id"]}],
        "trials": 2,
    }
    run_id = start_run(client, request, key="idem-uncertainty-1")
    detail = wait_for_run(client, run_id)
    attempts = attempts_by(detail, names)
    good_scores = {"p1": 5, "p2": 5, "p3": 4, "p4": 5}
    base_scores = {"p1": 2, "p2": 3, "p3": 2, "p4": 2}
    for trial in (1, 2):
        for prompt in prompts:
            score(client, attempts[("good", prompt, trial)]["id"], good_scores[prompt])
            score(client, attempts[("base", prompt, trial)]["id"], base_scores[prompt])

    plain = client.get("/api/comparisons", params={"run_id": run_id}).json()
    assert plain["prompts_matched"] == 4 and plain["interval_min_prompts"] == 3 and plain["baseline"] is None
    keys = {c["label"]: c["candidate_key"] for c in plain["candidates"]}
    by = {c["label"]: c for c in plain["candidates"]}
    # Two trials per prompt are averaged first: the interval counts 4 prompts, not 8 cells.
    assert by["deepseek-good"]["manual_scored"] == 8 and by["deepseek-good"]["manual_interval"]["prompts"] == 4
    assert by["deepseek-good"]["cost_interval"]["prompts"] == 4
    assert Decimal(by["deepseek-good"]["cost_interval"]["low"]) >= 0
    assert all(c["vs_baseline"] is None for c in plain["candidates"])

    compared = client.get("/api/comparisons", params={"run_id": run_id, "baseline": keys["deepseek-base"]}).json()
    assert compared["baseline"] == keys["deepseek-base"]
    by = {c["label"]: c for c in compared["candidates"]}
    assert by["deepseek-base"]["vs_baseline"] is None
    manual = by["deepseek-good"]["vs_baseline"]["manual"]
    diffs = [3.0, 2.0, 2.0, 3.0]
    sd = math.sqrt(sum((d - 2.5) ** 2 for d in diffs) / 3)
    assert manual["mean"] == pytest.approx(2.5) and manual["prompts"] == 4 and manual["verdict"] == "higher"
    assert manual["low"] == pytest.approx(2.5 - 3.182 * sd / 2)
    assert by["deepseek-good"]["vs_baseline"]["cost"]["prompts"] == 4

    unknown = client.get("/api/comparisons", params={"run_id": run_id, "baseline": "no-such-key"}).json()
    assert unknown["baseline"] is None and any("baseline" in n for n in unknown["notes"])


def wait_judging(client: TestClient, run_id: str) -> list[dict]:
    deadline = time.time() + 20
    while time.time() < deadline:
        found = client.get(f"/api/runs/{run_id}/evaluations").json()
        if found and all(e["status"] not in ("queued", "running") for e in found):
            return found
        time.sleep(0.05)
    raise AssertionError("judging did not finish")


def test_calibration_compares_the_judge_with_manual_scores(client):
    endpoint = create_endpoint(client)
    first = chat_profile(client, endpoint["id"], "deepseek-c1")
    second = chat_profile(client, endpoint["id"], "deepseek-c2")
    names = {first["id"]: "c1", second["id"]: "c2"}
    judge = create_profile(
        client,
        endpoint["id"],
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-4.1-judge",
        model_name="gpt-4.1",
        context_window=100_000,
        name="Calibrated judge",
    )
    prompts = [f"q{i}" for i in range(5)]
    request = {
        "prompts": [{"user": p} for p in prompts],
        "candidates": [{"profile_id": first["id"]}, {"profile_id": second["id"]}],
    }
    run_id = start_run(client, request, key="idem-calibration-1")
    detail = wait_for_run(client, run_id)
    response = client.post(
        f"/api/runs/{run_id}/evaluations",
        json={"judge_profile_id": judge["id"]},
        headers={"Idempotency-Key": "idem-cal-j"},
    )
    assert response.status_code == 201, response.text
    evaluations = wait_judging(client, run_id)
    attempts = attempts_by(detail, names)
    manual: dict[str, int] = {}
    for index, prompt in enumerate(prompts):
        manual[attempts[("c1", prompt, 1)]["id"]] = 5 if index % 2 else 3
        manual[attempts[("c2", prompt, 1)]["id"]] = 2 if index % 2 else 4
    for attempt_id, value in manual.items():
        score(client, attempt_id, value)

    result = client.get("/api/analysis/calibration", params={"run_id": run_id}).json()
    assert result["min_pairs"] == 5
    [group] = result["groups"]
    assert group["judge"] == "Calibrated judge" and group["rubric"].endswith("v1")
    assert group["pairs"] == 10 and group["participant_pairs_excluded"] == 0
    judged = client.get(f"/api/runs/{run_id}").json()["judge_scores"]
    pairs = [(float(manual[a]), judged[a]["weighted_overall"]) for a in manual]  # default rubric is already 1-5
    assert group["within_one"] == pytest.approx(sum(1 for m, j in pairs if abs(j - m) <= 1) / 10)
    assert group["mean_bias"] == pytest.approx(sum(j - m for m, j in pairs) / 10)
    expected_rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    assert group["spearman"] == (pytest.approx(expected_rho) if expected_rho is not None else None)
    agree = 0
    for evaluation in evaluations:
        best = max(evaluation["candidates"], key=lambda c: manual[c["attempt_id"]])
        agree += int(evaluation["preferred"] == best["label"])
    assert group["top_pick_total"] == 5 and group["top_pick_agree"] == agree
