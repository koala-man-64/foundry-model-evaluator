"""Pure builders for the run export: a JSON document (``fme.run.v1``) and a per-attempt CSV.

The builders take already-loaded data and return a dict or text, so they can be tested without HTTP or a database.
They read stored data and immutable price snapshots only. They never include request bodies, raw provider
responses, API keys, or the endpoint URL. Prompt and answer text, provider error messages and stop strings are
included only when ``include_text`` is true. Profiles are exported through an allow-list of fields.

Money is exported as exact decimal strings. An unknown value is ``null`` in JSON and an empty cell in CSV; the cost
status always says why (``complete``, ``incomplete``, ``not_sent`` or ``pending``). Unknown cost is never zero.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..execution.totals import CostTotal
from ..judging.service import JudgeScore
from ..money import to_text
from ..pricing.calculator import CostEstimate
from ..pricing.ratecard import RateCard, RateSource
from ..repos.evaluations import Evaluation
from ..repos.runs import Attempt, ManualScore, Run, RunCandidate, RunPrompt

SCHEMA = "fme.run.v1"

CSV_COLUMNS: tuple[str, ...] = (
    "run_id",
    "prompt_id",
    "prompt_position",
    "prompt_title",
    "content_hash",
    "candidate_id",
    "candidate_label",
    "candidate_key",
    "trial_index",
    "attempt_id",
    "retry_of",
    "superseded",
    "status",
    "finish_reason",
    "error_type",
    "latency_ms",
    "ttft_ms",
    "input_total",
    "input_uncached",
    "cache_read",
    "cache_write",
    "output_total",
    "reasoning",
    "cost_status",
    "cost_total",
    "cost_lower",
    "cost_upper",
    "currency",
    "manual_score",
    "judge_weighted_overall",
    "judge_is_participant",
    "judge_evaluation_id",
    "rubric_version_id",
)
# Columns holding text that comes from users, models or providers. Only these are protected against
# spreadsheet formula injection; the numeric columns are produced by the app.
CSV_TEXT_COLUMNS = frozenset(
    {"prompt_title", "candidate_label", "candidate_key", "finish_reason", "error_type", "output_text"}
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

NOTE_SUPERSEDED = (
    "Every attempt is listed, retries included. An attempt with superseded=true was replaced by a later retry "
    "(a later attempt's retry_of names it); only attempts that are not superseded count for their cell."
)
NOTES: tuple[str, ...] = (
    "Costs are estimates from token counts and the rates frozen with this run; they are not bills.",
    "Incomplete costs carry status 'incomplete' and a lower bound; they are never shown as 0. "
    "A null value means 'not reported' or 'unknown'.",
    "Cost status 'not_sent' means the request was never sent, so nothing was billed. "
    "Status 'pending' means the attempt had not finished when the export was made.",
    NOTE_SUPERSEDED,
)
NOTE_NO_TEXT = (
    "Prompts, answers, reasoning summaries, provider error messages, stop strings, manual-score notes and judge "
    "explanations were left out. Export with include_text=true to add them."
)


@dataclass(frozen=True)
class RunExportData:
    """Everything the builders need, already loaded."""

    run: Run
    prompts: list[RunPrompt]
    candidates: list[RunCandidate]
    attempts: list[Attempt]
    manual_scores: dict[str, ManualScore]
    evaluations: list[Evaluation]
    rate_cards: dict[str, RateCard]  # by price snapshot id
    judge_scores: dict[str, JudgeScore]  # latest succeeded evaluation per attempt, as judge_scores() computes
    generation_cost: CostTotal
    judging_cost: CostTotal
    combined_cost: CostTotal
    currency: str = "USD"
    budget_limit: Decimal | None = None
    budget_committed: Decimal | None = None


# ---------------------------------------------------------------------------------------------------- helpers
def _money(value: Decimal | None) -> str | None:
    return None if value is None else to_text(value)


def cost_json(cost: CostEstimate | None) -> dict[str, Any]:
    """An estimate as JSON. No estimate yet means ``pending``, never zero."""
    if cost is None:
        return {
            "status": "pending",
            "total": None,
            "lower": None,
            "upper": None,
            "currency": None,
            "reasons": [],
            "assumptions": [],
            "lines": [],
        }
    return {
        "status": cost.status,
        "total": _money(cost.total),
        "lower": _money(cost.lower),
        "upper": _money(cost.upper),
        "currency": cost.currency,
        "reasons": list(cost.reasons),
        "assumptions": list(cost.assumptions),
        "lines": [
            {
                "category": line.category.value,
                "status": line.status.value,
                "tokens": line.tokens,
                "rate": _money(line.rate_per_million),
                "rate_unit": "per_million_tokens",
                "cost": _money(line.cost),
                "source": line.source.value if line.source else None,
                "custom_rate": line.custom_rate,
            }
            for line in cost.lines
        ],
    }


def rates_json(card: RateCard) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tier in card.sorted_tiers():
        for category, entry in tier.categories.items():
            rate = entry.rate
            rows.append(
                {
                    "category": category.value,
                    "tier_above_input": tier.above_input_tokens,
                    "status": entry.status.value,
                    "reason": entry.reason,
                    "price": _money(rate.price) if rate else None,
                    "unit_tokens": rate.unit_tokens if rate else None,
                    "unit_label": rate.unit_label if rate else None,
                    "per_million": _money(rate.per_million) if rate else None,
                    "currency": rate.currency if rate else card.currency,
                    "source": rate.source.value if rate else None,
                    "custom_override": bool(rate and rate.source is RateSource.CUSTOM_OVERRIDE),
                    "meter_id": rate.meter_id if rate else None,
                    "meter_name": rate.meter_name if rate else None,
                    "source_url": rate.source_url if rate else None,
                    "effective_date": rate.effective_date if rate else None,
                    "retrieved_at": rate.retrieved_at if rate else None,
                }
            )
    return rows


_PROFILE_FIELDS: tuple[str, ...] = (
    "name",
    "api_style",
    "route",
    "deployment",
    "publisher",
    "model_name",
    "model_version",
    "deployment_type",
    "processing_tier",
    "context_window",
    "max_output_tokens",
    "streaming",
    "include_usage",
)
_ENDPOINT_FIELDS: tuple[str, ...] = ("name", "region")


def _profile_json(candidate: RunCandidate) -> dict[str, Any]:
    """An allow-list of the profile snapshot. Notes, meter terms, custom path, API version, capabilities and the
    endpoint's URL and credential details are never exported."""
    snapshot = candidate.profile_snapshot
    profile: dict[str, Any] = {k: snapshot[k] for k in _PROFILE_FIELDS if k in snapshot}
    endpoint = snapshot.get("endpoint")
    if isinstance(endpoint, dict):
        profile["endpoint"] = {k: endpoint[k] for k in _ENDPOINT_FIELDS if k in endpoint}
    return profile


def _settings_json(candidate: RunCandidate, include_text: bool) -> dict[str, Any]:
    """Generation settings. Stop strings can hold free text, so they follow ``include_text``."""
    settings = candidate.settings.model_dump(mode="json", exclude_none=True)
    if not include_text:
        settings.pop("stop", None)
    return settings


# ---------------------------------------------------------------------------------------------------- JSON
def _prompt_json(prompt: RunPrompt, include_text: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": prompt.id,
        "position": prompt.position,
        "title": prompt.title,
        "content_hash": prompt.content_hash,
    }
    if include_text:
        item["system_text"] = prompt.system_text
        item["user_text"] = prompt.user_text
    return item


def _candidate_json(candidate: RunCandidate, data: RunExportData, include_text: bool) -> dict[str, Any]:
    card = data.rate_cards.get(candidate.price_snapshot_id)
    return {
        "id": candidate.id,
        "position": candidate.position,
        "label": candidate.label,
        "candidate_key": candidate.candidate_key,
        "profile_id": candidate.profile_id,
        "profile": _profile_json(candidate),
        "settings": _settings_json(candidate, include_text),
        "price_snapshot_id": candidate.price_snapshot_id,
        "rates": rates_json(card) if card is not None else [],
    }


def _attempt_json(a: Attempt, include_text: bool, superseded: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": a.id,
        "retry_of": a.retry_of,
        "superseded": superseded,
        "prompt_id": a.prompt_id,
        "candidate_id": a.candidate_id,
        "trial_index": a.trial_index,
        "status": a.status.value,
        "finish_reason": a.finish_reason,
        "raw_finish_reason": a.raw_finish_reason,
        "error_type": a.error_type,
        "request_sent_at": a.request_sent_at,
        "latency_ms": a.latency_ms,
        "ttft_ms": a.ttft_ms,
        "streamed": a.streamed,
        "response_model": a.response_model,
        "service_tier": a.service_tier,
        "usage": a.usage.model_dump(mode="json") if a.usage is not None else None,
        "cost": cost_json(a.cost),
    }
    if include_text:
        item["error_message"] = a.error_message
        item["output_text"] = a.output_text
        item["reasoning_summary"] = a.reasoning_summary
    return item


def _evaluation_json(e: Evaluation, include_text: bool) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for ec in e.candidates:
        item: dict[str, Any] = {
            "label": ec.label,
            "attempt_id": ec.attempt_id,
            "candidate_id": ec.candidate_id,
            "judge_overall": ec.judge_overall,
            "weighted_overall": ec.weighted_overall,
            "criteria": [
                {"key": c.key, "score": c.score, **({"explanation": c.explanation} if include_text else {})}
                for c in ec.criteria
            ],
        }
        if include_text:
            item["explanation"] = ec.explanation
        candidates.append(item)
    result: dict[str, Any] = {
        "id": e.id,
        "prompt_id": e.prompt_id,
        "trial_index": e.trial_index,
        "status": e.status,
        "trigger": e.trigger,
        "judge_profile_id": e.judge_profile_id,
        "judge_deployment": e.judge_profile_snapshot.get("deployment"),
        "judge_model_name": e.judge_profile_snapshot.get("model_name"),
        "rubric_version_id": e.rubric_version_id,
        "judge_is_participant": e.judge_is_participant,
        "preferred": e.preferred,
        "preferred_candidate_id": e.preferred_candidate_id,
        "error_type": e.error_type,
        "created_at": e.created_at,
        "cost": cost_json(e.cost),
        "candidates": candidates,
    }
    if include_text:
        result["error_message"] = e.error_message
        result["preference_explanation"] = e.preference_explanation
    return result


def superseded_ids(attempts: list[Attempt]) -> frozenset[str]:
    """Attempts replaced by a later retry (some attempt's ``retry_of`` names them)."""
    return frozenset(a.retry_of for a in attempts if a.retry_of)


def build_run_json(data: RunExportData, *, include_text: bool, exported_at: str, app_version: str) -> dict[str, Any]:
    run = data.run
    budget = (
        {"limit": _money(data.budget_limit), "committed": _money(data.budget_committed)}
        if data.budget_limit is not None
        else None
    )
    manual_scores: list[dict[str, Any]] = []
    for a in data.attempts:
        score = data.manual_scores.get(a.id)
        if score is None:
            continue
        entry: dict[str, Any] = {"attempt_id": score.attempt_id, "score": score.score, "updated_at": score.updated_at}
        if include_text:
            entry["notes"] = score.notes
        manual_scores.append(entry)

    replaced = superseded_ids(data.attempts)
    notes = list(NOTES)
    if not include_text:
        notes.append(NOTE_NO_TEXT)

    return {
        "schema": SCHEMA,
        "exported_at": exported_at,
        "app_version": app_version,
        "include_text": include_text,
        "run": {
            "id": run.id,
            "title": run.title,
            "kind": run.kind,
            "status": run.status.value,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "trials": run.trials,
            "suite_id": run.suite_id,
            "currency": data.currency,
            "budget": budget,
            "judge_config": run.judge_config,
        },
        "prompts": [_prompt_json(p, include_text) for p in data.prompts],
        "candidates": [_candidate_json(c, data, include_text) for c in data.candidates],
        "attempts": [_attempt_json(a, include_text, a.id in replaced) for a in data.attempts],
        "manual_scores": manual_scores,
        "evaluations": [_evaluation_json(e, include_text) for e in data.evaluations],
        "totals": {
            "generation": data.generation_cost.model_dump(mode="json"),
            "judging": data.judging_cost.model_dump(mode="json"),
            "combined": data.combined_cost.model_dump(mode="json"),
        },
        "notes": notes,
    }


# ---------------------------------------------------------------------------------------------------- CSV
def csv_safe(value: str) -> str:
    """Neutralize spreadsheet formulas: text starting with = + - @ tab or CR gets a leading single quote."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def _cost_cells(cost: CostEstimate | None) -> dict[str, str]:
    """Cost cells: an unknown amount is empty, and ``cost_status`` says why. No estimate yet is ``pending``."""
    if cost is None:
        return {"cost_status": "pending", "cost_total": "", "cost_lower": "", "cost_upper": "", "currency": ""}
    return {
        "cost_status": cost.status,
        "cost_total": _cell(_money(cost.total)),
        "cost_lower": _cell(_money(cost.lower)),
        "cost_upper": _cell(_money(cost.upper)),
        "currency": cost.currency,
    }


def build_run_csv(data: RunExportData, *, include_text: bool) -> str:
    """One row per attempt (retries included). An empty cell means "not reported"; ``cost_status`` says why."""
    columns = (*CSV_COLUMNS, "output_text") if include_text else CSV_COLUMNS
    prompts = {p.id: p for p in data.prompts}
    candidates = {c.id: c for c in data.candidates}
    replaced = superseded_ids(data.attempts)
    rubric_by_evaluation = {e.id: e.rubric_version_id for e in data.evaluations}
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for a in data.attempts:
        prompt = prompts.get(a.prompt_id)
        candidate = candidates.get(a.candidate_id)
        usage = a.usage
        manual = data.manual_scores.get(a.id)
        judge = data.judge_scores.get(a.id)
        row: dict[str, object] = {
            "run_id": a.run_id,
            "prompt_id": a.prompt_id,
            "prompt_position": prompt.position if prompt else None,
            "prompt_title": prompt.title if prompt else None,
            "content_hash": prompt.content_hash if prompt else None,
            "candidate_id": a.candidate_id,
            "candidate_label": candidate.label if candidate else None,
            "candidate_key": candidate.candidate_key if candidate else None,
            "trial_index": a.trial_index,
            "attempt_id": a.id,
            "retry_of": a.retry_of,
            "superseded": "true" if a.id in replaced else "false",
            "status": a.status.value,
            "finish_reason": a.finish_reason,
            "error_type": a.error_type,
            "latency_ms": a.latency_ms,
            "ttft_ms": a.ttft_ms,
            "input_total": usage.input_total if usage else None,
            "input_uncached": usage.input_uncached if usage else None,
            "cache_read": usage.cache_read if usage else None,
            "cache_write": usage.cache_write if usage else None,
            "output_total": usage.output_total if usage else None,
            "reasoning": usage.reasoning if usage else None,
            **_cost_cells(a.cost),
            "manual_score": manual.score if manual else None,
            "judge_weighted_overall": judge.weighted_overall if judge else None,
            "judge_is_participant": ("true" if judge.judge_is_participant else "false") if judge else None,
            "judge_evaluation_id": judge.evaluation_id if judge else None,
            "rubric_version_id": rubric_by_evaluation.get(judge.evaluation_id) if judge else None,
        }
        if include_text:
            row["output_text"] = a.output_text
        writer.writerow([csv_safe(_cell(row[c])) if c in CSV_TEXT_COLUMNS else _cell(row[c]) for c in columns])
    return out.getvalue()
