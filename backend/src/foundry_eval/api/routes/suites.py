"""Saved suites and suite runs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from ...domain.generation import validate_settings
from ...domain.suites import Suite, SuiteIn
from ...execution.runs import CandidateSpec, PromptSpec, RunPreview, RunRequest
from ...money import Money
from ...repos.endpoints import NotFoundError
from ..deps import IdempotencyKey, ServicesDep
from .runs import CreatedRun

router = APIRouter(prefix="/api", tags=["suites"])


class SuiteRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    budget: Money | None = None


class SuiteArchiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archived: bool


def _validate(services: ServicesDep, payload: SuiteIn) -> None:
    problems: list[str] = []
    for index, candidate in enumerate(payload.candidates, start=1):
        try:
            profile = services.profiles.get(candidate.profile_id)
        except NotFoundError:
            problems.append(f"Candidate {index}: the model profile does not exist.")
            continue
        problems.extend(
            f"Candidate {index} ({profile.name}): {issue.message}"
            for issue in validate_settings(candidate.settings, profile.capabilities)
        )
    judge = payload.judge
    if judge is not None and judge.enabled:
        try:
            judge_profile = services.profiles.get(judge.judge_profile_id or "")
            problems.extend(
                f"Judge ({judge_profile.name}): {issue.message}"
                for issue in validate_settings(judge.settings, judge_profile.capabilities)
            )
        except NotFoundError:
            problems.append("The judge model profile does not exist.")
        try:
            services.rubrics.get(judge.rubric_id)
        except NotFoundError:
            problems.append("The judge rubric does not exist.")
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))


def suite_run_request(services: ServicesDep, suite: Suite, body: SuiteRunIn) -> tuple[RunRequest, list[str]]:
    errors: list[str] = []
    judge_config = None
    if suite.judge is not None and suite.judge.enabled:
        try:
            judge_config = services.judge.freeze_suite_judge(suite.judge)
        except NotFoundError:
            errors.append("Automatic judging: the rubric does not exist.")
    request = RunRequest(
        kind="suite",
        title=body.title.strip() or suite.name,
        prompts=[PromptSpec(title=p.title, system=p.system, user=p.user, suite_prompt_id=p.id) for p in suite.prompts],
        candidates=[
            CandidateSpec(profile_id=c.profile_id, settings=c.settings, label=c.label) for c in suite.candidates
        ],
        trials=suite.trials,
        budget=body.budget,
        suite_id=suite.id,
        judge_config=judge_config,
    )
    return request, errors


@router.get("/suites", response_model=list[Suite])
def list_suites(services: ServicesDep, include_archived: bool = False) -> list[Suite]:
    return services.suites.list(include_archived=include_archived)


@router.post("/suites", response_model=Suite, status_code=201)
def create_suite(payload: SuiteIn, services: ServicesDep) -> Suite:
    _validate(services, payload)
    return services.suites.create(payload)


@router.get("/suites/{suite_id}", response_model=Suite)
def get_suite(suite_id: str, services: ServicesDep) -> Suite:
    return services.suites.get(suite_id)


@router.put("/suites/{suite_id}", response_model=Suite)
def update_suite(suite_id: str, payload: SuiteIn, services: ServicesDep) -> Suite:
    _validate(services, payload)
    return services.suites.update(suite_id, payload)


@router.post("/suites/{suite_id}/archive", response_model=Suite)
def archive_suite(suite_id: str, payload: SuiteArchiveIn, services: ServicesDep) -> Suite:
    return services.suites.set_archived(suite_id, payload.archived)


@router.delete("/suites/{suite_id}", status_code=204)
def delete_suite(suite_id: str, services: ServicesDep) -> Response:
    services.suites.delete(suite_id)
    return Response(status_code=204)


@router.post("/suites/{suite_id}/preview", response_model=RunPreview)
def preview_suite_run(suite_id: str, body: SuiteRunIn, services: ServicesDep) -> RunPreview:
    suite = services.suites.get(suite_id)
    request, errors = suite_run_request(services, suite, body)
    preview = services.run_service.preview(request)
    if errors:
        preview = preview.model_copy(update={"ok": False, "errors": [*preview.errors, *errors]})
    return preview


@router.post("/suites/{suite_id}/runs", response_model=CreatedRun, status_code=201)
def run_suite(
    suite_id: str, body: SuiteRunIn, key: IdempotencyKey, services: ServicesDep, response: Response
) -> CreatedRun:
    suite = services.suites.get(suite_id)
    if suite.archived:
        raise HTTPException(status_code=409, detail="This suite is archived; restore it to run it.")
    request, errors = suite_run_request(services, suite, body)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    run_id, created = services.run_service.create(request, key)
    if created:
        services.engine.start(run_id)
    else:
        response.status_code = 200
    return CreatedRun(run_id=run_id, created=created)
