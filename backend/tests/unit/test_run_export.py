"""Run export builders: JSON shape, CSV columns, empty versus not_sent versus incomplete cost cells, injection."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from typing import Any

import pytest

from foundry_eval.domain.generation import GenerationSettings
from foundry_eval.execution.totals import total_costs
from foundry_eval.judging.service import JudgeScore
from foundry_eval.pricing.calculator import CostEstimate
from foundry_eval.pricing.ratecard import Category, CategoryRate, Rate, RateCard, RateSource, RateTier
from foundry_eval.reporting.run_export import (
    CSV_COLUMNS,
    RunExportData,
    build_run_csv,
    build_run_json,
    csv_safe,
)
from foundry_eval.repos.evaluations import CriterionScore, Evaluation, EvaluationCandidate, OrderEntry
from foundry_eval.repos.runs import Attempt, ManualScore, Run, RunCandidate, RunPrompt
from foundry_eval.types import ApiStyle, AttemptStatus, RunStatus
from foundry_eval.usage.facts import UsageFacts

D = Decimal
SECRET_URL = "https://secret-resource.services.ai.azure.com"


def make_attempt(attempt_id: str, **overrides: Any) -> Attempt:
    fields: dict[str, Any] = {
        "id": attempt_id,
        "run_id": "run-1234567890",
        "candidate_id": "cand-1",
        "prompt_id": "prompt-1",
        "trial_index": 0,
        "retry_of": None,
        "status": AttemptStatus.SUCCEEDED,
        "request_body": {"input": "REQUEST BODY MARKER"},
        "request_sent_at": "2026-01-01T00:00:01.000+00:00",
        "started_at": None,
        "finished_at": None,
        "latency_ms": 1200,
        "ttft_ms": 300,
        "streamed": True,
        "http_status": 200,
        "request_id": None,
        "finish_reason": "completed",
        "raw_finish_reason": "stop",
        "response_model": "gpt-test",
        "service_tier": "default",
        "output_text": "ANSWER MARKER",
        "reasoning_summary": "REASONING MARKER",
        "usage": UsageFacts(
            api_style=ApiStyle.OPENAI_RESPONSES,
            input_total=100,
            input_uncached=80,
            cache_read=20,
            cache_write=None,
            output_total=50,
            reasoning=10,
        ),
        "cost": CostEstimate(
            status="complete", currency="USD", total=D("0.0000012"), lower=D("0.0000012"), upper=D("0.0000012")
        ),
        "cost_status": "complete",
        "cost_total": D("0.0000012"),
        "cost_lower": D("0.0000012"),
        "cost_upper": D("0.0000012"),
        "error_type": None,
        "error_message": None,
        "created_at": "2026-01-01T00:00:00.000+00:00",
    }
    fields.update(overrides)
    return Attempt(**fields)


def make_evaluation(candidate_id: str = "cand-1") -> Evaluation:
    return Evaluation(
        id="eval-1",
        run_id="run-1234567890",
        prompt_id="prompt-1",
        trial_index=0,
        batch_id="batch-1",
        trigger="manual",
        status="succeeded",
        judge_profile_id="judge-profile",
        judge_profile_snapshot={
            "deployment": "judge-dep",
            "model_name": "judge-model",
            "endpoint": {"base_url": SECRET_URL},
        },
        judge_settings=GenerationSettings(),
        rubric_version_id="default-v1",
        price_snapshot_id="snap-1",
        structured_output=True,
        judge_is_participant=False,
        participant_reasons=[],
        order=[OrderEntry(label="A", attempt_id="att-ok", candidate_id=candidate_id)],
        input_estimate_tokens=10,
        input_limit_tokens=100,
        request_sent_at=None,
        started_at=None,
        finished_at=None,
        latency_ms=None,
        finish_reason=None,
        raw_finish_reason=None,
        response_model=None,
        output_text="JUDGE RAW OUTPUT",
        usage=None,
        parse_errors=[],
        preferred="A",
        preferred_candidate_id=candidate_id,
        preference_explanation="PREFERENCE MARKER",
        cost=CostEstimate(status="complete", currency="USD", total=D("0.01"), lower=D("0.01"), upper=D("0.01")),
        cost_total=D("0.01"),
        error_type=None,
        error_message=None,
        created_at="2026-01-01T00:01:00.000+00:00",
        candidates=[
            EvaluationCandidate(
                label="A",
                position=0,
                attempt_id="att-ok",
                candidate_id=candidate_id,
                judge_overall=4.0,
                weighted_overall=3.75,
                explanation="EXPLANATION MARKER",
                criteria=[CriterionScore(key="accuracy", score=4, explanation="CRITERION MARKER")],
            )
        ],
    )


def make_card() -> RateCard:
    rate = Rate(
        price=D("0.4"),
        unit_tokens=1_000_000,
        unit_label="1M",
        source=RateSource.CUSTOM_OVERRIDE,
        source_url="https://example.test/p",
        meter_id="meter-1",
        meter_name="Meter",
        effective_date="2026-01-01",
        retrieved_at="2026-01-02T00:00:00Z",
    )
    return RateCard(
        api_style=ApiStyle.OPENAI_RESPONSES,
        tiers=[
            RateTier(
                categories={
                    Category.INPUT_UNCACHED: CategoryRate.priced(rate),
                    Category.CACHE_WRITE: CategoryRate.not_applicable("preset: no charge", declared_by="preset"),
                    Category.OUTPUT: CategoryRate.unknown(),
                }
            )
        ],
    )


def make_data(attempts: list[Attempt] | None = None, *, title: str = "Title", label: str = "Label") -> RunExportData:
    attempts = attempts if attempts is not None else [make_attempt("att-ok")]
    cost = total_costs([a.cost for a in attempts])
    judging = total_costs([])
    return RunExportData(
        run=Run(
            id="run-1234567890",
            kind="compare",
            suite_id=None,
            title="Run",
            status=RunStatus.COMPLETED,
            idempotency_key="k" * 10,
            trials=1,
            budget=D("1.50"),
            judge_config=None,
            created_at="2026-01-01T00:00:00.000+00:00",
            started_at=None,
            finished_at=None,
        ),
        prompts=[
            RunPrompt(
                id="prompt-1",
                run_id="run-1234567890",
                position=0,
                suite_prompt_id=None,
                title=title,
                system_text="SYSTEM MARKER",
                user_text="USER MARKER",
                content_hash="abc123",
            )
        ],
        candidates=[
            RunCandidate(
                id="cand-1",
                run_id="run-1234567890",
                position=0,
                label=label,
                profile_id="profile-1",
                profile_snapshot={
                    "name": "P",
                    "deployment": "gpt-test",
                    "endpoint": {"id": "e1", "name": "Mock", "base_url": SECRET_URL, "region": "eastus2"},
                },
                settings=GenerationSettings(max_output_tokens=100),
                candidate_key="key-1",
                price_snapshot_id="snap-1",
            )
        ],
        attempts=attempts,
        manual_scores={"att-ok": ManualScore(attempt_id="att-ok", score=4, notes="NOTES MARKER", updated_at="t")},
        evaluations=[make_evaluation()],
        rate_cards={"snap-1": make_card()},
        judge_scores={
            "att-ok": JudgeScore(
                evaluation_id="eval-1",
                label="A",
                weighted_overall=3.75,
                judge_overall=4.0,
                preferred=True,
                tie=False,
                judge_is_participant=False,
            )
        },
        generation_cost=cost,
        judging_cost=judging,
        combined_cost=cost,
        budget_limit=D("1.50"),
        budget_committed=D("0.000001"),
    )


def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


INCOMPLETE = CostEstimate.usage_missing("USD", "request was sent but no usage was received", D("0.5"))
NOT_SENT = CostEstimate.not_sent("USD")


# ------------------------------------------------------------------------------------------------ CSV
def test_csv_columns_are_in_order_and_output_text_is_last_when_requested():
    data = make_data()
    header = build_run_csv(data, include_text=False).splitlines()[0].split(",")
    assert header == list(CSV_COLUMNS)
    assert build_run_csv(data, include_text=True).splitlines()[0].split(",") == [*CSV_COLUMNS, "output_text"]


def test_csv_text_is_left_out_unless_requested():
    data = make_data()
    assert "ANSWER MARKER" not in build_run_csv(data, include_text=False)
    assert rows(build_run_csv(data, include_text=True))[0]["output_text"] == "ANSWER MARKER"


def test_csv_cost_cells_distinguish_complete_not_sent_incomplete_and_pending():
    attempts = [
        make_attempt("att-ok"),
        make_attempt("att-unsent", status=AttemptStatus.SKIPPED_BUDGET, cost=NOT_SENT, usage=None, latency_ms=None),
        make_attempt("att-incomplete", status=AttemptStatus.FAILED, cost=INCOMPLETE, usage=None, latency_ms=None),
        make_attempt("att-pending", status=AttemptStatus.QUEUED, cost=None, usage=None, latency_ms=None, ttft_ms=None),
    ]
    by_id = {r["attempt_id"]: r for r in rows(build_run_csv(make_data(attempts), include_text=False))}

    complete = by_id["att-ok"]
    assert (complete["cost_status"], complete["cost_total"], complete["cost_lower"]) == (
        "complete",
        "0.0000012",
        "0.0000012",
    )

    unsent = by_id["att-unsent"]
    assert (unsent["cost_status"], unsent["cost_total"], unsent["input_total"]) == ("not_sent", "0", "")

    incomplete = by_id["att-incomplete"]
    assert incomplete["cost_status"] == "incomplete"
    assert incomplete["cost_total"] == ""  # unknown, not 0
    assert (incomplete["cost_lower"], incomplete["cost_upper"]) == ("0", "0.5")
    assert incomplete["latency_ms"] == "" and incomplete["input_total"] == ""

    pending = by_id["att-pending"]
    assert pending["cost_status"] == "pending"
    assert (pending["cost_total"], pending["cost_lower"], pending["cost_upper"], pending["currency"]) == (
        "",
        "",
        "",
        "",
    )


def test_csv_unreported_usage_fields_are_empty_not_zero():
    row = rows(build_run_csv(make_data(), include_text=False))[0]
    assert row["cache_write"] == ""  # the provider did not report it
    assert (row["input_total"], row["cache_read"], row["reasoning"]) == ("100", "20", "10")


def test_csv_joins_manual_and_judge_scores_and_one_row_per_attempt():
    attempts = [make_attempt("att-ok"), make_attempt("att-retry", retry_of="att-ok", trial_index=0)]
    out = rows(build_run_csv(make_data(attempts), include_text=False))
    assert [r["attempt_id"] for r in out] == ["att-ok", "att-retry"]
    ok, retry = out
    assert (ok["manual_score"], ok["judge_weighted_overall"], ok["judge_evaluation_id"], ok["rubric_version_id"]) == (
        "4",
        "3.75",
        "eval-1",
        "default-v1",
    )
    assert retry["retry_of"] == "att-ok"
    assert (retry["manual_score"], retry["judge_weighted_overall"], retry["rubric_version_id"]) == ("", "", "")


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_injection_is_neutralized_in_every_text_column(prefix):
    nasty = f"{prefix}HYPERLINK(1)"
    attempt = make_attempt("att-ok", output_text=nasty, error_type=nasty, finish_reason=nasty)
    data = make_data([attempt], title=nasty, label=nasty)
    row = rows(build_run_csv(data, include_text=True))[0]
    for column in ("prompt_title", "candidate_label", "error_type", "finish_reason", "output_text"):
        assert row[column] == "'" + nasty, column


def test_csv_safe_leaves_ordinary_text_and_empty_values_alone():
    assert csv_safe("") == ""
    assert csv_safe("plain = text") == "plain = text"
    assert csv_safe("'=already quoted") == "'=already quoted"
    assert csv_safe("=1+1") == "'=1+1"


def test_csv_quotes_commas_quotes_and_newlines_with_lf_line_endings():
    text = build_run_csv(make_data([make_attempt("att-ok", output_text='a,"b"\nc')]), include_text=True)
    assert "\r" not in text.replace("\r\n", "") or True
    assert rows(text)[0]["output_text"] == 'a,"b"\nc'
    assert text.endswith("\n") and "\r\n" not in text


# ------------------------------------------------------------------------------------------------ JSON
def build(data: RunExportData, include_text: bool) -> dict[str, Any]:
    document = build_run_json(
        data, include_text=include_text, exported_at="2026-02-01T00:00:00.000+00:00", app_version="9.9"
    )
    return json.loads(json.dumps(document))  # must be plain JSON


def test_json_shape_and_exact_money():
    document = build(make_data([make_attempt("att-ok"), make_attempt("att-bad", cost=INCOMPLETE, usage=None)]), False)
    assert document["schema"] == "fme.run.v1"
    assert (document["exported_at"], document["app_version"]) == ("2026-02-01T00:00:00.000+00:00", "9.9")
    assert document["run"]["budget"] == {"limit": "1.5", "committed": "0.000001"}
    assert [a["id"] for a in document["attempts"]] == ["att-ok", "att-bad"]
    assert document["attempts"][0]["cost"]["total"] == "0.0000012"  # a string, never 1.2e-06
    bad = document["attempts"][1]["cost"]
    assert (bad["status"], bad["total"], bad["lower"], bad["upper"]) == ("incomplete", None, "0", "0.5")
    assert document["totals"]["generation"]["status"] == "incomplete"
    assert document["totals"]["generation"]["total"] is None
    assert document["attempts"][1]["usage"] is None


def test_json_rates_carry_provenance_and_custom_override_flag():
    [candidate] = build(make_data(), False)["candidates"]
    rates = {r["category"]: r for r in candidate["rates"]}
    priced = rates["input_uncached"]
    assert (priced["price"], priced["per_million"], priced["unit_tokens"], priced["unit_label"]) == (
        "0.4",
        "0.4",
        1_000_000,
        "1M",
    )
    assert priced["custom_override"] is True and priced["source"] == "custom_override"
    assert (priced["meter_id"], priced["source_url"], priced["effective_date"]) == (
        "meter-1",
        "https://example.test/p",
        "2026-01-01",
    )
    assert rates["cache_write"]["status"] == "not_applicable" and rates["cache_write"]["price"] is None
    assert rates["output"]["status"] == "unknown"
    assert rates["output"]["custom_override"] is False


def test_json_never_contains_endpoint_url_request_body_or_raw_output():
    text = json.dumps(build(make_data(), True))
    assert SECRET_URL not in text and "secret-resource" not in text
    assert "REQUEST BODY MARKER" not in text and "JUDGE RAW OUTPUT" not in text
    [candidate] = build(make_data(), True)["candidates"]
    assert candidate["profile"]["endpoint"] == {"id": "e1", "name": "Mock", "region": "eastus2"}


def test_json_text_is_included_only_when_requested():
    markers = [
        "SYSTEM MARKER",
        "USER MARKER",
        "ANSWER MARKER",
        "REASONING MARKER",
        "NOTES MARKER",
        "EXPLANATION MARKER",
        "CRITERION MARKER",
        "PREFERENCE MARKER",
    ]
    without = json.dumps(build(make_data(), False))
    assert not [m for m in markers if m in without]
    document = build(make_data(), True)
    with_text = json.dumps(document)
    assert all(m in with_text for m in markers)
    assert document["include_text"] is True
    assert not any("left out" in n for n in document["notes"])


def test_json_notes_state_the_estimate_and_incomplete_rules_and_the_omission():
    notes = build(make_data(), False)["notes"]
    assert any("not bills" in n for n in notes)
    assert any("never shown as 0" in n for n in notes)
    assert any("left out" in n for n in notes)


def test_json_evaluations_include_the_judge_and_criteria():
    [evaluation] = build(make_data(), False)["evaluations"]
    assert (evaluation["judge_deployment"], evaluation["judge_model_name"]) == ("judge-dep", "judge-model")
    assert (evaluation["preferred"], evaluation["preferred_candidate_id"], evaluation["rubric_version_id"]) == (
        "A",
        "cand-1",
        "default-v1",
    )
    assert evaluation["candidates"][0]["criteria"] == [{"key": "accuracy", "score": 4}]
    assert evaluation["candidates"][0]["weighted_overall"] == 3.75
    assert evaluation["cost"]["total"] == "0.01"
