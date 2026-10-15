"""Comparisons across runs and cost projections."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ...analysis.comparisons import Comparison
from ...analysis.projections import ProjectionRequest, ProjectionResult
from ..deps import ServicesDep

router = APIRouter(prefix="/api", tags=["analysis"])


@router.get("/comparisons", response_model=Comparison)
def compare_runs(
    services: ServicesDep,
    run_id: Annotated[list[str], Query(min_length=1, max_length=50)],
    candidate_key: Annotated[list[str] | None, Query(max_length=32)] = None,
) -> Comparison:
    return services.comparisons.compare(run_id, candidate_key, services.settings.get().currency)


@router.post("/projections", response_model=ProjectionResult)
def project_costs(payload: ProjectionRequest, services: ServicesDep) -> ProjectionResult:
    return services.projections.project(payload, services.settings.get().currency)
