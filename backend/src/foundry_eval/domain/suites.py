"""Saved suites: prompts, candidates (profile + settings), trials and optional automatic judging."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .generation import GenerationSettings


class SuiteJudge(BaseModel):
    """Automatic judging after each suite run; frozen into the run when the run is created."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    judge_profile_id: str | None = None
    settings: GenerationSettings = Field(default_factory=GenerationSettings)
    rubric_id: str = "default"

    @model_validator(mode="after")
    def _check(self) -> SuiteJudge:
        if self.enabled and not self.judge_profile_id:
            raise ValueError("choose a judge model profile to enable automatic judging")
        return self


class SuitePromptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str = Field(default="", max_length=200)
    system: str = Field(default="", max_length=200_000)
    user: str = Field(min_length=1, max_length=400_000)


class SuiteCandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    profile_id: str
    label: str | None = Field(default=None, max_length=120)
    settings: GenerationSettings = Field(default_factory=GenerationSettings)


class SuiteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    trials: int = Field(default=1, ge=1, le=50)
    prompts: list[SuitePromptIn] = Field(min_length=1, max_length=500)
    candidates: list[SuiteCandidateIn] = Field(min_length=1, max_length=16)
    judge: SuiteJudge | None = None


class SuitePrompt(BaseModel):
    id: str
    position: int
    title: str
    system: str
    user: str


class SuiteCandidate(BaseModel):
    id: str
    position: int
    profile_id: str
    label: str | None
    settings: GenerationSettings


class Suite(BaseModel):
    id: str
    name: str
    description: str
    trials: int
    judge: SuiteJudge | None
    archived: bool
    prompts: list[SuitePrompt]
    candidates: list[SuiteCandidate]
    created_at: str
    updated_at: str
