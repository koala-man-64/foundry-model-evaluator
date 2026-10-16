"""Read-only run export: a JSON document (``fme.run.v1``) or a per-attempt CSV, sent as a download."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from ... import __version__
from ...pricing.ratecard import RateCard
from ...pricing.snapshots import load_snapshot
from ...reporting.run_export import RunExportData, build_run_csv, build_run_json
from ...timeutil import iso_now
from ..deps import ServicesDep
from .runs import run_detail

router = APIRouter(prefix="/api", tags=["exports"])

_EXPORT_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "description": (
            "A file download: the run as an fme.run.v1 JSON document, or one CSV row per attempt. "
            "Request bodies, raw responses and keys are never included; prompt and answer text only with "
            "include_text=true."
        ),
        "content": {"application/json": {}, "text/csv": {}},
    }
}


@router.get("/runs/{run_id}/export", response_class=Response, responses=_EXPORT_RESPONSES)
def export_run(
    run_id: str,
    services: ServicesDep,
    export_format: Annotated[Literal["json", "csv"], Query(alias="format")] = "json",
    include_text: bool = False,
) -> Response:
    """Download a run as JSON or CSV. Read-only: stored data and frozen price snapshots only."""
    detail = run_detail(services, run_id)
    candidates = services.runs.candidates(run_id)
    cards: dict[str, RateCard] = {}
    with services.db.connect() as conn:
        for candidate in candidates:
            if candidate.price_snapshot_id not in cards:
                cards[candidate.price_snapshot_id], _meta = load_snapshot(conn, candidate.price_snapshot_id)
    data = RunExportData(
        run=detail.run,
        prompts=detail.prompts,
        candidates=candidates,
        attempts=detail.attempts,
        manual_scores=detail.manual_scores,
        evaluations=services.evaluations.list_for_run(run_id),
        rate_cards=cards,
        judge_scores=detail.judge_scores,
        generation_cost=detail.generation_cost,
        judging_cost=detail.judging_cost,
        combined_cost=detail.combined_cost,
        currency=detail.generation_cost.currency,
        budget_limit=detail.budget.limit if detail.budget else None,
        budget_committed=detail.budget.committed if detail.budget else None,
    )
    stem = f"fme-run-{run_id[:8]}"
    if export_format == "csv":
        body = build_run_csv(data, include_text=include_text).encode("utf-8")
        media_type = "text/csv; charset=utf-8"
    else:
        document = build_run_json(data, include_text=include_text, exported_at=iso_now(), app_version=__version__)
        body = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
        media_type = "application/json"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{export_format}"'},
    )
