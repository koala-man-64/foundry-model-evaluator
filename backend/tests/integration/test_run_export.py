"""Run export through the API: JSON and CSV downloads, secrecy, omitted text, and the read-only guard rules."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from foundry_eval.reporting.run_export import CSV_COLUMNS
from foundry_eval.types import ApiStyle
from tests.api_support import (
    CANARY,
    ORIGIN,
    create_endpoint,
    create_profile,
    set_manual,
    start_run,
    wait_for_run,
)

SYSTEM_TEXT = "SYSTEM-TEXT-MARKER be brief."
USER_TEXT = "USER-TEXT-MARKER explain prompt caching."
INJECTED_LABEL = '=HYPERLINK("http://evil.test","x")'


@pytest.fixture
def exported_run(client: TestClient) -> str:
    """A finished run: a Claude profile with manual rates, a GPT profile from Azure rates and a failing deployment."""
    endpoint = create_endpoint(client)
    gpt = create_profile(
        client,
        endpoint["id"],
        preset="openai-reasoning-cache-write",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-5.6-terra",
        meter_terms="5.6 terra",
        notes="PROFILE-NOTE-MARKER private remark",
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
    judge = create_profile(
        client,
        endpoint["id"],
        preset="openai-standard",
        api_style=ApiStyle.OPENAI_RESPONSES,
        deployment="gpt-4.1-judge",
        name="Judge profile",
        model_name="gpt-4.1",
        context_window=100_000,
    )
    assert client.post("/api/pricing/refresh", json={}).status_code == 200
    for category, price in [
        ("input_uncached", "4"),
        ("cache_read", "0.2"),
        ("cache_write_5m", "5"),
        ("cache_write_1h", "8"),
        ("output", "20"),
    ]:
        set_manual(client, claude["id"], category, price)
    run_id = start_run(
        client,
        {
            "title": "Export run",
            "prompts": [{"title": "Caching", "system": SYSTEM_TEXT, "user": USER_TEXT}],
            "candidates": [
                {"profile_id": gpt["id"], "settings": {"max_output_tokens": 2000, "effort": "low"}},
                {"profile_id": claude["id"], "settings": {"max_output_tokens": 4000}, "label": INJECTED_LABEL},
                {"profile_id": broken["id"], "settings": {"max_output_tokens": 100}},
            ],
        },
    )
    detail = wait_for_run(client, run_id)
    succeeded = [a for a in detail["attempts"] if a["status"] == "succeeded"]
    assert len(succeeded) == 2
    client.put(
        f"/api/attempts/{succeeded[0]['id']}/manual-score",
        json={"score": 4, "notes": "NOTES-MARKER quotes the answer"},
    ).raise_for_status()
    judged = client.post(
        f"/api/runs/{run_id}/evaluations",
        json={"judge_profile_id": judge["id"], "settings": {"max_output_tokens": 2000}},
        headers={"Idempotency-Key": "idem-judge-0001"},
    )
    assert judged.status_code == 201, judged.text
    wait_for_run(client, run_id)
    evaluation = client.get(f"/api/runs/{run_id}/evaluations").json()[0]
    assert evaluation["status"] == "succeeded", evaluation
    # An explicit retry adds a second record for the failed attempt, and both stay in the export.
    failed = next(a for a in detail["attempts"] if a["status"] == "failed")
    assert (
        client.post(f"/api/attempts/{failed['id']}/retry", headers={"Idempotency-Key": "retry-key-001"}).status_code
        == 200
    )
    wait_for_run(client, run_id)
    return run_id


def export(client: TestClient, run_id: str, **params: str):
    return client.get(f"/api/runs/{run_id}/export", params=params)


def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


def test_json_export_is_an_attachment_with_the_schema_and_exact_money(client, exported_run):
    response = export(client, exported_run)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == f'attachment; filename="fme-run-{exported_run[:8]}.json"'
    assert response.headers["cache-control"] == "no-store"
    document = response.json()
    assert document["schema"] == "fme.run.v1"
    assert document["exported_at"] and document["app_version"]
    assert document["include_text"] is False
    assert document["run"]["id"] == exported_run and document["run"]["budget"] is None

    detail = client.get(f"/api/runs/{exported_run}").json()
    assert len(document["attempts"]) == len(detail["attempts"]) == 4  # three first attempts and one retry
    for key in ("generation", "judging", "combined"):
        assert document["totals"][key] == detail[f"{key}_cost"]  # exactly as the run page computes them

    ok = [a for a in document["attempts"] if a["status"] == "succeeded"]
    assert len(ok) == 2
    for attempt in ok:
        cost = attempt["cost"]
        assert cost["status"] == "complete" and isinstance(cost["total"], str)
        assert attempt["usage"]["output_total"] > 0
        assert {line["category"] for line in cost["lines"]} >= {"input_uncached", "output"}
        assert all(isinstance(line["cost"], str) or line["cost"] is None for line in cost["lines"])

    # Money is never a JSON number anywhere in the document.
    def walk(value, path=""):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in {"total", "lower", "upper", "price", "per_million", "rate", "limit", "committed"}:
                    assert v is None or isinstance(v, str), f"{path}.{k} is {v!r}"
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for item in value:
                walk(item, path)

    walk(document)


def test_failed_attempts_carry_a_status_not_zero(client, exported_run):
    document = export(client, exported_run).json()
    failed = [a for a in document["attempts"] if a["status"] == "failed"]
    assert len(failed) == 2 and {a["retry_of"] is None for a in failed} == {True, False}
    for attempt in failed:
        cost = attempt["cost"]
        assert cost["status"] == "incomplete"
        assert cost["total"] is None  # unknown, not "0"
        assert cost["reasons"]
        assert attempt["error_type"] is not None and attempt["usage"] is None
    assert document["totals"]["generation"]["status"] == "incomplete"
    assert document["totals"]["generation"]["total"] is None

    by_label = {row["candidate_label"]: row for row in rows(export(client, exported_run, format="csv").text)}
    csv_failed = by_label["fail-500"]
    assert csv_failed["cost_status"] == "incomplete" and csv_failed["cost_total"] == ""


def test_retried_attempts_are_marked_superseded_and_profiles_are_an_allow_list(client, exported_run):
    response = export(client, exported_run)
    document = response.json()
    failed = {a["retry_of"] is None: a for a in document["attempts"] if a["status"] == "failed"}
    assert failed[True]["superseded"] is True and failed[False]["superseded"] is False
    assert failed[False]["retry_of"] == failed[True]["id"]
    assert [a["superseded"] for a in document["attempts"]].count(True) == 1
    assert any("not superseded" in note for note in document["notes"])

    for text in (response.text, export(client, exported_run, format="csv").text):
        assert "PROFILE-NOTE-MARKER" not in text
    for candidate in document["candidates"]:
        assert {"notes", "meter_terms", "custom_path", "api_version", "capabilities"}.isdisjoint(candidate["profile"])
        assert "stop" not in candidate["settings"]


def test_rates_candidates_and_evaluations_are_exported_without_the_endpoint_url(client, exported_run):
    response = export(client, exported_run)
    document = response.json()
    assert "mock.services.ai.azure.com" not in response.text
    candidates = {c["label"]: c for c in document["candidates"]}
    claude = candidates[INJECTED_LABEL]
    assert claude["profile"]["endpoint"] == {
        "name": "Mock",
        "region": "eastus2",
    }
    rates = {r["category"]: r for r in claude["rates"]}
    assert (rates["output"]["price"], rates["output"]["per_million"], rates["output"]["source"]) == (
        "20",
        "20",
        "manual",
    )
    assert rates["output"]["custom_override"] is False
    assert rates["output"]["source_url"] == "https://www.anthropic.com/pricing"
    assert rates["input_uncached"]["currency"] == "USD" and rates["input_uncached"]["tier_above_input"] == 0

    [evaluation] = document["evaluations"]
    assert evaluation["status"] == "succeeded" and evaluation["judge_deployment"] == "gpt-4.1-judge"
    assert evaluation["rubric_version_id"] == "default-v1"
    assert {c["label"] for c in evaluation["candidates"]} == {"A", "B"}
    assert all(c["criteria"] and "explanation" not in c["criteria"][0] for c in evaluation["candidates"])
    assert len(document["manual_scores"]) == 1 and "notes" not in document["manual_scores"][0]


def test_text_is_left_out_unless_requested(client, exported_run):
    markers = (SYSTEM_TEXT, USER_TEXT, "NOTES-MARKER")
    for params in ({}, {"include_text": "false"}):
        response = export(client, exported_run, **params)
        document = response.json()
        assert not [m for m in markers if m in response.text]
        assert all("system_text" not in p and "user_text" not in p for p in document["prompts"])
        assert all("output_text" not in a and "reasoning_summary" not in a for a in document["attempts"])
        assert any("left out" in note for note in document["notes"])
        assert "output_text" not in export(client, exported_run, format="csv", **params).text.splitlines()[0]

    response = export(client, exported_run, include_text="true")
    document = response.json()
    assert document["include_text"] is True
    assert document["prompts"][0]["system_text"] == SYSTEM_TEXT and document["prompts"][0]["user_text"] == USER_TEXT
    assert document["manual_scores"][0]["notes"] == "NOTES-MARKER quotes the answer"
    assert any(a["output_text"] for a in document["attempts"])
    assert not any("left out" in note for note in document["notes"])


def test_csv_has_one_row_per_attempt_in_the_documented_columns(client, exported_run):
    response = export(client, exported_run, format="csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="fme-run-{exported_run[:8]}.csv"'
    assert "\r\n" not in response.text
    reader = csv.reader(io.StringIO(response.text, newline=""))
    header = next(reader)
    assert header == list(CSV_COLUMNS)
    body = list(reader)
    detail = client.get(f"/api/runs/{exported_run}").json()
    assert len(body) == len(detail["attempts"]) == 4
    assert {r[header.index("attempt_id")] for r in body} == {a["id"] for a in detail["attempts"]}

    by_label = {}
    for row in rows(response.text):
        by_label.setdefault(row["candidate_label"], []).append(row)
    # The label that looks like a formula is quoted so a spreadsheet will not run it.
    assert "'" + INJECTED_LABEL in by_label
    claude_row = by_label["'" + INJECTED_LABEL][0]
    assert claude_row["cost_status"] == "complete" and claude_row["cost_total"] != ""
    assert claude_row["judge_weighted_overall"] != "" and claude_row["rubric_version_id"] == "default-v1"
    assert claude_row["currency"] == "USD" and claude_row["input_total"] != ""
    fail_rows = by_label["fail-500"]
    assert sorted(r["retry_of"] != "" for r in fail_rows) == [False, True]
    assert sorted((r["retry_of"] != "", r["superseded"]) for r in fail_rows) == [(False, "true"), (True, "false")]
    assert {r["superseded"] for r in by_label["'" + INJECTED_LABEL]} == {"false"}
    assert claude_row["judge_is_participant"] == "false"
    assert all(r["judge_is_participant"] == "" for r in fail_rows)
    assert all(r["status"] == "failed" and r["input_total"] == "" for r in fail_rows)
    manual = [r for r in rows(response.text) if r["manual_score"]]
    assert len(manual) == 1 and manual[0]["manual_score"] == "4"


def test_the_api_key_never_appears_in_either_export(client, exported_run):
    for fmt in ("json", "csv"):
        for include_text in ("false", "true"):
            text = export(client, exported_run, format=fmt, include_text=include_text).text
            assert CANARY not in text
            assert "request_body" not in text and "raw_response" not in text


def test_bad_format_and_unknown_run_are_rejected(client, exported_run):
    assert export(client, exported_run, format="xml").status_code == 422
    assert export(client, "does-not-exist").status_code == 404


def test_export_is_a_read_only_top_level_get(client, exported_run, mock_app):
    before = client.get(f"/api/runs/{exported_run}").json()
    calls_before = dict(mock_app.state.mock.requests)

    # A page navigation (a plain link click) sends no Origin and no X-FME-Client header.
    # Not entered as a context manager: the app is already running (its lifespan holds the data folder).
    navigation = TestClient(client.app, base_url=ORIGIN)
    link = navigation.get(
        f"/api/runs/{exported_run}/export?format=csv&include_text=false", headers={"Sec-Fetch-Site": "none"}
    )
    same_origin = navigation.get(f"/api/runs/{exported_run}/export", headers={"Sec-Fetch-Site": "same-origin"})
    cross_site = navigation.get(f"/api/runs/{exported_run}/export", headers={"Sec-Fetch-Site": "cross-site"})
    # Nothing but GET is routed here.
    post = navigation.post(f"/api/runs/{exported_run}/export", headers={"Origin": ORIGIN, "X-FME-Client": "1"})
    assert link.status_code == 200 and link.headers["content-disposition"].startswith("attachment")
    assert same_origin.status_code == 200
    assert cross_site.status_code == 403
    assert post.status_code == 405

    assert client.get(f"/api/runs/{exported_run}").json() == before  # nothing changed
    assert dict(mock_app.state.mock.requests) == calls_before  # and no provider call was made


def test_budget_limit_and_committed_are_exported_as_decimal_strings(client):
    endpoint = create_endpoint(client)
    claude = create_profile(
        client,
        endpoint["id"],
        preset="claude-adaptive",
        api_style=ApiStyle.ANTHROPIC_MESSAGES,
        deployment="claude-opus-test",
    )
    for category, price in [
        ("input_uncached", "4"),
        ("cache_read", "0.2"),
        ("cache_write_5m", "5"),
        ("cache_write_1h", "8"),
        ("output", "20"),
    ]:
        set_manual(client, claude["id"], category, price)
    run_id = start_run(
        client,
        {
            "prompts": [{"user": USER_TEXT}],
            "candidates": [{"profile_id": claude["id"], "settings": {"max_output_tokens": 1000}}],
            "budget": "5",
        },
    )
    wait_for_run(client, run_id)
    budget = export(client, run_id).json()["run"]["budget"]
    detail_budget = client.get(f"/api/runs/{run_id}").json()["budget"]
    assert budget == detail_budget and budget["limit"] == "5" and isinstance(budget["committed"], str)
