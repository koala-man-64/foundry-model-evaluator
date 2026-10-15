"""Rubrics and judge evaluations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from ...judging.rubric import RubricDefinition
from ...judging.service import JudgePreview, JudgeRequest
from ...repos.evaluations import Evaluation
from ...repos.rubrics import Rubric, RubricVersion
from ..deps import IdempotencyKey, ServicesDep

router = APIRouter(prefix="/api", tags=["judging"])


class RubricIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    definition: RubricDefinition


class RubricPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=120)
    archived: bool | None = None


class EvaluationOut(Evaluation):
    judge_profile_name: str
    rubric_name: str
    rubric: RubricVersion
    # Revealed after judging: run candidate id -> candidate label.
    candidate_labels: dict[str, str]


class CreatedEvaluations(BaseModel):
    evaluation_ids: list[str]
    created: bool


class EvaluationRaw(BaseModel):
    request_body: dict[str, Any]
    raw_response: Any
    raw_usage: Any


def _evaluation_out(services: ServicesDep, evaluation: Evaluation, labels: dict[str, str]) -> EvaluationOut:
    rubric = services.rubrics.version(evaluation.rubric_version_id)
    return EvaluationOut(
        **evaluation.model_dump(),
        judge_profile_name=str(evaluation.judge_profile_snapshot.get("name", "")),
        rubric_name=services.rubrics.get(rubric.rubric_id).name,
        rubric=rubric,
        candidate_labels=labels,
    )


# ---------------------------------------------------------------------- rubrics
@router.get("/rubrics", response_model=list[Rubric])
def list_rubrics(services: ServicesDep, include_archived: bool = False) -> list[Rubric]:
    return services.rubrics.list_all(include_archived=include_archived)


@router.post("/rubrics", response_model=Rubric, status_code=201)
def create_rubric(payload: RubricIn, services: ServicesDep) -> Rubric:
    return services.rubrics.create(payload.name, payload.definition)


@router.get("/rubrics/{rubric_id}", response_model=Rubric)
def get_rubric(rubric_id: str, services: ServicesDep) -> Rubric:
    return services.rubrics.get(rubric_id)


@router.patch("/rubrics/{rubric_id}", response_model=Rubric)
def patch_rubric(rubric_id: str, payload: RubricPatch, services: ServicesDep) -> Rubric:
    return services.rubrics.update(rubric_id, name=payload.name, archived=payload.archived)


@router.get("/rubrics/{rubric_id}/versions", response_model=list[RubricVersion])
def rubric_versions(rubric_id: str, services: ServicesDep) -> list[RubricVersion]:
    return services.rubrics.versions(rubric_id)


@router.post("/rubrics/{rubric_id}/versions", response_model=RubricVersion, status_code=201)
def add_rubric_version(rubric_id: str, payload: RubricDefinition, services: ServicesDep) -> RubricVersion:
    """Editing a rubric always creates a new version; earlier versions never change."""
    return services.rubrics.add_version(rubric_id, payload)


@router.get("/rubric-versions/{version_id}", response_model=RubricVersion)
def get_rubric_version(version_id: str, services: ServicesDep) -> RubricVersion:
    return services.rubrics.version(version_id)


# ---------------------------------------------------------------------- evaluations
@router.post("/runs/{run_id}/evaluations/preview", response_model=JudgePreview)
def preview_evaluations(run_id: str, payload: JudgeRequest, services: ServicesDep) -> JudgePreview:
    return services.judge.preview(run_id, payload)


@router.post("/runs/{run_id}/evaluations", response_model=CreatedEvaluations, status_code=201)
def create_evaluations(
    run_id: str, payload: JudgeRequest, key: IdempotencyKey, services: ServicesDep, response: Response
) -> CreatedEvaluations:
    """Run the judge (a paid action) on the run's succeeded responses in the requested scope."""
    ids, created = services.judge.create(run_id, payload, key)
    if created:
        services.judge.start(run_id, ids)
    else:
        response.status_code = 200
    return CreatedEvaluations(evaluation_ids=ids, created=created)


@router.get("/runs/{run_id}/evaluations", response_model=list[EvaluationOut])
def list_evaluations(run_id: str, services: ServicesDep) -> list[EvaluationOut]:
    services.runs.get(run_id)
    labels = {c.id: c.label for c in services.runs.candidates(run_id)}
    return [_evaluation_out(services, e, labels) for e in services.evaluations.list_for_run(run_id)]


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationOut)
def get_evaluation(evaluation_id: str, services: ServicesDep) -> EvaluationOut:
    evaluation = services.evaluations.get(evaluation_id)
    labels = {c.id: c.label for c in services.runs.candidates(evaluation.run_id)}
    return _evaluation_out(services, evaluation, labels)


@router.get("/evaluations/{evaluation_id}/raw", response_model=EvaluationRaw)
def evaluation_raw(evaluation_id: str, services: ServicesDep) -> EvaluationRaw:
    request_body, raw_response, raw_usage = services.evaluations.raw(evaluation_id)
    return EvaluationRaw(request_body=request_body, raw_response=raw_response, raw_usage=raw_usage)
