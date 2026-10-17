"""Reference answers and deterministic checks: from a suite into runs, the judge prompt and comparisons."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from foundry_eval.grading.checks import load_checks, run_checks
from foundry_eval.types import ApiStyle
from tests.api_support import create_endpoint, create_profile, set_manual, wait_for_run

PROMPTS = ["What is the capital of France?", "Name a prime number above 10.", "Spell the word cat backwards."]


def chat_profile(client: TestClient, endpoint_id: str, deployment: str) -> dict:
    profile = create_profile(
        client, endpoint_id, preset="generic-chat", api_style=ApiStyle.OPENAI_CHAT, deployment=deployment
    )
    set_manual(client, profile["id"], "input_uncached", "1")
    set_manual(client, profile["id"], "output", "10")
    return profile


def suite_payload(profile_ids: list[str], reference: str = "Paris") -> dict:
    return {
        "name": "Ground truth",
        "trials": 1,
        "prompts": [
            {
                "title": f"Q{i + 1}",
                "user": text,
                "reference": reference if i == 0 else "",
                # The mock echoes the question, so the first check passes for every answer; the second depends on
                # the answer style the mock picks per deployment.
                "checks": [
                    {"kind": "contains", "value": "You asked"},
                    {"kind": "contains", "value": "concise answer"},
                ],
            }
            for i, text in enumerate(PROMPTS)
        ],
        "candidates": [{"profile_id": pid} for pid in profile_ids],
    }


def run_suite(client: TestClient, suite_id: str, key: str) -> dict:
    response = client.post(f"/api/suites/{suite_id}/runs", json={}, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return wait_for_run(client, response.json()["run_id"])


def test_checks_and_references_flow_through_runs_and_comparisons(client):
    endpoint = create_endpoint(client)
    profiles = [
        chat_profile(client, endpoint["id"], name) for name in ("deepseek-alpha", "deepseek-beta", "deepseek-gamma")
    ]
    created = client.post("/api/suites", json=suite_payload([p["id"] for p in profiles]))
    assert created.status_code == 201, created.text
    suite = created.json()
    assert suite["prompts"][0]["reference"] == "Paris"
    assert suite["prompts"][0]["checks"][1] == {"kind": "contains", "value": "concise answer", "case_sensitive": False}

    detail = run_suite(client, suite["id"], "idem-gt-run-1")
    prompts = {p["id"]: p for p in detail["prompts"]}
    assert prompts[detail["attempts"][0]["prompt_id"]]["checks"]  # frozen into the run
    assert [p["reference_text"] for p in detail["prompts"]] == ["Paris", "", ""]
    expected: dict[str, bool] = {}
    for attempt in detail["attempts"]:
        assert attempt["status"] == "succeeded"
        outcome = detail["check_results"][attempt["id"]]
        checks = load_checks(json.dumps(prompts[attempt["prompt_id"]]["checks"]))
        assert outcome == run_checks(checks, attempt["output_text"]).model_dump()
        assert outcome["results"][0]["passed"] is True
        expected[attempt["candidate_id"]] = outcome["passed"]

    assert set(expected.values()) == {True, False}, "the deployments should give both passing and failing answers"
    comparison = client.get("/api/comparisons", params={"run_id": detail["run"]["id"]}).json()
    assert comparison["has_checks"] is True
    by_label = {c["label"]: c for c in comparison["candidates"]}
    labels = {c["id"]: c["label"] for c in detail["candidates"]}
    for candidate_id, passed in expected.items():
        item = by_label[labels[candidate_id]]
        # The mock answers every prompt in the same style per deployment, so a candidate passes all or none.
        assert item["avg_checks_passed"] == pytest.approx(1.0 if passed else 0.0)
        assert item["checks_cells"] == 3


def test_the_judge_sees_the_reference_and_cells_ignore_reference_edits(client):
    endpoint = create_endpoint(client)
    first = chat_profile(client, endpoint["id"], "deepseek-one")
    second = chat_profile(client, endpoint["id"], "deepseek-two")
    judge = create_profile(
        client,
        endpoint["id"],
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-4.1-judge",
        model_name="gpt-4.1",
        context_window=100_000,
        name="Judge",
    )
    suite = client.post("/api/suites", json=suite_payload([first["id"], second["id"]])).json()
    detail = run_suite(client, suite["id"], "idem-gt-run-2")
    run_id = detail["run"]["id"]
    response = client.post(
        f"/api/runs/{run_id}/evaluations",
        json={"judge_profile_id": judge["id"]},
        headers={"Idempotency-Key": "idem-gt-j"},
    )
    assert response.status_code == 201, response.text
    deadline = time.time() + 20
    while time.time() < deadline:
        evaluations = client.get(f"/api/runs/{run_id}/evaluations").json()
        if evaluations and all(e["status"] not in ("queued", "running") for e in evaluations):
            break
        time.sleep(0.05)
    first_prompt = detail["prompts"][0]["id"]
    with_reference = next(e for e in evaluations if e["prompt_id"] == first_prompt)
    raw = client.get(f"/api/evaluations/{with_reference['id']}/raw").json()
    text = raw["request_body"]["instructions"] + json.dumps(raw["request_body"]["input"], ensure_ascii=False)
    assert "Reference answer (begin" in text and "Paris" in text and "may be imperfect" in text
    without = next(e for e in evaluations if e["prompt_id"] != first_prompt)
    raw = client.get(f"/api/evaluations/{without['id']}/raw").json()
    assert "Reference answer" not in raw["request_body"]["instructions"] + json.dumps(raw["request_body"]["input"])

    # Editing the reference does not change which prompt a cell is, so the runs still match.
    updated = suite_payload([first["id"], second["id"]], reference="Paris, France")
    updated["prompts"] = [{**p, "id": s["id"]} for p, s in zip(updated["prompts"], suite["prompts"], strict=True)]
    assert client.put(f"/api/suites/{suite['id']}", json=updated).status_code == 200
    again = run_suite(client, suite["id"], "idem-gt-run-3")
    assert again["prompts"][0]["reference_text"] == "Paris, France"
    comparison = client.get("/api/comparisons", params={"run_id": [run_id, again["run"]["id"]]}).json()
    assert comparison["cells_total"] == 3 and comparison["cells_matched"] == 3


@pytest.mark.parametrize(
    "check",
    [
        {"kind": "regex", "pattern": "[unclosed"},
        {"kind": "json_schema", "schema": {"minimum": "5"}},
        {"kind": "json_schema", "schema": {"type": "object", "oneOf": []}},
        {"kind": "number", "value": 0.5},
        {"kind": "unknown"},
    ],
)
def test_a_check_that_cannot_run_is_refused_when_the_suite_is_saved(client, check):
    endpoint = create_endpoint(client)
    profile = chat_profile(client, endpoint["id"], "deepseek-refuse")
    payload = suite_payload([profile["id"]])
    payload["prompts"][0]["checks"] = [check]
    assert client.post("/api/suites", json=payload).status_code == 422


def test_ad_hoc_runs_accept_references_and_checks(client):
    endpoint = create_endpoint(client)
    profile = chat_profile(client, endpoint["id"], "deepseek-adhoc")
    request = {
        "prompts": [{"user": "Hello there", "reference": "Hi", "checks": [{"kind": "contains", "value": "Hello"}]}],
        "candidates": [{"profile_id": profile["id"]}],
    }
    response = client.post("/api/runs", json=request, headers={"Idempotency-Key": "idem-gt-adhoc"})
    assert response.status_code == 201, response.text
    detail = wait_for_run(client, response.json()["run_id"])
    [attempt] = detail["attempts"]
    assert detail["check_results"][attempt["id"]]["passed"] is True


def test_cells_graded_differently_across_runs_are_left_out(client):
    endpoint = create_endpoint(client)
    first = chat_profile(client, endpoint["id"], "deepseek-north")
    second = chat_profile(client, endpoint["id"], "deepseek-south")
    judge = create_profile(
        client,
        endpoint["id"],
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-4.1-judge",
        model_name="gpt-4.1",
        context_window=100_000,
        name="Judge",
    )
    suite = client.post("/api/suites", json=suite_payload([first["id"], second["id"]])).json()
    one = run_suite(client, suite["id"], "idem-gt-mixed-1")
    # The second run uses stricter checks and another reference on the first prompt only.
    updated = suite_payload([first["id"], second["id"]], reference="Paris, the capital")
    updated["prompts"] = [{**p, "id": s["id"]} for p, s in zip(updated["prompts"], suite["prompts"], strict=True)]
    updated["prompts"][0]["checks"] = [{"kind": "contains", "value": "Paris"}]
    updated["prompts"][1]["checks"] = []  # checked in the first run, not in the second
    assert client.put(f"/api/suites/{suite['id']}", json=updated).status_code == 200
    two = run_suite(client, suite["id"], "idem-gt-mixed-2")
    for index, detail in enumerate((one, two)):
        run_id = detail["run"]["id"]
        response = client.post(
            f"/api/runs/{run_id}/evaluations",
            json={"judge_profile_id": judge["id"]},
            headers={"Idempotency-Key": f"idem-gt-mixed-j{index}"},
        )
        assert response.status_code == 201, response.text
        deadline = time.time() + 20
        while time.time() < deadline:
            found = client.get(f"/api/runs/{run_id}/evaluations").json()
            if found and all(e["status"] not in ("queued", "running") for e in found):
                break
            time.sleep(0.05)

    comparison = client.get("/api/comparisons", params={"run_id": [one["run"]["id"], two["run"]["id"]]}).json()
    assert comparison["cells_matched"] == 3
    notes = " ".join(comparison["notes"])
    assert "2 cells used different checks in different runs and are left out" in notes
    assert "1 cell was judged with different reference answers" in notes
    for candidate in comparison["candidates"]:
        assert candidate["checks_cells"] == 1 and candidate["judge_scored"] == 2


def test_exports_carry_check_outcomes_and_keep_reference_text_out_by_default(client):
    endpoint = create_endpoint(client)
    profiles = [chat_profile(client, endpoint["id"], name) for name in ("deepseek-alpha", "deepseek-beta")]
    suite = client.post("/api/suites", json=suite_payload([p["id"] for p in profiles])).json()
    detail = run_suite(client, suite["id"], "idem-gt-export")
    run_id = detail["run"]["id"]

    plain = client.get(f"/api/runs/{run_id}/export", params={"format": "json"}).json()
    first = next(p for p in plain["prompts"] if p["position"] == 0)
    assert first["has_reference"] is True and first["checks_count"] == 2
    assert "reference_text" not in first and "checks" not in first
    for attempt in plain["attempts"]:
        outcome = detail["check_results"][attempt["id"]]
        assert attempt["checks"]["passed"] == outcome["passed"]
        assert all("detail" not in r for r in attempt["checks"]["results"])  # details can quote the answer
    assert "Paris" not in json.dumps(plain)

    full = client.get(f"/api/runs/{run_id}/export", params={"format": "json", "include_text": "true"}).json()
    first = next(p for p in full["prompts"] if p["position"] == 0)
    assert first["reference_text"] == "Paris" and first["checks"][0]["kind"] == "contains"
    assert all("detail" in r for a in full["attempts"] for r in a["checks"]["results"])

    csv_text = client.get(f"/api/runs/{run_id}/export", params={"format": "csv"}).text
    header, *rows = csv_text.strip().split("\n")
    column = header.split(",").index("checks_passed")
    expected = {"true" if detail["check_results"][a["id"]]["passed"] else "false" for a in detail["attempts"]}
    assert {row.split(",")[column] for row in rows} == expected
